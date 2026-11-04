"""SQLite persistence for the botville pairing backend (A2.1).

Standard library only. Schema:

  invites(id, secret_hash, created_at, expires_at, used_at)
      One-shot pairing credentials. Only sha256(secret) is ever stored;
      the plaintext secret appears once, in the QR payload handed to the
      inviting admin.
  peers(server_name PK, state active|severed, paired_at, severed_at,
        severed_by local|remote, peer_api_url)
      Pairing state per remote server. fedgate admits federation traffic
      from `server_name` only while a row exists with state='active'
      (absence of a row means "not paired" = deny).
  events(id, at, kind, detail)
      Append-only audit log (pairings, severs, rejects).

Timestamps are POSIX floats (time.time()). redeem_invite() is a single
transaction covering the invite checks (unknown / already used / expired /
wrong secret with a constant-time compare), the one-time `used_at` mark and
the peer upsert, so two concurrent redeems cannot double-spend an invite.
"""

import hashlib
import hmac
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    id          TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
);
CREATE TABLE IF NOT EXISTS peers (
    server_name TEXT PRIMARY KEY,
    state       TEXT NOT NULL CHECK (state IN ('active', 'severed')),
    paired_at   REAL NOT NULL,
    severed_at  REAL,
    severed_by  TEXT CHECK (severed_by IN ('local', 'remote') OR severed_by IS NULL),
    peer_api_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     REAL NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

# Failure reasons returned by redeem_invite(); mapped to HTTP responses by
# the service layer. Keep stable: tests and the audit log rely on them.
REDEEM_UNKNOWN = "unknown_invite"
REDEEM_USED = "invite_already_used"
REDEEM_EXPIRED = "invite_expired"
REDEEM_BAD_SECRET = "invalid_secret"


def hash_secret(secret):
    """sha256 hex digest of a secret string (what gets persisted)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class PairingStore:
    """Thread-safe access to the pairing SQLite database.

    One connection guarded by a lock (ThreadingHTTPServer handlers run in
    threads); WAL journaling so fedgate's read-only connections (separate
    process, same volume) never block on a write.
    """

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # --- events ----------------------------------------------------------

    def record_event(self, kind, now=None, **detail):
        """Append one audit row; detail is stored as sorted JSON."""
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)",
                (now, kind, json.dumps(detail, sort_keys=True)),
            )
            self._conn.commit()

    # --- invites ---------------------------------------------------------

    def create_invite(self, invite_id, secret, ttl, now=None):
        """Store a fresh one-shot invite; returns its expiry timestamp."""
        now = time.time() if now is None else now
        expires_at = now + ttl
        with self._lock:
            self._conn.execute(
                "INSERT INTO invites (id, secret_hash, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (invite_id, hash_secret(secret), now, expires_at),
            )
            self._conn.commit()
        self.record_event("invite_created", now=now, invite_id=invite_id,
                          expires_at=expires_at)
        return expires_at

    def redeem_invite(self, invite_id, secret, server_name, api_url, now=None):
        """Atomically consume an invite and mark `server_name` active.

        Returns (True, None) on success or (False, reason) with reason one
        of REDEEM_*. Re-pairing a previously severed peer is allowed: the
        upsert resets state to active and clears severed_at/severed_by.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT secret_hash, expires_at, used_at FROM invites WHERE id = ?",
                    (invite_id,),
                ).fetchone()
                reason = None
                if row is None:
                    reason = REDEEM_UNKNOWN
                elif row["used_at"] is not None:
                    reason = REDEEM_USED
                elif now > row["expires_at"]:
                    reason = REDEEM_EXPIRED
                elif not hmac.compare_digest(row["secret_hash"], hash_secret(secret)):
                    reason = REDEEM_BAD_SECRET
                if reason is not None:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(
                        "UPDATE invites SET used_at = ? WHERE id = ?", (now, invite_id)
                    )
                    self._conn.execute(
                        "INSERT INTO peers (server_name, state, paired_at,"
                        " severed_at, severed_by, peer_api_url)"
                        " VALUES (?, 'active', ?, NULL, NULL, ?)"
                        " ON CONFLICT(server_name) DO UPDATE SET"
                        "   state = 'active', paired_at = excluded.paired_at,"
                        "   severed_at = NULL, severed_by = NULL,"
                        "   peer_api_url = excluded.peer_api_url",
                        (server_name, now, api_url),
                    )
                    self._conn.execute(
                        "INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)",
                        (now, "invite_redeemed",
                         json.dumps({"invite_id": invite_id,
                                     "server_name": server_name}, sort_keys=True)),
                    )
                    self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass  # e.g. COMMIT itself failed; keep the original error
                raise
        if reason is not None:
            self.record_event("redeem_rejected", now=now, invite_id=invite_id,
                              server_name=server_name, reason=reason)
            return False, reason
        return True, None

    # --- peers -----------------------------------------------------------

    def _peer_row(self, row):
        if row is None:
            return None
        return {
            "server_name": row["server_name"],
            "state": row["state"],
            "paired_at": row["paired_at"],
            "severed_at": row["severed_at"],
            "severed_by": row["severed_by"],
            "peer_api_url": row["peer_api_url"],
        }

    def get_peer(self, server_name):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM peers WHERE server_name = ?", (server_name,)
            ).fetchone()
        return self._peer_row(row)

    def list_peers(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM peers ORDER BY server_name"
            ).fetchall()
        return [self._peer_row(row) for row in rows]

    def active_server_names(self):
        """Server names fedgate must admit; read path for the gate."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT server_name FROM peers WHERE state = 'active'"
                " ORDER BY server_name"
            ).fetchall()
        return [row["server_name"] for row in rows]

    def upsert_peer_active(self, server_name, api_url, now=None):
        """Record a peer as active without consuming a local invite.

        Used by the accepting side of a pairing: the invite lives on the
        *other* server, so after its /redeem succeeds there is nothing to
        redeem locally — just write the peer row (resetting any severed
        state from an earlier pairing).
        """
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                "INSERT INTO peers (server_name, state, paired_at,"
                " severed_at, severed_by, peer_api_url)"
                " VALUES (?, 'active', ?, NULL, NULL, ?)"
                " ON CONFLICT(server_name) DO UPDATE SET"
                "   state = 'active', paired_at = excluded.paired_at,"
                "   severed_at = NULL, severed_by = NULL,"
                "   peer_api_url = excluded.peer_api_url",
                (server_name, now, api_url),
            )
            self._conn.execute(
                "INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)",
                (now, "peer_paired",
                 json.dumps({"server_name": server_name}, sort_keys=True)),
            )
            self._conn.commit()

    def sever_peer(self, server_name, by, now=None):
        """Mark a peer severed (`by` is 'local' or 'remote').

        Returns True when a peer row existed (and was updated), False when
        the server was never paired. Idempotent for an already-severed
        peer; re-pairing later resets it via redeem_invite().
        """
        assert by in ("local", "remote")
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "UPDATE peers SET state = 'severed', severed_at = ?, severed_by = ?"
                " WHERE server_name = ?",
                (now, by, server_name),
            )
            self._conn.commit()
            existed = cur.rowcount > 0
        if existed:
            self.record_event("peer_severed", now=now, server_name=server_name,
                              severed_by=by)
        return existed

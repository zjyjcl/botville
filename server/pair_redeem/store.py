"""SQLite store for App QR pairing codes and audit events.

Only sha256(code) is persisted. Plaintext codes appear once, at issue()
return. Audit rows never contain a code or an auth token. Retention
follows A2.1 store.prune(): 30 days, unused and unexpired codes are kept.
"""

import base64
import hashlib
import json
import logging
import sqlite3
import string
import threading
import time
import urllib.parse

log = logging.getLogger("pair_redeem.store")

PRUNE_DEFAULT_DAYS = 30
CODE_TTL_SECONDS = 300
ALLOWED_STATUS = ("unused", "used", "revoked")
AUDIT_KINDS = ("issued", "redeemed", "revoked", "rate_limited", "rejected")
# First cut: only registration_token may be stored or issued (B4).
ALLOWED_AUTH_TYPES = ("registration_token",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS codes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code_sha256         TEXT NOT NULL UNIQUE,
    hs                  TEXT NOT NULL,
    gw                  TEXT,
    fp                  TEXT,
    exp                 REAL NOT NULL,  -- wall-clock seconds; REAL so
    -- int/float clocks compare uniformly. SQLite numeric affinity still
    -- reads older INTEGER rows written by earlier schemas.
    status              TEXT NOT NULL CHECK (status IN ('unused', 'used', 'revoked')),
    auth_type           TEXT NOT NULL CHECK (auth_type IN ('registration_token')),
    issued_at           REAL NOT NULL,
    used_at             REAL,
    revoked_at          REAL,
    device_display_name TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     REAL NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


CODE_ALPHABET = set(string.ascii_letters + string.digits + "-_")


def code_sha256(code):
    """Hex sha256 of the 43-character code string (utf-8)."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _has_ctrl_or_space(value):
    return any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in value)


def require_https_url(value, what):
    """hs/gw: canonical https URL, no userinfo/query/fragment. Rejects // suffix."""
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValueError("%s must be an https:// URL" % what)
    if _has_ctrl_or_space(value) or "%" in value:
        raise ValueError("%s must be a canonical https:// URL" % what)
    if value.endswith("//"):
        raise ValueError("%s must not end with //" % what)
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port
    except ValueError as exc:
        raise ValueError("%s is not a valid URL" % what) from exc
    if parts.username is not None or parts.password is not None:
        raise ValueError("%s must not include userinfo" % what)
    if not parts.hostname or parts.query or parts.fragment:
        raise ValueError("%s must be an https URL without query or fragment" % what)
    return value


def require_code(code):
    """43-character canonical base64url (32 bytes), same alphabet as the contract."""
    if not isinstance(code, str) or len(code) != 43:
        raise ValueError("code must be 43-character base64url")
    if any(c not in CODE_ALPHABET for c in code):
        raise ValueError("code must be 43-character base64url")
    raw = base64.urlsafe_b64decode(code + "=")
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != code:
        raise ValueError("code must be canonical base64url")
    return code


class PairRedeemStore:
    def __init__(self, path=":memory:"):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    def issue(self, hs, code, gw=None, fp=None, now=None,
              ttl_seconds=CODE_TTL_SECONDS, auth_type="registration_token"):
        """Persist sha256(code) and return the record id.

        ``code`` is accepted from the caller so tests can inject a fixture
        value; production issuance should pass 32 CSPRNG bytes encoded as
        base64url (no pad). The plaintext is not stored.
        """
        if auth_type not in ALLOWED_AUTH_TYPES:
            raise ValueError("this build only issues registration_token")
        require_https_url(hs, "hs")
        require_code(code)
        if gw is not None:
            require_https_url(gw, "gw")
        now = time.time() if now is None else now
        exp = float(now) + float(ttl_seconds)
        digest = code_sha256(code)
        detail = {"id": None, "code_sha256": digest, "hs": hs,
                  "has_gw": gw is not None, "exp": exp, "result": "issued"}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "INSERT INTO codes (code_sha256, hs, gw, fp, exp, status,"
                    " auth_type, issued_at) VALUES (?, ?, ?, ?, ?, 'unused', ?, ?)",
                    (digest, hs, gw, fp, exp, auth_type, float(now)),
                )
                record_id = cur.lastrowid
                detail["id"] = record_id
                self._insert_event_locked("issued", detail, now)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return record_id, digest, exp

    def revoke(self, digest, now=None, operator=None):
        """Revoke an unused code. Already used/revoked/unknown is a no-op
        success for the caller to interpret; returns the new status or None."""
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_locked(digest)
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return None
                if row["status"] != "unused":
                    self._conn.execute("ROLLBACK")
                    return row["status"]
                self._conn.execute(
                    "UPDATE codes SET status = 'revoked', revoked_at = ? "
                    "WHERE code_sha256 = ? AND status = 'unused'",
                    (float(now), digest),
                )
                self._insert_event_locked("revoked", {
                    "id": row["id"], "code_sha256": digest,
                    "operator": operator, "result": "revoked",
                }, now)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return "revoked"

    def get(self, digest):
        with self._lock:
            return self._row_to_dict(self._get_locked(digest))

    def _get_locked(self, digest):
        return self._conn.execute(
            "SELECT * FROM codes WHERE code_sha256 = ?", (digest,)
        ).fetchone()

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    def redeem_atomic(self, digest, device_display_name, client_ip,
                      issuer, now=None, expires_in_ms=None):
        """One transaction: state checks, mark used, issue token, audit.

        ``issuer(expires_in_ms)`` must return ``{type, token, expires_in_ms}``
        or raise. On any failure the used mark is rolled back. The returned
        dict includes the auth token for the HTTP layer only; it is never
        written to the store or to the audit row.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_locked(digest)
                if row is None:
                    self._insert_event_locked("rejected", {
                        "code_sha256": digest, "ip": client_ip,
                        "result": "rejected", "reason": "unknown_code",
                    }, now)
                    self._conn.execute("COMMIT")
                    return "invalid", None
                status = row["status"]
                if status == "used":
                    self._insert_event_locked("rejected", {
                        "id": row["id"], "code_sha256": digest, "ip": client_ip,
                        "result": "rejected", "reason": "used",
                        "device_display_name": device_display_name,
                    }, now)
                    self._conn.execute("COMMIT")
                    return "used", None
                if status == "revoked":
                    self._insert_event_locked("rejected", {
                        "id": row["id"], "code_sha256": digest, "ip": client_ip,
                        "result": "rejected", "reason": "revoked",
                        "device_display_name": device_display_name,
                    }, now)
                    self._conn.execute("COMMIT")
                    return "revoked", None
                if now >= row["exp"]:
                    self._insert_event_locked("rejected", {
                        "id": row["id"], "code_sha256": digest, "ip": client_ip,
                        "result": "rejected", "reason": "expired",
                        "device_display_name": device_display_name,
                    }, now)
                    self._conn.execute("COMMIT")
                    return "expired", None
                if row["auth_type"] != "registration_token":
                    # Fail closed without consuming the row (no new errcode).
                    self._conn.execute("ROLLBACK")
                    return "unavailable", None
                updated = self._conn.execute(
                    "UPDATE codes SET status = 'used', used_at = ?,"
                    " device_display_name = ? "
                    "WHERE code_sha256 = ? AND status = 'unused'",
                    (float(now), device_display_name, digest),
                ).rowcount
                if updated != 1:
                    self._insert_event_locked("rejected", {
                        "id": row["id"], "code_sha256": digest, "ip": client_ip,
                        "result": "rejected", "reason": "used",
                        "device_display_name": device_display_name,
                    }, now)
                    self._conn.execute("COMMIT")
                    return "used", None
                auth = issuer(expires_in_ms)
                if (not isinstance(auth, dict)
                        or auth.get("type") != "registration_token"
                        or not auth.get("token")
                        or int(auth.get("expires_in_ms") or 0) < 1):
                    self._conn.execute("ROLLBACK")
                    return "unavailable", None
                self._insert_event_locked("redeemed", {
                    "id": row["id"], "code_sha256": digest, "ip": client_ip,
                    "result": "redeemed",
                    "device_display_name": device_display_name,
                    "auth_type": "registration_token",
                    "expires_in_ms": int(auth["expires_in_ms"]),
                }, now)
                self._conn.execute("COMMIT")
                result = {
                    "v": "1",
                    "homeserver": row["hs"],
                    "gateway": row["gw"],
                    "auth": {
                        "type": "registration_token",
                        "token": auth["token"],
                        "expires_in_ms": int(auth["expires_in_ms"]),
                    },
                }
                if row["gw"]:
                    result["features_url"] = (
                        row["gw"].rstrip("/") + "/v1/capabilities")
                return "ok", result
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def record_event(self, kind, detail, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                event_id = self._insert_event_locked(kind, detail, now)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return event_id

    def update_event_suppressed(self, event_id, suppressed):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT detail FROM events WHERE id = ?", (event_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return
                payload = json.loads(row["detail"])
                payload["suppressed"] = int(suppressed)
                self._conn.execute(
                    "UPDATE events SET detail = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False,
                                separators=(",", ":")),
                     event_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _insert_event_locked(self, kind, detail, now):
        if kind not in AUDIT_KINDS:
            raise ValueError("unknown audit kind")
        payload = dict(detail)
        # Belt-and-braces: never persist secrets even if a caller slips.
        for key in ("code", "token", "auth", "admin_token", "secret"):
            payload.pop(key, None)
        cur = self._conn.execute(
            "INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)",
            (float(now), kind, json.dumps(payload, ensure_ascii=False,
                                          separators=(",", ":"),
                                          default=str)),
        )
        return cur.lastrowid

    def events(self, kind=None):
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT id, at, kind, detail FROM events ORDER BY id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, at, kind, detail FROM events "
                    "WHERE kind = ? ORDER BY id", (kind,),
                ).fetchall()
        out = []
        for row in rows:
            item = {"id": row["id"], "at": row["at"], "kind": row["kind"],
                    "detail": json.loads(row["detail"])}
            out.append(item)
        return out

    def prune(self, now=None, older_than_days=PRUNE_DEFAULT_DAYS):
        now = time.time() if now is None else now
        cutoff = now - older_than_days * 86400.0
        with self._lock:
            codes = self._conn.execute(
                "DELETE FROM codes WHERE "
                "(status != 'unused' AND COALESCE(used_at, revoked_at, issued_at) < ?) "
                "OR (status = 'unused' AND exp < ?)",
                (cutoff, cutoff),
            ).rowcount
            events = self._conn.execute(
                "DELETE FROM events WHERE at < ?", (cutoff,)
            ).rowcount
        return {"codes": codes, "events": events}

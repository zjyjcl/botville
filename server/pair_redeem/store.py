"""SQLite store for App QR pairing codes and audit events.

Only sha256(code) is persisted. Plaintext codes appear once, at issue()
return. Audit rows never contain a code or an auth token. Retention
follows A2.1 store.prune(): 30 days, unused and unexpired codes are kept.
"""

from contextlib import contextmanager

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
    """hs/gw: canonical https URL, no userinfo/query/fragment. Rejects // suffix.

    Also reject `&`, `?`, and `#` in the raw string. `build_pair_uri`
    concatenates these values into a `botville://pair?` query; a raw `&`
    in the path would let parse_pair_uri see an injected `gw` or `fp`.
    `=` in a path is kept (the parser splits on the first `=` only).
    """
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValueError("%s must be an https:// URL" % what)
    if _has_ctrl_or_space(value) or "%" in value or any(
            ch in value for ch in "&?#"):
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


FP_PREFIX = "sha256:"


def require_fp(value):
    """fp = 'sha256:' + 43-character canonical base64url (32-byte digest)."""
    if not isinstance(value, str) or not value.startswith(FP_PREFIX):
        raise ValueError("fp must be sha256:<43-character base64url>")
    digest = value[len(FP_PREFIX):]
    if len(digest) != 43 or any(c not in CODE_ALPHABET for c in digest):
        raise ValueError("fp must be sha256:<43-character base64url>")
    raw = base64.urlsafe_b64decode(digest + "=")
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != digest:
        raise ValueError("fp must be sha256:<43-character base64url>")
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
        self._request = threading.local()
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    def close(self):
        with self._locked():
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
        if fp is not None:
            require_fp(fp)
        now = time.time() if now is None else now
        exp = float(now) + float(ttl_seconds)
        digest = code_sha256(code)
        detail = {"id": None, "code_sha256": digest, "hs": hs,
                  "has_gw": gw is not None, "exp": exp, "result": "issued"}
        with self._locked():
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
        with self._locked():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self.check_request_deadline()
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

    def list_codes(self, status=None, hs=None, expired=None, limit=50,
                   after_id=None, now=None):
        """Return newest-first rows for the admin list API.

        Does not include code_sha256. ``after_id`` is a cursor: only rows
        with a smaller id are returned. ``expired`` filters unused rows
        whose exp is at or before ``now`` (or the opposite when false).
        """
        if status is not None and status not in ALLOWED_STATUS:
            raise ValueError("unknown status")
        if hs is not None:
            require_https_url(hs, "hs")
        if expired is not None and not isinstance(expired, bool):
            raise ValueError("expired must be a bool")
        try:
            if isinstance(limit, bool):
                raise ValueError("invalid limit")
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError("invalid limit") from None
        if limit < 1 or limit > 100:
            raise ValueError("invalid limit")
        if after_id is not None:
            try:
                if isinstance(after_id, bool):
                    raise ValueError("invalid after_id")
                after_id = int(after_id)
            except (TypeError, ValueError):
                raise ValueError("invalid after_id") from None
            if after_id < 1:
                raise ValueError("invalid after_id")
        now = time.time() if now is None else now
        clauses = []
        args = []
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        if hs is not None:
            clauses.append("hs = ?")
            args.append(hs)
        if expired is True:
            clauses.append("status = 'unused' AND exp <= ?")
            args.append(float(now))
        elif expired is False:
            clauses.append("NOT (status = 'unused' AND exp <= ?)")
            args.append(float(now))
        if after_id is not None:
            clauses.append("id < ?")
            args.append(after_id)
        sql = (
            "SELECT id, hs, gw, fp, exp, status, auth_type, issued_at, "
            "used_at, revoked_at, device_display_name FROM codes"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._locked():
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def revoke_ids(self, record_ids, now=None, operator=None):
        """Revoke unused codes by id in one transaction.

        Each id yields one result in request order: ``revoked`` (this
        call), ``already_revoked``, ``used``, or ``not_found``. Used
        rows stay used. Does not accept a digest or a plaintext code.
        """
        now = time.time() if now is None else now
        results = []
        with self._locked():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self.check_request_deadline()
                for record_id in record_ids:
                    self.check_request_deadline()
                    row = self._conn.execute(
                        "SELECT id, status, code_sha256 FROM codes "
                        "WHERE id = ?", (record_id,),
                    ).fetchone()
                    if row is None:
                        results.append({"id": record_id, "result": "not_found"})
                        continue
                    if row["status"] == "used":
                        results.append({"id": record_id, "result": "used"})
                        continue
                    if row["status"] == "revoked":
                        results.append(
                            {"id": record_id, "result": "already_revoked"})
                        continue
                    self._conn.execute(
                        "UPDATE codes SET status = 'revoked', revoked_at = ? "
                        "WHERE id = ? AND status = 'unused'",
                        (float(now), record_id),
                    )
                    # Audit may include the stored digest (already in the
                    # issued row) but never a plaintext code, token, or URI.
                    detail = {
                        "id": record_id,
                        "code_sha256": row["code_sha256"],
                        "result": "revoked",
                    }
                    if operator is not None:
                        detail["operator"] = operator
                    self._insert_event_locked("revoked", detail, now)
                    results.append({"id": record_id, "result": "revoked"})
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return results

    def get(self, digest):
        with self._locked():
            return self._row_to_dict(self._get_locked(digest))

    def _get_locked(self, digest):
        return self._conn.execute(
            "SELECT * FROM codes WHERE code_sha256 = ?", (digest,)
        ).fetchone()

    def current_request_deadline(self):
        return getattr(self._request, "deadline", None)

    def check_request_deadline(self):
        deadline = self.current_request_deadline()
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("request_deadline")

    @contextmanager
    def request_scope(self, deadline):
        previous = self.current_request_deadline()
        self._request.deadline = deadline
        try:
            yield
        finally:
            self._request.deadline = previous

    @contextmanager
    def _locked(self):
        deadline = self.current_request_deadline()
        if deadline is None:
            self._lock.acquire()
        else:
            left = deadline - time.monotonic()
            if left <= 0 or not self._lock.acquire(timeout=left):
                raise TimeoutError("request_deadline")
        try:
            self.check_request_deadline()
            # SQLite also has a separate cross-process busy wait. Cap it
            # to the same remaining budget, not its usual five seconds.
            budget = 5000 if deadline is None else max(
                1, int((deadline - time.monotonic()) * 1000))
            self._conn.execute("PRAGMA busy_timeout = %d" % budget)
            yield
        finally:
            self._lock.release()

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
        with self._locked():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self.check_request_deadline()
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
                self.check_request_deadline()
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
                self.check_request_deadline()
                auth = issuer(expires_in_ms)
                self.check_request_deadline()
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
                self.check_request_deadline()
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
        with self._locked():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                event_id = self._insert_event_locked(kind, detail, now)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return event_id

    def update_event_suppressed(self, event_id, suppressed):
        with self._locked():
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
        with self._locked():
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
        with self._locked():
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

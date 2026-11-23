"""HTTP service for POST /_botville/pair/redeem.

Processing order (pairing-qr.md §4.5):
  1) two-layer rate limit (IP + global), counting only, before any lookup
  2) request structure (64 KiB, JSON object, known fields, formats)
  3) sha256(code) lookup (miss -> 400 BV_PAIR_INVALID, same as bad format)
  4) status: used -> revoked -> expired
  5) one transaction: mark used, issue a Synapse registration_token, audit

This module does not import or call the federation pairing redeem path.
"""

import collections
import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import (
    CODE_ALPHABET,
    PairRedeemStore,
    code_sha256,
)

log = logging.getLogger("pair_redeem")

REDEEM_PATH = "/_botville/pair/redeem"
MAX_BODY_BYTES = 64 * 1024
CODE_PATTERN_LEN = 43
DEVICE_NAME_MIN = 1
DEVICE_NAME_MAX = 128
AUTH_EXPIRES_IN_MS = 600000
IP_LIMIT_DEFAULT = 10
GLOBAL_LIMIT_DEFAULT = 60
RATE_WINDOW_SECONDS = 60
# Issuer HTTP runs inside the store write lock (see redeem_atomic). Default
# stays short so a stuck Synapse fails fast instead of stalling the process;
# operators may raise BOTVILLE_PAIR_REDEEM_ISSUER_TIMEOUT_SECONDS if needed.
ISSUER_TIMEOUT_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 5
PRUNE_INTERVAL_SECONDS = 3600.0
# Cap in-memory rate-limit maps so a spoofed-XFF flood cannot grow them
# without bound (r2 A3 leftover).
RATE_BUCKET_MAX_KEYS = 4096
RATE_AUDIT_MAX_KEYS = 4096

ERROR_STATUS = {
    "BV_PAIR_INVALID": 400,
    "BV_PAIR_USED": 409,
    "BV_PAIR_EXPIRED": 410,
    "BV_PAIR_REVOKED": 410,
    "BV_PAIR_RATE_LIMITED": 429,
}
ERROR_TEXT = {
    "BV_PAIR_INVALID": "invalid or unknown pairing code",
    "BV_PAIR_USED": "pairing code already redeemed",
    "BV_PAIR_EXPIRED": "pairing code expired",
    "BV_PAIR_REVOKED": "pairing code revoked",
    "BV_PAIR_RATE_LIMITED": "too many redeem attempts; retry later",
}

# Env key names defined by this implementation PR (pairing-qr.md §4.5).
ENV_IP_LIMIT = "BOTVILLE_PAIR_REDEEM_IP_LIMIT"
ENV_GLOBAL_LIMIT = "BOTVILLE_PAIR_REDEEM_GLOBAL_LIMIT"
ENV_RATE_WINDOW = "BOTVILLE_PAIR_REDEEM_RATE_WINDOW_SECONDS"
ENV_TRUSTED_PROXIES = "BOTVILLE_PAIR_REDEEM_TRUSTED_PROXIES"
ENV_BIND = "BOTVILLE_PAIR_REDEEM_BIND"
ENV_STORE = "BOTVILLE_PAIR_REDEEM_DB"
ENV_SYNAPSE_URL = "BOTVILLE_PAIR_REDEEM_SYNAPSE_URL"
ENV_SYNAPSE_ADMIN_TOKEN = "BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN"
ENV_AUTH_EXPIRES_MS = "BOTVILLE_PAIR_REDEEM_TOKEN_EXPIRES_IN_MS"
ENV_ISSUER_TIMEOUT = "BOTVILLE_PAIR_REDEEM_ISSUER_TIMEOUT_SECONDS"


class ConfigError(Exception):
    """Process configuration is invalid; refuse to start."""


class IssuerError(Exception):
    """The homeserver refused or failed to mint a registration token.

    The message is a short code only — never a request or response body.
    """


class PairRedeemError(Exception):
    """A contract error ready to be serialized as a PairingError body."""

    def __init__(self, errcode, retry_after_ms=None):
        self.errcode = errcode
        self.status = ERROR_STATUS[errcode]
        self.retry_after_ms = retry_after_ms
        text = ERROR_TEXT[errcode]
        super().__init__(text)
        self.error = text


class RateLimiter:
    """Sliding window: ``limit`` hits per ``window`` seconds per key."""

    def __init__(self, limit, window=RATE_WINDOW_SECONDS):
        self._limit = int(limit)
        self._window = float(window)
        self._hits = {}
        self._lock = threading.Lock()

    def _trim(self, key, now):
        hits = self._hits.get(key)
        if hits is None:
            return None
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
            return None
        return hits

    def _evict_overflow_locked(self):
        overflow = len(self._hits) - RATE_BUCKET_MAX_KEYS
        if overflow <= 0:
            return
        # Prefer evicting per-IP buckets; keep the global key.
        oldest = [
            (key, hits) for key, hits in self._hits.items()
            if key != "global" and hits
        ]
        oldest.sort(key=lambda item: item[1][0])
        for key, _ in oldest[:overflow]:
            self._hits.pop(key, None)

    def peek(self, key, now=None):
        """Return (allowed, retry_after_ms) without recording a hit."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._trim(key, now)
            if hits is not None and len(hits) >= self._limit:
                retry = max(1, int(((hits[0] + self._window) - now) * 1000 + 0.999))
                return False, retry
            return True, None

    def commit(self, key, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._trim(key, now)
            if hits is None:
                hits = collections.deque()
                self._hits[key] = hits
            hits.append(now)
            self._evict_overflow_locked()

    def check(self, key, now=None):
        """Return (allowed, retry_after_ms). Denied requests are not recorded."""
        now = time.monotonic() if now is None else now
        allowed, retry = self.peek(key, now=now)
        if allowed:
            self.commit(key, now=now)
        return allowed, retry


def parse_trusted_proxies(raw):
    """Comma-separated IPs/CIDRs. Empty means trust nothing (ignore XFF)."""
    networks = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ConfigError(
                "%s entry %r is not a valid IP or CIDR" % (ENV_TRUSTED_PROXIES, item)
            ) from exc
    return networks


def client_ip(direct, headers, trusted_proxies):
    """Same rule as A2.1 pairing: honour X-Forwarded-For only if the
    direct peer is in the trusted-proxy set; use the last hop."""
    try:
        address = ipaddress.ip_address(direct)
    except ValueError:
        return direct
    for network in trusted_proxies:
        if address in network:
            forwarded = (headers.get("X-Forwarded-For") or "").split(",")
            last = forwarded[-1].strip() if forwarded else ""
            return last or direct
    return direct


def json_media_type(content_type):
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json"


def _env_int(env, key, default, minimum=1):
    raw = env.get(key, "")
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError("%s must be an integer, got %r" % (key, raw))
    if value < minimum:
        raise ConfigError("%s must be >= %s" % (key, minimum))
    return value


def parse_bind(raw, default="127.0.0.1:8092"):
    raw = (raw or default).strip()
    if raw.startswith("["):
        # [v6]:port
        host, _, port = raw[1:].partition("]:")
        if not port:
            raise ConfigError("%s must be host:port" % ENV_BIND)
    else:
        host, _, port = raw.rpartition(":")
        if not host or not port:
            raise ConfigError("%s must be host:port" % ENV_BIND)
    try:
        return host, int(port)
    except ValueError:
        raise ConfigError("%s port is not an integer" % ENV_BIND)


class PairRedeemConfig:
    def __init__(self, ip_limit=IP_LIMIT_DEFAULT,
                 global_limit=GLOBAL_LIMIT_DEFAULT,
                 rate_window=RATE_WINDOW_SECONDS,
                 trusted_proxies=None,
                 bind_host="127.0.0.1", bind_port=8092,
                 store_path=":memory:",
                 synapse_url="", synapse_admin_token="",
                 auth_expires_in_ms=AUTH_EXPIRES_IN_MS,
                 request_timeout=REQUEST_TIMEOUT_SECONDS,
                 issuer_timeout=ISSUER_TIMEOUT_SECONDS):
        self.ip_limit = ip_limit
        self.global_limit = global_limit
        self.rate_window = rate_window
        self.trusted_proxies = list(trusted_proxies or [])
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.store_path = store_path
        self.synapse_url = (synapse_url or "").rstrip("/")
        self.synapse_admin_token = synapse_admin_token or ""
        self.auth_expires_in_ms = auth_expires_in_ms
        self.request_timeout = request_timeout
        self.issuer_timeout = issuer_timeout

    @classmethod
    def from_env(cls, env):
        host, port = parse_bind(env.get(ENV_BIND, ""))
        return cls(
            ip_limit=_env_int(env, ENV_IP_LIMIT, IP_LIMIT_DEFAULT),
            global_limit=_env_int(env, ENV_GLOBAL_LIMIT, GLOBAL_LIMIT_DEFAULT),
            rate_window=_env_int(env, ENV_RATE_WINDOW, RATE_WINDOW_SECONDS),
            trusted_proxies=parse_trusted_proxies(env.get(ENV_TRUSTED_PROXIES, "")),
            bind_host=host,
            bind_port=port,
            store_path=env.get(ENV_STORE, "") or ":memory:",
            synapse_url=env.get(ENV_SYNAPSE_URL, ""),
            synapse_admin_token=env.get(ENV_SYNAPSE_ADMIN_TOKEN, ""),
            auth_expires_in_ms=_env_int(env, ENV_AUTH_EXPIRES_MS,
                                        AUTH_EXPIRES_IN_MS),
            issuer_timeout=_env_int(env, ENV_ISSUER_TIMEOUT,
                                    ISSUER_TIMEOUT_SECONDS),
        )


class SynapseRegistrationTokenIssuer:
    """Mint a one-shot Synapse registration token via the admin API.

    Mock this in tests. The live client is opt-in and never logs tokens.

    The contract requires issuing the token in the same transaction as
    marking the code used (read → mark used → issue → audit → commit,
    roll back on failure). That HTTP call therefore runs under the
    store's BEGIN IMMEDIATE plus the process write lock; a long timeout
    would stall every redeem and rate-limit audit. Fail fast instead.
    """

    def __init__(self, base_url, admin_token, timeout=ISSUER_TIMEOUT_SECONDS):
        if not base_url or not admin_token:
            raise ConfigError(
                "registration token issuer requires %s and %s"
                % (ENV_SYNAPSE_URL, ENV_SYNAPSE_ADMIN_TOKEN)
            )
        self.base_url = base_url.rstrip("/")
        self._token = admin_token
        self.timeout = timeout

    def issue(self, expires_in_ms, now=None):
        now = time.time() if now is None else now
        expiry_time = int(now * 1000) + int(expires_in_ms)
        body = json.dumps({
            "uses_allowed": 1,
            "expiry_time": expiry_time,
        }, separators=(",", ":")).encode("utf-8")
        url = self.base_url + "/_synapse/admin/v1/registration_tokens/new"
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            try:
                response = opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = getattr(response, "status", None) or response.getcode()
                raw = response.read(4096)
        except Exception:
            raise IssuerError("http_unavailable") from None
        if status != 200:
            raise IssuerError("unusable_result")
        try:
            payload = json.loads(raw.decode("utf-8"))
            token = payload["token"]
            if not isinstance(token, str) or not token:
                raise KeyError("token")
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise IssuerError("unusable_result") from None
        return {
            "type": "registration_token",
            "token": token,
            "expires_in_ms": int(expires_in_ms),
        }


class PairRedeemApp:
    def __init__(self, store, issuer, config=None, clock=None):
        self.store = store
        self.issuer = issuer
        self.config = config or PairRedeemConfig()
        self.clock = clock or time.time
        self.ip_limiter = RateLimiter(self.config.ip_limit,
                                      self.config.rate_window)
        self.global_limiter = RateLimiter(self.config.global_limit,
                                          self.config.rate_window)
        self._rate_lock = threading.Lock()
        self._rate_audit_lock = threading.Lock()
        self._rate_audit = {}

    def check_rate_limit(self, ip, now_mono=None):
        now = time.monotonic() if now_mono is None else now_mono
        with self._rate_lock:
            ip_ok, ip_retry = self.ip_limiter.peek(ip, now=now)
            global_ok, global_retry = self.global_limiter.peek("global", now=now)
            if ip_ok and global_ok:
                self.ip_limiter.commit(ip, now=now)
                self.global_limiter.commit("global", now=now)
                return
        retries = [value for value in (ip_retry, global_retry) if value]
        # Take the later of the triggered layers' retry instants (max of
        # the delays). pairing-qr.md §4.5's "earliest retry" means the
        # earliest time at which *both* layers will accept a retry;
        # using min would still 429 on the other layer.
        retry_after_ms = max(retries) if retries else 1
        self._record_rate_limited(ip, retry_after_ms)
        raise PairRedeemError("BV_PAIR_RATE_LIMITED",
                              retry_after_ms=retry_after_ms)

    def _evict_rate_audit_locked(self, now, keep=None):
        """Drop expired (and overflow) audit keys. Return pending flushes."""
        window = self.config.rate_window
        stale = [key for key, rec in self._rate_audit.items()
                 if key != keep and (now - rec["at"]) >= window]
        flush = []
        for key in stale:
            rec = self._rate_audit.pop(key)
            if rec.get("event_id") is not None:
                flush.append((rec["event_id"], rec["suppressed"]))
        overflow = len(self._rate_audit) - RATE_AUDIT_MAX_KEYS
        if overflow > 0:
            oldest = [
                (key, rec) for key, rec in self._rate_audit.items()
                if key != keep
            ]
            oldest.sort(key=lambda item: item[1]["at"])
            for key, rec in oldest[:overflow]:
                self._rate_audit.pop(key, None)
                if rec.get("event_id") is not None:
                    flush.append((rec["event_id"], rec["suppressed"]))
        return flush

    def _record_rate_limited(self, ip, retry_after_ms):
        """Write at most one rate_limited audit per IP per window; count the rest.

        In-window repeats stay in memory and are flushed onto the previous
        event only when the window rolls (r2 A3). A new window always
        starts at suppressed=0 so the count does not inherit (r2 §3.2).
        """
        now = self.clock()
        flush_items = []
        start_new = False
        with self._rate_audit_lock:
            previous = self._rate_audit.get(ip)
            if previous is not None and (now - previous["at"]) < self.config.rate_window:
                previous["suppressed"] += 1
                flush_items.extend(self._evict_rate_audit_locked(now, keep=ip))
            else:
                if previous is not None and previous.get("event_id") is not None:
                    flush_items.append((previous["event_id"], previous["suppressed"]))
                start_new = True
                self._rate_audit[ip] = {"at": now, "suppressed": 0, "event_id": None}
                flush_items.extend(self._evict_rate_audit_locked(now, keep=ip))
        for event_id, suppressed in flush_items:
            self.store.update_event_suppressed(event_id, suppressed)
        if not start_new:
            return
        event_id = self.store.record_event("rate_limited", {
            "ip": ip, "result": "rate_limited",
            "retry_after_ms": retry_after_ms,
            "suppressed": 0,
        }, now=now)
        with self._rate_audit_lock:
            current = self._rate_audit.get(ip)
            if current is not None and current.get("event_id") is None:
                current["event_id"] = event_id

    def redeem(self, code, device_display_name, client_ip, now=None):
        now = self.clock() if now is None else now
        expires_in_ms = self.config.auth_expires_in_ms

        def _issue(expires):
            return self.issuer.issue(expires, now=now)

        digest = code_sha256(code)
        try:
            outcome, payload = self.store.redeem_atomic(
                digest, device_display_name, client_ip,
                issuer=_issue, now=now, expires_in_ms=expires_in_ms,
            )
        except IssuerError:
            log.warning("registration token issuer failed; code not consumed")
            raise
        if outcome == "ok":
            return payload
        if outcome == "invalid":
            raise PairRedeemError("BV_PAIR_INVALID")
        if outcome == "used":
            raise PairRedeemError("BV_PAIR_USED")
        if outcome == "revoked":
            raise PairRedeemError("BV_PAIR_REVOKED")
        if outcome == "expired":
            raise PairRedeemError("BV_PAIR_EXPIRED")
        raise IssuerError("unusable_result")

    def make_server(self, host=None, port=None):
        host = self.config.bind_host if host is None else host
        port = self.config.bind_port if port is None else port
        handler = _handler_for(self)
        handler.timeout = self.config.request_timeout

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            # serve_forever does not consult Server.timeout; the
            # per-recv deadline is handler.timeout (set above).

        return Server((host, port), handler)


def start_pruner(app, interval=PRUNE_INTERVAL_SECONDS, stop=None):
    """Retention loop equivalent to A2.1 pairing start_pruner.

    Prune once at startup, then every ``interval`` seconds. Daemon thread;
    failures are logged and never fatal. ``stop`` is an optional Event
    for tests.
    """
    def run():
        while True:
            try:
                counts = app.store.prune(now=app.clock())
                if any(counts.values()):
                    log.info("pruned old pair-redeem rows: %s", counts)
            except Exception as exc:
                log.warning("pair_redeem prune failed: %s", exc)
            if stop is None:
                time.sleep(interval)
            elif stop.wait(interval):
                return

    thread = threading.Thread(target=run, name="pair-redeem-pruner", daemon=True)
    thread.start()
    return thread


def parse_redeem_request(raw, content_type, content_length=None):
    """Validate the HTTP body; raise PairRedeemError(BV_PAIR_INVALID)."""
    if content_length is not None and content_length > MAX_BODY_BYTES:
        raise PairRedeemError("BV_PAIR_INVALID")
    if raw is None or len(raw) > MAX_BODY_BYTES:
        raise PairRedeemError("BV_PAIR_INVALID")
    if not json_media_type(content_type):
        raise PairRedeemError("BV_PAIR_INVALID")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise PairRedeemError("BV_PAIR_INVALID") from None
    if not isinstance(body, dict):
        raise PairRedeemError("BV_PAIR_INVALID")
    allowed = {"code", "device_display_name"}
    if set(body) - allowed:
        raise PairRedeemError("BV_PAIR_INVALID")
    code = body.get("code")
    name = body.get("device_display_name")
    if not isinstance(code, str) or not isinstance(name, str):
        raise PairRedeemError("BV_PAIR_INVALID")
    if len(code) != CODE_PATTERN_LEN or any(c not in CODE_ALPHABET for c in code):
        raise PairRedeemError("BV_PAIR_INVALID")
    if not (DEVICE_NAME_MIN <= len(name) <= DEVICE_NAME_MAX):
        raise PairRedeemError("BV_PAIR_INVALID")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates pass json.loads but fail sqlite3 UTF-8 binding.
        raise PairRedeemError("BV_PAIR_INVALID") from None
    return code, name


def _handler_for(app):
    class PairRedeemHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT_SECONDS

        def log_message(self, fmt, *args):
            # Request line only; never interpolate a body or token.
            log.info("%s %s", self.address_string(), fmt % args)

        def _client_ip(self):
            return client_ip(self.client_address[0], self.headers,
                             app.config.trusted_proxies)

        def _send_json(self, status, payload, retry_after_ms=None):
            raw = json.dumps(payload, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
            if status >= 400:
                # All error responses close the connection. Per-path
                # _close_unread() is easy to miss (r2 S1: the 429 early
                # exit left keep-alive open with an unread body).
                self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if retry_after_ms is not None:
                seconds = retry_after_ms // 1000
                if seconds >= 1:
                    self.send_header("Retry-After", str(seconds))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def _send_error_body(self, exc):
            # Unconditionally close, matching A2.1 pairing _error.
            self.close_connection = True
            body = {"errcode": exc.errcode, "error": exc.error}
            if exc.errcode == "BV_PAIR_RATE_LIMITED":
                body["retry_after_ms"] = int(exc.retry_after_ms or 1)
            self._send_json(exc.status, body,
                            retry_after_ms=exc.retry_after_ms)

        def _close_unread(self):
            """Stop reusing this connection when the request body is unfinished."""
            self.close_connection = True

        def _reject(self, ip, reason="invalid_request"):
            app.store.record_event("rejected", {
                "ip": ip, "result": "rejected", "reason": reason,
            }, now=app.clock())
            raise PairRedeemError("BV_PAIR_INVALID")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path != REDEEM_PATH:
                self._close_unread()
                self.send_error(404)
                return
            ip = self._client_ip()
            try:
                app.check_rate_limit(ip)
                try:
                    # Any Transfer-Encoding (including chunked) is rejected
                    # closed; we do not parse chunked bodies.
                    if (self.headers.get("Transfer-Encoding") or "").strip():
                        self._close_unread()
                        self._reject(ip, "transfer_encoding")
                    length_values = self.headers.get_all("Content-Length") or []
                    if not length_values:
                        self._close_unread()
                        self._reject(ip, "missing_length")
                    length_raw = length_values[0]
                    # Repeated or comma-joined Content-Length is a CL.CL
                    # smuggling surface; reject closed rather than picking one.
                    if len(length_values) > 1 or "," in length_raw:
                        self._close_unread()
                        self._reject(ip, "duplicate_length")
                    try:
                        length = int(length_raw)
                    except (TypeError, ValueError):
                        self._close_unread()
                        self._reject(ip, "bad_length")
                    if length < 0 or length > MAX_BODY_BYTES:
                        # Do not slurp an oversized body into memory.
                        self._close_unread()
                        self._reject(ip, "body_too_large")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        self._close_unread()
                        self._reject(ip, "short_body")
                    try:
                        code, name = parse_redeem_request(
                            raw, self.headers.get("Content-Type"), length)
                    except PairRedeemError:
                        # Body was fully read; keep-alive is safe. Still
                        # audit the illegal request (§4.6 / A1).
                        self._reject(ip, "invalid_request")
                except PairRedeemError:
                    raise
                except (TimeoutError, socket.timeout):
                    self._close_unread()
                    self._reject(ip, "timeout")
                except Exception:
                    self._close_unread()
                    self._reject(ip, "invalid_request")
                payload = app.redeem(code, name, ip)
                self._send_json(200, payload)
            except PairRedeemError as exc:
                self._send_error_body(exc)
            except IssuerError:
                app.store.record_event("rejected", {
                    "ip": ip, "result": "rejected", "reason": "issuer_failed",
                }, now=app.clock())
                # 5xx is allowed to be off-contract (§4.4). Do not invent
                # an errcode (e.g. M_UNKNOWN) the App might treat as
                # non-retryable; the HTTP status carries the retry hint.
                self._send_json(503, {"error": "temporarily unavailable"})
            except Exception:
                log.exception("pair_redeem handler failed")
                self._send_json(503, {"error": "temporarily unavailable"})

        def do_GET(self):
            self._method_not_allowed()

        def do_PUT(self):
            self._method_not_allowed()

        def do_DELETE(self):
            self._method_not_allowed()

        def __getattr__(self, name):
            # PATCH/OPTIONS/HEAD and any other verb: 405, not the default 501.
            if name.startswith("do_"):
                return self._method_not_allowed
            raise AttributeError(name)

        def _method_not_allowed(self):
            self._close_unread()
            path = self.path.split("?", 1)[0]
            if path != REDEEM_PATH:
                self.send_error(404)
                return
            self.send_error(405)

    return PairRedeemHandler

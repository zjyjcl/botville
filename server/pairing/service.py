"""botville pairing service (A2.1) — invite/redeem/accept/sever HTTP API.

Python standard library only; run with `python -m pairing.service`.
Endpoints (JSON everywhere; errors are `{"errcode", "error"}`):

  POST /v1/pairing/invites               admin  create a one-shot invite;
         -> {invite_id, expires_at, qr_payload}
         qr_payload = botville-pair:v1:<server_name>:<b64url(api_url)>:
                      <invite_id>:<secret>   (secret shown this once only)
  POST /v1/pairing/redeem                peer   consume an invite: body
         {invite_id, secret, server_name, api_url}. The invite is fully
         validated (read-only, constant-time) *before* the optional peer
         key check (PAIRING_VERIFY_PEER_KEY), so unauthenticated callers
         cannot trigger outbound requests; success returns the per-peer
         shared_secret (hex) used to authenticate /notify-severed. Rate
         limited per source IP and per invite_id (review B3/B4).
  POST /v1/pairing/accept                admin  body {qr_payload}: parse the
         payload scanned from the *other* server, call its /redeem, then
         mark it active locally (storing the redeem response's
         shared_secret).
  GET  /v1/pairing/peers                 admin  list pairing state.
  DELETE /v1/pairing/peers/{server_name} admin  unilateral sever: local row
         goes severed immediately, all unused invites are invalidated,
         then a best-effort HMAC-signed POST to the peer's
         /notify-severed (failure ignored, still logged).
  POST /v1/pairing/notify-severed        peer   body {server_name}: mark that
         peer severed_by=remote. Authenticated with the pairing-time
         shared_secret (review B2): X-Botville-Origin/-Ts/-Nonce/-Sig
         headers, HMAC-SHA256 over "origin\\nts\\nnonce\\nsha256(body)",
         ±300s timestamp window, nonce replay guard, 30/min per source
         IP; every failure is a bare 401 and writes no events.
  GET  /v1/pairing/allowed               admin  active server names (for
         debugging; fedgate reads the SQLite file directly).
  GET  /health                           any    liveness probe.

Paths under /_matrix/pairing/... are accepted as aliases of /v1/pairing/...
so fedgate can forward them unmodified (`/_matrix/pairing/redeem` ==
`/v1/pairing/redeem`); fedgate only forwards redeem and notify-severed.

server_names are canonicalised to lower-case DNS names on every path in
(validate.normalize_server_name). Rate limiting keys on the *effective*
client IP: X-Forwarded-For is only honoured when the direct peer is in
PAIRING_TRUSTED_PROXIES (fedgate overwrites that header with the direct
client IP, review B3).

Admin auth: `Authorization: Bearer $PAIRING_ADMIN_TOKEN` (constant-time
compare); the service refuses to start without it. Secrets/tokens are
never written to the log.
"""

import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .store import (
    REDEEM_BAD_SECRET,
    REDEEM_EXPIRED,
    REDEEM_UNKNOWN,
    REDEEM_USED,
    PairingStore,
)
from .validate import (
    ServerNameError,
    normalize_server_name,
    path_is_suspicious,
)

log = logging.getLogger("pairing.service")

QR_PREFIX = "botville-pair"
QR_VERSION = "v1"
QR_FIELD_COUNT = 6  # prefix:version:server_name:b64(api_url):invite_id:secret
DEFAULT_INVITE_TTL = 600.0
REDEEM_PER_MINUTE = 10
REDEEM_PER_INVITE_PER_MINUTE = 5
NOTIFY_PER_MINUTE = 30
RATE_WINDOW_SECONDS = 60.0
NOTIFY_TS_WINDOW_SECONDS = 300.0
NONCE_MIN_HEX_CHARS = 32  # >=16 random bytes, hex-encoded
NOTIFY_SECRET_BYTES = 32  # per-peer shared secret, stored as hex
NOTIFY_BODY_TIMEOUT = 5.0
PRUNE_INTERVAL_SECONDS = 3600.0
MAX_BODY_BYTES = 65536
PEER_HTTP_TIMEOUT = 15.0
INVITE_ID_BYTES = 12  # urlsafe -> 16 chars
SECRET_BYTES = 32  # urlsafe -> 43 chars

REDEEM_ERRORS = {
    REDEEM_UNKNOWN: (404, "M_NOT_FOUND", "unknown invite"),
    REDEEM_USED: (403, "M_FORBIDDEN", "invite already used"),
    REDEEM_EXPIRED: (403, "M_FORBIDDEN", "invite expired"),
    REDEEM_BAD_SECRET: (403, "M_FORBIDDEN", "invalid invite secret"),
}


class ConfigError(Exception):
    """Invalid or missing configuration; fatal at startup."""


# --- QR payload -----------------------------------------------------------

class QrPayloadError(ValueError):
    pass


def encode_api_url(api_url):
    return base64.urlsafe_b64encode(api_url.encode("utf-8")).decode("ascii")


def decode_api_url(encoded):
    try:
        # Pad back to a multiple of 4: the payload omits no chars, but be
        # tolerant of stripped padding when humans copy it around.
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        url = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise QrPayloadError("api_url is not valid base64/UTF-8") from exc
    if not (url.startswith("http://") or url.startswith("https://")):
        raise QrPayloadError("api_url must be an http(s) URL")
    return url


def build_qr_payload(server_name, api_url, invite_id, secret):
    return ":".join((QR_PREFIX, QR_VERSION, server_name,
                     encode_api_url(api_url), invite_id, secret))


def parse_qr_payload(payload):
    """Split a scanned payload into (server_name, api_url, invite_id, secret).

    Server names containing ':' (host:port) cannot round-trip through the
    ':'-separated format and are rejected here, as are non-DNS names (the
    pairing grammar, see validate.normalize_server_name); the returned
    server_name is the canonical lower-case form. See
    docs/deploy/paired-federation.md.
    """
    if not isinstance(payload, str):
        raise QrPayloadError("qr_payload must be a string")
    parts = payload.split(":")
    if len(parts) != QR_FIELD_COUNT:
        raise QrPayloadError(
            "qr_payload must have {} ':'-separated fields".format(QR_FIELD_COUNT))
    prefix, version, server_name, api_b64, invite_id, secret = parts
    if prefix != QR_PREFIX:
        raise QrPayloadError("not a {} payload".format(QR_PREFIX))
    if version != QR_VERSION:
        raise QrPayloadError("unsupported payload version {!r}".format(version))
    if not server_name or not invite_id or not secret:
        raise QrPayloadError("empty field in qr_payload")
    try:
        server_name = normalize_server_name(server_name)
    except ServerNameError as exc:
        raise QrPayloadError(str(exc)) from exc
    return server_name, decode_api_url(api_b64), invite_id, secret


# --- outbound HTTP helpers -------------------------------------------------

def _maybe_insecure_context(insecure_tls):
    if not insecure_tls:
        return None
    # CI-only (PAIRING_INSECURE_TLS=true): accept self-signed fedgate
    # certificates. Never enable outside throwaway test stacks.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def fetch_peer_key_server_name(server_name, url_template, insecure_tls,
                               timeout=PEER_HTTP_TIMEOUT):
    """GET the peer's key endpoint and return its server_name (or None).

    Raises OSError/ValueError on unreachable/broken responses. Matrix
    server names are case-insensitive, so the caller compares lowercased.
    """
    url = url_template.format(server_name=server_name)
    req = urllib.request.Request(
        url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout,
                                context=_maybe_insecure_context(insecure_tls)) as resp:
        raw = resp.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("key response is not a JSON object")
    return payload.get("server_name")


def post_json(url, body, insecure_tls=False, timeout=PEER_HTTP_TIMEOUT,
              headers=None):
    """POST JSON; returns (status, decoded body or None) like smoke.request_json.

    `body` may be a dict (serialised here) or raw bytes — bytes are sent
    as-is, so a signed notify-severed request hashes exactly what goes on
    the wire. `headers` adds/overrides request headers.
    """
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, method="POST", headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_maybe_insecure_context(insecure_tls)) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, None


# --- notify-severed authentication (review B2) --------------------------------

def notify_signature(shared_secret_hex, origin, ts, nonce, body_bytes):
    """hex HMAC-SHA256(secret, "origin\\nts\\nnonce\\nsha256hex(body)").

    The per-peer secret is the one negotiated at pairing time (stored on
    both peers rows); `ts` is unix seconds as a string, `nonce` hex, and
    `body_bytes` the exact request body. Same helper computes and verifies
    (verification goes through hmac.compare_digest).
    """
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    payload = "{}\n{}\n{}\n{}".format(origin, ts, nonce, body_hash)
    key = bytes.fromhex(shared_secret_hex)
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def build_notify_headers(shared_secret_hex, origin, body_bytes, now=None):
    """The X-Botville-* headers for a signed /notify-severed request."""
    ts = str(int(time.time() if now is None else now))
    nonce = secrets.token_hex(16)  # 16 bytes -> 32 hex chars
    return {
        "X-Botville-Origin": origin,
        "X-Botville-Ts": ts,
        "X-Botville-Nonce": nonce,
        "X-Botville-Sig": notify_signature(
            shared_secret_hex, origin, ts, nonce, body_bytes),
    }


class NonceGuard:
    """In-memory replay guard for notify nonces, keyed per origin.

    Remembers (origin, nonce) pairs for `window` seconds (twice the
    timestamp window, so a nonce cannot age out while its timestamp is
    still acceptable) and a bounded number of entries (oldest evicted).
    check_and_mark() is the atomic "seen before?" test.
    """

    def __init__(self, window=2 * NOTIFY_TS_WINDOW_SECONDS, capacity=8192):
        self._window = window
        self._capacity = capacity
        self._seen = collections.OrderedDict()
        self._lock = threading.Lock()

    def check_and_mark(self, key, now=None):
        """True when the key was already recorded; otherwise records it."""
        now = time.monotonic() if now is None else now
        with self._lock:
            # Insertion order == time order (monotonic clock), so the
            # oldest entries expire first.
            while self._seen:
                oldest_at = next(iter(self._seen.values()))
                if now - oldest_at <= self._window:
                    break
                self._seen.popitem(last=False)
            if key in self._seen:
                return True
            self._seen[key] = now
            if len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return False


# --- rate limiting ----------------------------------------------------------

class RateLimiter:
    """Sliding-window limiter (`limit` hits per `window` seconds per key).

    Keys are effective client IPs for the per-IP buckets (redeem 10/min,
    notify-severed 30/min) and invite ids for the per-invite bucket
    (5/min, review B3).
    """

    def __init__(self, limit=REDEEM_PER_MINUTE, window=RATE_WINDOW_SECONDS):
        self._limit = limit
        self._window = window
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, collections.deque())
            while hits and hits[0] <= now - self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True


# --- configuration ----------------------------------------------------------

def _env_flag(env, key, default):
    value = env.get(key, "")
    if not value:
        return default
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise ConfigError("{} must be true/1/yes or false/0/no, got {!r}".format(
        key, value))


def _parse_trusted_proxies(raw):
    """Parse PAIRING_TRUSTED_PROXIES (comma-separated IPs/CIDRs, may be empty).

    An empty value means "trust nothing": X-Forwarded-For is ignored and
    the direct peer address is always used (review B3).
    """
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ConfigError(
                "PAIRING_TRUSTED_PROXIES entry {!r} is not a valid IP or"
                " CIDR".format(item)) from exc
    return networks


class PairingConfig:
    def __init__(self, db_path, admin_token, server_name, api_url,
                 invite_ttl=DEFAULT_INVITE_TTL, verify_peer_key=True,
                 peer_key_url_template="https://{server_name}/_matrix/key/v2/server",
                 insecure_tls=False, host="127.0.0.1", port=8090,
                 trusted_proxies=()):
        self.db_path = db_path
        self.admin_token = admin_token
        try:
            self.server_name = normalize_server_name(server_name)
        except ServerNameError as exc:
            raise ConfigError(
                "PAIRING_SERVER_NAME must be a DNS name without port ({})".format(
                    exc))
        self.api_url = api_url
        self.invite_ttl = invite_ttl
        self.verify_peer_key = verify_peer_key
        self.peer_key_url_template = peer_key_url_template
        self.insecure_tls = insecure_tls
        self.host = host
        self.port = port
        self.trusted_proxies = list(trusted_proxies)

    @classmethod
    def from_env(cls, env):
        admin_token = env.get("PAIRING_ADMIN_TOKEN", "")
        if not admin_token:
            raise ConfigError(
                "PAIRING_ADMIN_TOKEN is required; refusing to start without it")
        server_name = env.get("PAIRING_SERVER_NAME", "")
        if not server_name:
            raise ConfigError("PAIRING_SERVER_NAME is required")
        api_url = env.get("PAIRING_API_URL", "")
        if not api_url:
            raise ConfigError(
                "PAIRING_API_URL is required (the pairing base URL peers see,"
                " e.g. https://hs.example:8448/_matrix/pairing)")
        if not (api_url.startswith("http://") or api_url.startswith("https://")):
            raise ConfigError("PAIRING_API_URL must be an http(s) URL")
        try:
            invite_ttl = float(env.get("PAIRING_INVITE_TTL", DEFAULT_INVITE_TTL))
        except ValueError as exc:
            raise ConfigError("PAIRING_INVITE_TTL must be seconds") from exc
        if invite_ttl <= 0:
            raise ConfigError("PAIRING_INVITE_TTL must be positive")
        return cls(
            db_path=env.get("PAIRING_DB", "pairing.db"),
            admin_token=admin_token,
            server_name=server_name,
            api_url=api_url.rstrip("/"),
            invite_ttl=invite_ttl,
            verify_peer_key=_env_flag(env, "PAIRING_VERIFY_PEER_KEY", True),
            peer_key_url_template=env.get(
                "PAIRING_PEER_KEY_URL_TEMPLATE",
                "https://{server_name}/_matrix/key/v2/server"),
            insecure_tls=_env_flag(env, "PAIRING_INSECURE_TLS", False),
            host=env.get("PAIRING_HOST", "127.0.0.1"),
            port=int(env.get("PAIRING_PORT", "8090")),
            trusted_proxies=_parse_trusted_proxies(
                env.get("PAIRING_TRUSTED_PROXIES", "")),
        )


# --- HTTP handler -----------------------------------------------------------

class PairingApp:
    """Everything the handler needs; attached to the server instance."""

    def __init__(self, config):
        self.config = config
        self.store = PairingStore(config.db_path)
        self.limiter = RateLimiter()  # redeem, per effective client IP
        self.invite_limiter = RateLimiter(limit=REDEEM_PER_INVITE_PER_MINUTE)
        self.notify_limiter = RateLimiter(limit=NOTIFY_PER_MINUTE)
        self.nonces = NonceGuard()
        self.notify_rejected = 0


def _iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class PairingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "botville-pairing/1.0"
    timeout = 60

    @property
    def app(self):
        return self.server.app

    @property
    def config(self):
        return self.app.config

    def log_message(self, fmt, *args):
        # Goes to the shared logger; paths never carry secrets (qr payloads
        # travel in bodies), so the default formatting is safe.
        log.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, errcode, message):
        # Error paths may not have drained the request body (e.g. the 401
        # fires before _read_json); close the connection so unread bytes
        # cannot corrupt a keep-alive follow-up request.
        self.close_connection = True
        self._send_json(status, {"errcode": errcode, "error": message})

    def _read_body(self):
        """Read the raw request body (capped); returns (bytes, error_response)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            return None, (413, "M_TOO_LARGE", "request body too large")
        return (self.rfile.read(length) if length else b""), None

    def _read_json(self):
        """Read the request body as a JSON object; returns (obj, error_response)."""
        raw, err = self._read_body()
        if err:
            return None, err
        if not raw:
            return None, (400, "M_BAD_JSON", "request body must be JSON")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, (400, "M_BAD_JSON", "request body must be JSON")
        if not isinstance(payload, dict):
            return None, (400, "M_BAD_JSON", "request body must be a JSON object")
        return payload, None

    def _client_ip(self):
        """Effective client IP for the rate-limit buckets (review B3).

        X-Forwarded-For is honoured only when the *direct* peer is a
        configured trusted proxy (fedgate, which overwrites the header
        with the direct client IP); client-supplied values from anyone
        else are treated as spoofed and ignored. The *last* value of the
        header is used — the proxy appends, so that is the hop the
        trusted proxy actually observed.
        """
        direct = self.client_address[0]
        try:
            address = ipaddress.ip_address(direct)
        except ValueError:
            return direct
        for network in self.config.trusted_proxies:
            if address in network:
                forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")
                last = forwarded[-1].strip() if forwarded else ""
                return last or direct
        return direct

    def _is_admin(self):
        auth = self.headers.get("Authorization") or ""
        expected = "Bearer " + self.config.admin_token
        return hmac.compare_digest(auth.encode("utf-8"),
                                   expected.encode("utf-8"))

    def _dispatch(self, method):
        # Raw request-target, not urlsplit(): "//x/y" would otherwise be
        # parsed as a network-location and hide the empty segment.
        path = self.path.split("?", 1)[0]
        # Path hygiene (review: bypass-via-path): refuse traversal
        # segments, empty segments and encoded separators before routing,
        # so prefix matching can never be fed a normalisable path.
        if path_is_suspicious(path):
            self._error(404, "M_UNRECOGNIZED", "malformed request path")
            return
        # fedgate forwards /_matrix/pairing/<x> unchanged; treat it as an
        # alias of /v1/pairing/<x> so peers only need one public port.
        if path.startswith("/_matrix/pairing/"):
            path = "/v1/pairing/" + path[len("/_matrix/pairing/"):]

        if path == "/health":
            self._send_json(200, {"ok": True})
            return

        if path == "/v1/pairing/redeem" and method == "POST":
            self._handle_redeem()
            return
        if path == "/v1/pairing/notify-severed" and method == "POST":
            self._handle_notify_severed()
            return

        # Everything below is admin-only.
        if not self._is_admin():
            self._error(401, "M_UNKNOWN_TOKEN",
                        "admin endpoints need Authorization: Bearer <token>")
            return
        if path == "/v1/pairing/invites" and method == "POST":
            self._handle_create_invite()
        elif path == "/v1/pairing/accept" and method == "POST":
            self._handle_accept()
        elif path == "/v1/pairing/peers" and method == "GET":
            peers = self.app.store.list_peers()
            self._send_json(200, {"peers": [self._peer_view(p) for p in peers]})
        elif path == "/v1/pairing/allowed" and method == "GET":
            self._send_json(200, {"allowed": self.app.store.active_server_names()})
        elif (path.startswith("/v1/pairing/peers/") and method == "DELETE"
              and len(path) > len("/v1/pairing/peers/")):
            server_name = unquote(path[len("/v1/pairing/peers/"):])
            self._handle_sever(server_name)
        else:
            self._error(404, "M_UNRECOGNIZED", "no such pairing endpoint")

    @staticmethod
    def _peer_view(peer):
        return {
            "server_name": peer["server_name"],
            "state": peer["state"],
            "paired_at": _iso(peer["paired_at"]),
            "severed_at": _iso(peer["severed_at"]),
            "severed_by": peer["severed_by"],
            "peer_api_url": peer["peer_api_url"],
        }

    # --- endpoint bodies -------------------------------------------------

    def _handle_create_invite(self):
        invite_id = secrets.token_urlsafe(INVITE_ID_BYTES)
        secret = secrets.token_urlsafe(SECRET_BYTES)
        expires_at = self.app.store.create_invite(
            invite_id, secret, self.config.invite_ttl)
        self._send_json(200, {
            "invite_id": invite_id,
            "expires_at": _iso(expires_at),
            "qr_payload": build_qr_payload(
                self.config.server_name, self.config.api_url,
                invite_id, secret),
        })

    @staticmethod
    def _validate_redeem_body(payload):
        """Extract the redeem fields or return an error tuple.

        server_name is normalised to the canonical lower-case DNS form
        (strict grammar: no ports, no IP literals, no localhost — review
        B4); api_url stays free-form http(s) (it is only ever *called*,
        never used to build the key-verification URL — that comes from
        PAIRING_PEER_KEY_URL_TEMPLATE + the validated server_name).
        """
        invite_id = payload.get("invite_id")
        secret = payload.get("secret")
        server_name = payload.get("server_name")
        api_url = payload.get("api_url")
        if not all(isinstance(v, str) and v for v in (invite_id, secret)):
            return None, (400, "M_INVALID_PARAM", "invite_id and secret are required")
        try:
            server_name = normalize_server_name(server_name)
        except ServerNameError as exc:
            return None, (400, "M_INVALID_PARAM", str(exc))
        if not isinstance(api_url, str) or not (
                api_url.startswith("http://") or api_url.startswith("https://")):
            return None, (400, "M_INVALID_PARAM", "api_url must be an http(s) URL")
        return (invite_id, secret, server_name, api_url), None

    def _handle_redeem(self):
        client_ip = self._client_ip()
        payload, err = self._read_json()
        if err:
            self._error(*err)
            return
        if not self.app.limiter.allow(client_ip):
            self.app.store.record_event("redeem_rate_limited", ip=client_ip)
            self._error(429, "M_LIMIT_EXCEEDED",
                        "too many redeem attempts; retry later")
            return
        fields, err = self._validate_redeem_body(payload)
        if err:
            self._error(*err)
            return
        invite_id, secret, server_name, api_url = fields
        if not self.app.invite_limiter.allow("invite:" + invite_id):
            self.app.store.record_event("redeem_rate_limited", ip=client_ip,
                                         invite_id=invite_id)
            self._error(429, "M_LIMIT_EXCEEDED",
                        "too many redeem attempts for this invite; retry later")
            return

        # Review B4: fully validate the invite (read-only, constant-time)
        # BEFORE any outbound key fetch — an unauthenticated caller must
        # not be able to trigger requests or use the service as a
        # liveness oracle.
        ok, reason = self.app.store.check_invite(invite_id, secret)
        if not ok:
            self.app.store.record_event("redeem_rejected",
                                        invite_id=invite_id,
                                        server_name=server_name, reason=reason)
            status, errcode, message = REDEEM_ERRORS[reason]
            self._error(status, errcode, message)
            return

        # Peer identity check (anti-impersonation): fetch the key the peer
        # publishes for the server_name it claims and require a match.
        # The URL comes only from the configured template + the strictly
        # validated server_name above.
        if self.config.verify_peer_key:
            try:
                published = fetch_peer_key_server_name(
                    server_name, self.config.peer_key_url_template,
                    self.config.insecure_tls)
            except (OSError, ValueError) as exc:
                self.app.store.record_event(
                    "peer_verify_failed", server_name=server_name,
                    reason="unreachable")
                log.warning("peer key fetch for %s failed: %s", server_name, exc)
                self._error(403, "M_FORBIDDEN",
                            "could not verify peer server key")
                return
            if (published or "").lower() != server_name:
                self.app.store.record_event(
                    "peer_verify_failed", server_name=server_name,
                    reason="mismatch")
                self._error(403, "M_FORBIDDEN",
                            "peer server key does not match server_name")
                return

        # Both checks passed: mint the per-peer shared secret and consume
        # the invite atomically (review B2/B4).
        shared_secret = secrets.token_hex(NOTIFY_SECRET_BYTES)
        ok, reason = self.app.store.redeem_invite(
            invite_id, secret, server_name, api_url,
            shared_secret=shared_secret)
        if not ok:
            status, errcode, message = REDEEM_ERRORS[reason]
            self._error(status, errcode, message)
            return
        # The secret travels only in this TLS-protected response; the
        # accepting side stores it on its own peers row.
        self._send_json(200, {"ok": True, "server_name": self.config.server_name,
                              "shared_secret": shared_secret})

    def _handle_accept(self):
        payload, err = self._read_json()
        if err:
            self._error(*err)
            return
        qr_payload = payload.get("qr_payload")
        try:
            remote_name, api_url, invite_id, secret = parse_qr_payload(qr_payload)
        except QrPayloadError as exc:
            self._error(400, "M_INVALID_PARAM", str(exc))
            return

        # Call the other server's redeem as ourselves.
        status, resp = None, None
        try:
            status, resp = post_json(
                api_url + "/redeem",
                {"invite_id": invite_id, "secret": secret,
                 "server_name": self.config.server_name,
                 "api_url": self.config.api_url},
                insecure_tls=self.config.insecure_tls)
        except OSError as exc:
            log.warning("accept: redeem call to %s failed: %s", remote_name, exc)
            self._error(502, "M_UNKNOWN", "peer pairing endpoint unreachable")
            return
        if not (200 <= status < 300):
            # Surface the peer's own error (e.g. expired invite) as-is.
            errcode = (resp or {}).get("errcode", "M_UNKNOWN")
            message = (resp or {}).get(
                "error", "peer rejected the invite (HTTP {})".format(status))
            self.app.store.record_event("accept_rejected",
                                        server_name=remote_name,
                                        peer_status=status)
            self._send_json(status, {"errcode": errcode, "error": message})
            return

        # Pairing succeeded over there; record the peer locally. The invite
        # row lives on the other server, so there is nothing to redeem
        # locally — a direct upsert marks the pairing active here too. The
        # redeem response carries the per-peer shared secret for
        # notify-severed authentication (review B2); it travels only over
        # the TLS connection to the peer's gate.
        self.app.store.upsert_peer_active(
            remote_name, api_url,
            shared_secret=resp.get("shared_secret")
            if isinstance(resp, dict) else None)
        self._send_json(200, {"ok": True, "server_name": remote_name})

    def _handle_sever(self, server_name):
        try:
            server_name = normalize_server_name(server_name)
        except ServerNameError:
            # An invalid name can never be a paired peer.
            self._error(404, "M_NOT_FOUND", "unknown peer")
            return
        peer = self.app.store.get_peer(server_name)
        if peer is None:
            self._error(404, "M_NOT_FOUND", "unknown peer")
            return
        self.app.store.sever_peer(server_name, "local")
        # A severed pairing must not stay cancellable: burn every unused
        # invite so holders of an outstanding QR cannot resurrect it.
        invalidated = self.app.store.invalidate_unused_invites()
        # Best-effort heads-up, HMAC-signed with the pairing-time shared
        # secret (review B2): our own gate already refuses the peer both
        # ways from our side (they can neither push to us nor ask us to
        # pull). The one path left open without this notification is our
        # *outbound* pull — the peer's gate still lists us as active until
        # it hears about the sever. Failure to notify changes nothing
        # locally; severing is unilateral.
        body_bytes = json.dumps(
            {"server_name": self.config.server_name}).encode("utf-8")
        headers = None
        if peer.get("shared_secret"):
            headers = build_notify_headers(
                peer["shared_secret"], self.config.server_name, body_bytes)
        # else: legacy pairing from before shared secrets existed; the
        # notify is sent unsigned and will be refused by a current peer.
        notified = False
        try:
            status, resp = post_json(
                peer["peer_api_url"] + "/notify-severed", body_bytes,
                insecure_tls=self.config.insecure_tls,
                timeout=NOTIFY_BODY_TIMEOUT, headers=headers)
            notified = 200 <= status < 300
        except OSError as exc:
            log.warning("notify-severed to %s failed (ignored): %s",
                        server_name, exc)
        self.app.store.record_event(
            "sever_notified" if notified else "sever_notify_failed",
            server_name=server_name, invites_invalidated=invalidated)
        self._send_json(200, {
            "ok": True,
            "server_name": server_name,
            "state": "severed",
            "severed_by": "local",
            "notified": notified,
        })

    def _reject_notify(self, client_ip, reason):
        """Count + log a refused notify-severed; never writes events."""
        self.app.notify_rejected += 1
        log.warning("notify-severed rejected from %s (%s; %d rejected in"
                    " total)", client_ip, reason, self.app.notify_rejected)

    def _handle_notify_severed(self):
        """Peer-signed sever notification (review B2).

        Authentication: the pairing-time shared secret proves the caller
        really is the paired server. Every failure — unknown origin, bad
        signature, stale timestamp, replayed nonce, rate limit — is
        answered without writing events (log counter only) so the
        endpoint cannot be used as a write-amplification oracle.
        """
        client_ip = self._client_ip()
        if not self.app.notify_limiter.allow(client_ip):
            self._reject_notify(client_ip, "rate_limited")
            self._error(429, "M_LIMIT_EXCEEDED",
                        "too many notify attempts; retry later")
            return
        raw, err = self._read_body()
        if err:
            self._error(*err)
            return
        origin = self.headers.get("X-Botville-Origin") or ""
        ts = self.headers.get("X-Botville-Ts") or ""
        nonce = self.headers.get("X-Botville-Nonce") or ""
        sig = self.headers.get("X-Botville-Sig") or ""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            payload = None
        claimed = payload.get("server_name") if isinstance(payload, dict) else None
        if not isinstance(claimed, str) or not claimed:
            self._reject_notify(client_ip, "missing_body_server_name")
            self._error(401, "M_UNAUTHORIZED", "invalid notify authentication")
            return
        try:
            origin = normalize_server_name(origin)
        except ServerNameError:
            origin = ""
        if not origin or origin != claimed.lower():
            self._reject_notify(client_ip, "origin_mismatch")
            self._error(401, "M_UNAUTHORIZED", "invalid notify authentication")
            return
        peer = self.app.store.get_peer(origin)
        secret = (peer or {}).get("shared_secret")
        if peer is None or not secret:
            # Unknown peer, or a pairing from before shared secrets existed.
            self._reject_notify(client_ip, "unknown_peer")
            self._error(401, "M_UNAUTHORIZED", "invalid notify authentication")
            return
        if not ts.isdigit() or len(ts) > 12 or \
                abs(time.time() - int(ts)) > NOTIFY_TS_WINDOW_SECONDS:
            self._reject_notify(client_ip, "bad_timestamp")
            self._error(401, "M_UNAUTHORIZED", "invalid notify authentication")
            return
        if len(nonce) < NONCE_MIN_HEX_CHARS or \
                not re.fullmatch(r"[0-9a-fA-F]+", nonce):
            self._reject_notify(client_ip, "bad_nonce")
            self._error(401, "M_UNAUTHORIZED", "invalid notify authentication")
            return
        expected = notify_signature(secret, origin, ts, nonce, raw)
        # compare_digest requires ASCII strings, so gate the shape first.
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sig) or \
                not hmac.compare_digest(sig.lower(), expected):
            self._reject_notify(client_ip, "bad_signature")
            self._error(401, "M_UNAUTHORIZED", "invalid notify authentication")
            return
        if self.app.nonces.check_and_mark((origin, nonce)):
            self._reject_notify(client_ip, "replayed_nonce")
            self._error(401, "M_UNAUTHORIZED", "invalid notify authentication")
            return

        # Verified: active -> severed(severed_by=remote) only. An explicit
        # local sever is never overwritten (the peer row we re-read tells
        # the caller which one holds).
        changed = self.app.store.sever_peer(origin, "remote",
                                            only_if_active=True)
        peer = self.app.store.get_peer(origin)
        if changed:
            log.info("notify-severed accepted from %s", origin)
        self._send_json(200, {"ok": True, "server_name": origin,
                              "state": "severed",
                              "severed_by": peer["severed_by"]})


def build_server(config):
    """Bind the ThreadingHTTPServer (port 0 allowed for tests)."""
    httpd = ThreadingHTTPServer((config.host, config.port), PairingHandler)
    httpd.daemon_threads = True
    httpd.app = PairingApp(config)
    return httpd


def start_pruner(app, interval=PRUNE_INTERVAL_SECONDS):
    """Retention loop: prune consumed/expired invites and old events at
    startup, then hourly (store.prune). Daemon thread; never fatal."""
    def run():
        while True:
            try:
                counts = app.store.prune()
                if any(counts.values()):
                    log.info("pruned old pairing rows: %s", counts)
            except sqlite3.Error as exc:
                log.warning("pairing prune failed: %s", exc)
            time.sleep(interval)

    thread = threading.Thread(target=run, name="pairing-pruner", daemon=True)
    thread.start()
    return thread


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        config = PairingConfig.from_env(os.environ)
    except ConfigError as exc:
        print("pairing.service: {}".format(exc), file=sys.stderr)
        return 2
    httpd = build_server(config)
    start_pruner(httpd.app)
    host, port = httpd.server_address[:2]
    log.info("pairing service listening on %s:%s (db=%s, server_name=%s)",
             host, port, config.db_path, config.server_name)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

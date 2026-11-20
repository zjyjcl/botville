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
  GET  /health                           any    liveness probe:
         {ok: true, relations_gateway: none|test-recorder|http} (A-R4).

Pairing facts for the gateway (A-R1; service token, read-only):

  GET  /v1/pairing/internal/peers/{server_name}   service
         -> {state: active|severed|unpaired, changed_at: ISO-8601|null}
  GET  /v1/pairing/internal/events?since=<cursor>&limit=<1-500>   service
         -> {events: [{cursor, kind: peer_paired|peer_severed,
                       server_name, at, repaired | severed_by}],
             next_cursor, has_more}
         Cursors are integers (event ids): strictly increasing, never
         reused; omit `since` (= 0) to start at the beginning. A cursor
         whose following events were pruned (30-day retention) or that is
         ahead of the log (restored DB) gets 410 BV_CURSOR_EXPIRED with
         `resync_cursor` = the prune watermark (every later event is still
         retained): re-read the peers you track, then continue from it;
         replayed facts must be handled idempotently.
  Auth: `Authorization: Bearer $PAIRING_SERVICE_TOKEN` (constant-time),
  a token distinct from the admin token. Unset token = every internal
  call is refused (401, fail-closed); the admin token is never accepted
  here and the service token never opens admin endpoints. The internal
  paths are NOT reachable through the /_matrix/pairing/ alias, so fedgate
  can never expose them to peers.

Paths under /_matrix/pairing/... are accepted as aliases of /v1/pairing/...
so fedgate can forward them unmodified (`/_matrix/pairing/redeem` ==
`/v1/pairing/redeem`); fedgate only forwards redeem and notify-severed.

server_names are canonicalised to lower-case DNS names on every path in
(validate.normalize_server_name). Rate limiting keys on the *effective*
client IP: X-Forwarded-For is only honoured when the direct peer is in
PAIRING_TRUSTED_PROXIES (fedgate overwrites that header with the direct
client IP, review B3).

Admin auth: `Authorization: Bearer $PAIRING_ADMIN_TOKEN` (constant-time
compare); the service refuses to start without it. PAIRING_SERVICE_TOKEN
is optional (internal endpoints answer 401 without it) but, when set,
must be at least 32 characters and differ from the admin token, or the
service refuses to start. A-R4: the outbound relation send API
(/v1/pairing/internal/relations/*) takes its own PAIRING_RELATIONS_SEND_TOKEN
(same rules, and it must also differ from the service token); the facts
token gets 401 there and the send token gets 401 on the facts endpoints.
Outbound targets must be https with only public addresses unless
PAIRING_RELATIONS_ALLOW_PRIVATE=true (test stacks; every such send is
logged as AUDIT). Secrets/tokens are never written to the log.
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

from . import relations
from .store import (
    CursorExpired,
    REDEEM_BAD_SECRET,
    REDEEM_EXPIRED,
    REDEEM_PREDATES_SEVER,
    REDEEM_UNKNOWN,
    REDEEM_USED,
    PairingStore,
)
from .hswhitelist import HsWhitelistWriter
from .hswhitelist import MarkerWriteError as HsMarkerWriteError
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
HS_WHITELIST_RESYNC_SECONDS = 60.0
MAX_BODY_BYTES = 65536
PEER_HTTP_TIMEOUT = 15.0
INVITE_ID_BYTES = 12  # urlsafe -> 16 chars
SERVICE_TOKEN_MIN_CHARS = 32
# A-R1 internal facts namespace. INTERNAL_ROOT (no trailing slash) and
# everything under INTERNAL_PREFIX are internal: both go through service
# auth, so the bare root can never fall through to another route.
INTERNAL_ROOT = "/v1/pairing/internal"
INTERNAL_PREFIX = INTERNAL_ROOT + "/"
# At most one "internal request refused" warning per interval; the rest
# are counted and reported in the next warning (#243 review 2).
INTERNAL_REJECT_LOG_INTERVAL = 60.0
# A-R3 relation transport rate limits (per minute). Inbound: per effective
# client IP before authentication, per authenticated origin after it.
# Outbound: per destination peer, so one gateway bug cannot hammer a peer.
RELATIONS_PER_IP_PER_MINUTE = 120
RELATIONS_PER_ORIGIN_PER_MINUTE = 60
RELATIONS_OUT_PER_PEER_PER_MINUTE = 60
RELATIONS_PREFIX = "/v1/pairing/relations/"
# Same throttle for refused inbound relation messages (counted always,
# logged at most once per interval with the suppressed count).
RELATIONS_REJECT_LOG_INTERVAL = 60.0
FACTS_DEFAULT_LIMIT = 100
FACTS_MAX_LIMIT = 500
CURSOR_MAX_DIGITS = 18  # fits a signed 64-bit SQLite integer
SECRET_BYTES = 32  # urlsafe -> 43 chars

REDEEM_ERRORS = {
    REDEEM_UNKNOWN: (404, "M_NOT_FOUND", "unknown invite"),
    REDEEM_USED: (403, "M_FORBIDDEN", "invite already used"),
    REDEEM_EXPIRED: (403, "M_FORBIDDEN", "invite expired"),
    REDEEM_BAD_SECRET: (403, "M_FORBIDDEN", "invalid invite secret"),
    REDEEM_PREDATES_SEVER: (403, "M_FORBIDDEN",
                            "invite predates the sever of this peer"),
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


class LogThrottle:
    """Rate-limit one repetitive warning: allow() is True at most once per
    `interval` seconds and reports how many calls it swallowed since the
    last allowed one. Thread-safe."""

    def __init__(self, interval):
        self.interval = interval
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._suppressed = 0

    def allow(self, now=None):
        """(allowed, suppressed_since_last_allowed)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if now >= self._next_at:
                suppressed, self._suppressed = self._suppressed, 0
                self._next_at = now + self.interval
                return True, suppressed
            self._suppressed += 1
            return False, 0


class PairingConfig:
    def __init__(self, db_path, admin_token, server_name, api_url,
                 invite_ttl=DEFAULT_INVITE_TTL, verify_peer_key=True,
                 peer_key_url_template="https://{server_name}/_matrix/key/v2/server",
                 insecure_tls=False, host="127.0.0.1", port=8090,
                 trusted_proxies=(), service_token=None,
                 hs_whitelist_path=None, hs_restart_marker=None,
                 relations_gateway="none", relations_test_record=None,
                 relations_send_token=None, relations_allow_private=False,
                 relations_gateway_url=None, relations_gateway_token=None):
        self.db_path = db_path
        self.admin_token = admin_token
        if service_token:
            if len(service_token) < SERVICE_TOKEN_MIN_CHARS:
                raise ConfigError(
                    "PAIRING_SERVICE_TOKEN must be at least {} characters".format(
                        SERVICE_TOKEN_MIN_CHARS))
            if hmac.compare_digest(service_token.encode("utf-8"),
                                   (admin_token or "").encode("utf-8")):
                raise ConfigError(
                    "PAIRING_SERVICE_TOKEN must differ from PAIRING_ADMIN_TOKEN")
        self.service_token = service_token or None
        # A-R4: the outbound relation send endpoint has its OWN token, so
        # the read-only facts token (PAIRING_SERVICE_TOKEN) can never send
        # anything. Same rules: >= 32 chars, distinct from the admin token
        # and from the facts token; unset = the send endpoint refuses all.
        if relations_send_token:
            if len(relations_send_token) < SERVICE_TOKEN_MIN_CHARS:
                raise ConfigError(
                    "PAIRING_RELATIONS_SEND_TOKEN must be at least {}"
                    " characters".format(SERVICE_TOKEN_MIN_CHARS))
            for other, name in ((admin_token, "PAIRING_ADMIN_TOKEN"),
                                (service_token, "PAIRING_SERVICE_TOKEN")):
                if other and hmac.compare_digest(
                        relations_send_token.encode("utf-8"),
                        other.encode("utf-8")):
                    raise ConfigError(
                        "PAIRING_RELATIONS_SEND_TOKEN must differ from"
                        " {}".format(name))
        self.relations_send_token = relations_send_token or None
        # A-R4 (#249 review 2): outbound relation targets must be https on a
        # public address unless this explicit switch is on (test stacks with
        # private compose addresses); every private/plain-http send is then
        # audit-logged.
        self.relations_allow_private = bool(relations_allow_private)
        # A-R3: how verified inbound relation messages reach the local
        # gateway. "none" (default) refuses them with 503; "test-recorder"
        # is for tests/CI only.
        # #257 aligns the real local HTTP endpoint; none stays the default.
        if relations_gateway == "http":
            try:
                relations.validate_gateway_http(relations_gateway_url,
                                                relations_gateway_token)
            except ValueError as exc:
                raise ConfigError(str(exc)) from None
        self.relations_gateway_url = relations_gateway_url
        self.relations_gateway_token = relations_gateway_token
        if relations_gateway not in relations.GATEWAY_MODES:
            raise ConfigError(
                "PAIRING_RELATIONS_GATEWAY must be one of: {}".format(
                    ", ".join(relations.GATEWAY_MODES)))
        if relations_gateway == "test-recorder" and \
                not relations.record_path_ok(relations_test_record):
            raise ConfigError(
                "PAIRING_RELATIONS_GATEWAY=test-recorder needs an absolute"
                " PAIRING_RELATIONS_TEST_RECORD")
        self.relations_gateway = relations_gateway
        self.relations_test_record = relations_test_record or None
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
        # A-R2 (#118 item 8): optional Synapse federation_domain_whitelist
        # include + restart marker; unset = feature off (fedgate only).
        if hs_restart_marker and not hs_whitelist_path:
            raise ConfigError(
                "PAIRING_HS_RESTART_MARKER needs PAIRING_HS_WHITELIST_PATH")
        for key, value in (("PAIRING_HS_WHITELIST_PATH", hs_whitelist_path),
                           ("PAIRING_HS_RESTART_MARKER", hs_restart_marker)):
            if value and not os.path.isabs(value):
                raise ConfigError("{} must be an absolute path".format(key))
        if (hs_whitelist_path and hs_restart_marker
                and os.path.abspath(hs_whitelist_path)
                == os.path.abspath(hs_restart_marker)):
            raise ConfigError(
                "PAIRING_HS_RESTART_MARKER must differ from"
                " PAIRING_HS_WHITELIST_PATH")
        self.hs_whitelist_path = hs_whitelist_path or None
        self.hs_restart_marker = hs_restart_marker or None

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
            service_token=env.get("PAIRING_SERVICE_TOKEN", "") or None,
            relations_send_token=env.get(
                "PAIRING_RELATIONS_SEND_TOKEN", "") or None,
            relations_allow_private=_env_flag(
                env, "PAIRING_RELATIONS_ALLOW_PRIVATE", False),
            relations_gateway=(env.get("PAIRING_RELATIONS_GATEWAY", "").strip()
                               or "none"),
            relations_gateway_url=env.get("PAIRING_RELATIONS_GATEWAY_URL", ""),
            relations_gateway_token=env.get("GATEWAY_RELATIONS_INBOUND_TOKEN", ""),
            relations_test_record=env.get(
                "PAIRING_RELATIONS_TEST_RECORD", "").strip() or None,
            hs_whitelist_path=env.get("PAIRING_HS_WHITELIST_PATH", "").strip(),
            hs_restart_marker=env.get("PAIRING_HS_RESTART_MARKER", "").strip(),
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
        self.internal_rejected = 0
        # A-R3 relation transport.
        self.relations_gateway = relations.build_gateway_client(
            config.relations_gateway, config.relations_test_record,
            config.relations_gateway_url, config.relations_gateway_token)
        self.relation_nonces = relations.StrictNonceGuard()
        self.relations_ip_limiter = RateLimiter(limit=RELATIONS_PER_IP_PER_MINUTE)
        self.relations_origin_limiter = RateLimiter(
            limit=RELATIONS_PER_ORIGIN_PER_MINUTE)
        self.relations_out_limiter = RateLimiter(
            limit=RELATIONS_OUT_PER_PEER_PER_MINUTE)
        self.relations_rejected = 0
        self._relations_rejected_lock = threading.Lock()
        self.relations_reject_log = LogThrottle(
            RELATIONS_REJECT_LOG_INTERVAL)
        self._internal_rejected_lock = threading.Lock()
        self.internal_reject_log = LogThrottle(INTERNAL_REJECT_LOG_INTERVAL)

        self.hs_whitelist = None
        if config.hs_whitelist_path:
            self.hs_whitelist = HsWhitelistWriter(
                config.hs_whitelist_path, config.server_name,
                marker_path=config.hs_restart_marker)
        self.hs_whitelist_failures = 0
        self._hs_whitelist_failures_lock = threading.Lock()

    def note_relation_rejected(self):
        """Count one refused inbound relation message; returns the total."""
        with self._relations_rejected_lock:
            self.relations_rejected += 1
            return self.relations_rejected

    def note_internal_rejected(self):
        """Count one refused internal request; returns the new total."""
        with self._internal_rejected_lock:
            self.internal_rejected += 1
            return self.internal_rejected

    def _hs_whitelist_failure_gap(self):
        """Test hook between the counter read and write (N1 / #253 nit 5)."""

    def _note_hs_whitelist_failure(self):
        # Return value unused (#253 review nit 12); the lock covers a
        # split read-modify-write so tests can stretch the gap.
        with self._hs_whitelist_failures_lock:  # #247 review N1
            current = self.hs_whitelist_failures
            self._hs_whitelist_failure_gap()
            self.hs_whitelist_failures = current + 1

    def sync_hs_whitelist(self):
        """Re-render the homeserver whitelist from the peers table (A-R2).

        Called after every pairing state change, at startup and from the
        background loop; idempotent (unchanged content writes nothing).
        Never raises (#247 review S3: any Exception, not only OSError /
        sqlite3.Error): the pairing change is already committed and
        fedgate enforces it regardless, so a failure is logged and retried
        on the next call. Returns True when a restart marker was written.
        The peers table is read under the writer lock (review S1).
        Lock order is writer -> store (#253 review nit 10): this method
        must not be called while holding the store lock.
        """
        if self.hs_whitelist is None:
            return False
        try:
            changed = self.hs_whitelist.sync(self.store.active_server_names)
        except HsMarkerWriteError as exc:
            self._note_hs_whitelist_failure()
            log.error("homeserver whitelist updated but the restart marker"
                      " could not be written (%s); a restart is pending and"
                      " the marker is retried every %ds", exc,
                      int(HS_WHITELIST_RESYNC_SECONDS))
            return False
        except Exception as exc:  # noqa: BLE001 - must never fail a request
            self._note_hs_whitelist_failure()
            log.error("homeserver whitelist render failed (%s: %s); fedgate"
                      " still enforces pairing, will retry",
                      type(exc).__name__, exc)
            return False
        if changed:
            log.warning("homeserver federation whitelist changed: restart the"
                        " homeserver, then delete %s",
                        self.hs_whitelist.marker_path)
        return changed


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

    def _is_service(self):
        """Service-token check for the internal facts API (fail-closed:
        no configured token means nobody is a service caller)."""
        token = self.config.service_token
        if not token:
            return False
        auth = self.headers.get("Authorization") or ""
        return hmac.compare_digest(auth.encode("utf-8"),
                                   ("Bearer " + token).encode("utf-8"))

    def _is_relation_sender(self):
        """Send-token check for the outbound relations endpoint (A-R4;
        fail-closed like _is_service)."""
        token = self.config.relations_send_token
        if not token:
            return False
        auth = self.headers.get("Authorization") or ""
        return hmac.compare_digest(auth.encode("utf-8"),
                                   ("Bearer " + token).encode("utf-8"))

    def _handle_internal(self, method, path, query):
        """A-R1 pairing facts (read-only, PAIRING_SERVICE_TOKEN) + A-R3
        relation send (relations/<kind>, POST, PAIRING_RELATIONS_SEND_TOKEN
        since A-R4). Auth first, uniformly 401, so an unauthenticated
        caller learns nothing about routes or peers; each token opens only
        its own namespace (the facts token cannot send, the send token
        cannot read facts)."""
        rest = path[len(INTERNAL_PREFIX):] if path.startswith(INTERNAL_PREFIX) \
            else ""  # the bare INTERNAL_ROOT names no endpoint
        is_send_route = rest.startswith("relations/")
        authorized = self._is_relation_sender() if is_send_route \
            else self._is_service()
        if not authorized:
            total = self.app.note_internal_rejected()
            allowed, suppressed = self.app.internal_reject_log.allow()
            if allowed:
                # Never the token; at most one line per interval so a
                # misconfigured or hostile poller cannot flood the log.
                log.warning("internal facts request refused from %s (%d"
                            " refused in total; %d similar warnings"
                            " suppressed since the last one)",
                            self._client_ip(), total, suppressed)
            self._error(401, "M_UNKNOWN_TOKEN",
                        "internal endpoints need the pairing service token")
            return
        if is_send_route:
            self._handle_relation_outbound(method, rest[len("relations/"):])
            return
        if rest == "events":
            route = "events"
        elif rest.startswith("peers/") and len(rest) > len("peers/") \
                and "/" not in rest[len("peers/"):]:
            route = "peer"
        else:
            self._error(404, "M_UNRECOGNIZED", "no such internal endpoint")
            return
        if method != "GET":
            self.close_connection = True
            body = json.dumps({"errcode": "M_UNRECOGNIZED",
                               "error": "method not allowed"}).encode("utf-8")
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "peer":
            try:
                server_name = normalize_server_name(
                    unquote(rest[len("peers/"):]))
            except ServerNameError:
                self._error(400, "M_INVALID_PARAM", "invalid server_name")
                return
            fact = self.app.store.peer_fact(server_name)
            self._send_json(200, {"state": fact["state"],
                                  "changed_at": _iso(fact["changed_at"])})
            return
        params, err = self._parse_facts_query(query)
        if err:
            self._error(*err)
            return
        since, limit = params
        try:
            events, next_cursor, has_more = self.app.store.fact_events(
                since, limit)
        except CursorExpired as exc:
            self._send_json(410, {
                "errcode": "BV_CURSOR_EXPIRED",
                "error": "cursor can no longer be served gap-free ({});"
                         " resync peers, then continue from resync_cursor"
                         .format(exc.reason),
                "reason": exc.reason,
                "resync_cursor": exc.resync_cursor,
            })
            return
        for event in events:
            event["at"] = _iso(event["at"])
        self._send_json(200, {"events": events, "next_cursor": next_cursor,
                              "has_more": has_more})

    @staticmethod
    def _parse_facts_query(query):
        """since/limit for the events feed; anything else is a 400."""
        since, limit = 0, FACTS_DEFAULT_LIMIT
        seen = set()
        for part in query.split("&") if query else ():
            key, sep, value = part.partition("=")
            if not sep or key in seen or key not in ("since", "limit"):
                return None, (400, "M_INVALID_PARAM",
                              "only since and limit are accepted, once each")
            seen.add(key)
            if not value.isdigit() or not value.isascii() \
                    or len(value) > CURSOR_MAX_DIGITS:
                return None, (400, "M_INVALID_PARAM",
                              "{} must be a non-negative integer".format(key))
            if key == "since":
                since = int(value)
            else:
                limit = int(value)
                if not 1 <= limit <= FACTS_MAX_LIMIT:
                    return None, (400, "M_INVALID_PARAM",
                                  "limit must be within 1-{}".format(
                                      FACTS_MAX_LIMIT))
        return (since, limit), None

    def _dispatch(self, method):
        # Raw request-target, not urlsplit(): "//x/y" would otherwise be
        # parsed as a network-location and hide the empty segment.
        path, _, query = self.path.partition("?")
        # Path hygiene (review: bypass-via-path): refuse traversal
        # segments, empty segments and encoded separators before routing,
        # so prefix matching can never be fed a normalisable path.
        if path_is_suspicious(path):
            self._error(404, "M_UNRECOGNIZED", "malformed request path")
            return
        # fedgate forwards /_matrix/pairing/<x> unchanged; treat it as an
        # alias of /v1/pairing/<x> so peers only need one public port.
        aliased = path.startswith("/_matrix/pairing/")
        if aliased:
            path = "/v1/pairing/" + path[len("/_matrix/pairing/"):]

        # A-R1 internal facts: service token only, and never via the
        # /_matrix/pairing/ alias (that prefix is what fedgate forwards).
        # Decision (#243 review): the alias answers 404 BEFORE auth, even
        # for the right token. /_matrix/pairing/ is the public namespace
        # fedgate forwards; internal endpoints do not exist there, and a
        # 401 would advertise an authenticated variant worth guessing
        # tokens against on the public prefix.
        if path == INTERNAL_ROOT or path.startswith(INTERNAL_PREFIX):
            if aliased:
                self._error(404, "M_UNRECOGNIZED", "no such pairing endpoint")
                return
            self._handle_internal(method, path, query)
            return

        if path == "/health":
            # A-R4 (#249 review 3): the relations gateway mode is visible
            # so a test-recorder left on in production is easy to spot.
            self._send_json(200, {"ok": True, "relations_gateway":
                                  self.config.relations_gateway})
            return

        if path == "/v1/pairing/redeem" and method == "POST":
            self._handle_redeem()
            return
        if path == "/v1/pairing/notify-severed" and method == "POST":
            self._handle_notify_severed()
            return
        if path.startswith(RELATIONS_PREFIX):
            # Peer-facing (fedgate forwards /_matrix/pairing/relations/*);
            # authenticated by the per-peer HMAC, never by a bearer token.
            self._handle_relation_inbound(method, path[len(RELATIONS_PREFIX):])
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
        ok, reason = self.app.store.check_invite(invite_id, secret,
                                                  server_name=server_name)
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
        self.app.sync_hs_whitelist()
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
        shared_secret = resp.get("shared_secret") if isinstance(resp, dict) else None
        if not (isinstance(shared_secret, str)
                and re.fullmatch(r"[0-9a-f]{64}", shared_secret)):
            # A peer handing back an unusable secret would later break our
            # signed notify-severed (review r2 #5); refuse the pairing.
            self.app.store.record_event("accept_rejected",
                                        server_name=remote_name,
                                        reason="bad_shared_secret")
            self._error(502, "M_UNKNOWN",
                        "peer returned an invalid shared secret")
            return
        self.app.store.upsert_peer_active(
            remote_name, api_url, shared_secret=shared_secret)
        self.app.sync_hs_whitelist()
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
        self.app.sync_hs_whitelist()
        # A severed pairing must not stay cancellable: invites issued before
        # this sever are refused for this peer at redeem time
        # (REDEEM_PREDATES_SEVER); unrelated pending invites stay valid.
        invalidated = 0
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
            try:
                headers = build_notify_headers(
                    peer["shared_secret"], self.config.server_name, body_bytes)
            except (ValueError, TypeError) as exc:
                log.warning("cannot sign notify-severed for %s: %s",
                            server_name, exc)
                headers = None
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
            self.app.sync_hs_whitelist()
        self._send_json(200, {"ok": True, "server_name": origin,
                              "state": "severed",
                              "severed_by": peer["severed_by"]})


    # --- A-R3 relation transport ------------------------------------------

    def _method_not_allowed(self, allow):
        self.close_connection = True
        body = json.dumps({"errcode": "M_UNRECOGNIZED",
                           "error": "method not allowed"}).encode("utf-8")
        self.send_response(405)
        self.send_header("Allow", allow)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject_relation(self, client_ip, reason, status=401,
                         errcode="M_UNAUTHORIZED",
                         message="invalid relations authentication"):
        """Count + log a refused inbound relation message; never writes
        events and never echoes request content or secrets. The warning is
        throttled like the internal-401 one (LogThrottle, #243 review 2):
        every refusal is counted, at most one line per interval is logged
        with the number swallowed since the previous line."""
        total = self.app.note_relation_rejected()
        allowed, suppressed = self.app.relations_reject_log.allow()
        if allowed:
            log.warning("relation message rejected from %s (%s; %d rejected"
                        " in total; %d similar warnings suppressed since the"
                        " last one)", client_ip, reason, total, suppressed)
        self._error(status, errcode, message)

    def _handle_relation_inbound(self, method, kind):
        """POST /v1/pairing/relations/<kind> from a paired peer (A-R3).

        Order: per-IP limit -> bounded body -> origin/peer -> timestamp ->
        nonce shape -> HMAC -> per-origin limit -> replay guard -> active
        gate -> envelope format -> local gateway. Every failure refuses;
        nothing is delivered on a doubt."""
        if kind not in relations.RELATION_KINDS:
            self._error(404, "M_UNRECOGNIZED", "no such relations endpoint")
            return
        if method != "POST":
            self._method_not_allowed("POST")
            return
        client_ip = self._client_ip()
        if not self.app.relations_ip_limiter.allow(client_ip):
            self._reject_relation(client_ip, "rate_limited_ip", 429,
                                  "M_LIMIT_EXCEEDED",
                                  "too many relation messages; retry later")
            return
        raw, err = self._read_body()
        if err:
            self._reject_relation(client_ip, "body_too_large", *err)
            return
        if len(raw) > relations.MAX_ENVELOPE_BYTES:
            self._reject_relation(client_ip, "body_too_large", 413,
                                  "M_TOO_LARGE", "relation envelope too large")
            return
        origin = self.headers.get("X-Botville-Origin") or ""
        ts = self.headers.get("X-Botville-Ts") or ""
        nonce = self.headers.get("X-Botville-Nonce") or ""
        sig = self.headers.get("X-Botville-Sig") or ""
        try:
            origin = normalize_server_name(origin)
        except ServerNameError:
            self._reject_relation(client_ip, "bad_origin")
            return
        peer = self.app.store.get_peer(origin)
        secret = (peer or {}).get("shared_secret")
        if peer is None or not secret:
            self._reject_relation(client_ip, "unknown_peer")
            return
        if not relations.timestamp_ok(ts):
            self._reject_relation(client_ip, "bad_timestamp")
            return
        if not relations.nonce_ok(nonce):
            self._reject_relation(client_ip, "bad_nonce")
            return
        if not relations.signature_ok(secret, kind, origin, ts, nonce, raw, sig):
            self._reject_relation(client_ip, "bad_signature")
            return
        if not self.app.relations_origin_limiter.allow(origin):
            self._reject_relation(client_ip, "rate_limited_origin", 429,
                                  "M_LIMIT_EXCEEDED",
                                  "too many relation messages; retry later")
            return
        seen = self.app.relation_nonces.check_and_mark((origin, nonce))
        if seen == relations.StrictNonceGuard.SEEN:
            self._reject_relation(client_ip, "replayed_nonce")
            return
        if seen == relations.StrictNonceGuard.FULL:
            self._reject_relation(client_ip, "replay_guard_full", 503,
                                  "M_UNAVAILABLE",
                                  "relations temporarily unavailable")
            return
        # Re-read: a sever may have landed while we verified.
        peer = self.app.store.get_peer(origin)
        if peer is None or peer["state"] != "active":
            self._reject_relation(client_ip, "peer_inactive", 403,
                                  "BV_PEER_INACTIVE", "pairing is not active")
            return
        try:
            envelope = relations.parse_envelope_bytes(
                raw, kind, origin, self.config.server_name)
        except relations.EnvelopeError as exc:
            self._reject_relation(client_ip, "bad_envelope", 400,
                                  "BV_BAD_ENVELOPE", str(exc))
            return
        meta = {"origin": origin, "sent_at": int(ts),
                "received_at": int(time.time())}
        try:
            status, body = relations.checked_gateway_result(
                self.app.relations_gateway.deliver(kind, envelope, meta))
        except relations.GatewayUnavailable as exc:
            # Only the client's fixed reason code, never its message: a
            # client may put request/response bodies in exception text
            # (#249 review 4).
            log.warning("relation %s from %s not delivered (%s)", kind,
                        origin, relations.safe_reason(exc))
            self._error(503, "BV_RELATIONS_UNAVAILABLE",
                        "relations are not available on this server")
            return
        except Exception as exc:  # noqa: BLE001 - must fail closed
            # Type name only -- no message, no traceback (both may carry
            # the envelope or the gateway's answer).
            log.error("relations gateway client failed (%s)",
                      type(exc).__name__)
            self._error(503, "BV_RELATIONS_UNAVAILABLE",
                        "relations are not available on this server")
            return
        log.info("relation %s %s from %s delivered (gateway %d)", kind,
                 envelope["request_id"], origin, status)
        self._send_json(status, body)

    def _handle_relation_outbound(self, method, kind):
        """POST /v1/pairing/internal/relations/<kind> from the local gateway
        (service token already checked): sign and send to an ACTIVE peer,
        with a timeout and no retries. The reply wraps the peer's answer so
        transport failures (5xx here) never look like business answers."""
        if kind not in relations.RELATION_KINDS:
            self._error(404, "M_UNRECOGNIZED", "no such internal endpoint")
            return
        if method != "POST":
            self._method_not_allowed("POST")
            return
        payload, err = self._read_json()
        if err:
            self._error(*err)
            return
        try:
            relations.validate_outbound_request(payload)
            destination = normalize_server_name(payload["destination"])
        except relations.EnvelopeError as exc:
            self._error(400, "BV_BAD_ENVELOPE", str(exc))
            return
        except ServerNameError:
            self._error(400, "BV_BAD_ENVELOPE", "invalid destination")
            return
        if destination == self.config.server_name:
            self._error(400, "BV_BAD_ENVELOPE", "destination is this server")
            return
        peer = self.app.store.get_peer(destination)
        if peer is None or peer["state"] != "active" or \
                not peer.get("shared_secret") or not peer.get("peer_api_url"):
            self._error(409, "BV_PEER_INACTIVE",
                        "destination is not an active paired server")
            return
        if not self.app.relations_out_limiter.allow(destination):
            self._error(429, "M_LIMIT_EXCEEDED",
                        "too many relation messages to this server")
            return
        try:
            envelope = relations.build_envelope(
                kind, self.config.server_name, destination,
                payload["request_id"], payload["payload"])
        except relations.EnvelopeError as exc:
            self._error(400, "BV_BAD_ENVELOPE", str(exc))
            return
        body = relations.encode_envelope(envelope)
        try:
            headers = relations.build_relation_headers(
                peer["shared_secret"], kind, self.config.server_name, body)
        except (ValueError, TypeError):  # corrupt stored secret
            log.error("stored shared secret for %s is unusable", destination)
            self._error(409, "BV_PEER_INACTIVE",
                        "destination is not an active paired server")
            return
        url = peer["peer_api_url"].rstrip("/") + "/relations/" + kind
        verdict, detail = relations.target_policy(
            url, self.config.relations_allow_private)
        if verdict == relations.TARGET_REFUSED:
            log.warning("relation %s to %s refused: peer_api_url %s (set"
                        " PAIRING_RELATIONS_ALLOW_PRIVATE=true only on test"
                        " stacks)", kind, destination, detail)
            self._error(409, "BV_PEER_TARGET_REFUSED",
                        "peer address not allowed for relations")
            return
        if verdict == relations.TARGET_AUDIT:
            log.warning("AUDIT relation %s %s to %s via non-public target"
                        " (%s; PAIRING_RELATIONS_ALLOW_PRIVATE=true)", kind,
                        envelope["request_id"], destination, detail)
        try:
            peer_status, peer_body = relations.post_relation(
                url, body, headers,
                ssl_context=_maybe_insecure_context(self.config.insecure_tls),
                timeout=relations.PEER_TIMEOUT_SECONDS)
        except relations.PeerTimeout:
            log.warning("relation %s to %s timed out", kind, destination)
            self._error(504, "BV_PEER_TIMEOUT", "peer did not answer in time")
            return
        except OSError as exc:
            log.warning("relation %s to %s failed: %s", kind, destination, exc)
            self._error(502, "BV_PEER_UNREACHABLE", "peer unreachable")
            return
        self._send_json(200, {"delivered": 200 <= peer_status < 300,
                              "peer_status": peer_status,
                              "peer_body": peer_body})


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


def start_hs_whitelist_loop(app, interval=HS_WHITELIST_RESYNC_SECONDS,
                            stop=None):
    """Re-render the homeserver whitelist every `interval` seconds (A-R2):
    retries a failed write and repairs an include someone edited or
    deleted by hand. Unchanged content writes nothing. Daemon thread.
    `stop` is an optional threading.Event used by tests to end the loop
    (#253 review nit 11); production leaves it unset."""
    def run():
        while True:
            if stop is None:
                time.sleep(interval)
            elif stop.wait(interval):
                return
            try:  # #247 review S3: the loop must outlive any surprise
                app.sync_hs_whitelist()
            except Exception:  # noqa: BLE001
                log.exception("homeserver whitelist loop iteration failed")

    thread = threading.Thread(target=run, name="pairing-hs-whitelist",
                              daemon=True)
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
    if httpd.app.hs_whitelist is not None:
        # Render before serving so a homeserver that waits for this
        # service (compose depends_on: service_healthy) starts with the
        # current list.
        httpd.app.sync_hs_whitelist()
        log.info("homeserver whitelist include: %s (marker %s)",
                 config.hs_whitelist_path, httpd.app.hs_whitelist.marker_path)
        start_hs_whitelist_loop(httpd.app)
    start_pruner(httpd.app)
    host, port = httpd.server_address[:2]
    if not config.trusted_proxies:
        log.warning("PAIRING_TRUSTED_PROXIES is empty: X-Forwarded-For is "
                    "ignored; behind fedgate every peer shares one rate-limit "
                    "bucket. Set it to fedgate's address.")
    if not config.service_token:
        log.warning("PAIRING_SERVICE_TOKEN is unset: the internal pairing-facts"
                    " endpoints refuse every request (401).")
    if not config.relations_send_token:
        log.warning("PAIRING_RELATIONS_SEND_TOKEN is unset: the internal"
                    " relation send endpoint refuses every request (401).")
    if config.relations_gateway != "none":
        log.warning("PAIRING_RELATIONS_GATEWAY=%s: inbound relation delivery"
                    " enabled; test-recorder is for tests/CI only",
                    config.relations_gateway)
    if config.relations_allow_private:
        log.warning("PAIRING_RELATIONS_ALLOW_PRIVATE=true: relations may go"
                    " to plain-http / private / loopback peer addresses"
                    " (every such send is audit-logged); test stacks only")
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

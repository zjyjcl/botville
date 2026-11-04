"""botville pairing service (A2.1) — invite/redeem/accept/sever HTTP API.

Python standard library only; run with `python -m pairing.service`.
Endpoints (JSON everywhere; errors are `{"errcode", "error"}`):

  POST /v1/pairing/invites               admin  create a one-shot invite;
         -> {invite_id, expires_at, qr_payload}
         qr_payload = botville-pair:v1:<server_name>:<b64url(api_url)>:
                      <invite_id>:<secret>   (secret shown this once only)
  POST /v1/pairing/redeem                peer   consume an invite: body
         {invite_id, secret, server_name, api_url}; optionally verifies the
         peer's /_matrix/key/v2/server (PAIRING_VERIFY_PEER_KEY) and marks
         the peer active. Rate-limited per IP.
  POST /v1/pairing/accept                admin  body {qr_payload}: parse the
         payload scanned from the *other* server, call its /redeem, then
         mark it active locally.
  GET  /v1/pairing/peers                 admin  list pairing state.
  DELETE /v1/pairing/peers/{server_name} admin  unilateral sever: local row
         goes severed immediately, then a best-effort POST to the peer's
         /notify-severed (failure ignored, still logged).
  POST /v1/pairing/notify-severed        peer   body {server_name}: mark that
         peer severed_by=remote.
  GET  /v1/pairing/allowed               admin  active server names (for
         debugging; fedgate reads the SQLite file directly).
  GET  /health                           any    liveness probe.

Paths under /_matrix/pairing/... are accepted as aliases of /v1/pairing/...
so fedgate can forward them unmodified (`/_matrix/pairing/redeem` ==
`/v1/pairing/redeem`).

Admin auth: `Authorization: Bearer $PAIRING_ADMIN_TOKEN` (constant-time
compare); the service refuses to start without it. Secrets/tokens are
never written to the log.
"""

import base64
import collections
import hmac
import json
import logging
import os
import secrets
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

log = logging.getLogger("pairing.service")

QR_PREFIX = "botville-pair"
QR_VERSION = "v1"
QR_FIELD_COUNT = 6  # prefix:version:server_name:b64(api_url):invite_id:secret
DEFAULT_INVITE_TTL = 600.0
REDEEM_PER_MINUTE = 10
RATE_WINDOW_SECONDS = 60.0
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
    ':'-separated format and are rejected here; see
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


def post_json(url, body, insecure_tls=False, timeout=PEER_HTTP_TIMEOUT):
    """POST JSON; returns (status, decoded body or None) like smoke.request_json."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
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


# --- redeem rate limiting ---------------------------------------------------

class RateLimiter:
    """Sliding-window per-IP limiter (REDEEM_PER_MINUTE per RATE_WINDOW)."""

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


class PairingConfig:
    def __init__(self, db_path, admin_token, server_name, api_url,
                 invite_ttl=DEFAULT_INVITE_TTL, verify_peer_key=True,
                 peer_key_url_template="https://{server_name}/_matrix/key/v2/server",
                 insecure_tls=False, host="127.0.0.1", port=8090):
        self.db_path = db_path
        self.admin_token = admin_token
        self.server_name = server_name
        self.api_url = api_url
        self.invite_ttl = invite_ttl
        self.verify_peer_key = verify_peer_key
        self.peer_key_url_template = peer_key_url_template
        self.insecure_tls = insecure_tls
        self.host = host
        self.port = port

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
        )


# --- HTTP handler -----------------------------------------------------------

class PairingApp:
    """Everything the handler needs; attached to the server instance."""

    def __init__(self, config):
        self.config = config
        self.store = PairingStore(config.db_path)
        self.limiter = RateLimiter()


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

    def _read_json(self):
        """Read the request body (capped); returns (obj, error_response)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            return None, (413, "M_TOO_LARGE", "request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None, (400, "M_BAD_JSON", "request body must be JSON")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, (400, "M_BAD_JSON", "request body must be JSON")
        if not isinstance(payload, dict):
            return None, (400, "M_BAD_JSON", "request body must be a JSON object")
        return payload, None

    def _is_admin(self):
        auth = self.headers.get("Authorization") or ""
        expected = "Bearer " + self.config.admin_token
        return hmac.compare_digest(auth.encode("utf-8"),
                                   expected.encode("utf-8"))

    def _dispatch(self, method):
        path = urlsplit(self.path).path
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
        """Extract the redeem fields or return an error tuple."""
        invite_id = payload.get("invite_id")
        secret = payload.get("secret")
        server_name = payload.get("server_name")
        api_url = payload.get("api_url")
        if not all(isinstance(v, str) and v for v in (invite_id, secret)):
            return None, (400, "M_INVALID_PARAM", "invite_id and secret are required")
        if not isinstance(server_name, str) or not server_name:
            return None, (400, "M_INVALID_PARAM", "server_name is required")
        if len(server_name) > 253 or ":" in server_name:
            return None, (400, "M_INVALID_PARAM",
                          "server_name must be a hostname without port")
        if not isinstance(api_url, str) or not (
                api_url.startswith("http://") or api_url.startswith("https://")):
            return None, (400, "M_INVALID_PARAM", "api_url must be an http(s) URL")
        return (invite_id, secret, server_name, api_url), None

    def _handle_redeem(self):
        client_ip = self.client_address[0]
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

        # Peer identity check (anti-impersonation): fetch the key the peer
        # publishes for the server_name it claims and require a match.
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
            if (published or "").lower() != server_name.lower():
                self.app.store.record_event(
                    "peer_verify_failed", server_name=server_name,
                    reason="mismatch")
                self._error(403, "M_FORBIDDEN",
                            "peer server key does not match server_name")
                return

        ok, reason = self.app.store.redeem_invite(
            invite_id, secret, server_name, api_url)
        if not ok:
            status, errcode, message = REDEEM_ERRORS[reason]
            self._error(status, errcode, message)
            return
        self._send_json(200, {"ok": True, "server_name": self.config.server_name})

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
        # locally — a direct upsert marks the pairing active here too.
        self.app.store.upsert_peer_active(remote_name, api_url)
        self._send_json(200, {"ok": True, "server_name": remote_name})

    def _handle_sever(self, server_name):
        peer = self.app.store.get_peer(server_name)
        if peer is None:
            self._error(404, "M_NOT_FOUND", "unknown peer")
            return
        self.app.store.sever_peer(server_name, "local")
        # Best-effort heads-up: our own gate already refuses the peer both
        # ways from our side (they can neither push to us nor ask us to
        # pull). The one path left open without this notification is our
        # *outbound* pull — the peer's gate still lists us as active until
        # it hears about the sever. Failure to notify changes nothing
        # locally; severing is unilateral.
        notified = False
        try:
            status, resp = post_json(
                peer["peer_api_url"] + "/notify-severed",
                {"server_name": self.config.server_name},
                insecure_tls=self.config.insecure_tls, timeout=5.0)
            notified = 200 <= status < 300
        except OSError as exc:
            log.warning("notify-severed to %s failed (ignored): %s",
                        server_name, exc)
        self.app.store.record_event(
            "sever_notified" if notified else "sever_notify_failed",
            server_name=server_name)
        self._send_json(200, {
            "ok": True,
            "server_name": server_name,
            "state": "severed",
            "severed_by": "local",
            "notified": notified,
        })

    def _handle_notify_severed(self):
        payload, err = self._read_json()
        if err:
            self._error(*err)
            return
        server_name = payload.get("server_name")
        if not isinstance(server_name, str) or not server_name:
            self._error(400, "M_INVALID_PARAM", "server_name is required")
            return
        if not self.app.store.sever_peer(server_name, "remote"):
            self._error(404, "M_NOT_FOUND", "unknown peer")
            return
        self._send_json(200, {"ok": True, "server_name": server_name,
                              "state": "severed", "severed_by": "remote"})


def build_server(config):
    """Bind the ThreadingHTTPServer (port 0 allowed for tests)."""
    httpd = ThreadingHTTPServer((config.host, config.port), PairingHandler)
    httpd.daemon_threads = True
    httpd.app = PairingApp(config)
    return httpd


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

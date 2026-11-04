"""botville fedgate (A2.1) — the paired-federation reverse proxy.

Python standard library only; run with `python -m pairing.fedgate`.
It is the only component exposed to other servers (TLS on :8448 by
default) and the authoritative gate for Matrix server-server traffic:

  /_matrix/key/v2/server*     open (our public key; no room content) —
                              forwarded to the upstream homeserver.
  /_matrix/federation/*       gated: requires exactly one Authorization
  /_matrix/key/v2/query*        header of the form `X-Matrix origin=...`
  /_matrix/media/*              (quoted or unquoted) whose origin is an
                              *active* peer; otherwise
                              403 {"errcode":"M_FORBIDDEN",
                                   "error":"server not paired"}.
                              PUT /send from an unpaired origin is sunk:
                              200 {"pdus":{}} and nothing reaches the
                              homeserver (see _sink_transaction).
  /_matrix/pairing/redeem      forwarded to PAIRING_UPSTREAM (the pairing
  /_matrix/pairing/notify-...  service) so peers need only this one port;
  ...severed                   every other /_matrix/pairing/* path is 404
                              (admin endpoints stay off the public port).
  anything else               404.

Signature verification stays with the homeserver; fedgate only decides
*whether a conversation may happen at all* (default deny — no pairing
record means no access). The X-Matrix parsing is kept in lockstep with
Synapse's (v1.164.1) _parse_auth_header — with one deliberate
difference: anything Synapse would disambiguate by "last one wins"
(multiple X-Matrix headers, repeated parameter names) is rejected here
outright, so the origin the gate checks is always the origin the
homeserver will authenticate (review B1; stdlib has no ed25519, so the
gate cannot verify signatures itself). Residual risk: Synapse does not
verify federation signatures on /_matrix/media/* endpoints, so a sender
able to forge an X-Matrix origin of an *active* peer can fetch media —
documented in docs/deploy/paired-federation.md.

The active-peer set is read from the pairing service's SQLite database
(same volume) and cached for at most 1s, so a sever takes effect
immediately without touching the homeserver. A database that cannot be
read is NOT treated as "no peers": the last good snapshot keeps serving
for a short grace period, then gated requests answer 503
M_UNAVAILABLE and /health answers 503 {"ok":false,"db":false} — a loud
failure instead of silently dropping legitimate traffic.

Method, headers, body and status are forwarded (end-to-end headers
verbatim); responses are streamed (Content-Length passthrough, or
chunked re-framing when the upstream sends none). Hop-by-hop headers
and the client's Content-Length are never forwarded — the length is
recomputed from the (already read) body. When proxying to
PAIRING_UPSTREAM, any client-supplied X-Forwarded-For/X-Real-IP/
Forwarded headers are dropped and X-Forwarded-For is set to the direct
client IP (the pairing service only trusts that header from configured
proxies, review B3).
"""

import collections
import http.client
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import pathname2url

from .validate import is_valid_server_name, path_is_suspicious

log = logging.getLogger("pairing.fedgate")

ALLOWED_REFRESH_SECONDS = 1.0
ALLOWED_STALE_OK_SECONDS = 30.0
UPSTREAM_TIMEOUT = 120.0
STREAM_CHUNK = 65536
MAX_BODY_BYTES = 64 * 1024 * 1024  # federation state dumps can be large
SINK_MAX_BODY_BYTES = 1024 * 1024  # unpaired /send bodies are drained, capped
SINK_LOG_INTERVAL = 60.0  # per-origin "dropped transaction" log throttle

# Path prefixes (matched against the path only, query excluded).
OPEN_PREFIXES = ("/_matrix/key/v2/server",)
SEND_PREFIX = "/_matrix/federation/v1/send/"
GATED_PREFIXES = (
    "/_matrix/federation/",
    "/_matrix/key/v2/query",
    "/_matrix/media/",
)
PAIRING_PREFIX = "/_matrix/pairing/"
# Only the two peer-facing endpoints are proxied to PAIRING_UPSTREAM; the
# admin API (invites/accept/peers/allowed/DELETE) must stay on the
# loopback/internal port (review: pairing-surface).
PAIRING_FORWARD_PATHS = (
    "/_matrix/pairing/redeem",
    "/_matrix/pairing/notify-severed",
)

# Client-supplied forwarding hints are spoofable; never forwarded — when
# proxying to the pairing service, fedgate writes its own (review B3).
FORWARDED_HINTS = {"x-forwarded-for", "x-real-ip", "forwarded"}

# RFC 2616 hop-by-hop headers; never forwarded in either direction.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "trailer", "transfer-encoding", "upgrade",
}


class ConfigError(Exception):
    """Invalid or missing configuration; fatal at startup."""


# --- X-Matrix origin parsing (review B1) ---------------------------------------

def _strip_quotes(value):
    """Synapse v1.164.1 strip_quotes, copied verbatim in behaviour.

    A value that starts with '"' loses its first and last character and
    has backslash escapes unfolded ('\\\\.' -> '.'); anything else is
    returned as-is (no whitespace stripping — Synapse doesn't either).
    """
    if value.startswith('"'):
        return re.sub(r"\\(.)", lambda match: match.group(1), value[1:-1])
    return value


_CTL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def headers_are_clean(headers):
    """False when any header is folded (obs-fold) or carries a control
    character other than HTAB, in its name or value; or when the parser
    recorded defects. Fail-closed input check for the gate."""
    if getattr(headers, "defects", None):
        return False
    for name, value in headers.items():
        if not isinstance(value, str) or _CTL_RE.search(name) or \
                _CTL_RE.search(value):
            return False
    return True


def parse_x_matrix_origin(auth_headers):
    """Origin declared by the request's single X-Matrix Authorization header.

    `auth_headers` is e.g. BaseHTTPRequestHandler.headers.get_all(
    "Authorization"). The Matrix server-server auth format is
    `X-Matrix origin="name",destination="...",key="...",sig="..."` —
    values may be quoted or bare; parameter names are case-insensitive.

    Parsing mirrors Synapse v1.164.1's Authenticator/_parse_auth_header
    (scheme split on `X-Matrix +`, parameters split on `[ \\t]*,[ \\t]*`
    at the first '=', quoted values unquoted with backslash escapes
    unfolded, parameter names lower-cased) with one deliberate
    hardening: Synapse lets repeated headers/parameters overwrite each
    other ("last one wins"), which would let a second header name an
    origin different from the one this gate checks — the exact B1
    bypass. Here ANY ambiguity fails closed:

      - more or fewer than one X-Matrix Authorization header -> None
        (non-X-Matrix Authorization headers are ignored, like Synapse);
      - a repeated parameter name -> None;
      - missing `origin` or `sig` -> None;
      - origin that is not a valid DNS-style server_name in canonical
        lower-case form -> None (peers rows are keyed lower-case, so a
        case mismatch can never be an accidental allow);
      - any parse anomaly -> None.

    Returns the origin string, or None (deny). The gate guarantees that
    whenever it accepts an origin (and headers_are_clean() has already
    refused folded / CTL-bearing headers, without which the claim is
    false across parsers), Synapse parses the *same* origin from
    the same bytes; whether the signature is genuine is then the
    homeserver's job (it is not checked on /_matrix/media/* — see the
    module docstring).
    """
    matrix_headers = [value for value in (auth_headers or [])
                      if value[:8].lower() == "x-matrix"]
    if len(matrix_headers) != 1:
        return None
    if _CTL_RE.search(matrix_headers[0]):
        return None  # folded / CR / LF / other CTL: parsers disagree
    parts = re.split(r"^X-Matrix +", matrix_headers[0], maxsplit=1)
    if len(parts) != 2:
        return None  # no space-separated parameter section
    params = {}
    try:
        for item in re.split(r"[ \t]*,[ \t]*", parts[1]):
            key, eq, raw = item.partition("=")
            if not eq or not key:
                return None  # parameter without '=' (Synapse: parse error)
            name = key.lower()
            if name in params:
                return None  # ambiguous: Synapse would let the last one win
            params[name] = raw
        origin = _strip_quotes(params["origin"])
        if "sig" not in params:
            return None
    except (KeyError, IndexError, ValueError, re.error):
        return None  # fail closed on anything unexpected
    if not is_valid_server_name(origin):
        return None
    return origin


# --- active-peer cache -------------------------------------------------------

class AllowedPeers:
    """Read-only view of `peers WHERE state='active'`, cached for 1s.

    fedgate and the pairing service share the SQLite file (WAL), so a
    sever becomes visible here on the next refresh without any signal
    from the pairing service.

    A missing/unreadable database is NOT "no peers" (that would silently
    200-sink legitimate traffic forever, review: sink-vs-db-failure):
    the last good snapshot keeps serving for ALLOWED_STALE_OK_SECONDS
    (30s), after which refresh_if_stale() reports the set as unavailable
    — gated requests then answer 503 M_UNAVAILABLE (never sunk) and
    /health reports {"ok": false, "db": false}.
    """

    def __init__(self, db_path, refresh_interval=ALLOWED_REFRESH_SECONDS,
                 stale_ok_seconds=ALLOWED_STALE_OK_SECONDS):
        self._db_path = db_path
        self._interval = refresh_interval
        self._stale_ok = stale_ok_seconds
        self._lock = threading.Lock()
        self._names = frozenset()
        self._ok = False
        self._attempt_at = 0.0
        self._success_at = 0.0

    def _load(self):
        # Percent-encode the path for the file: URI (spaces / non-ASCII
        # in checkout paths must not corrupt the query string). Raises
        # sqlite3.Error when the database is missing or unreadable.
        conn = sqlite3.connect(
            "file:{}?mode=ro".format(pathname2url(self._db_path)),
            uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT lower(server_name) FROM peers WHERE state = 'active'"
            ).fetchall()
        finally:
            conn.close()
        return frozenset(row[0] for row in rows)

    def refresh_if_stale(self, now=None):
        """Returns (ok, names); ok is False once the DB is unreadable
        beyond the stale-grace window (then `names` is the last snapshot
        and must not be trusted for allows)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._attempt_at >= self._interval:
                self._attempt_at = now
                try:
                    self._names = self._load()
                    self._ok = True
                    self._success_at = now
                except sqlite3.Error as exc:
                    log.warning("pairing DB read failed (%s); stale-ok=%s",
                                exc, now - self._success_at <= self._stale_ok)
                    if now - self._success_at > self._stale_ok:
                        self._ok = False
                    # else: keep serving the last good snapshot.
            return self._ok, self._names

    def contains(self, server_name):
        ok, names = self.refresh_if_stale()
        return ok and server_name in names


# --- configuration -------------------------------------------------------------

class FedgateConfig:
    def __init__(self, host, port, upstream, pairing_upstream, pairing_db,
                 tls_cert=None, tls_key=None):
        self.host = host
        self.port = port
        self.upstream = upstream
        self.pairing_upstream = pairing_upstream
        self.pairing_db = pairing_db
        self.tls_cert = tls_cert
        self.tls_key = tls_key

    @classmethod
    def from_env(cls, env):
        upstream = _http_url(env.get("FEDGATE_UPSTREAM", "http://127.0.0.1:8008"),
                             "FEDGATE_UPSTREAM")
        pairing_upstream = env.get("PAIRING_UPSTREAM", "")
        if pairing_upstream:
            pairing_upstream = _http_url(pairing_upstream, "PAIRING_UPSTREAM")
        tls_cert = env.get("FEDGATE_TLS_CERT", "")
        tls_key = env.get("FEDGATE_TLS_KEY", "")
        if bool(tls_cert) != bool(tls_key):
            raise ConfigError(
                "FEDGATE_TLS_CERT and FEDGATE_TLS_KEY must be set together")
        config = cls(
            host=env.get("FEDGATE_HOST", "0.0.0.0"),
            port=int(env.get("FEDGATE_PORT", "8448")),
            upstream=upstream,
            pairing_upstream=pairing_upstream,
            pairing_db=env.get("FEDGATE_PAIRING_DB", "pairing.db"),
            tls_cert=tls_cert or None,
            tls_key=tls_key or None,
        )
        # Optional /.well-known/matrix/server delegation ("host:port").
        # An explicit port in the delegated name makes peers skip the SRV
        # lookup, which on Docker's embedded DNS can stall ~10s per name.
        config.well_known_server = env.get("FEDGATE_WELL_KNOWN_SERVER", "") or None
        return config


def _http_url(value, name):
    parts = urlsplit(value)
    if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/"):
        raise ConfigError(
            "{} must be a plain http:// URL to an internal service".format(name))
    return value.rstrip("/")


# --- sink log throttle ---------------------------------------------------------

_sink_log_lock = threading.Lock()
_sink_log_at = collections.OrderedDict()  # bounded LRU: origin -> last log
SINK_LOG_MAX_ORIGINS = 1024
SINK_LOG_GLOBAL_PER_INTERVAL = 20
_sink_log_window = [0.0, 0]  # [window start, lines logged in window]


def _should_log_sink(origin, now=None, interval=SINK_LOG_INTERVAL):
    """Drop-log throttle: at most once per `interval` per origin AND at
    most SINK_LOG_GLOBAL_PER_INTERVAL lines per interval overall, with a
    bounded per-origin table, so rotating fresh origins can neither flood
    the log nor grow memory (review r2 #1)."""
    now = time.monotonic() if now is None else now
    with _sink_log_lock:
        if now - _sink_log_window[0] >= interval:
            _sink_log_window[0], _sink_log_window[1] = now, 0
        if _sink_log_window[1] >= SINK_LOG_GLOBAL_PER_INTERVAL:
            return False
        last = _sink_log_at.get(origin)
        if last is not None and now - last < interval:
            return False
        _sink_log_at[origin] = now
        _sink_log_at.move_to_end(origin)
        while len(_sink_log_at) > SINK_LOG_MAX_ORIGINS:
            _sink_log_at.popitem(last=False)
        _sink_log_window[1] += 1
        return True


# --- HTTP handler ----------------------------------------------------------------

class FedgateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "botville-fedgate/1.0"
    timeout = 300  # long federation transactions must not idle-time out

    def setup(self):
        # TLS handshake runs here, in the per-connection thread, so a slow
        # or stalled client cannot block the accept loop.
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(30)
            self.request.do_handshake()
        super().setup()

    @property
    def config(self):
        return self.server.config

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status, payload):
        # Local rejections answer without reading the (possibly large)
        # request body; close the connection so unread bytes cannot
        # corrupt a keep-alive follow-up request.
        self.close_connection = True
        self._send_json(status, payload)

    def _handle(self):
        # Header hygiene (review r2, B1 residue): Python's email parser keeps
        # obs-fold continuation lines verbatim ("...\r\n origin=...") while
        # Twisted/Synapse folds them into one space, so the same bytes yield
        # different X-Matrix parameters on each side. ANY cross-parser
        # normalisation difference is a bypass, so we do not try to mimic
        # Twisted: a folded header or any control character (other than
        # HTAB) in any header name/value is refused outright, before any
        # routing. This also closes CRLF injection into the upstream request.
        if not headers_are_clean(self.headers):
            self._reject(400, {"errcode": "M_UNKNOWN",
                               "error": "malformed request headers"})
            return
        # Raw request-target, not urlsplit(): "//x/y" would otherwise be
        # parsed as a network-location and hide the empty segment.
        path = self.path.split("?", 1)[0]
        # Path hygiene (review: bypass-via-path): refuse traversal
        # segments, empty segments and encoded separators before any
        # prefix matching, so an upstream router can never be fed a path
        # that normalises into a different route.
        if path_is_suspicious(path):
            self._reject(404, {"errcode": "M_UNRECOGNIZED",
                               "error": "malformed request path"})
            return
        if path == "/health":
            ok, _names = self.server.allowed.refresh_if_stale()
            if ok:
                self._send_json(200, {"ok": True})
            else:
                self._send_json(503, {"ok": False, "db": False})
            return
        if path == "/.well-known/matrix/server":
            delegated = getattr(self.config, "well_known_server", None)
            if delegated:
                self._send_json(200, {"m.server": delegated})
            else:
                self._reject(404, {"errcode": "M_NOT_FOUND",
                                   "error": "no well-known delegation"})
            return
        if path.startswith(PAIRING_PREFIX):
            forwardable = path in PAIRING_FORWARD_PATHS
            if not forwardable or not self.config.pairing_upstream:
                self._reject(404, {"errcode": "M_UNRECOGNIZED",
                                   "error": "pairing endpoint not served by"
                                            " fedgate"})
                return
            self._proxy(self.config.pairing_upstream, overwrite_forwarded=True)
            return
        if any(path.startswith(p) for p in OPEN_PREFIXES):
            self._proxy(self.config.upstream)
            return
        if any(path.startswith(p) for p in GATED_PREFIXES):
            origin = parse_x_matrix_origin(
                self.headers.get_all("Authorization"))
            if origin is None:
                self._reject(403, {"errcode": "M_FORBIDDEN",
                                   "error": "server not paired"})
                return
            ok, names = self.server.allowed.refresh_if_stale()
            if not ok:
                # Unreadable pairing DB: never sink (that would be a
                # silent, permanent drop) — fail loud instead.
                self._reject(503, {"errcode": "M_UNAVAILABLE",
                                   "error": "pairing database unavailable"})
                return
            if origin not in names:
                if self.command == "PUT" and path.startswith(SEND_PREFIX):
                    self._sink_transaction(origin)
                    return
                self._reject(403, {"errcode": "M_FORBIDDEN",
                                   "error": "server not paired"})
                return
            self._proxy(self.config.upstream)
            return
        self._reject(404, {"errcode": "M_UNRECOGNIZED",
                           "error": "path not served by fedgate"})

    def _sink_transaction(self, origin):
        """Swallow a /send transaction from an unpaired/severed origin.

        Synapse backs a destination off for 10+ minutes on *any* error to
        a /send transaction and only resets that on inbound traffic, so a
        403 here would leave both sides backed off long after a re-pair.
        Answering 200 with no per-PDU results drops the PDUs/EDUs (nothing
        reaches the homeserver) without poisoning the sender's retry state.
        Missing PDUs are fetched via the DAG once the servers re-pair —
        EDUs (to-device, key shares) are gone for good, which is the
        documented price of this trade-off.

        The body is drained only up to SINK_MAX_BODY_BYTES (1 MiB):
        unauthenticated senders do not get to burn 64 MiB of memory per
        request; oversized bodies end with the connection closed. The
        drop log is throttled per origin so a flood cannot flood the log.
        """
        try:
            self._read_request_body(max_bytes=SINK_MAX_BODY_BYTES)
        except (ValueError, OSError):
            self.close_connection = True
            return
        if _should_log_sink(origin):
            log.info("dropped /send transaction from unpaired origin %s",
                     origin)
        self._send_json(200, {"pdus": {}})

    # --- proxy core --------------------------------------------------------

    def _read_request_body(self, max_bytes=MAX_BODY_BYTES):
        """Read the client body: Content-Length or chunked (capped).

        The cap is enforced *before* large chunks are read, so a caller
        cannot make the gate buffer more than max_bytes (+ one chunk
        header) of an oversized body.
        """
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = b""
            while True:
                line = self.rfile.readline(1024)
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    # Trailer section up to the final blank line.
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    return body
                if len(body) + size > max_bytes:
                    raise ValueError("request body too large")
                body += self.rfile.read(size)
                self.rfile.read(2)  # CRLF after each chunk
        length = self.headers.get("Content-Length")
        if not length:
            return None
        length = int(length)
        if length < 0 or length > max_bytes:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    def _proxy(self, upstream, overwrite_forwarded=False):
        target = urlsplit(upstream)
        try:
            body = self._read_request_body()
        except (ValueError, OSError) as exc:
            self._reject(400, {"errcode": "M_UNKNOWN",
                               "error": "bad request body: {}".format(exc)})
            return

        # Forward end-to-end headers verbatim (Authorization/X-Matrix
        # included — the homeserver still verifies signatures); join
        # repeated headers per RFC 7230 list semantics. The client's
        # Content-Length is never forwarded: the body was already read
        # (chunked bodies de-chunked) and http.client recomputes the
        # length from those bytes, so a CL/TE mismatch cannot smuggle a
        # second request past the upstream's framing (review:
        # CL/TE re-framing).
        headers = {}
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered in ("host", "content-length"):
                continue
            if overwrite_forwarded and lowered in FORWARDED_HINTS:
                continue  # client-supplied forwarding hints are spoofable
            if key in headers:
                headers[key] += ", " + value
            else:
                headers[key] = value
        headers["Host"] = target.netloc
        if overwrite_forwarded:
            # fedgate is the reverse proxy the pairing service trusts for
            # X-Forwarded-For (PAIRING_TRUSTED_PROXIES must list this
            # host): report the direct client IP, discarding whatever the
            # client claimed (review B3).
            headers["X-Forwarded-For"] = self.client_address[0]

        conn = http.client.HTTPConnection(
            target.hostname, target.port or 80, timeout=UPSTREAM_TIMEOUT)
        response_started = False
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            self.send_response_only(resp.status, resp.reason)
            response_started = True
            for key, value in resp.getheaders():
                if key.lower() in HOP_BY_HOP or key.lower() in (
                        "content-length", "date", "server"):
                    continue
                self.send_header(key, value)
            length = resp.getheader("Content-Length")
            if length is not None:
                self.send_header("Content-Length", length)
                self.end_headers()
                remaining = int(length)
                while remaining > 0:
                    chunk = resp.read(min(STREAM_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            else:
                # No length upstream (Synapse answers chunked). Re-frame
                # with chunked encoding: close-delimited bodies left
                # Twisted federation clients waiting until their 10s
                # timeout (observed in federation-e2e).
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read1(STREAM_CHUNK) if hasattr(resp, "read1") else resp.read(STREAM_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            log.warning("proxy %s %s -> %s failed: %s",
                        self.command, urlsplit(self.path).path, upstream, exc)
            if not response_started:
                self._send_json(502, {"errcode": "M_UNKNOWN",
                                      "error": "upstream unavailable"})
            else:
                self.close_connection = True
        finally:
            conn.close()


def build_server(config):
    """Bind the ThreadingHTTPServer (port 0 allowed) + optional TLS."""
    httpd = ThreadingHTTPServer((config.host, config.port), FedgateHandler)
    httpd.daemon_threads = True
    httpd.config = config
    httpd.allowed = AllowedPeers(config.pairing_db)
    if config.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(config.tls_cert, config.tls_key)
        httpd.socket = context.wrap_socket(
            httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        config = FedgateConfig.from_env(os.environ)
    except ConfigError as exc:
        print("pairing.fedgate: {}".format(exc), file=sys.stderr)
        return 2
    httpd = build_server(config)
    host, port = httpd.server_address[:2]
    log.info("fedgate listening on %s:%s (tls=%s, upstream=%s, db=%s)",
             host, port, bool(config.tls_cert), config.upstream,
             config.pairing_db)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

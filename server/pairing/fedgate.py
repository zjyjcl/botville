"""botville fedgate (A2.1) — the paired-federation reverse proxy.

Python standard library only; run with `python -m pairing.fedgate`.
It is the only component exposed to other servers (TLS on :8448 by
default) and the authoritative gate for Matrix server-server traffic:

  /_matrix/key/v2/server*     open (our public key; no room content) —
                              forwarded to the upstream homeserver.
  /_matrix/federation/*       gated: requires an Authorization header of
  /_matrix/key/v2/query*        the form `X-Matrix origin=...` (quoted or
  /_matrix/media/*              unquoted; first X-Matrix header wins) whose
                              origin is an *active* peer; otherwise
                              403 {"errcode":"M_FORBIDDEN",
                                   "error":"server not paired"}.
  /_matrix/pairing/*          forwarded to PAIRING_UPSTREAM (the pairing
                              service) so peers need only this one port.
  anything else               404.

Signature verification stays with the homeserver; fedgate only decides
*whether a conversation may happen at all* (default deny — no pairing
record means no access). The active-peer set is read from the pairing
service's SQLite database (same volume) and cached for at most 1s, so a
sever takes effect immediately without touching the homeserver.

Method, headers, body and status are forwarded verbatim; responses are
streamed (Content-Length passthrough, or connection-close framing when
the upstream sends none). Hop-by-hop headers are dropped both ways.
"""

import http.client
import json
import logging
import os
import sqlite3
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import pathname2url

log = logging.getLogger("pairing.fedgate")

ALLOWED_REFRESH_SECONDS = 1.0
UPSTREAM_TIMEOUT = 120.0
STREAM_CHUNK = 65536
MAX_BODY_BYTES = 64 * 1024 * 1024  # federation state dumps can be large

# Path prefixes (matched against the path only, query excluded).
OPEN_PREFIXES = ("/_matrix/key/v2/server",)
SEND_PREFIX = "/_matrix/federation/v1/send/"
GATED_PREFIXES = (
    "/_matrix/federation/",
    "/_matrix/key/v2/query",
    "/_matrix/media/",
)
PAIRING_PREFIX = "/_matrix/pairing/"

# RFC 2616 hop-by-hop headers; never forwarded in either direction.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "trailer", "transfer-encoding", "upgrade",
}


class ConfigError(Exception):
    """Invalid or missing configuration; fatal at startup."""


# --- X-Matrix origin parsing -------------------------------------------------

def parse_x_matrix_origin(auth_headers):
    """Origin declared by the first X-Matrix Authorization header.

    `auth_headers` is e.g. BaseHTTPRequestHandler.headers.get_all(
    "Authorization"). The Matrix server-server auth format is
    `X-Matrix origin="name",destination="...",key="...",sig="..."` —
    values may be quoted or bare; parameter names are case-insensitive.
    Returns the origin string, or None when no usable X-Matrix header
    exists (malformed X-Matrix headers also yield None: default deny).
    """
    for value in auth_headers or []:
        scheme, sep, rest = value.partition(" ")
        if not sep or scheme.lower() != "x-matrix":
            continue
        for item in rest.split(","):
            key, eq, raw = item.partition("=")
            if not eq or key.strip().lower() != "origin":
                continue
            origin = raw.strip()
            if len(origin) >= 2 and origin[0] == '"' and origin[-1] == '"':
                origin = origin[1:-1]
            if origin:
                return origin
        return None
    return None


# --- active-peer cache -------------------------------------------------------

class AllowedPeers:
    """Read-only view of `peers WHERE state='active'`, cached for 1s.

    fedgate and the pairing service share the SQLite file (WAL), so a
    sever becomes visible here on the next refresh without any signal
    from the pairing service. A missing/unreadable database means an
    empty set — default deny.
    """

    def __init__(self, db_path, refresh_interval=ALLOWED_REFRESH_SECONDS):
        self._db_path = db_path
        self._interval = refresh_interval
        self._lock = threading.Lock()
        self._names = frozenset()
        self._loaded_at = 0.0

    def _load(self):
        try:
            # Percent-encode the path for the file: URI (spaces / non-ASCII
            # in checkout paths must not corrupt the query string).
            conn = sqlite3.connect(
                "file:{}?mode=ro".format(pathname2url(self._db_path)),
                uri=True, timeout=5)
            try:
                rows = conn.execute(
                    "SELECT server_name FROM peers WHERE state = 'active'"
                ).fetchall()
            finally:
                conn.close()
            return frozenset(row[0] for row in rows)
        except sqlite3.Error:
            return frozenset()

    def refresh_if_stale(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._loaded_at >= self._interval:
                self._names = self._load()
                self._loaded_at = now
            return self._names

    def contains(self, server_name):
        return server_name in self.refresh_if_stale()


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
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, {"ok": True})
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
            if not self.config.pairing_upstream:
                self._reject(404, {"errcode": "M_UNRECOGNIZED",
                                   "error": "pairing upstream not configured"})
                return
            self._proxy(self.config.pairing_upstream)
            return
        if any(path.startswith(p) for p in OPEN_PREFIXES):
            self._proxy(self.config.upstream)
            return
        if any(path.startswith(p) for p in GATED_PREFIXES):
            origin = parse_x_matrix_origin(
                self.headers.get_all("Authorization"))
            if origin is None or not self.server.allowed.contains(origin):
                if origin is not None and self.command == "PUT" and \
                        path.startswith(SEND_PREFIX):
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
        Missing events are fetched via the DAG once the servers re-pair.
        """
        try:
            self._read_request_body()
        except (ValueError, OSError):
            self.close_connection = True
        log.info("dropped /send transaction from unpaired origin %s", origin)
        self._send_json(200, {"pdus": {}})

    # --- proxy core --------------------------------------------------------

    def _read_request_body(self):
        """Read the client body: Content-Length or chunked (capped)."""
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
                body += self.rfile.read(size)
                self.rfile.read(2)  # CRLF after each chunk
                if len(body) > MAX_BODY_BYTES:
                    raise ValueError("request body too large")
        length = self.headers.get("Content-Length")
        if not length:
            return None
        length = int(length)
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    def _proxy(self, upstream):
        target = urlsplit(upstream)
        try:
            body = self._read_request_body()
        except (ValueError, OSError) as exc:
            self._reject(400, {"errcode": "M_UNKNOWN",
                               "error": "bad request body: {}".format(exc)})
            return

        # Forward end-to-end headers verbatim (Authorization/X-Matrix
        # included — the homeserver still verifies signatures); join
        # repeated headers per RFC 7230 list semantics.
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() == "host":
                continue
            if key in headers:
                headers[key] += ", " + value
            else:
                headers[key] = value
        headers["Host"] = target.netloc

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

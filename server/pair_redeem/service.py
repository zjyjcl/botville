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
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .issue import (
    ENV_ISSUE_TOKEN,
    ISSUE_PATH,
    IssueConfigError,
    IssueError,
    bearer_token,
    issue_authorized,
    issue_code,
    parse_issue_request,
    public_issue_payload,
    validate_issue_token,
)
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
# Total wall-clock budget for one HTTP request (r2 suggestion 7).
# Per-recv timeout alone cannot bound a slow drip (one byte every few seconds).
REQUEST_DEADLINE_SECONDS = 10
# Cap concurrent handler threads so ThreadingHTTPServer cannot grow without
# bound (r2 suggestion 7). Excess connections get 503 + Connection: close.
MAX_CONCURRENT_DEFAULT = 32
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
ENV_REQUEST_TIMEOUT = "BOTVILLE_PAIR_REDEEM_REQUEST_TIMEOUT_SECONDS"
ENV_REQUEST_DEADLINE = "BOTVILLE_PAIR_REDEEM_REQUEST_DEADLINE_SECONDS"
ENV_MAX_CONCURRENT = "BOTVILLE_PAIR_REDEEM_MAX_CONCURRENT"


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
            network = ipaddress.ip_network(item, strict=False)
            if (network.version == 6 and network.prefixlen >= 96
                    and network.network_address.ipv4_mapped is not None):
                network = ipaddress.ip_network((
                    network.network_address.ipv4_mapped, network.prefixlen - 96))
            networks.append(network)
        except ValueError as exc:
            raise ConfigError(
                "%s entry %r is not a valid IP or CIDR" % (ENV_TRUSTED_PROXIES, item)
            ) from exc
    return networks


def client_ip(direct, headers, trusted_proxies):
    """Same rule as A2.1 pairing: honour X-Forwarded-For only if the
    direct peer is in the trusted-proxy set; use the last hop."""
    try:
        raw = ipaddress.ip_address(direct)
        address = _canonical_ip(raw)
    except ValueError:
        return direct
    for network in trusted_proxies:
        try:
            matched = address in network
        except TypeError:
            matched = False
        if matched:
            forwarded = (headers.get("X-Forwarded-For") or "").split(",")
            last = forwarded[-1].strip() if forwarded else ""
            if last:
                try:
                    return str(_canonical_ip(ipaddress.ip_address(last)))
                except ValueError:
                    pass
            return str(address)
    # Unpack IPv4-mapped peers so rate-limit buckets match the embedded IPv4.
    if getattr(raw, "ipv4_mapped", None) is not None:
        return str(address)
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


def _canonical_ip(ip):
    """Unpack IPv4-mapped IPv6 so classifiers see the embedded IPv4.

    Python treats the whole ::ffff:0:0/96 prefix as private, which would
    otherwise let ::ffff:8.8.8.8 and ::ffff:169.254.169.254 through the
    http-target and bind allowlists. Same idea as
    server/pairing/relations.py _non_public.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def allow_bind_host(host):
    """Loopback, unspecified (0.0.0.0/::), or private. Reject public binds.

    Link-local is refused (cloud metadata). IPv4-mapped IPv6 is unpacked
    before the check so ::ffff:8.8.8.8 cannot slip through as 'private'.
    """
    if host in ("localhost",):
        return True
    try:
        ip = _canonical_ip(ipaddress.ip_address(host))
    except ValueError:
        return False
    if ip.is_link_local:
        return False
    return ip.is_unspecified or ip.is_loopback or ip.is_private


def _bind_address_family(host):
    """AF_INET6 for IPv6 literals (::, ::1); AF_INET otherwise."""
    try:
        ip = _canonical_ip(ipaddress.ip_address(host))
    except ValueError:
        return socket.AF_INET
    return socket.AF_INET6 if ip.version == 6 else socket.AF_INET


def _is_http_trusted_ip(ip):
    """HTTP (cleartext) admin calls: loopback or RFC1918/ULA only.

    Link-local (169.254.0.0/16, fe80::/10) is excluded so an admin
    token cannot be sent to cloud metadata endpoints. IPv4-mapped
    IPv6 is unpacked first.
    """
    ip = _canonical_ip(ip)
    if ip.is_unspecified:
        return False
    if ip.is_loopback:
        return True
    if ip.is_private and not ip.is_link_local:
        return True
    return False


def _is_docker_service_host(host):
    if not host or "." in host or ":" in host or "%" in host:
        return False
    if host[0] == "-" or host[-1] == "-":
        return False
    return all(c.isalnum() or c == "-" for c in host) and len(host) <= 63


def require_admin_token(token):
    """Reject empty/whitespace/control tokens. Never include the value."""
    if not isinstance(token, str) or not token:
        raise ConfigError("%s is missing" % ENV_SYNAPSE_ADMIN_TOKEN)
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in token):
        raise ConfigError("%s contains whitespace or control characters"
                          % ENV_SYNAPSE_ADMIN_TOKEN)
    return token


def validate_synapse_admin_url(raw):
    """Return a canonical base URL, or raise ConfigError.

    https is allowed for any host (redirects are still refused at request
    time). http is only allowed for loopback, RFC1918/ULA literals (after
    unpacking IPv4-mapped IPv6), or a single-label docker-compose service
    name. Resolved addresses are pinned at request time, not here.
    Userinfo is rejected so a token cannot be embedded in the URL.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError("%s is missing" % ENV_SYNAPSE_URL)
    url = raw.strip().rstrip("/")
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in url):
        raise ConfigError("%s is not a valid URL" % ENV_SYNAPSE_URL)
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port
    except ValueError as exc:
        raise ConfigError("%s is not a valid URL" % ENV_SYNAPSE_URL) from exc
    if parts.scheme not in ("http", "https"):
        raise ConfigError("%s must be an http(s) URL" % ENV_SYNAPSE_URL)
    if parts.username is not None or parts.password is not None:
        raise ConfigError("%s must not include userinfo" % ENV_SYNAPSE_URL)
    host = parts.hostname
    if not host or parts.query or parts.fragment:
        raise ConfigError("%s must be an http(s) URL without query or fragment"
                          % ENV_SYNAPSE_URL)
    if parts.scheme == "http":
        try:
            ip = ipaddress.ip_address(host)
            if not _is_http_trusted_ip(ip):
                raise ConfigError(
                    "%s http URL must target loopback or a private network"
                    % ENV_SYNAPSE_URL)
        except ValueError:
            if host != "localhost" and not _is_docker_service_host(host):
                raise ConfigError(
                    "%s http URL must be loopback, a private IP, or a "
                    "docker-internal service name" % ENV_SYNAPSE_URL)
    return url


def _getaddrinfo_with_deadline(host, port, deadline):
    """DNS lookup that cannot run past the issuer total deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise IssuerError("http_unavailable")
    box = []

    def lookup():
        try:
            box.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:
            box.append(exc)

    thread = threading.Thread(target=lookup, name="pair-redeem-dns", daemon=True)
    thread.start()
    thread.join(remaining)
    if thread.is_alive():
        raise IssuerError("http_unavailable")
    if not box:
        raise IssuerError("http_unavailable")
    if isinstance(box[0], Exception):
        raise IssuerError("http_unavailable") from box[0]
    if not box[0]:
        raise IssuerError("http_unavailable")
    return box[0]


def _resolved_connect_ips(host, port, scheme, deadline):
    """Resolve once, validate http targets, return unique connect IPs."""
    infos = _getaddrinfo_with_deadline(host, port, deadline)
    ips = []
    seen = set()
    for info in infos:
        ip = _canonical_ip(ipaddress.ip_address(info[4][0]))
        text = str(ip)
        if text in seen:
            continue
        seen.add(text)
        if scheme == "http" and not _is_http_trusted_ip(ip):
            raise IssuerError("untrusted_destination")
        ips.append(text)
    if not ips:
        raise IssuerError("http_unavailable")
    return ips


def _connect_pinned(ips, port, deadline, source_address=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("issuer_deadline")
    last_exc = None
    for ip in ips:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("issuer_deadline")
        try:
            sock = socket.create_connection((ip, port), left, source_address)
        except OSError as exc:
            last_exc = exc
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            try:
                sock.close()
            except OSError:
                pass
            raise TimeoutError("issuer_deadline")
        sock.settimeout(left)
        return sock
    if last_exc is not None:
        raise last_exc
    raise OSError("no resolved addresses")


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    """Connect to pre-validated IPs; Host header stays the original name."""

    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                 source_address=None, blocksize=8192, *, resolved_ips=None,
                 deadline=None, sockets=None):
        http.client.HTTPConnection.__init__(
            self, host, port=port, timeout=timeout,
            source_address=source_address, blocksize=blocksize)
        self._resolved_ips = list(resolved_ips or [])
        self._deadline = deadline
        self._sockets = sockets

    def connect(self):
        self.sock = _connect_pinned(
            self._resolved_ips, self.port, self._deadline, self.source_address)
        if self._sockets is not None:
            self._sockets.append(self.sock)


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """Same IP pin as HTTP; SNI and cert checks use the original hostname."""

    def __init__(self, host, port=None,
                 timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None,
                 *, context=None, blocksize=8192,
                 resolved_ips=None, deadline=None, sockets=None):
        http.client.HTTPSConnection.__init__(
            self, host, port=port, timeout=timeout,
            source_address=source_address, context=context, blocksize=blocksize)
        self._resolved_ips = list(resolved_ips or [])
        self._deadline = deadline
        self._sockets = sockets

    def connect(self):
        sock = _connect_pinned(
            self._resolved_ips, self.port, self._deadline, self.source_address)
        left = self._deadline - time.monotonic()
        if left <= 0:
            sock.close()
            raise TimeoutError("issuer_deadline")
        sock.settimeout(left)
        context = self._context
        if context is None:
            context = ssl.create_default_context()
        # server_hostname=self.host keeps SNI on the original name, not the IP.
        self.sock = context.wrap_socket(sock, server_hostname=self.host)
        left = self._deadline - time.monotonic()
        if left <= 0:
            try:
                self.sock.close()
            except OSError:
                pass
            raise TimeoutError("issuer_deadline")
        self.sock.settimeout(left)
        if self._sockets is not None:
            self._sockets.append(self.sock)


def _admin_opener(resolved_ips, deadline, sockets):
    def http_class(*args, **kwargs):
        kwargs["resolved_ips"] = resolved_ips
        kwargs["deadline"] = deadline
        kwargs["sockets"] = sockets
        return _DeadlineHTTPConnection(*args, **kwargs)

    def https_class(*args, **kwargs):
        kwargs["resolved_ips"] = resolved_ips
        kwargs["deadline"] = deadline
        kwargs["sockets"] = sockets
        return _DeadlineHTTPSConnection(*args, **kwargs)

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(http_class, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(https_class, req)

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        HTTPHandler(),
        HTTPSHandler(),
    )


def _read_response_with_deadline(response, limit, deadline):
    """Read at most limit bytes under a total deadline, not per-recv only.

    Always go through HTTPResponse.read() so chunked/decoded bodies stay
    intact; the watchdog on the issuer socket is the hard cap if a single
    read loops internally.
    """
    buf = bytearray()
    fp = getattr(response, "fp", None)
    while len(buf) < limit:
        left = deadline - time.monotonic()
        if left <= 0:
            raise IssuerError("http_unavailable")
        sock = None
        if fp is not None:
            raw = getattr(fp, "raw", None)
            sock = getattr(raw, "_sock", None) if raw is not None else None
        if sock is not None:
            try:
                sock.settimeout(left)
            except OSError:
                raise IssuerError("http_unavailable") from None
        piece = response.read(min(256, limit - len(buf)))
        if not piece:
            break
        buf.extend(piece)
    return bytes(buf)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so a 3xx cannot leak the admin token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IssuerError("redirect_refused")


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
                 request_deadline=REQUEST_DEADLINE_SECONDS,
                 max_concurrent=MAX_CONCURRENT_DEFAULT,
                 issuer_timeout=ISSUER_TIMEOUT_SECONDS,
                 issue_token=""):
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
        self.request_deadline = request_deadline
        self.max_concurrent = max_concurrent
        self.issuer_timeout = issuer_timeout
        try:
            self.issue_token = validate_issue_token(
                issue_token or "", self.synapse_admin_token)
        except IssueConfigError as exc:
            raise ConfigError(str(exc)) from exc

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
            request_timeout=_env_int(env, ENV_REQUEST_TIMEOUT,
                                     REQUEST_TIMEOUT_SECONDS),
            request_deadline=_env_int(env, ENV_REQUEST_DEADLINE,
                                      REQUEST_DEADLINE_SECONDS),
            max_concurrent=_env_int(env, ENV_MAX_CONCURRENT,
                                    MAX_CONCURRENT_DEFAULT),
            issue_token=env.get(ENV_ISSUE_TOKEN, ""),
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
        self.base_url = validate_synapse_admin_url(base_url)
        self._token = require_admin_token(admin_token)
        self.timeout = timeout

    def issue(self, expires_in_ms, now=None, deadline=None):
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
        # Resolve once, pin the connect IP, and keep the original Host
        # (and HTTPS SNI) so a second DNS lookup cannot rebind mid-request.
        # The timeout is a total deadline (DNS + connect + response), not
        # just a per-recv socket timeout.
        parts = urllib.parse.urlsplit(self.base_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        issuer_deadline = time.monotonic() + float(self.timeout)
        deadline = min(deadline, issuer_deadline) if deadline is not None else issuer_deadline
        if deadline <= time.monotonic():
            raise TimeoutError("request_deadline")
        sockets = []

        def _kill_issuer_sockets():
            for sock in list(sockets):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        timer = threading.Timer(max(0.001, deadline - time.monotonic()),
                                _kill_issuer_sockets)
        timer.daemon = True
        timer.start()
        try:
            resolved_ips = _resolved_connect_ips(
                parts.hostname, port, parts.scheme, deadline)
            opener = _admin_opener(resolved_ips, deadline, sockets)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IssuerError("http_unavailable")
            try:
                try:
                    response = opener.open(request, timeout=remaining)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    status = getattr(response, "status", None) or response.getcode()
                    raw = _read_response_with_deadline(response, 4096, deadline)
            except IssuerError:
                raise
            except Exception:
                raise IssuerError("http_unavailable") from None
        finally:
            timer.cancel()
            _kill_issuer_sockets()
        if 300 <= int(status) < 400:
            raise IssuerError("redirect_refused")
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
            self.store.check_request_deadline()
            if isinstance(self.issuer, SynapseRegistrationTokenIssuer):
                return self.issuer.issue(
                    expires, now=now, deadline=self.store.current_request_deadline())
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
        if not allow_bind_host(host):
            raise ConfigError(
                "%s host %r is not loopback, private, or unspecified"
                % (ENV_BIND, host)
            )
        handler = _handler_for(self)
        handler.timeout = self.config.request_timeout
        max_concurrent = self.config.max_concurrent
        family = _bind_address_family(host)

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            address_family = family
            # serve_forever does not consult Server.timeout; the
            # per-recv deadline is handler.timeout (set above). The
            # total request deadline is armed in handle_one_request.

            def __init__(self, addr, handler_cls):
                ThreadingHTTPServer.__init__(self, addr, handler_cls)
                self._pair_sema = threading.BoundedSemaphore(max_concurrent)
                self.handler_started = threading.Event()

            def process_request(self, request, client_address):
                if not self._pair_sema.acquire(blocking=False):
                    _reject_busy(request)
                    return
                try:
                    ThreadingHTTPServer.process_request(
                        self, request, client_address)
                except Exception:
                    self._pair_sema.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    ThreadingHTTPServer.process_request_thread(
                        self, request, client_address)
                finally:
                    self._pair_sema.release()

        return Server((host, port), handler)


def _reject_busy(sock):
    """503 + close when the concurrent-request cap is already full."""
    body = b'{"error":"temporarily unavailable"}'
    payload = (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )
    try:
        sock.settimeout(0.1)
        sock.sendall(payload)
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


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


class _DeadlineRFile:
    """rfile wrapper that re-arms the total deadline on every recv.

    readline/read pull modest chunks (one underlying recv via read1) so a
    slow drip cannot reset a per-recv timeout, but a complete request
    already in the kernel is buffered in user space. That avoids a TCP
    RST on close when an unread leftover body remains only in the kernel.
    """

    def __init__(self, rfile, arm):
        self._rfile = rfile
        self._arm = arm
        self._buf = bytearray()

    def _pull(self, want):
        self._arm()
        want = max(1, int(want))
        if hasattr(self._rfile, "read1"):
            return self._rfile.read1(want)
        return self._rfile.read(want)

    def readline(self, limit=-1):
        cap = 65537 if limit is None or limit < 0 else limit
        while b"\n" not in self._buf and len(self._buf) < cap:
            piece = self._pull(min(8192, cap - len(self._buf)))
            if not piece:
                break
            self._buf.extend(piece)
        if b"\n" in self._buf:
            idx = self._buf.index(b"\n") + 1
            if idx > cap:
                idx = cap
        else:
            idx = min(len(self._buf), cap)
        line = bytes(self._buf[:idx])
        del self._buf[:idx]
        return line

    def read(self, n=-1):
        if n is None or n < 0:
            self._arm()
            rest = bytes(self._buf) + self._rfile.read(n)
            self._buf.clear()
            return rest
        while len(self._buf) < n:
            piece = self._pull(min(2048, n - len(self._buf)))
            if not piece:
                break
            self._buf.extend(piece)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read1(self, n=-1):
        if self._buf:
            if n is None or n < 0:
                n = len(self._buf)
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out
        return self._pull(2048 if n is None or n < 0 else n)

    def __getattr__(self, name):
        return getattr(self._rfile, name)


def _handler_for(app):
    class PairRedeemHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT_SECONDS

        def log_message(self, fmt, *args):
            # Parser errors may contain arbitrary request-line/path bytes.
            # Never interpolate them, even for malformed HTTP.
            log.info("HTTP parser event")

        def log_request(self, code="-", size="-"):
            # parse_request / REQUEST_URI_TOO_LONG may call send_error
            # before self.path (and sometimes a usable command) exists.
            method = getattr(self, "command", None)
            method = method if method in ("GET", "POST", "HEAD") else "OTHER"
            path = (getattr(self, "path", "") or "").split("?", 1)[0]
            endpoint = path if path in (REDEEM_PATH, ISSUE_PATH, "/health") else "<unknown>"
            try:
                status = int(code)
            except (TypeError, ValueError):
                status = code
            # Peer address is not a secret; keep it for abuse tracing.
            log.info("%s %s %s %s", self.address_string(), method, endpoint, status)

        def send_error(self, code, message=None, explain=None):
            # parse_request leaves the default HTTP/0.9 version on a bad
            # request line, which would omit the status line. Always emit
            # a real HTTP/1.1 error so the 400/414/505 is visible.
            if getattr(self, "request_version", "HTTP/0.9") in ("HTTP/0.9", "", None):
                self.request_version = "HTTP/1.1"
            return BaseHTTPRequestHandler.send_error(self, code, message, explain)

        def log_error(self, fmt, *args):
            self.log_message(fmt, *args)

        def handle_one_request(self):
            started = getattr(self.server, "handler_started", None)
            if started is not None:
                started.set()
            self._deadline = time.monotonic() + app.config.request_deadline
            if not isinstance(self.rfile, _DeadlineRFile):
                self.rfile = _DeadlineRFile(self.rfile, self._arm_socket_timeout)

            def _kill_request_socket():
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.connection.close()
                except OSError:
                    pass

            left = self._deadline - time.monotonic()
            timer = threading.Timer(max(0.0, left), _kill_request_socket)
            timer.daemon = True
            timer.start()
            try:
                self._arm_socket_timeout()
                with app.store.request_scope(self._deadline):
                    BaseHTTPRequestHandler.handle_one_request(self)
            except (TimeoutError, socket.timeout, ConnectionResetError,
                    ConnectionAbortedError, BrokenPipeError, OSError):
                # Mid-line deadline, watchdog close, or a timeout that
                # escaped do_POST: drop the connection. do_POST already
                # emits 400 when it can.
                self.close_connection = True
            finally:
                timer.cancel()

        def _arm_socket_timeout(self):
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("request_deadline")
            cap = self.timeout or REQUEST_TIMEOUT_SECONDS
            self.connection.settimeout(min(cap, left))

        def _read_exactly(self, length):
            """Read length bytes, re-arming the total deadline on every chunk."""
            buf = bytearray()
            while len(buf) < length:
                self._arm_socket_timeout()
                # read1: at most one underlying recv, so a slow drip cannot
                # reset the socket timeout inside a single buffered read().
                want = min(2048, length - len(buf))
                piece = self.rfile.read1(want) if hasattr(self.rfile, "read1") else self.rfile.read(want)
                if not piece:
                    break
                buf.extend(piece)
            return bytes(buf)

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
            if path == ISSUE_PATH:
                self._do_issue()
                return
            if path != REDEEM_PATH:
                self._close_unread()
                self.send_error(404)
                return
            ip = self._client_ip()
            try:
                app.check_rate_limit(ip)
                if "?" in (self.path or ""):
                    # Contract puts the code in the JSON body. A query
                    # string is rejected so it cannot be processed or
                    # logged raw.
                    self._close_unread()
                    self._reject(ip, "invalid_request")
                try:
                    self._arm_socket_timeout()
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
                    raw = self._read_exactly(length)
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
            except TimeoutError:
                self.close_connection = True
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

        def _do_issue(self):
            # Default off: do not advertise the route when the token is unset.
            if not app.config.issue_token:
                self._close_unread()
                self.send_error(404)
                return
            if "?" in (self.path or ""):
                self._close_unread()
                self._send_json(400, {"error": "invalid request"})
                return
            provided = bearer_token(self.headers)
            if not issue_authorized(provided, app.config.issue_token):
                self._close_unread()
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                if (self.headers.get("Transfer-Encoding") or "").strip():
                    self._close_unread()
                    raise IssueError(400, "invalid request")
                length_values = self.headers.get_all("Content-Length") or []
                if not length_values:
                    self._close_unread()
                    raise IssueError(400, "invalid request")
                length_raw = length_values[0]
                if len(length_values) > 1 or "," in length_raw:
                    self._close_unread()
                    raise IssueError(400, "invalid request")
                try:
                    length = int(length_raw)
                except (TypeError, ValueError):
                    self._close_unread()
                    raise IssueError(400, "invalid request")
                if length < 0 or length > MAX_BODY_BYTES:
                    self._close_unread()
                    raise IssueError(400, "invalid request")
                raw = self._read_exactly(length)
                if len(raw) != length:
                    self._close_unread()
                    raise IssueError(400, "invalid request")
                fields = parse_issue_request(
                    raw, self.headers.get("Content-Type"))
                result = issue_code(
                    app.store, fields["hs"], gw=fields["gw"],
                    fp=fields["fp"], now=int(app.clock()))
                self._send_json(200, public_issue_payload(result))
            except IssueError as exc:
                self._send_json(exc.status, {"error": exc.error})
            except ValueError:
                self._send_json(400, {"error": "invalid request"})
            except (TimeoutError, socket.timeout):
                self.close_connection = True
            except Exception:
                log.exception("pair_redeem issue handler failed")
                self._send_json(503, {"error": "temporarily unavailable"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == ISSUE_PATH:
                if not app.config.issue_token:
                    self.send_error(404)
                    return
                self.send_error(405)
                return
            if path == "/health":
                # Compose liveness only; not part of the App contract.
                self.close_connection = True
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return
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
            if path == ISSUE_PATH and app.config.issue_token:
                self.send_error(405)
                return
            if path != REDEEM_PATH:
                self.send_error(404)
                return
            self.send_error(405)

    return PairRedeemHandler

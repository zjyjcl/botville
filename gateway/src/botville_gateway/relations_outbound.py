# SPDX-License-Identifier: Apache-2.0
"""Pairing transport and stoppable durable outbox pump. Never log remote text."""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Official A-line send token. The URL env name stays a gateway placeholder;
# A 线 did not define a separate outbound base-URL variable.
OUTBOUND_TOKEN_ENV = "PAIRING_RELATIONS_SEND_TOKEN"
OUTBOUND_URL_ENV = "GATEWAY_PAIRING_RELATIONS_URL"
ALLOW_PRIVATE_ENV = "PAIRING_RELATIONS_ALLOW_PRIVATE"
ADMIN_TOKEN_ENV = "PAIRING_ADMIN_TOKEN"
SERVICE_TOKEN_ENV = "PAIRING_SERVICE_TOKEN"
OUTBOUND_TOKEN_MIN_CHARS = 32
OUTBOUND_TIMEOUT = 10
BACKOFF = (5, 30, 120, 600, 3600)
_LOG = logging.getLogger(__name__)


def _tokens_equal(left, right):
    """Constant-time when lengths match. Empty or unequal lengths are not equal."""
    if not left or not right or len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def outbound_url_requires_private_allowance(url):
    host = (urllib.parse.urlsplit(url).hostname or "").strip("[]")
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback or address.is_link_local)


def validate_outbound_settings(url, token, *, allow_private=False,
                               admin_token="", service_token=""):
    """Raise ValueError with a message that never contains secret values."""
    if token:
        if len(token) < OUTBOUND_TOKEN_MIN_CHARS:
            raise ValueError(
                f"{OUTBOUND_TOKEN_ENV} must be at least {OUTBOUND_TOKEN_MIN_CHARS} characters")
        if _tokens_equal(token, admin_token):
            raise ValueError(f"{OUTBOUND_TOKEN_ENV} must differ from {ADMIN_TOKEN_ENV}")
        if _tokens_equal(token, service_token):
            raise ValueError(f"{OUTBOUND_TOKEN_ENV} must differ from {SERVICE_TOKEN_ENV}")
    if not url:
        return
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid pairing relations URL: expected http/https base URL")
    if outbound_url_requires_private_allowance(url) and not allow_private:
        raise ValueError(
            f"{OUTBOUND_URL_ENV} points at a private or loopback address; "
            f"set {ALLOW_PRIVATE_ENV}=true only for the federation CI stack "
            "or local host-side access")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def transport(url, token, body, timeout):
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode('utf-8'), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, ValueError):
        # Local transport failures are equivalent to an unavailable upstream.
        return 502, {}
    with response:
        raw = response.read(65537)
        try:
            body = json.loads(raw) if len(raw) <= 65536 else {}
        except (ValueError, UnicodeError):
            body = {}
        return response.status, body if isinstance(body, dict) else {}


class OutboundWorker:
    def __init__(self, federation, *, url='', token='', transport=transport,
                 timeout=OUTBOUND_TIMEOUT, clock=time.time):
        self.federation = federation
        self.url, self._token = url.rstrip('/'), token
        self.transport, self.timeout, self.clock = transport, timeout, clock
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._thread = None

    @property
    def configured(self):
        return bool(self.url and self._token)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.warning('Relations outbox iteration failed; retrying later')
            self._stop.wait(1)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.timeout + 2)
        return self._thread is None or not self._thread.is_alive()

    def run_once(self, now=None):
        if not self.configured or not self.federation.relations.available:
            return
        now = self.clock() if now is None else now
        with self._run_lock:
            # One delivery per tick keeps stop latency bounded by one timeout.
            job = self.federation.next_job(now)
            if job is None or self._stop.is_set():
                return
            body = {k: job[k] for k in ('destination', 'request_id')}
            body['payload'] = json.loads(job['payload'])
            try:
                status, result = self.transport(self.url + '/v1/pairing/internal/relations/' +
                    job['kind'], self._token, body, self.timeout)
            except Exception:
                # Do not let exception text (possibly containing credentials) escape.
                status, result = 502, {}
            self.federation.delivered(job, status, result, now)

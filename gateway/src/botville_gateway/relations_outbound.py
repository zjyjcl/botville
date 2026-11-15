# SPDX-License-Identifier: Apache-2.0
"""Pairing transport and stoppable durable outbox pump. Never log remote text."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

OUTBOUND_TOKEN_ENV = "GATEWAY_PAIRING_RELATIONS_TOKEN"  # 占位，待 A 线定名
OUTBOUND_URL_ENV = "GATEWAY_PAIRING_RELATIONS_URL"
OUTBOUND_TIMEOUT = 10
BACKOFF = (5, 30, 120, 600, 3600)
_LOG = logging.getLogger(__name__)


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

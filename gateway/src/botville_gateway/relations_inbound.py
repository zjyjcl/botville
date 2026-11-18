# SPDX-License-Identifier: Apache-2.0
"""Private pairing ingress. No request, credential or exception text is logged."""
from __future__ import annotations

import hmac
import http.server
import json
import logging
import re
import socket
import threading
import unicodedata
from urllib.parse import urlsplit

from .engine.personas import _MATRIX_LOCALPART_RE
from .relations_federation import canonical, failure

MAX_BYTES = 16 * 1024
KINDS = frozenset(('request', 'respond', 'revoke'))
REQUEST_ID = re.compile(r'[0-9A-Za-z_-]{16,64}\Z')


class Invalid(ValueError):
    def __init__(self, status=400, code='bad_payload', request_id=None):
        self.response = failure(status, code, request_id)


def user_id(value):
    if not isinstance(value, str) or len(value) > 255 or not value.startswith('@'):
        raise Invalid()
    local, sep, server = value[1:].partition(':')
    if not sep or not _MATRIX_LOCALPART_RE.fullmatch(local):
        raise Invalid()
    from .config import normalize_matrix_server_name, ConfigError
    try:
        server = normalize_matrix_server_name(server)
    except ConfigError:
        raise Invalid() from None
    return '@' + local + ':' + server


def validate(kind, body, server_name, now):
    if not isinstance(body, dict) or set(body) != {'kind', 'envelope', 'meta'}:
        raise Invalid()
    envelope, meta = body['envelope'], body['meta']
    if not isinstance(envelope, dict) or set(envelope) != {'v', 'kind', 'origin', 'destination', 'request_id', 'payload'}:
        raise Invalid()
    if not isinstance(meta, dict) or set(meta) != {'origin', 'sent_at', 'received_at'}:
        raise Invalid()
    if type(envelope['v']) is not int or envelope['v'] != 1:
        raise Invalid()
    if kind not in KINDS or envelope['kind'] != kind or body['kind'] != kind:
        raise Invalid(422, 'unsupported')
    rid = envelope['request_id']
    if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
        raise Invalid()
    if len(canonical(envelope)) > MAX_BYTES:
        raise Invalid(request_id=rid)
    if (not isinstance(envelope['destination'], str) or not server_name or
            envelope['destination'].lower() != server_name.lower()):
        raise Invalid(request_id=rid)
    if (not isinstance(envelope['origin'], str) or not isinstance(meta['origin'], str)
            or envelope['origin'].lower() != meta['origin'].lower()):
        raise Invalid(request_id=rid)
    if type(meta['sent_at']) is not int or type(meta['received_at']) is not int or meta['sent_at'] > now + 300:
        raise Invalid(request_id=rid)
    payload = envelope['payload']
    fields = {'from_user', 'to_user'} | ({'from_kind', 'message', 'created_at', 'expires_at'} if kind == 'request'
                                      else {'decision'} if kind == 'respond' else {'reason'})
    if not isinstance(payload, dict) or set(payload) != fields:
        raise Invalid(request_id=rid)
    sender = user_id(payload['from_user'])
    user_id(payload['to_user'])
    if sender.partition(':')[2] != envelope['origin'].lower():
        raise Invalid(request_id=rid)
    if kind == 'request':
        message = payload['message']
        if (not isinstance(payload['from_kind'], str) or payload['from_kind'] not in ('ai', 'human')
                or not isinstance(message, str) or len(message) > 280
                or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') and c != '\n' for c in message)
                or type(payload['created_at']) is not int or type(payload['expires_at']) is not int
                or payload['created_at'] > meta['sent_at'] + 300):
            raise Invalid(request_id=rid)
    else:
        field, choices = ('decision', ('accept', 'reject')) if kind == 'respond' else ('reason', ('withdrawn', 'unfriend'))
        if not isinstance(payload[field], str):
            raise Invalid(request_id=rid)
        if payload[field] not in choices:
            raise Invalid(422, 'unsupported', rid)
    # Keep payload unchanged for exact canonical fingerprinting; normalized identities
    # are applied only after hashing by the business layer.
    return envelope, meta


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


class InboundHandler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(0.8)

    def log_message(self, *args):
        pass

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self._dispatch
        raise AttributeError(name)

    def _send(self, status, body, allow=False):
        raw = canonical(body)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        if allow:
            self.send_header('Allow', 'POST')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)

    def _dispatch(self):
        path = urlsplit(self.path).path
        prefix = '/_internal/relations/'
        if not path.startswith(prefix) or '/' in path[len(prefix):] or not path[len(prefix):]:
            self._send(404, {'errcode': 'relation.not_found', 'error': 'Not found'})
            return
        if self.command != 'POST':
            self._send(405, {'errcode': 'relation.unsupported', 'error': 'POST required'}, allow=True)
            return
        token = self.server.token
        supplied = self.headers.get_all('Authorization', [])
        authorized = (len(supplied) == 1 and supplied[0].startswith('Bearer ') and
                      hmac.compare_digest(supplied[0][7:].encode('utf-8'), token.encode('utf-8')))
        if len(token) < 32 or not authorized:
            self._send(401, {'errcode': 'M_UNKNOWN_TOKEN', 'error': 'Invalid internal credentials'})
            return
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get('Transfer-Encoding'):
                raise Invalid()
            length = int(lengths[0])
            if length > MAX_BYTES:
                raise Invalid()
            self.connection.settimeout(0.8)
            try:
                raw = self.rfile.read(length)
            except socket.timeout:
                raise Invalid() from None
            if len(raw) != length:
                raise Invalid()
            body = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            service = self.server.relations
            envelope, meta = validate(path[len(prefix):], body, service.server_name, service.clock())
        except Invalid as exc:
            status, response = exc.response
        except (ValueError, UnicodeError, RecursionError):
            status, response = failure(400, 'bad_payload', None)
        else:
            try:
                status, response = service.federation.inbound(path[len(prefix):], envelope, meta)
            except Exception:
                status, response = 500, {'errcode': 'relation.unavailable', 'error': 'Relations unavailable', 'request_id': None}
        self._send(status, response)


class InboundServer(http.server.ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, relations, token):
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        self.relations, self.token = relations, token
        if len(token) < 32:
            logging.getLogger(__name__).warning('Relations inbound credentials missing or too short; requests denied')
        super().__init__(address, InboundHandler)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join()

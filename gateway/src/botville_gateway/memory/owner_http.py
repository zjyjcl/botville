# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Owner HTTP boundary. No request bodies or URLs are logged here."""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs

from ..capability_manifest import DEFAULTS, BASE_CAPABILITIES, MEMORY_MANAGE
from ..engine.personas import _is_local_owner
from ..errors import ValidationError
from .profile import FACT_KEYS

READS_PER_MINUTE = 60
WRITES_PER_MINUTE = 20
WINDOW_SECONDS = 60
IDEMPOTENCY_TTL = 86400
IDEMPOTENCY_MAX = 2000
IDEMPOTENCY_PER_OWNER_MAX = 200
PUBLIC_ID = re.compile(r"m[rsf]_[0-9a-f]{16}\Z")
MEMORY_FIELDS = frozenset(('id', 'persona_id', 'layer', 'scope', 'visibility',
    'trust', 'source', 'pinned', 'text', 'key', 'value', 'dirty', 'redacted',
    'created_at', 'updated_at', 'expires_at'))


class HTTPError(Exception):
    def __init__(self, status, code, retry=None):
        self.status, self.code, self.retry = status, code, retry


class MemoryHTTPState:
    def __init__(self, store, secret):
        from .owner import OwnerMemoryService
        self.service = OwnerMemoryService(store, secret) if store is not None else None
        self.lock = threading.RLock()
        self.rates = {}
        self.idempotency = OrderedDict()

    def rate(self, caller, read):
        now = time.monotonic()
        with self.lock:
            for key in list(self.rates):
                queue = self.rates[key]
                while queue and queue[0] <= now - WINDOW_SECONDS:
                    queue.popleft()
                if not queue:
                    del self.rates[key]
            queue = self.rates.setdefault((caller, read), deque())
            cap = READS_PER_MINUTE if read else WRITES_PER_MINUTE
            if len(queue) >= cap:
                raise HTTPError(429, 'rate_limited.transport',
                                max(1, math.ceil(queue[0] + WINDOW_SECONDS - now)))
            queue.append(now)


def _remember_idempotency(state, caller, key, value):
    """Store a fact replay entry without letting one owner evict another."""
    state.idempotency[key] = value
    state.idempotency.move_to_end(key)
    owner_keys = [entry_key for entry_key in state.idempotency if entry_key[0] == caller]
    while len(owner_keys) > IDEMPOTENCY_PER_OWNER_MAX:
        state.idempotency.pop(owner_keys.pop(0), None)
    while len(state.idempotency) > IDEMPOTENCY_MAX:
        state.idempotency.popitem(last=False)


def _expire_idempotency(state, now):
    """Remove expired entries even when replay has changed LRU order."""
    for cached_key, cached in list(state.idempotency.items()):
        if cached[0] <= now - IDEMPOTENCY_TTL:
            state.idempotency.pop(cached_key, None)


def capabilities(server, query):
    params = parse_qs(query)
    entries = deepcopy(BASE_CAPABILITIES)
    if server.memory_store is not None:
        manage = deepcopy(MEMORY_MANAGE)
        manage['owner_can_read_dm'] = server.config.memory_owner_can_read_dm
        # Keep the published manifest order stable (memory.manage follows
        # memory.search); clients still must not depend on it.
        entries.insert(2, manage)
    for name in ('module', 'status'):
        if name in params:
            entries = [entry for entry in entries if entry[name] == params[name][-1]]
    return dict(manifest_version='1.3.0', contract_version=server.config.contract_version,
                generated_at=datetime.now(timezone.utc).isoformat(),
                cache_ttl_seconds=300, defaults=deepcopy(DEFAULTS), capabilities=entries)


def _owner(server, persona_id, caller):
    if not isinstance(persona_id, str) or not persona_id:
        raise HTTPError(400, 'request.invalid')
    persona = server.memory_personas.get(persona_id)
    if persona is None:
        raise HTTPError(404, 'persona.not_found')
    if not persona.owner_user_id or persona.owner_user_id != caller:
        raise HTTPError(403, 'persona.not_owned')


def _redact(server, caller, item):
    item = {key: value for key, value in item.items() if key in MEMORY_FIELDS}
    scope = item['scope']
    redact = (not server.config.memory_owner_can_read_dm and
              (scope['kind'] == 'dm' or
               (item['layer'] == 'profile' and scope.get('counterpart') is not None)))
    if scope['kind'] == 'group':
        source = server.room_member_source
        try:
            joined = source is not None and source.is_joined(
                scope.get('room_id'), caller, item['persona_id'])
        except Exception:
            joined = False
        redact = redact or not joined
    if redact:
        item = dict(item, text=None, redacted=True)
        if 'value' in item:
            item['value'] = None
    return item


def handle(handler):
    state = handler.server.memory_http
    # Authentication is the same end-user verifier used by /moments.
    if not handler._auth_user_ok():
        return
    if state.service is None:
        handler._send_problem(404, 'memory.disabled', 'Memory disabled')
        return
    caller = handler._user_identity()
    _, query, path = handler._route_path()
    params = parse_qs(query, keep_blank_values=True)
    method = handler.command
    rest = path[len('/v1/memories'):]
    action = ('list' if not rest else 'view') if method == 'GET' else (
        'set_fact' if rest == '/facts' else
        ('pin' if method == 'PUT' else 'unpin') if rest.endswith('/pin') else 'delete')
    audit = dict(caller=caller, request_id=handler._request_id(), action=action,
                 persona_id=None, memory_id=None, layer=None, counterpart=None)
    try:
        state.rate(caller, method == 'GET')
        status, result = _route(handler, state, caller, params, rest, audit)
        state.service.audit(**audit, result_code=str(status))
    except HTTPError as exc:
        _failure(handler, state, audit, exc)
        return
    except ValidationError:
        _failure(handler, state, audit, HTTPError(400, 'request.invalid'))
        return
    except ValueError:
        _failure(handler, state, audit, HTTPError(400, 'request.invalid'))
        return
    except sqlite3.Error:
        _failure(handler, state, audit, HTTPError(503, 'memory.unavailable', 5))
        return
    except Exception:
        _failure(handler, state, audit, HTTPError(500, 'internal.error'))
        return
    if status == 204:
        handler.send_response(204)
        handler.send_header('X-Request-Id', handler._request_id())
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
    else:
        handler._send_json(status, result)


def _failure(handler, state, audit, error):
    try:
        state.service.audit(**audit, result_code=error.code)
    except sqlite3.Error:
        error = HTTPError(503, 'memory.unavailable', 5)
    body = handler._problem_body(error.status, error.code, error.code)
    headers = None
    if error.retry:
        headers = {'Retry-After': str(error.retry)}
        body['retry_after_seconds'] = error.retry
    handler._send_json(error.status, body, problem=True, extra_headers=headers)


def _route(handler, state, caller, params, rest, audit):
    server, service, method = handler.server, state.service, handler.command
    if not rest and method == 'GET':
        allowed = {'persona_id', 'layer', 'scope_kind', 'counterpart', 'room_id',
                   'pinned', 'cursor', 'limit'}
        if set(params) - allowed or any(len(v) != 1 for k, v in params.items() if k != 'layer'):
            raise HTTPError(400, 'request.invalid')
        one = lambda key, default=None: params.get(key, [default])[0]
        persona = one('persona_id')
        _owner(server, persona, caller)
        audit['persona_id'] = persona
        layers = params.get('layer', [])
        if any(layer not in ('recent', 'summary', 'profile') for layer in layers):
            raise HTTPError(400, 'request.invalid')
        if one('scope_kind') not in (None, 'dm', 'group') or one('pinned') not in (None, 'true', 'false'):
            raise HTTPError(400, 'request.invalid')
        limit = int(one('limit', '20'))
        if not 1 <= limit <= 100:
            raise HTTPError(400, 'request.invalid')
        items, cursor = service.list(persona, layers=layers, scope_kind=one('scope_kind'),
            counterpart=one('counterpart'), room_id=one('room_id'),
            pinned=None if one('pinned') is None else one('pinned') == 'true',
            cursor=one('cursor'), limit=limit)
        return 200, dict(items=[_redact(server, caller, item) for item in items],
                         page=dict(next_cursor=cursor, has_more=cursor is not None))
    if rest == '/facts' and method == 'PUT':
        body, problem = handler._read_json()
        if problem is not None:
            raise HTTPError(problem['status'], 'request.invalid')
        if set(body) - {'persona_id', 'counterpart', 'key', 'value', 'visibility'}:
            raise HTTPError(400, 'request.invalid')
        persona = body.get('persona_id')
        _owner(server, persona, caller)
        audit.update(persona_id=persona, layer='profile')
        counterpart, key, value = body.get('counterpart'), body.get('key'), body.get('value')
        if not isinstance(key, str) or key not in FACT_KEYS:
            raise HTTPError(422, 'memory.key_not_allowed')
        if not _is_local_owner(counterpart, server.config.matrix_server_name):
            raise HTTPError(422, 'memory.counterpart_invalid')
        audit['counterpart'] = counterpart
        visibility = body.get('visibility', 'dm_only')
        if not isinstance(value, str) or not 1 <= len(value) <= 500 or visibility not in ('dm_only', 'shared'):
            raise HTTPError(422, 'request.invalid')
        idem = handler._idempotency_key()
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        idem_key = (caller, idem) if idem else None
        with state.lock:
            now = time.monotonic()
            _expire_idempotency(state, now)
            previous = state.idempotency.get(idem_key) if idem_key else None
            if previous:
                state.idempotency.move_to_end(idem_key)
                if previous[1] != fingerprint:
                    raise HTTPError(409, 'idempotency.conflict')
                item = service.get(previous[2])
                if item is None:
                    raise HTTPError(404, 'memory.not_found')
                status = previous[3]
            else:
                item, created = service.set_fact(persona, counterpart, key, value, visibility)
                status = 201 if created else 200
                if idem_key:
                    _remember_idempotency(
                        state, caller, idem_key, (now, fingerprint, item['id'], status))
        audit['memory_id'] = item['id']
        return status, _redact(server, caller, item)
    segments = rest.split('/')[1:]
    if len(segments) not in (1, 2) or not PUBLIC_ID.fullmatch(segments[0]):
        raise HTTPError(404, 'memory.not_found')
    item = service.get(segments[0])
    if item is None:
        raise HTTPError(404, 'memory.not_found')
    persona = server.memory_personas.get(item['persona_id'])
    if persona is None or persona.owner_user_id != caller:
        raise HTTPError(404, 'memory.not_found')
    audit.update(persona_id=item['persona_id'], memory_id=item['id'], layer=item['layer'],
                 counterpart=item['scope'].get('counterpart'))
    if len(segments) == 1 and method == 'GET':
        return 200, _redact(server, caller, item)
    if len(segments) == 1 and method == 'DELETE':
        if service.delete(item['persona_id'], item['id']) is None:
            raise HTTPError(404, 'memory.not_found')
        return 204, None
    if len(segments) == 2 and segments[1] == 'pin' and method in ('PUT', 'DELETE'):
        item, reason = service.pin(item['persona_id'], item['id'], method == 'PUT')
        if reason:
            code = reason if reason.startswith('memory.') else 'memory.' + reason
            raise HTTPError(404 if code == 'memory.not_found' else
                            422 if code == 'memory.not_pinnable' else 409, code)
        if item is None:
            raise HTTPError(404, 'memory.not_found')
        return 200, _redact(server, caller, item)
    raise HTTPError(405, 'request.invalid')

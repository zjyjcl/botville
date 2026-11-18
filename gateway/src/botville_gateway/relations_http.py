# SPDX-License-Identifier: Apache-2.0
"""Owner-only relation HTTP boundary; request identifiers are never logged."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from urllib.parse import unquote, urlparse

from .errors import ValidationError
from .group_rooms_http import owner_persona
from .memory.owner_http import (HTTPError, _expire_idempotency,
                                _remember_idempotency)


_ACTIONS = frozenset(("approve", "reject", "revoke", "block", "unblock"))


def _failure(handler, error):
    body = handler._problem_body(error.status, error.code, error.code)
    headers = None
    if error.retry:
        body['retry_after_seconds'] = error.retry
        headers = {'Retry-After': str(error.retry)}
    handler._send_json(error.status, body, problem=True, extra_headers=headers)


def _unavailable(handler):
    _failure(handler, HTTPError(503, 'relation.unavailable', 5))


def _idem_key(state, caller, raw):
    if raw is None:
        return None
    # Share the owner cache key with memory.  A raw key is one caller's single
    # idempotency operation, so reuse across resources is a conflict rather
    # than a chance to decode a differently shaped cached response.
    digest = hmac.new(state._secret, b'idempotency\0' + raw.encode('utf-8'),
                      hashlib.sha256).hexdigest()
    return (caller, digest)


def _fingerprint(persona_id, relation_id, action, body):
    value = {'persona_id': persona_id, 'relation_id': relation_id,
             'action': action, 'body': body}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode('ascii')).hexdigest()


def _write(handler, state, caller, persona_id, relation_id, action, body):
    raw_key = handler._idempotency_key()
    key = _idem_key(state, caller, raw_key)
    fingerprint = ('relations', _fingerprint(persona_id, relation_id, action, body))
    with state.lock:
        now = time.monotonic()
        _expire_idempotency(state, now)
        previous = state.idempotency.get(key) if key else None
        if previous:
            state.idempotency.move_to_end(key)
            if previous[1] != fingerprint:
                raise HTTPError(409, 'idempotency.conflict')
            return previous[2], previous[3]
        if action == 'create':
            status, item = handler.server.relations.create(persona_id, body)
        else:
            item = handler.server.relations.action(persona_id, relation_id, action, body)
            status = 200
        if key:
            _remember_idempotency(state, caller, key, (now, fingerprint, status, item))
        return status, item


def handle(handler, encoded_id, encoded_ref=None, action=None):
    """Serve one relation resource after the router has parsed its segments."""
    persona = owner_persona(handler, encoded_id)
    if persona is None:
        return
    service = getattr(handler.server, 'relations', None)
    state = getattr(handler.server, 'memory_http', None)
    try:
        method = handler.command
        if action is not None:
            allowed = ('POST',)
        elif encoded_ref is None:
            allowed = ('GET', 'HEAD', 'POST')
        else:
            allowed = ('GET', 'HEAD')
        if method not in allowed:
            handler._method_not_allowed(allow=', '.join(allowed))
            return
        if service is None or state is None or not service.available:
            _unavailable(handler)
            return
        state.rate(persona.owner_user_id, read=method in ('GET', 'HEAD'))
        persona_id = persona.user_id
        if method in ('GET', 'HEAD'):
            if encoded_ref is None:
                result = service.list(persona_id, urlparse(handler.path).query)
            else:
                result = service.get(persona_id, unquote(encoded_ref, errors='strict'))
            handler._send_json(200, result)
            return
        body, error = handler._read_json()
        if error is not None:
            raise HTTPError(error['status'], 'request.invalid')
        if action is None:
            status, result = _write(handler, state, persona.owner_user_id,
                                    persona_id, None, 'create', body)
        else:
            if action not in _ACTIONS:
                raise HTTPError(404, 'relation.not_found')
            status, result = _write(handler, state, persona.owner_user_id, persona_id,
                                    unquote(encoded_ref, errors='strict'), action, body)
        handler._send_json(status, result)
    except UnicodeError:
        _failure(handler, HTTPError(404, 'relation.not_found'))
    except ValidationError:
        _failure(handler, HTTPError(400, 'request.invalid'))
    except HTTPError as exc:
        _failure(handler, exc)
    except sqlite3.Error:
        _unavailable(handler)
    except Exception:
        _failure(handler, HTTPError(500, 'internal.error'))

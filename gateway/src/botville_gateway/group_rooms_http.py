# SPDX-License-Identifier: Apache-2.0
"""Owner-only room management. Never reflect request or transport data in errors."""
import base64
import binascii
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

from .personas_list import PersonaListError, owned_persona_candidate


class RoomError(ValueError):
    def __init__(self, status=400, code='request.invalid'):
        self.status, self.code = status, code
        super().__init__(code)


def owner_persona(handler, encoded_id):
    # Identical identity and existence-hiding boundary to /state.
    if not handler._auth_capabilities_ok(persona_detail=True):
        return None
    caller = handler._persona_owner_identity()
    if caller is None:
        return None
    try:
        persona = owned_persona_candidate(handler.server, caller, unquote(encoded_id, errors='strict'))
        if persona.owner_user_id != caller:
            raise PersonaListError()
    except (PersonaListError, UnicodeError):
        handler._send_problem(404, 'persona.not_found', 'Persona not found')
        return None
    return persona


def room_item(row):
    def stamp(value):
        return (datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
                if value is not None else None)
    return dict(room_ref=row['room_key'], state=row['state'], mode=row['mode'],
                joined_at=stamp(row['joined_at']), updated_at=stamp(row['updated_at']))


def room_page(groups, persona_id, query):
    params = parse_qs(query, keep_blank_values=True)
    if set(params) - {'include', 'limit', 'cursor'} or any(len(v) != 1 for v in params.values()):
        raise RoomError()
    include = params.get('include', ['active'])[0]
    limit = params.get('limit', ['50'])[0]
    if include not in ('active', 'all') or not re.fullmatch(r'[0-9]{1,3}', limit) or not 1 <= int(limit) <= 200:
        raise RoomError()
    limit = int(limit)
    after = None
    if 'cursor' in params:
        try:
            cursor = params['cursor'][0]
            if len(cursor) > 512 or not re.fullmatch(r'[A-Za-z0-9_-]+', cursor):
                raise ValueError()
            after = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4),
                                               altchars=b'-_', validate=True))
            if (not isinstance(after, list) or len(after) != 2
                    or type(after[0]) not in (float, int) or not math.isfinite(after[0])
                    or not isinstance(after[1], str) or not re.fullmatch(r'[a-f0-9]{64}', after[1])):
                raise ValueError()
        except (ValueError, OverflowError, UnicodeError, binascii.Error):
            raise RoomError() from None
    empty = dict(items=[], next_cursor=None, has_more=False, groups_enabled=False)
    if groups.store is None:
        return empty
    where = 'persona_id=? AND key_id=?'
    args = [persona_id, groups.store.group_key_id()]
    if include != 'all':
        where += " AND state IN ('accepted','encrypted_disabled')"
    if after is not None:
        where += ' AND (updated_at > ? OR (updated_at = ? AND room_key > ?))'
        args.extend((after[0], after[0], after[1]))
    with groups.store._lock:
        rows = groups.store._db.execute(
            'SELECT room_key,state,mode,joined_at,updated_at FROM group_rooms WHERE ' + where
            + ' ORDER BY updated_at,room_key LIMIT ?', (*args, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = (base64.urlsafe_b64encode(json.dumps([rows[-1]['updated_at'], rows[-1]['room_key']],
                separators=(',', ':')).encode()).decode().rstrip('=') if more else None)
    return dict(items=[room_item(row) for row in rows], next_cursor=cursor,
                has_more=more, groups_enabled=bool(groups.enabled))


def lookup_rooms(groups, persona_id, body):
    if not isinstance(body, dict) or set(body) != {'room_ids'}:
        raise RoomError()
    room_ids = body['room_ids']
    if not isinstance(room_ids, list) or not 1 <= len(room_ids) <= 50:
        raise RoomError()
    seen = set()
    for room_id in room_ids:
        if (not isinstance(room_id, str) or len(room_id) > 255
                or any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 for c in room_id)):
            raise RoomError()
        local, separator, server = room_id.partition(':')
        if (not separator or not re.fullmatch(r'![A-Za-z0-9._~=\-]+', local)
                or not groups.server_name or server.lower() != groups.server_name.lower()):
            raise RoomError()
        # Reject server-case aliases within a batch, but hash the original ID:
        # changing HMAC input would break compatibility with existing room_refs.
        canonical = (local, server.lower())
        if canonical in seen:
            raise RoomError()
        seen.add(canonical)
    if groups.store is None:
        return dict(items=[None] * len(room_ids), groups_enabled=False)
    with groups.store._lock:
        key_id = groups.store.group_key_id()
        items = []
        for room_id in room_ids:
            row = groups.store._db.execute(
                'SELECT room_key,state,mode,joined_at,updated_at FROM group_rooms '
                'WHERE persona_id=? AND room_key=? AND key_id=?',
                (persona_id, groups._key('room', room_id), key_id)).fetchone()
            items.append(room_item(row) if row is not None else None)
    return dict(items=items, groups_enabled=bool(groups.enabled))


def handle_lookup(handler, encoded_id):
    from .memory.owner_http import HTTPError

    persona = owner_persona(handler, encoded_id)
    if persona is None:
        return
    try:
        state = getattr(handler.server, 'memory_http', None)
        if state is not None:
            state.rate(persona.owner_user_id, read=True)
        body, error = handler._read_json()
        if error is not None:
            raise RoomError()
        result = lookup_rooms(handler.server.group_invites, persona.user_id, body)
    except HTTPError as exc:
        # Match memory's rate-limit wire format without writing an audit row.
        body = handler._problem_body(exc.status, exc.code, exc.code)
        headers = None
        if exc.retry:
            body['retry_after_seconds'] = exc.retry
            headers = {'Retry-After': str(exc.retry)}
        handler._send_json(exc.status, body, problem=True, extra_headers=headers)
        return
    except RoomError as exc:
        handler._send_problem(exc.status, exc.code, 'Room request rejected')
        return
    except Exception:
        handler._send_problem(503, 'room.unavailable', 'Room service unavailable')
        return
    handler._send_json(200, result)


def handle(handler, encoded_id, encoded_ref=None):
    persona = owner_persona(handler, encoded_id)
    if persona is None:
        return
    try:
        groups = handler.server.group_invites
        if encoded_ref is None:
            result = room_page(groups, persona.user_id, urlparse(handler.path).query)
        else:
            body, error = handler._read_json()
            if error is not None:
                handler._send_json(error['status'], error, problem=True)
                return
            if (not body or set(body) - {'mode', 'action'}
                    or ('mode' in body and (not isinstance(body['mode'], str)
                        or body['mode'] not in ('mention_only', 'muted')))
                    or ('action' in body and body['action'] != 'leave')):
                raise RoomError()
            result = groups.update_room(persona.user_id, unquote(encoded_ref), body)
    except RoomError as exc:
        handler._send_problem(exc.status, exc.code, 'Room request rejected')
        return
    except Exception:
        handler._send_problem(503, 'room.unavailable', 'Room service unavailable')
        return
    handler._send_json(200, result)

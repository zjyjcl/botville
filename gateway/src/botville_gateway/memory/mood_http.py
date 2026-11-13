# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Owner-only mood state boundary; audit records never contain request content."""
from urllib.parse import unquote

from .owner_http import HTTPError, _failure
from .mood import MOOD_LABELS
from ..personas_list import PersonaListError, owned_persona_candidate


def handle(handler, encoded_id):
    if not handler._auth_capabilities_ok(persona_detail=True):
        return
    caller = handler._persona_owner_identity()
    if caller is None:
        return
    try:
        persona_id = unquote(encoded_id, errors='strict')
        persona = owned_persona_candidate(handler.server, caller, persona_id)
        if persona.owner_user_id != caller:
            raise PersonaListError()
    except (PersonaListError, UnicodeError):
        handler._send_problem(404, 'persona.not_found', 'Persona not found')
        return
    mood = handler.server.mood_service
    if mood is None:
        handler._send_problem(404, 'mood.disabled', 'Mood disabled')
        return
    state = handler.server.memory_http
    audit = dict(caller=caller, request_id=handler._request_id(),
                 action='mood_update' if handler.command == 'PATCH' else 'mood_view',
                 persona_id=persona_id, memory_id=None, layer=None, counterpart=None)
    try:
        state.rate(caller, handler.command != 'PATCH')
        if handler.command == 'PATCH':
            body, error = handler._read_json()
            if error is not None:
                raise HTTPError(error['status'], 'request.invalid')
            if (not body or set(body) - {'mood', 'mood_public'}
                    or ('mood' in body and (not isinstance(body['mood'], str)
                                           or body['mood'] not in MOOD_LABELS))
                    or ('mood_public' in body and type(body['mood_public']) is not bool)):
                raise HTTPError(422, 'request.invalid')
            result = mood.patch(persona_id, **body)
        else:
            result = mood.get(persona_id)
        state.service.audit(**audit, result_code='200')
    except HTTPError as exc:
        _failure(handler, state, audit, exc)
        return
    except Exception:
        _failure(handler, state, audit, HTTPError(503, 'mood.unavailable', 5))
        return
    handler._send_json(200, dict(persona_id=persona_id,
        mood={key: result[key] for key in ('label', 'source', 'set_at')},
        mood_public=result['mood_public'], updated_at=result['updated_at']))

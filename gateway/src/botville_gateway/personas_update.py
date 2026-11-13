# SPDX-License-Identifier: Apache-2.0
"""Serialized, atomic updates to the supported file-persona settings subset."""
import json
import os
import tempfile
from dataclasses import replace

from .engine.personas import PersonaConfigError, _validate_groups, load_personas
from .group_rooms_http import owner_persona
from .personas_list import persona_detail


def handle(handler, encoded_id):
    persona = owner_persona(handler, encoded_id)
    if persona is None:
        return
    body, error = handler._read_json()
    if error is not None:
        handler._send_json(error['status'], error, problem=True)
        return
    try:
        if not body or set(body) != {'groups'} or handler.headers.get('If-Match') is not None:
            raise PersonaConfigError('Unsupported update')
        # Same defaults and strict unknown-field validation as the startup loader.
        groups = _validate_groups(body['groups'], 'persona')
    except PersonaConfigError:
        handler._send_problem(400, 'request.invalid', 'Invalid persona settings')
        return
    server = handler.server
    path = server.config.personas_file
    temporary = None
    try:
        with server.personas_update_lock:
            # Read the current file, preserve unrelated settings, and reject an
            # externally changed ownership boundary rather than overwrite it.
            with open(path, 'rb') as source:
                original = source.read()
            loaded = load_personas(path, server_name=server.config.matrix_server_name,
                                   max_message_chars=server.config.max_message_chars)
            current = loaded.get(persona.user_id)
            if current != server.memory_personas[persona.user_id]:
                handler._send_problem(409, 'persona.state_conflict', 'Persona file changed; reload required')
                return
            payload = json.loads(original)
            for entry in payload['personas']:
                if entry['user_id'] == persona.user_id:
                    entry['groups'] = groups
                    break
            fd, temporary = tempfile.mkstemp(prefix='.personas-', suffix='.json', dir=os.path.dirname(path) or '.')
            with os.fdopen(fd, 'w', encoding='utf-8') as target:
                json.dump(payload, target, ensure_ascii=False, indent=2)
                target.write('\n')
                target.flush()
                os.fsync(target.fileno())
            load_personas(temporary, server_name=server.config.matrix_server_name,
                          max_message_chars=server.config.max_message_chars)
            with open(path, 'rb') as source:
                if source.read() != original:
                    handler._send_problem(409, 'persona.state_conflict', 'Persona file changed; retry required')
                    return
            os.replace(temporary, path)
            temporary = None
            # The group controller and owner HTTP share this dictionary; engine
            # copies need the same immutable replacement for future events.
            updated = replace(current, groups=groups)
            server.memory_personas[persona.user_id] = updated
            if server.engine_hooks is not None:
                server.engine_hooks._personas[persona.user_id] = updated
            result = persona_detail(server, persona.owner_user_id, persona.user_id)
    except Exception:
        handler._send_problem(503, 'persona.unavailable', 'Persona settings unavailable')
        return
    finally:
        if temporary is not None:
            os.unlink(temporary)
    handler._send_json(200, result)

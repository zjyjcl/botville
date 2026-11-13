# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Owner-scoped, allowlisted summaries of startup-loaded file personas."""
from __future__ import annotations

import base64
import binascii
import logging
import re
from urllib.parse import parse_qs

from .engine.personas import _is_local_owner


class PersonaListError(ValueError):
    def __init__(self, status=400, code="request.invalid"):
        super().__init__(code)
        self.status, self.code = status, code


def persona_page(server, owner, query):
    params = parse_qs(query, keep_blank_values=True)
    if any(len(values) != 1 for values in params.values()):
        raise PersonaListError()
    owner_filter = params.get("owner_id", [owner])[0]
    if "owner_id" in params and not _is_local_owner(owner_filter, server.config.matrix_server_name):
        raise PersonaListError()
    if owner_filter != owner:
        raise PersonaListError(403, "persona.not_owned")
    kind = params.get("kind", ["custom"])[0]
    status = params.get("status", ["active"])[0]
    if kind not in ("builtin_template", "custom", "adapter") or status not in (
            "active", "paused", "archived"):
        raise PersonaListError()
    raw_limit = params.get("limit", ["20"])[0]
    if not re.fullmatch(r"[0-9]{1,3}", raw_limit) or not 1 <= int(raw_limit) <= 100:
        raise PersonaListError()
    limit = int(raw_limit)
    after = ""
    if "cursor" in params:
        cursor = params["cursor"][0]
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
                raise ValueError()
            after = base64.b64decode(cursor + "=" * (-len(cursor) % 4),
                                     altchars=b"-_", validate=True).decode("utf-8")
            if (not re.fullmatch(r"@[^\s:]+:[^\s]+", after)
                    or base64.urlsafe_b64encode(after.encode()).decode().rstrip("=") != cursor):
                raise ValueError()
        except (ValueError, UnicodeError, binascii.Error):
            raise PersonaListError() from None
    rows = sorted((p for p in server.memory_personas.values()
                   if p.owner_user_id and p.owner_user_id == owner and p.user_id > after
                   and kind == "custom" and status == "active"), key=lambda p: p.user_id)
    items = [persona_summary(server, p) for p in rows[:limit]]
    has_more = len(rows) > limit
    cursor = (base64.urlsafe_b64encode(items[-1]["id"].encode()).decode().rstrip("=")
              if has_more else None)
    return {"items": items, "next_cursor": cursor, "has_more": has_more}


def persona_summary(server, persona):
    """One allowlist shared by owner detail and list responses."""
    return {"id": persona.user_id, "owner_id": persona.owner_user_id,
            "display_name": persona.display_name or persona.user_id[1:].split(":", 1)[0],
            "bio": persona.bio, "kind": "custom", "status": "active", "revision": 1,
            "created_at": server.personas_timestamp, "updated_at": server.personas_timestamp,
            "visibility": "private"}


def owned_persona_candidate(server, caller, persona_id):
    # Validate decoded IDs, including encoded path separators, without logging input.
    local_user = re.fullmatch(r"@[A-Za-z0-9._=/-]+:([^\s/?#%]+)", caller)
    server_name = server.config.matrix_server_name or ""
    if (not re.fullmatch(r"@ai_[A-Za-z0-9._=/-]+:[^\s/?#%]+", persona_id)
            or not local_user or local_user[1].lower() != server_name.strip().lower()):
        raise PersonaListError(404, "persona.not_found")
    persona = server.memory_personas.get(persona_id)
    if persona is None or not persona.owner_user_id:
        raise PersonaListError(404, "persona.not_found")
    return persona


def persona_detail(server, caller, persona_id):
    persona = owned_persona_candidate(server, caller, persona_id)
    summary = persona_summary(server, persona)
    mood_service = getattr(server, "mood_service", None)
    try:
        state = mood_service.get(persona_id) if mood_service is not None else None
    except Exception:
        logging.getLogger(__name__).warning("Persona detail mood unavailable; returning null")
        state = None
    label = state['label'] if state else None
    if persona.owner_user_id == caller:
        return dict(summary, mood=label, groups=dict(persona.groups))
    return {**{key: summary[key] for key in ("id", "display_name", "bio", "status")},
            "mood": label if state and state["mood_public"] else None}

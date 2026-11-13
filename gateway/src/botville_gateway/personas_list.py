# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Owner-scoped, allowlisted summaries of startup-loaded file personas."""
from __future__ import annotations

import base64
import binascii
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
    items = [{"id": p.user_id, "owner_id": p.owner_user_id,
              "display_name": p.display_name or p.user_id[1:].split(":", 1)[0],
              "kind": "custom", "status": "active", "revision": 1,
              "created_at": server.personas_timestamp, "updated_at": server.personas_timestamp,
              "visibility": "private"} for p in rows[:limit]]
    has_more = len(rows) > limit
    cursor = (base64.urlsafe_b64encode(items[-1]["id"].encode()).decode().rstrip("=")
              if has_more else None)
    return {"items": items, "next_cursor": cursor, "has_more": has_more}

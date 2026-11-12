# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Internal, non-HTTP owner API for PR3a memory.

A trusted local administrator supplies the caller ID. Every operation checks it
against the configured persona owner before accessing memory data.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

from ..engine.personas import _is_local_owner

from .profile import FACT_KEYS
from .store import MemoryStore


class MemoryPermissionError(PermissionError):
    """The caller is not the configured owner of this persona."""


class OwnerMemoryAPI:
    """Owner-gated management operations for one or more personas."""

    def __init__(self, store: MemoryStore,
                 owners: Mapping[str, Optional[str]], *,
                 server_name: Optional[str] = None) -> None:
        self._store = store
        self._owners = dict(owners)
        self._server_name = server_name or os.environ.get("GATEWAY_MATRIX_SERVER_NAME")

    def close(self) -> None:
        """Release the locally-owned store used by an administrative process."""
        self._store.close()

    def _require_owner(self, persona_id: str, caller: str) -> None:
        owner = self._owners.get(persona_id)
        if not owner or owner != caller:
            raise MemoryPermissionError("memory owner permission denied")

    def list_items(self, persona_id: str, caller: str) -> List[Dict[str, Any]]:
        self._require_owner(persona_id, caller)
        return self._store.items(persona_id)

    def list_facts(self, persona_id: str, caller: str) -> List[Dict[str, Any]]:
        self._require_owner(persona_id, caller)
        return self._store.facts(persona_id)

    def show_item(self, persona_id: str, caller: str,
                  item_id: int) -> Optional[Dict[str, Any]]:
        self._require_owner(persona_id, caller)
        return next((item for item in self._store.items(persona_id)
                     if item["id"] == item_id), None)

    def show_fact(self, persona_id: str, caller: str, key: str,
                  counterpart: str) -> Optional[Dict[str, Any]]:
        self._require_owner(persona_id, caller)
        return next((fact for fact in self._store.facts(persona_id, counterpart)
                     if fact["key"] == key), None)

    def delete_item(self, persona_id: str, caller: str, item_id: int) -> bool:
        self._require_owner(persona_id, caller)
        return self._store.delete_item(persona_id, item_id)

    def delete_fact(self, persona_id: str, caller: str, key: str,
                    counterpart: str) -> bool:
        self._require_owner(persona_id, caller)
        return self._store.delete_fact(persona_id, key, counterpart)

    def pin_item(self, persona_id: str, caller: str, item_id: int) -> bool:
        self._require_owner(persona_id, caller)
        return self._store.pin_item(persona_id, item_id, True)

    def unpin_item(self, persona_id: str, caller: str, item_id: int) -> bool:
        self._require_owner(persona_id, caller)
        return self._store.pin_item(persona_id, item_id, False)

    def pin_fact(self, persona_id: str, caller: str, key: str,
                 counterpart: str) -> bool:
        self._require_owner(persona_id, caller)
        return self._store.pin_fact(persona_id, key, counterpart, True)

    def unpin_fact(self, persona_id: str, caller: str, key: str,
                   counterpart: str) -> bool:
        self._require_owner(persona_id, caller)
        return self._store.pin_fact(persona_id, key, counterpart, False)

    def set_fact(self, persona_id: str, caller: str, key: str, value: str,
                 counterpart: str, visibility: str = "dm_only") -> bool:
        self._require_owner(persona_id, caller)
        if not _is_local_owner(counterpart, self._server_name):
            raise ValueError("counterpart must be a local non-ai Matrix user ID")
        if key not in FACT_KEYS:
            raise ValueError("fact key is not allowed")
        if visibility not in ("dm_only", "shared"):
            raise ValueError("invalid fact visibility")
        if not isinstance(value, str) or not value:
            raise ValueError("fact value must be a non-empty string")
        return self._store.upsert_fact(
            persona_id, counterpart, key, value, visibility, "owner", [],
            "trusted", False, True,
        )

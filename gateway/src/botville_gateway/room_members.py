# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Bounded local-AI room membership cache for the AS consumer.

The cache is deliberately a routing aid, not an authority: a failed homeserver
lookup returns no members so callers retain mention-only behaviour.
"""

from __future__ import annotations

from collections import OrderedDict
import threading
import time
from typing import Callable, Iterable, List, Optional, Sequence, Tuple


DEFAULT_MEMBER_CACHE_TTL_SECONDS = 30.0
DEFAULT_MEMBER_CACHE_ROOMS = 1000


class RoomMemberSource:
    """Return local joined AI members from a bounded cache or AS lookup.

    ``observe_member_event`` accepts raw Matrix member events.  Only joined
    local AI users are retained; leave and ban remove them, while invite does
    not make a user a member.  Entries expire quickly so missed state events
    cannot make direct-message routing stale indefinitely.
    """

    def __init__(
        self,
        *,
        fetcher: Optional[Callable[[str, Sequence[str]], Iterable[str]]],
        personas: Sequence[str],
        is_local_ai: Callable[[str], bool],
        ttl_seconds: float = DEFAULT_MEMBER_CACHE_TTL_SECONDS,
        max_rooms: int = DEFAULT_MEMBER_CACHE_ROOMS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if max_rooms < 1:
            raise ValueError("max_rooms must be >= 1")
        self._fetcher = fetcher
        self._personas = tuple(uid for uid in personas if is_local_ai(uid))
        self._is_local_ai = is_local_ai
        self._ttl = ttl_seconds
        self._max_rooms = max_rooms
        self._monotonic = monotonic
        self._items: "OrderedDict[str, Tuple[set[str], float]]" = OrderedDict()
        self._lock = threading.Lock()

    def members(self, room_id: str) -> List[str]:
        """Return cached members, or perform one fail-closed AS lookup."""
        now = self._monotonic()
        with self._lock:
            cached = self._items.get(room_id)
            if cached is not None and cached[1] > now:
                self._items.move_to_end(room_id)
                return sorted(cached[0])
            if cached is not None:
                del self._items[room_id]
        if self._fetcher is None or not self._personas:
            return []
        received = self._fetcher(room_id, self._personas)
        ids = {uid for uid in received if isinstance(uid, str) and self._is_local_ai(uid)}
        self._put(room_id, ids, now)
        return sorted(ids)

    def observe_member_event(self, event: object) -> None:
        """Incorporate a consumed ``m.room.member`` event when relevant."""
        if not isinstance(event, dict) or event.get("type") != "m.room.member":
            return
        room_id = event.get("room_id")
        state_key = event.get("state_key")
        content = event.get("content")
        if not isinstance(room_id, str) or not isinstance(state_key, str):
            return
        if not self._is_local_ai(state_key) or not isinstance(content, dict):
            return
        membership = content.get("membership")
        if membership not in ("join", "leave", "ban"):
            return
        now = self._monotonic()
        with self._lock:
            cached = self._items.get(room_id)
            ids = set(cached[0]) if cached is not None else set()
            if membership == "join":
                ids.add(state_key)
            else:
                ids.discard(state_key)
            self._items[room_id] = (ids, now + self._ttl)
            self._items.move_to_end(room_id)
            while len(self._items) > self._max_rooms:
                self._items.popitem(last=False)

    def _put(self, room_id: str, ids: set[str], now: float) -> None:
        with self._lock:
            self._items[room_id] = (ids, now + self._ttl)
            self._items.move_to_end(room_id)
            while len(self._items) > self._max_rooms:
                self._items.popitem(last=False)

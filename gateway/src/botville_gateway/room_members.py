# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Bounded local-AI room membership cache for the AS consumer.

The cache is deliberately a routing aid, not an authority: a failed homeserver
lookup raises so callers retain mention-only behaviour and count errors.
"""

from __future__ import annotations

from collections import OrderedDict
import threading
import time
from typing import Callable, Iterable, List, Optional, Sequence, Tuple


DEFAULT_MEMBER_CACHE_TTL_SECONDS = 30.0
DEFAULT_MEMBER_CACHE_ROOMS = 1000
NEGATIVE_TTL_SECONDS = 60.0
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 60.0


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
        # Owner disclosure checks must never reuse the AI-only routing cache:
        # their subject is normally a human Matrix user.  Cache the resulting
        # boolean by room and subject, while still using the same bounded
        # failure/backoff path as ``members``.
        self._joined: "OrderedDict[Tuple[str, str], Tuple[bool, float]]" = OrderedDict()
        self._negative: OrderedDict[str, float] = OrderedDict()
        self._consecutive_failures = 0
        self._breaker_until = 0.0
        self._lock = threading.Lock()

    def members(self, room_id: str) -> List[str]:
        """Return valid cache or look up members; failures/backoff raise.

        Negative entries are bounded independently of positive entries. During
        a global cooldown, valid cached membership remains usable.
        """
        now = self._monotonic()
        with self._lock:
            cached = self._items.get(room_id)
            if cached is not None and cached[1] > now:
                self._items.move_to_end(room_id)
                return sorted(cached[0])
            if cached is not None:
                del self._items[room_id]
            if self._negative.get(room_id, 0.0) > now or self._breaker_until > now:
                raise RuntimeError("room membership lookup temporarily unavailable")
            self._negative.pop(room_id, None)
        if self._fetcher is None or not self._personas:
            return []
        try:
            received = self._fetcher(room_id, self._personas)
            ids = {uid for uid in received if isinstance(uid, str) and self._is_local_ai(uid)}
        except Exception:
            with self._lock:
                failed_at = self._monotonic()
                self._record_failure(room_id, failed_at)
            raise
        with self._lock:
            self._negative.pop(room_id, None)
            self._consecutive_failures = 0
            self._breaker_until = 0.0
        self._put(room_id, ids, self._monotonic())
        return sorted(ids)

    def is_joined(self, room_id: str, user_id: str, persona_id: str) -> bool:
        """Return whether ``user_id`` is presently joined to ``room_id``.

        The AS lookup is made while impersonating ``persona_id``.  A lookup
        error deliberately propagates: callers that disclose private memory
        must treat it as ``False`` rather than serving cached or guessed text.
        The positive and negative membership results have the same short TTL
        and room bound as the routing cache.
        """
        if not all(isinstance(value, str) and value for value in
                   (room_id, user_id, persona_id)):
            return False
        key = (room_id, user_id)
        now = self._monotonic()
        with self._lock:
            cached = self._joined.get(key)
            if cached is not None and cached[1] > now:
                self._joined.move_to_end(key)
                return cached[0]
            if cached is not None:
                del self._joined[key]
            if self._negative.get(room_id, 0.0) > now or self._breaker_until > now:
                raise RuntimeError("room membership lookup temporarily unavailable")
            self._negative.pop(room_id, None)
        if self._fetcher is None:
            return False
        try:
            received = self._fetcher(room_id, (persona_id,))
            joined = user_id in {
                member for member in received if isinstance(member, str)
            }
        except Exception:
            with self._lock:
                self._record_failure(room_id, self._monotonic())
            raise
        with self._lock:
            self._negative.pop(room_id, None)
            self._consecutive_failures = 0
            self._breaker_until = 0.0
            self._joined[key] = (joined, self._monotonic() + self._ttl)
            self._joined.move_to_end(key)
            while len(self._joined) > self._max_rooms:
                self._joined.popitem(last=False)
        return joined

    def observe_member_event(self, event: object) -> None:
        """Incorporate a consumed ``m.room.member`` event when relevant."""
        if not isinstance(event, dict) or event.get("type") != "m.room.member":
            return
        room_id = event.get("room_id")
        state_key = event.get("state_key")
        content = event.get("content")
        if not isinstance(room_id, str) or not isinstance(state_key, str):
            return
        if not isinstance(content, dict):
            return
        membership = content.get("membership")
        if membership not in ("join", "leave", "ban"):
            return
        now = self._monotonic()
        with self._lock:
            # This is an authoritative state event, but it only updates an
            # existing owner-check cache entry.  Avoid creating an unbounded
            # cache from every human membership event consumed by the AS.
            joined_key = (room_id, state_key)
            if joined_key in self._joined:
                self._joined[joined_key] = (membership == "join", now + self._ttl)
                self._joined.move_to_end(joined_key)
            if not self._is_local_ai(state_key):
                return
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

    def _record_failure(self, room_id: str, failed_at: float) -> None:
        """Record one failed lookup while holding ``_lock``."""
        self._negative[room_id] = failed_at + NEGATIVE_TTL_SECONDS
        self._negative.move_to_end(room_id)
        while len(self._negative) > self._max_rooms:
            self._negative.popitem(last=False)
        self._consecutive_failures += 1
        if self._consecutive_failures >= BREAKER_FAILURE_THRESHOLD:
            self._breaker_until = failed_at + BREAKER_COOLDOWN_SECONDS

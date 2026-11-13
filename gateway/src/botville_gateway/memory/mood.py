# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Deterministic, content-free persona mood state backed by ``MemoryStore``."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Callable, Dict, Optional

MOOD_LABELS = frozenset(("calm", "happy", "sad", "excited", "tired"))
INTERACTION_WINDOW_SECONDS = 10 * 60
TIRED_WINDOW_SECONDS = 60 * 60
EXCITED_INTERACTIONS = 5
TIRED_REPLIES = 20


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MoodService:
    """The small mood state machine; it never reads message or memory text."""

    def __init__(self, store, half_life_minutes: int, *, clock: Callable[[], float] = time.time):
        if not isinstance(half_life_minutes, int) or isinstance(half_life_minutes, bool):
            raise ValueError("invalid mood half life")
        self._store = store
        self._half_life_seconds = half_life_minutes * 60
        self._clock = clock

    def _row(self, persona_id: str):
        return self._store._db.execute("SELECT * FROM persona_state WHERE persona_id=?", (persona_id,)).fetchone()

    @staticmethod
    def _timestamps(raw: str, cutoff: float) -> list[float]:
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            values = []
        return [float(value) for value in values if isinstance(value, (int, float)) and value >= cutoff]

    def _state(self, row, now: float) -> Dict[str, object]:
        if row is None:
            return {"label": "calm", "source": "rule", "set_at": None,
                    "mood_public": True, "updated_at": None}
        label = row["label"]
        # At exactly one half-life strength is .5; the requirement says strictly
        # below .5 becomes calm, so leave it intact until after that boundary.
        if label != "calm" and now - float(row["label_set_at"]) > self._half_life_seconds:
            label = "calm"
        return {"label": label, "source": row["source"],
                "set_at": _iso(float(row["label_set_at"])),
                "mood_public": bool(row["mood_public"]),
                "updated_at": _iso(float(row["updated_at"]))}

    def get(self, persona_id: str) -> Dict[str, object]:
        now = self._clock()
        with self._store._lock:
            return self._state(self._row(persona_id), now)

    def patch(self, persona_id: str, *, mood: Optional[str] = None,
              mood_public: Optional[bool] = None) -> Dict[str, object]:
        if mood is not None and mood not in MOOD_LABELS:
            raise ValueError("invalid mood")
        if mood_public is not None and type(mood_public) is not bool:
            raise ValueError("invalid mood public")
        if mood is None and mood_public is None:
            raise ValueError("empty mood update")
        now = self._clock()
        with self._store._lock, self._store._db:
            self._store._db.execute("BEGIN IMMEDIATE")
            row = self._row(persona_id)
            label = mood if mood is not None else (row["label"] if row else "calm")
            set_at = now if mood is not None or row is None else float(row["label_set_at"])
            source = "owner" if mood is not None else (row["source"] if row else "rule")
            public = int(mood_public if mood_public is not None else (bool(row["mood_public"]) if row else True))
            recent10 = row["recent_10_json"] if row else "[]"
            recent60 = row["recent_60_json"] if row else "[]"
            self._store._db.execute('''INSERT INTO persona_state
                (persona_id,label,label_set_at,source,mood_public,recent_10_json,recent_60_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(persona_id) DO UPDATE SET
                label=excluded.label,label_set_at=excluded.label_set_at,source=excluded.source,
                mood_public=excluded.mood_public,recent_10_json=excluded.recent_10_json,
                recent_60_json=excluded.recent_60_json,updated_at=excluded.updated_at''',
                (persona_id, label, set_at, source, public, recent10, recent60, now))
            return self._state(self._row(persona_id), now)

    def record_reply(self, persona_id: str, *, human: bool = True) -> Dict[str, object]:
        """Record a successful reply; only human turns affect interaction mood."""
        now = self._clock()
        with self._store._lock, self._store._db:
            self._store._db.execute("BEGIN IMMEDIATE")
            row = self._row(persona_id)
            recent10 = self._timestamps(row["recent_10_json"] if row else "[]", now - INTERACTION_WINDOW_SECONDS)
            recent60 = self._timestamps(row["recent_60_json"] if row else "[]", now - TIRED_WINDOW_SECONDS)
            if human:
                recent10.append(now)
            recent60.append(now)
            # Values beyond each threshold cannot change the result and would
            # make a high-traffic row grow forever.
            recent10 = recent10[-EXCITED_INTERACTIONS:]
            recent60 = recent60[-TIRED_REPLIES:]
            if len(recent60) >= TIRED_REPLIES:
                label = "tired"
            elif human:
                label = "excited" if len(recent10) >= EXCITED_INTERACTIONS else "happy"
            else:
                label = row["label"] if row else "calm"
            public = int(row["mood_public"]) if row else 1
            self._store._db.execute('''INSERT INTO persona_state
                (persona_id,label,label_set_at,source,mood_public,recent_10_json,recent_60_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(persona_id) DO UPDATE SET
                label=excluded.label,label_set_at=excluded.label_set_at,source=excluded.source,
                recent_10_json=excluded.recent_10_json,recent_60_json=excluded.recent_60_json,
                updated_at=excluded.updated_at''',
                (persona_id, label, now if (human or len(recent60) >= TIRED_REPLIES) else
                 (float(row["label_set_at"]) if row else now),
                 "rule" if (human or len(recent60) >= TIRED_REPLIES) else (row["source"] if row else "rule"), public,
                 json.dumps(recent10), json.dumps(recent60), now))
            return self._state(self._row(persona_id), now)

    def record_block(self, persona_id: str) -> Dict[str, object]:
        now = self._clock()
        with self._store._lock, self._store._db:
            self._store._db.execute("BEGIN IMMEDIATE")
            row = self._row(persona_id)
            self._store._db.execute('''INSERT INTO persona_state
                (persona_id,label,label_set_at,source,mood_public,recent_10_json,recent_60_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(persona_id) DO UPDATE SET
                label=excluded.label,label_set_at=excluded.label_set_at,source=excluded.source,
                updated_at=excluded.updated_at''',
                (persona_id, "sad", now, "rule", int(row["mood_public"]) if row else 1,
                 row["recent_10_json"] if row else "[]", row["recent_60_json"] if row else "[]", now))
            return self._state(self._row(persona_id), now)

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""内存 UsageEvent 日志。

设计约束：
- append-only；B2.1 替换 DB 时只需换实现，`UsageStore` Protocol 保持不变。
- 线程安全（HTTP 服务器可能并发写）。
- 滑动窗口限速依赖 `events_in_window()`，返回指定时段内满足过滤条件的事件列表。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional


class EventKind(str, Enum):
    SENT = "sent"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class UsageEvent:
    """单条用量/审计记录。字段名对齐契约 `usage_events` 约定。"""

    id: str
    kind: EventKind
    persona_id: str
    conversation_id: Optional[str]
    reason_code: Optional[str]
    text_preview: str
    recorded_at: datetime

    @staticmethod
    def make(
        kind: EventKind,
        persona_id: str,
        conversation_id: Optional[str] = None,
        reason_code: Optional[str] = None,
        text: str = "",
        at: Optional[datetime] = None,
    ) -> "UsageEvent":
        """构造事件。`at` 由调用方传入服务时钟，保证与策略评估用同一个 now。

        不要在内部直接读 `datetime.now()`：那样注入的时钟（测试、回放）会与
        事件时间戳脱节，滑动窗口判断随之失真。
        """
        preview = text[:80] if text else ""
        return UsageEvent(
            id=f"evt_{uuid.uuid4().hex}",
            kind=kind,
            persona_id=persona_id,
            conversation_id=conversation_id,
            reason_code=reason_code,
            text_preview=preview,
            recorded_at=at or datetime.now(timezone.utc),
        )


class UsageStore:
    """线程安全的内存事件日志。"""

    def __init__(self) -> None:
        self._events: List[UsageEvent] = []
        self._lock = threading.Lock()

    def append(self, event: UsageEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events_in_window(
        self,
        *,
        persona_id: str,
        kind: Optional[EventKind] = None,
        since: datetime,
    ) -> List[UsageEvent]:
        """返回 `since`（含）之后属于 `persona_id` 的事件；可按 kind 过滤。"""
        with self._lock:
            result = [
                e
                for e in self._events
                if e.persona_id == persona_id and e.recorded_at >= since
                and (kind is None or e.kind == kind)
            ]
        return result

    def sent_count_since(self, *, persona_id: str, since: datetime) -> int:
        return len(self.events_in_window(persona_id=persona_id, kind=EventKind.SENT, since=since))

    def blocked_events(self, *, persona_id: str) -> List[UsageEvent]:
        with self._lock:
            return [e for e in self._events if e.persona_id == persona_id and e.kind == EventKind.BLOCKED]

    def all_events(self) -> List[UsageEvent]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""内存 UsageEvent 日志（append-only、线程安全、容量有界）。

设计约束：
- append-only；B2.1 之后换 DB 时只需换实现，`UsageStore` 的公开方法保持不变。
- 线程安全（HTTP 服务器可能并发写）。
- 滑动窗口限速依赖 `events_in_window()`，返回指定时段内满足过滤条件的事件列表。
- **容量上限**（#92）：`max_events` 为 0 表示不设上限（测试用）。超过上限时
  按「最旧优先」淘汰，保证内存不会被无限增长的事件流拖垮。淘汰是粗粒度的
  批量裁剪（一次裁到 90% 上限），避免每条事件都触发列表整体移动。

隐私（#92 / 契约 `UsageEvent`）：事件**不保存消息正文**。只保存
`text_length`（字符数）与 `text_sha256`（正文摘要），既能对账（同内容同 hash），
又不会把用户内容落进审计日志。
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional


class EventKind(str, Enum):
    """内部事件种类。

    `SENT` / `BLOCKED` / `DEFERRED` / `PROACTIVE` 对应契约
    `usage_events.kind` 的 `message_sent` / `blocked` / `message_deferred` /
    `proactive_message`；`TOWN_ACTION` 对应 `town_action`。
    """

    SENT = "sent"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    PROACTIVE = "proactive"
    TOWN_ACTION = "town_action"


# 内部 kind → 契约 UsageEvent.kind
KIND_TO_CONTRACT: Dict[EventKind, str] = {
    EventKind.SENT: "message_sent",
    EventKind.BLOCKED: "blocked",
    EventKind.DEFERRED: "message_deferred",
    EventKind.PROACTIVE: "proactive_message",
    EventKind.TOWN_ACTION: "town_action",
}


def text_digest(text: str) -> str:
    """正文摘要（SHA-256 十六进制）。空正文返回空串，避免把空串伪装成可对账 hash。"""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UsageEvent:
    """单条用量/审计记录。字段名对齐契约 `usage_events` 约定。

    **不存正文**：`text_length` + `text_sha256` 足够对账与去重排查，
    且不会把用户消息内容写进用量账单（#92）。
    """

    id: str
    kind: EventKind
    persona_id: str
    conversation_id: Optional[str]
    reason_code: Optional[str]
    text_length: int
    text_sha256: str
    recorded_at: datetime
    # token 记账（B2.1）：发送类事件记实际入账预算的 token。
    tokens_in: int = 0
    tokens_out: int = 0
    town_tokens: int = 0

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def contract_kind(self) -> str:
        return KIND_TO_CONTRACT[self.kind]

    @staticmethod
    def make(
        kind: EventKind,
        persona_id: str,
        conversation_id: Optional[str] = None,
        reason_code: Optional[str] = None,
        text: str = "",
        at: Optional[datetime] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        town_tokens: int = 0,
    ) -> "UsageEvent":
        """构造事件。`at` 由调用方传入服务时钟，保证与策略评估用同一个 now。

        不要在内部直接读 `datetime.now()`：那样注入的时钟（测试、回放）会与
        事件时间戳脱节，滑动窗口判断随之失真。
        """
        return UsageEvent(
            id=f"evt_{uuid.uuid4().hex}",
            kind=kind,
            persona_id=persona_id,
            conversation_id=conversation_id,
            reason_code=reason_code,
            text_length=len(text or ""),
            text_sha256=text_digest(text or ""),
            recorded_at=at or datetime.now(timezone.utc),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            town_tokens=town_tokens,
        )


class UsageStore:
    """线程安全、容量有界的内存事件日志。

    `max_events=0` 表示不设上限（测试/回放用）。默认上限来自
    `Config.max_usage_events`，由 `build_server()` 注入。
    """

    def __init__(self, max_events: int = 10_000) -> None:
        if max_events < 0:
            raise ValueError("max_events must be >= 0")
        self._events: List[UsageEvent] = []
        self._max_events = max_events
        self._lock = threading.Lock()

    @property
    def max_events(self) -> int:
        return self._max_events

    def append(self, event: UsageEvent) -> None:
        with self._lock:
            self._events.append(event)
            self._evict_locked()

    def _evict_locked(self) -> None:
        """超限时按最旧优先批量裁剪到 90% 上限（兜底至少留 1 条）。"""
        if self._max_events <= 0:
            return
        overflow = len(self._events) - self._max_events
        if overflow <= 0:
            return
        keep = max(1, int(self._max_events * 0.9))
        del self._events[: len(self._events) - keep]

    def events_in_window(
        self,
        *,
        persona_id: Optional[str] = None,
        kind: Optional[EventKind] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        conversation_id: Optional[str] = None,
    ) -> List[UsageEvent]:
        """返回窗口 `(since, until]` 内的事件；任一过滤条件为 None 表示不过滤。

        窗口**左开右闭**（#92）：
        - 左端开：一条恰好 `window_seconds` 前的事件已经「过期」，不应再占名额。
          若用闭区间，`reset_at` 恰好等于 now 时调用方重试仍被拦，形成差一秒的
          活锁；开区间让「reset_at 到了 ⇒ 名额真的腾出来」成立。
        - 右端闭：`since == until` 得到空集，但同一秒内已记录的事件不会被漏算。
        不传 `until`（策略评估的默认）表示不设上限，避免时钟回拨把已记用量漏掉。
        """
        with self._lock:
            return [
                e
                for e in self._events
                if (persona_id is None or e.persona_id == persona_id)
                and (kind is None or e.kind == kind)
                and (conversation_id is None or e.conversation_id == conversation_id)
                and (since is None or e.recorded_at > since)
                and (until is None or e.recorded_at <= until)
            ]

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

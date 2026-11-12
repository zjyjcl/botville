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
`text_length`（字符数）与 `text_sha256`（正文摘要），既能对账，又不会把用户
内容落进审计日志。

**#114 PR-a：`text_sha256` 改为 HMAC-SHA256(部署密钥, 正文)**。裸 SHA-256 的
短/低熵文本可被字典反推；HMAC 让没有密钥的一方无法离线猜测。字段名保持不变
（契约兼容），同一部署同内容仍同值可对账；轮换密钥后不可跨期比对。未传密钥
（直接构造 `UsageEvent` 的测试路径）退回裸 SHA-256，生产路径由 `build_server`
注入 `Config.hmac_secret`。
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional


class EventKind(str, Enum):
    """内部事件种类。

    `SENT` / `BLOCKED` / `DEFERRED` / `PROACTIVE` 对应契约
    `usage_events.kind` 的 `message_sent` / `blocked` / `message_deferred` /
    `proactive_message`；`TOWN_ACTION` 对应 `town_action`。
    `HUMAN` 是内部辅助事件（人类消息到达），不映射到契约 kind，
    `list_usage_events` 中过滤掉，聚合计数也不计入。
    """

    SENT = "sent"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    PROACTIVE = "proactive"
    TOWN_ACTION = "town_action"
    MEMORY = "memory"  # Internal accounting only; no public kind added.
    HUMAN = "human"  # B2.2: 人类消息记录，用于 anti_spam / ai_loop 计数重置


# 内部 kind → 契约 UsageEvent.kind；HUMAN 不在契约 enum 中，list_usage_events 过滤掉。
KIND_TO_CONTRACT: Dict[EventKind, str] = {
    EventKind.SENT: "message_sent",
    EventKind.BLOCKED: "blocked",
    EventKind.DEFERRED: "message_deferred",
    EventKind.PROACTIVE: "proactive_message",
    EventKind.TOWN_ACTION: "town_action",
    EventKind.MEMORY: "",
    EventKind.HUMAN: "",  # 哨兵：空串；list_usage_events 按此过滤
}


def text_digest(text: str, secret: Optional[bytes] = None) -> str:
    """正文摘要：有部署密钥时 `HMAC-SHA256(secret, b"text\\x00" + text)`。

    空正文返回空串，避免把空串伪装成可对账 hash。未传 `secret` 时退回裸
    SHA-256（仅为直接构造事件的测试路径保留；生产由 `UsageStore` 注入密钥）。
    `b"text\\x00"` 是与 service 的 txnId（`b"txn\\x00"`）区分的域分隔前缀。
    """
    if not text:
        return ""
    payload = text.encode("utf-8")
    if secret is None:
        return hashlib.sha256(payload).hexdigest()
    return hmac.new(secret, b"text\x00" + payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class UsageEvent:
    """单条用量/审计记录。字段名对齐契约 `usage_events` 约定。

    **不存正文**：`text_length` + `text_sha256` 足够对账与去重排查，
    且不会把用户消息内容写进用量账单（#92）。`text_sha256` 自 #114 PR-a 起
    是 HMAC-SHA256(部署密钥, 正文)（字段名不变，契约兼容）。
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
    # B2.2：peer_id 对应 OutgoingMessageRequest.peer_id；sent/blocked 事件写入。
    # 旧事件无此字段（None），不计入任何 peer 层计数（历史数据不回填）。
    peer_id: Optional[str] = None

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
        peer_id: Optional[str] = None,
        secret: Optional[bytes] = None,
    ) -> "UsageEvent":
        """构造事件。`at` 由调用方传入服务时钟，保证与策略评估用同一个 now。

        不要在内部直接读 `datetime.now()`：那样注入的时钟（测试、回放）会与
        事件时间戳脱节，滑动窗口判断随之失真。

        `secret` 是部署密钥（#114 PR-a）：省略时退回裸 SHA-256（测试兼容）。
        """
        return UsageEvent(
            id=f"evt_{uuid.uuid4().hex}",
            kind=kind,
            persona_id=persona_id,
            conversation_id=conversation_id,
            reason_code=reason_code,
            text_length=len(text or ""),
            text_sha256=text_digest(text or "", secret),
            recorded_at=at or datetime.now(timezone.utc),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            town_tokens=town_tokens,
            peer_id=peer_id,
        )


class UsageStore:
    """线程安全、容量有界的内存事件日志。

    `max_events=0` 表示不设上限（测试/回放用）。默认上限来自
    `Config.max_usage_events`，由 `build_server()` 注入。
    """

    def __init__(
        self,
        max_events: int = 10_000,
        retention_seconds: int = 0,
        *,
        now_fn=None,
        secret: Optional[bytes] = None,
    ) -> None:
        if max_events < 0:
            raise ValueError("max_events must be >= 0")
        if retention_seconds < 0:
            raise ValueError("retention_seconds must be >= 0")
        self._events: List[UsageEvent] = []
        self._max_events = max_events
        self._retention_seconds = retention_seconds
        self._now = now_fn
        # #114 PR-a：正文摘要用的部署密钥（Config.hmac_secret）；None 退回裸 SHA-256。
        self._secret = secret
        self._lock = threading.Lock()

    @property
    def secret(self) -> Optional[bytes]:
        return self._secret

    @property
    def max_events(self) -> int:
        return self._max_events

    @property
    def retention_seconds(self) -> int:
        return self._retention_seconds

    def append(self, event: UsageEvent, retention_seconds: Optional[int] = None) -> None:
        """追加事件；按保留期 + 容量淘汰旧事件（B2.2 #114-7 / #117 B4）。

        `retention_seconds` 省略时用构造时注入的保留期（`Config.usage_retention_seconds`）；
        显式传 `0` 表示本次不做保留期淘汰（纯容量）。
        """
        with self._lock:
            self._events.append(event)
            self._evict_locked(
                self._retention_seconds if retention_seconds is None else retention_seconds,
                now=event.recorded_at,
            )

    def _evict_locked(
        self, retention_seconds: int = 0, *, now: Optional[datetime] = None
    ) -> None:
        """超限时优先丢弃早于保留期的事件，容量仍超时优先丢 BLOCKED、再按最旧淘汰
       （B2.2 #114-7 保留期 / #117 B6）。

        `retention_seconds > 0` 时先将早于 `now - retention_seconds` 的事件批量清除，
        再检查容量。容量仍超时**优先丢弃最旧的 BLOCKED 事件**（保护作为计数来源的
        SENT/DEFERRED/HUMAN），仍超才按最旧优先裁剪到 90% 上限（兜底至少留 1 条）。
        `retention_seconds=0` 时退化为纯容量淘汰，优先级同上。

        `now` 取**被 append 事件的时间**（service 注入的时钟，即事件 `recorded_at`），
        不读 `datetime.now()`：注入/回放时钟下淘汰边界才不会失真（#117 B4 / N9）。
        仅在未注入时钟且未传 `now` 的回退路径才读墙钟（生产不会触发）。
        """
        if self._max_events <= 0 and retention_seconds <= 0:
            return
        # 按保留期淘汰（仅当 retention_seconds > 0）
        if retention_seconds > 0:
            if now is None:
                now = self._now() if self._now is not None else datetime.now(timezone.utc)
            cutoff = now - timedelta(seconds=retention_seconds)
            self._events = [e for e in self._events if e.recorded_at > cutoff]
        if self._max_events <= 0:
            return
        overflow = len(self._events) - self._max_events
        if overflow <= 0:
            return
        # B6 (#117)：容量淘汰优先丢 BLOCKED（最旧优先），保护计数事件。
        blocked_indices = [
            i for i, e in enumerate(self._events) if e.kind == EventKind.BLOCKED
        ]
        if blocked_indices:
            drop = min(overflow, len(blocked_indices))
            drop_set = set(blocked_indices[:drop])
            self._events = [
                e for i, e in enumerate(self._events) if i not in drop_set
            ]
            overflow = len(self._events) - self._max_events
        if overflow <= 0:
            return
        # 仍超：按最旧优先裁剪到 90% 上限（兜底至少留 1 条）。
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
        peer_id: Optional[str] = None,
    ) -> List[UsageEvent]:
        """返回窗口 `(since, until]` 内的事件；任一过滤条件为 None 表示不过滤。

        窗口**左开右闭**（#92）：
        - 左端开：一条恰好 `window_seconds` 前的事件已经「过期」，不应再占名额。
          若用闭区间，`reset_at` 恰好等于 now 时调用方重试仍被拦，形成差一秒的
          活锁；开区间让「reset_at 到了 ⇒ 名额真的腾出来」成立。
        - 右端闭：`since == until` 得到空集，但同一秒内已记录的事件不会被漏算。
        不传 `until`（策略评估的默认）表示不设上限，避免时钟回拨把已记用量漏掉。

        `peer_id` 过滤（B2.2）：传入时只返回 event.peer_id == peer_id 的事件；
        旧事件（peer_id=None）不匹配，符合「旧事件不计入 peer 层」的契约约定。
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
                and (peer_id is None or e.peer_id == peer_id)
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

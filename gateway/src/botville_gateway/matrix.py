# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Matrix 传输抽象。

  - `MatrixTransport`：Protocol，网关只依赖这几个方法；
  - `InMemoryMatrixTransport`：内存假后端，消息进列表，供测试与本地跑通用。

A1.3 起真实后端是 `matrix_appservice.AppServiceMatrixTransport`（appservice
注册的 AS token + `?user_id=` masquerade，调用
`PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`），同样
满足本 Protocol，service 层不用改。
"""

from __future__ import annotations

import itertools
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Protocol, runtime_checkable

# Matrix 事务 ID 允许的字符（spec: `[A-Za-z0-9._=-]`）。
_TXN_ID_RE = re.compile(r"^[A-Za-z0-9._=-]{1,255}$")


@dataclass(frozen=True)
class SentEvent:
    """一次成功投递的结果。"""

    event_id: str
    room_id: str
    sender_id: str
    text: str
    sent_at: datetime
    reply_to: Optional[str] = None
    content_type: str = "text"
    # Matrix 事务 ID：service 有 Idempotency-Key 时由该键稳定推导（#99）。
    # 保留在结果里便于对账与测试断言「重试是否落在同一事务」。
    txn_id: Optional[str] = None


@dataclass
class DeliveryError(Exception):
    """传输失败。网关据此返回 5xx problem，而不是策略结论。"""

    code: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 仅日志用
        return f"{self.code}: {self.detail}"


@runtime_checkable
class MatrixTransport(Protocol):
    """网关需要的 Matrix 能力。appservice 实现同样满足此 Protocol。"""

    def send_text(
        self,
        *,
        room_id: str,
        sender_id: str,
        text: str,
        reply_to: Optional[str] = None,
        content_type: str = "text",
        txn_id: Optional[str] = None,
    ) -> SentEvent:
        """以 `sender_id` 身份向 `room_id` 发送消息。失败抛 DeliveryError。

        `content_type` 原样携带请求的 `content.type`（`text` / `markdown`），
        实现方不得擅自降级为 `text`。

        `txn_id` 是 Matrix 事务 ID（`PUT .../send/m.room.message/{txnId}`）：
        服务层在有 `Idempotency-Key` 时传入由该键稳定推导的值，使**重试落在
        同一个事务**上（HS 按 `(room, sender, txnId)` 去重）；省略时实现方
        自行生成随机值。
        """
        ...

    def healthy(self) -> bool:
        """传输层是否可用（供 /health 使用）。"""
        ...


def resolve_txn_id(txn_id: Optional[str]) -> str:
    """统一 txnId 规整：省略时给随机值；显式值只允许 Matrix txnId 安全字符。

    调用方（service）给的是 `sha256(idempotency_key).hexdigest()[:64]`，但
    传输实现不该盲信外部输入——非法字符会让 PUT 路径断裂或需要转义。
    """
    if txn_id is None:
        return uuid.uuid4().hex
    if not txn_id or not _TXN_ID_RE.fullmatch(txn_id):
        raise DeliveryError(
            "matrix.invalid_txn_id",
            f"txn_id must match ^[A-Za-z0-9._=-]{{1,255}}$, got {txn_id!r}",
        )
    return txn_id


@dataclass
class InMemoryMatrixTransport:
    """内存假后端：消息保存在 `sent` 列表里，不做网络调用。

    线程安全：HTTP 雏形用多线程服务器，多个请求可能并发写。
    """

    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))
    sent: List[SentEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    fail: bool = False

    def send_text(
        self,
        *,
        room_id: str,
        sender_id: str,
        text: str,
        reply_to: Optional[str] = None,
        content_type: str = "text",
        txn_id: Optional[str] = None,
    ) -> SentEvent:
        if self.fail:
            raise DeliveryError("matrix.unavailable", "in-memory transport is set to fail")
        txn = resolve_txn_id(txn_id)  # 与真实传输一致的 txnId 校验
        with self._lock:
            event = SentEvent(
                event_id=f"$inmem_{next(self._counter)}_{uuid.uuid4().hex[:8]}",
                room_id=room_id,
                sender_id=sender_id,
                text=text,
                sent_at=datetime.now(timezone.utc),
                reply_to=reply_to,
                content_type=content_type,
                txn_id=txn,
            )
            self.sent.append(event)
            return event

    def healthy(self) -> bool:
        return not self.fail

    # 测试辅助
    def messages_for(self, room_id: str) -> List[SentEvent]:
        with self._lock:
            return [e for e in self.sent if e.room_id == room_id]

    def clear(self) -> None:
        with self._lock:
            self.sent.clear()

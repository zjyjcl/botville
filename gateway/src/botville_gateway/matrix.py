# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Matrix 传输抽象。

生产环境（A1.3 落地后）会用 appservice 注册的 AS token 调用
`PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`，
以角色身份把消息写进房间。A1.3 未就绪，所以这里只定义：

  - `MatrixTransport`：Protocol，网关只依赖这几个方法；
  - `InMemoryMatrixTransport`：内存假后端，消息进列表，供测试与本地跑通用。

接 appservice 时新增 `AppServiceMatrixTransport`，service 层不用改。
"""

from __future__ import annotations

import itertools
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class SentEvent:
    """一次成功投递的结果。"""

    event_id: str
    room_id: str
    sender_id: str
    text: str
    sent_at: datetime
    reply_to: Optional[str] = None


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
    ) -> SentEvent:
        """以 `sender_id` 身份向 `room_id` 发送文本消息。失败抛 DeliveryError。"""
        ...

    def healthy(self) -> bool:
        """传输层是否可用（供 /health 使用）。"""
        ...


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
    ) -> SentEvent:
        if self.fail:
            raise DeliveryError("matrix.unavailable", "in-memory transport is set to fail")
        with self._lock:
            event = SentEvent(
                event_id=f"$inmem_{next(self._counter)}_{uuid.uuid4().hex[:8]}",
                room_id=room_id,
                sender_id=sender_id,
                text=text,
                sent_at=datetime.now(timezone.utc),
                reply_to=reply_to,
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

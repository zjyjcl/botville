# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.4 内置角色引擎（简单接入，contract §1.7.8 ① ②）。

把消费骨架（`as_consumer.py`）的 `on_human_text` 钩子与模型对接：
  `limits.evaluate → model.complete → transport.send_text`

每条 on_human_text 事件按以下顺序执行：

1. **限额前置**（contract §1.7.8 ①）：对每个目标 persona 分别调用内部
   `policy.evaluate`（同 `POST /policy/evaluate` 的三层限额 + 防刷屏 + 熔断
   + 静默）；`blocked` → 跳过，零模型调用；
2. **去重**：`PersistentSpeechDedupe`（`as_speech_dedupe` 表）；
3. **上下文拼装**：system = persona 指令区；用户消息环形缓冲（最近
   `MAX_CONTEXT_MESSAGES` 条）放进 user role，不可信内容经 §7.2 四区隔离包裹；
4. **模型调用**：`ModelClient.complete`；
5. **投递**：`transport.send_text`（`user_id=` AS masquerade，幂等 txnId）。

未配置模型 / 人设为空 → 只记录计数，不发言，`/health` 不 degraded。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Callable, Dict, Deque, List, Optional, Tuple

from ..as_consumer import (
    ConsumerContext,
    ConsumerHooks,
    TransientUnavailable,
    speech_idempotency_key,
    truncate_text,
)
from ..isolation import build_untrusted_segment
from ..matrix import DeliveryError, MatrixTransport
from ..policy import DECISION_BLOCK, PolicyEngine
from ..timeutil import utcnow
from .model_client import ModelClient, ModelError
from .personas import Persona

_log = logging.getLogger(__name__)

# 每个房间保留的最近消息条数上限（进程内缓冲；PR3 换持久化记忆）。
MAX_CONTEXT_MESSAGES = 20
# 进 ring buffer 的单条消息正文上限（已截断的；与 as_consumer 的截断同量级）。
_RING_TEXT_LIMIT = 8000


class _RoomBuffer:
    """进程内 per-room 最近消息环形缓冲（线程安全）。

    只存 `(role, text)` 对：`role` 是 `"user"` / `"assistant"`，`text` 是
    已截断的正文。不存 sender / event_id / 时间 / trust：只用来给模型提供上下文，
    不做任何幂等或计费决策。
    """

    def __init__(self, max_entries: int = MAX_CONTEXT_MESSAGES) -> None:
        self._buf: Deque[Tuple[str, str]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def append(self, role: str, text: str) -> None:
        with self._lock:
            self._buf.append((role, text))

    def snapshot(self) -> List[Tuple[str, str]]:
        with self._lock:
            return list(self._buf)


class EngineHooks(ConsumerHooks):
    """把 `on_human_text` 接到角色引擎（限额 → 模型 → 发言）。

    参数：
      - `personas`：`user_id -> Persona`；
      - `client`：已配置的 `ModelClient`；
      - `transport`：发言传输（`AppServiceMatrixTransport` 或 `InMemoryMatrixTransport`）；
      - `policy_engine`：`PolicyEngine`（三层限额/熔断判定）；
      - `dedupe`：持久化去重账本；
      - `now_fn`：时钟（测试注入用）。
    """

    def __init__(
        self,
        *,
        personas: Dict[str, Persona],
        client: ModelClient,
        transport: MatrixTransport,
        policy_engine: Optional[PolicyEngine] = None,
        dedupe: Optional["SpeechDedupe"] = None,
        now_fn: Callable[[], datetime] = utcnow,
    ) -> None:
        self._personas = personas
        self._client = client
        self._transport = transport
        self._policy_engine = policy_engine
        self._dedupe = dedupe
        self._now = now_fn
        self._buffers: Dict[str, _RoomBuffer] = {}
        self._buf_lock = threading.Lock()

    def _get_buffer(self, room_id: str) -> _RoomBuffer:
        with self._buf_lock:
            if room_id not in self._buffers:
                self._buffers[room_id] = _RoomBuffer()
            return self._buffers[room_id]

    def on_human_text(self, ctx: ConsumerContext) -> None:
        """per-event 入口：对 target_user_ids 里每个 persona 执行 evaluate→model→send。"""
        room_id = ctx.room_id or ""
        text = ctx.text
        sender = ctx.sender or ""
        event_id = ctx.event_id or ""

        # 先把人类消息放进 ring buffer（不管是否会回复）。
        if room_id:
            buf = self._get_buffer(room_id)
            buf.append("user", truncate_text(text, _RING_TEXT_LIMIT))

        targets = list(ctx.target_user_ids) if ctx.target_user_ids else (
            [ctx.persona_user_id] if ctx.persona_user_id else []
        )
        if not targets:
            return

        for persona_user_id in targets:
            persona = self._personas.get(persona_user_id)
            if persona is None:
                continue
            self._reply_as(ctx, persona, room_id, text, event_id)

    def _reply_as(
        self,
        ctx: ConsumerContext,
        persona: Persona,
        room_id: str,
        text: str,
        event_id: str,
    ) -> None:
        """一个 persona 对一条消息的完整回复流程。"""
        idem_key = speech_idempotency_key(persona.user_id, event_id)

        # 去重：重放时已 mark 的键直接跳过。
        if self._dedupe is not None and self._dedupe.seen(idem_key):
            return

        # 1. 限额前置：与 POST /policy/evaluate 相同的三层判定（blocked → 零模型调用）。
        if self._policy_engine is not None:
            try:
                token_est = max(1, len(text) // 4 + 1)
                decision = self._policy_engine.evaluate(
                    persona_id=persona.user_id,
                    conversation_id=room_id or None,
                    text=text,
                    token_estimate=token_est,
                    allow_deferred=False,
                )
            except Exception as exc:  # noqa: BLE001 - 限额失败不阻塞消费
                _log.warning("engine policy evaluation failed: %s", type(exc).__name__)
                return
            if decision.decision == DECISION_BLOCK:
                _log.debug(
                    "engine speech blocked for a persona by policy (%d reason(s))",
                    len(decision.reasons),
                )
                return

        # 2. 上下文拼装（§7.2 四区隔离：人设进 system 区，对话进数据区）。
        system_text = persona.instruction_text()
        buf = self._get_buffer(room_id) if room_id else None
        history = buf.snapshot() if buf else []
        messages = _build_messages(history, text)

        # 3. 模型调用。TransientUnavailable / ModelError 原样上抛给消费侧：
        #    transient 不计 attempts 只短退避；ModelError 属普通失败。
        reply_text = self._client.complete(
            system_text=system_text,
            messages=messages,
            persona=persona,
        )

        # 4. 投递（AS masquerade 以 persona.user_id 发言；txnId 由幂等键推导，
        #    HS 按 (room, sender, txnId) 去重，重放不会重复发言）。
        reply_text = truncate_text(reply_text, persona.max_reply_chars)
        try:
            self._transport.send_text(
                room_id=room_id,
                sender_id=persona.user_id,
                text=reply_text,
                txn_id=idem_key,
            )
        except DeliveryError as exc:
            # HS 暂时不可用 → transient；其余（命名空间/参数错误）→ 普通失败。
            if exc.code in ("matrix.unreachable", "matrix.unavailable"):
                raise TransientUnavailable(
                    f"matrix transport unavailable ({exc.code})"
                ) from exc
            raise

        # 5. 成功：写去重账本 + 把 AI 回复存 ring buffer。
        if self._dedupe is not None:
            self._dedupe.mark(idem_key)
        if buf is not None:
            buf.append("assistant", truncate_text(reply_text, _RING_TEXT_LIMIT))


def _build_messages(
    history: List[Tuple[str, str]],
    current_text: str,
) -> List[Dict[str, str]]:
    """把历史对话 + 当前消息打包成 messages 列表（数据区一律经 nonce 包裹）。

    `history` 的末尾一条是本次人类消息（`on_human_text` 已 append），先去掉再
    统一追加，避免同一条消息出现两次。历史里角色为 `assistant` 的 AI 回复
    是模型自己说过的话（低风险），直接作为 assistant turn；用户历史与当前
    消息一律用 §7.2 的 `<untrusted id="...">` 数据区包裹。
    """
    messages: List[Dict[str, str]] = []
    trimmed = list(history)
    if trimmed and trimmed[-1][0] == "user":
        trimmed = trimmed[:-1]

    for role, text in trimmed:
        if role == "assistant":
            messages.append({"role": "assistant", "content": text})
        else:
            wrapped, _ = build_untrusted_segment(text)
            messages.append({"role": "user", "content": wrapped})

    wrapped, _ = build_untrusted_segment(current_text)
    messages.append({"role": "user", "content": wrapped})
    return messages


class InMemorySpeechDedupeForEngine:
    """进程内发言去重（未配置 AS 事务库 / 测试用；语义同持久化账本）。线程安全。"""

    def __init__(self) -> None:
        self._keys: set = set()
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        with self._lock:
            return key in self._keys

    def mark(self, key: str) -> None:
        with self._lock:
            self._keys.add(key)

    def claim(self, key: str) -> None:
        self.mark(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)

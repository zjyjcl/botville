# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.4 内置角色引擎（简单接入，contract §1.7.8 ① ②）。

把消费骨架（`as_consumer.py`）的 `on_human_text` 钩子与模型对接：
  `service.evaluate_policy → model.complete → service.send_ai_message`

每条 on_human_text 事件按以下顺序执行：

1. **去重**：`PersistentSpeechDedupe`（`as_speech_dedupe` 表）；
2. **限额前置**（contract §1.7.8 ①）：对每个目标 persona 分别调用内部
   `service.evaluate_policy`（同 `POST /policy/evaluate` 的三层限额 + 防刷屏 + 熔断
   + 静默）；非 allow → 停止，零模型调用；
3. **上下文拼装**：system = persona 指令区；用户消息环形缓冲（最近
   `MAX_CONTEXT_MESSAGES` 条）；所有聊天文本经 §7.2 数据区隔离包裹；
4. **模型调用**：`ModelClient.complete`；
5. **投递**：`service.send_ai_message`（预留、AS masquerade、提交用量，幂等 txnId）。

未配置模型 / 人设为空 → 只记录计数，不发言，`/health` 不 degraded。
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from typing import Any, Dict, Deque, List, Optional, Tuple

from ..as_consumer import (
    ConsumerContext,
    DeferredUntil,
    ConsumerHooks,
    ConsumerStats,
    SpeechDedupe,
    TransientUnavailable,
    speech_idempotency_key,
    truncate_text,
)
from ..isolation import SYSTEM_ZONE_DECLARATION, build_untrusted_segment
from ..errors import GatewayDeliveryError
from ..policy import DECISION_ALLOW, DECISION_DEFER, CODE_SILENT_HOURS
from ..service import GatewayService
from .model_client import ModelClient
from .personas import Persona

# 每个房间保留的最近消息条数上限（进程内缓冲；PR3 换持久化记忆）。
MAX_CONTEXT_MESSAGES = 20
MAX_ROOM_BUFFERS = 1000
# 进 ring buffer 的单条消息正文上限（已截断的；与 as_consumer 的截断同量级）。
_RING_TEXT_LIMIT = 8000


def estimate_tokens(text: str) -> int:
    """Estimate tokens by character count, with a minimum of one."""
    return max(1, len(text) // 4 + 1)


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
      - `service`：与 HTTP 共用的 GatewayService（含 AS masquerade 传输）；
      - `dedupe`：持久化去重账本；
    `stats` 属性提供内部计数，不含标识或正文。
    """

    manages_speech_dedupe = True

    def __init__(
        self,
        *,
        personas: Dict[str, Persona],
        client: ModelClient,
        service: GatewayService,
        dedupe: Optional[SpeechDedupe] = None,
    ) -> None:
        self._personas = personas
        self._client = client
        self._service = service
        self._dedupe = dedupe
        self.stats = ConsumerStats()
        self._buffers: OrderedDict[str, _RoomBuffer] = OrderedDict()
        self._buf_lock = threading.Lock()

    def _get_buffer(self, room_id: str) -> _RoomBuffer:
        with self._buf_lock:
            if room_id not in self._buffers:
                self._buffers[room_id] = _RoomBuffer()
            self._buffers.move_to_end(room_id)
            while len(self._buffers) > MAX_ROOM_BUFFERS:
                self._buffers.popitem(last=False)
            return self._buffers[room_id]

    def on_human_text(self, ctx: ConsumerContext) -> None:
        """per-event 入口：对 target_user_ids 里每个 persona 执行 evaluate→model→send。"""
        room_id = ctx.room_id or ""
        text = ctx.text
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
            self._reply_as(persona, room_id, text, event_id)

    def _reply_as(
        self,
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

        # 先拼装完整提示词，再估算提示词及最大回复的预算。
        system_text = SYSTEM_ZONE_DECLARATION + "\n" + persona.instruction_text()
        buf = self._get_buffer(room_id) if room_id else None
        history = buf.snapshot() if buf else []
        messages = _build_messages(history, text)
        token_estimate = (estimate_tokens(system_text)
                          + sum(estimate_tokens(m["content"]) for m in messages)
                          + estimate_tokens("x" * persona.max_reply_chars))
        decision = self._service.evaluate_policy({
            "persona_id": persona.user_id,
            "conversation_id": room_id,
            "content": text,
            "token_estimate": token_estimate,
            "kind": "message",
        })
        if decision["decision"] != DECISION_ALLOW:
            if decision["decision"] == DECISION_DEFER:
                raise self._deferred_until(decision, persona, room_id)
            self.stats.inc("precheck_blocked")
            return

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
            receipt = self._service.send_ai_message(
                conversation_id=room_id,
                payload={
                    "sender_persona_id": persona.user_id,
                    "content": {"type": "text", "text": reply_text},
                    "token_estimate": estimate_tokens(reply_text),
                    "allow_deferred": False,
                },
                idempotency_key=idem_key,
            )
        except GatewayDeliveryError as exc:
            # HS 暂时不可用 → transient；其余（命名空间/参数错误）→ 普通失败。
            if exc.code in ("matrix.unreachable", "matrix.unavailable"):
                raise TransientUnavailable(
                    f"matrix transport unavailable ({exc.code})"
                ) from exc
            raise

        # 只有发送成功才消耗 speech key；静默边界竞争延后到窗口结束。
        if receipt["outcome"] not in ("sent", "delivered"):
            decision = receipt.get("decision", {})
            if decision.get("decision") == DECISION_DEFER or any(
                    r.get("code") == CODE_SILENT_HOURS
                    for r in decision.get("reasons", [])):
                raise self._deferred_until(decision, persona, room_id, receipt)
            self.stats.inc("send_blocked")
            return
        if self._dedupe is not None:
            self._dedupe.mark(idem_key)
        if buf is not None:
            buf.append("assistant", truncate_text(reply_text, _RING_TEXT_LIMIT))

    def _deferred_until(
        self, decision: Dict[str, Any], persona: Persona, room_id: str,
        receipt: Optional[Dict[str, Any]] = None,
    ) -> DeferredUntil:
        candidates = [decision.get("defer_until"), decision.get("retry_at"),
                      decision.get("may_clear_at"),
                      (receipt or {}).get("may_clear_at")]
        candidates.extend(c.get("reset_at") for c in decision.get("checks", [])
                          if c.get("key") == "silent_hours" and not c.get("allowed"))
        for value in candidates:
            if value:
                return DeferredUntil(datetime.fromisoformat(value.replace("Z", "+00:00")))
        delay = decision.get("retry_after_seconds")
        if delay is not None:
            at = decision.get("evaluated_at")
            now = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else self._service.now
            return DeferredUntil(now + timedelta(seconds=delay))
        retry_at = self._service.silent_hours_end(persona.user_id, room_id)
        if retry_at is None:
            raise TransientUnavailable("deferred policy has no active window")
        return DeferredUntil(retry_at)


def _build_messages(
    history: List[Tuple[str, str]],
    current_text: str,
) -> List[Dict[str, str]]:
    """把历史对话 + 当前消息打包成 messages 列表（数据区一律经 nonce 包裹）。

    历史末尾为本次人类消息，去掉后统一追加；所有聊天文本（包括 AI 回复）
    都经数据区包裹，trust 不赋予指令权限。每条最多 8000 码点，每轮最多 20 条。
    """
    messages: List[Dict[str, str]] = []
    trimmed = list(history)
    if trimmed and trimmed[-1][0] == "user":
        trimmed = trimmed[:-1]

    for role, text in trimmed[-(MAX_CONTEXT_MESSAGES - 1):]:
        wrapped, _ = build_untrusted_segment(truncate_text(text, _RING_TEXT_LIMIT))
        messages.append({"role": role, "content": wrapped})

    wrapped, _ = build_untrusted_segment(truncate_text(current_text, _RING_TEXT_LIMIT))
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

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)

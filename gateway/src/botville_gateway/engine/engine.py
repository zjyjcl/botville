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

import logging
import threading
import time
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
    MemoryDBUnavailable,
    speech_idempotency_key,
    truncate_text,
)
from ..isolation import SYSTEM_ZONE_DECLARATION, build_untrusted_segment
from ..errors import GatewayDeliveryError
from ..policy import DECISION_ALLOW, DECISION_DEFER, CODE_SILENT_HOURS, estimate_tokens
from ..service import GatewayService
from .model_client import ModelClient
from .personas import Persona

# 每个房间保留的最近消息条数上限（进程内缓冲；PR3 换持久化记忆）。
MAX_CONTEXT_MESSAGES = 20
MAX_ROOM_BUFFERS = 1000
# 进 ring buffer 的单条消息正文上限（已截断的；与 as_consumer 的截断同量级）。
_RING_TEXT_LIMIT = 8000
_LOG = logging.getLogger(__name__)


class _RoomBuffer:
    """进程内 per-room 最近消息环形缓冲（线程安全）。

    存 role、已截断正文与可选 event_id；只对仍在缓冲中的事件去重，
    不做任何发送幂等或计费决策。
    """

    def __init__(self, max_entries: int = MAX_CONTEXT_MESSAGES) -> None:
        self._buf: Deque[Tuple[str, str, Optional[str]]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def append(self, role: str, text: str, event_id: Optional[str] = None) -> None:
        with self._lock:
            if event_id and any(entry[2] == event_id for entry in self._buf):
                return
            self._buf.append((role, text, event_id))

    def snapshot(self) -> List[Tuple[str, str]]:
        with self._lock:
            return [(role, text) for role, text, _ in self._buf]


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
        memory_store: Optional[object] = None,
        memory_embedding: Optional[object] = None,
        memory_vectors: Optional[object] = None,
    ) -> None:
        self._personas = personas
        self._client = client
        self._service = service
        self._dedupe = dedupe
        self._memory_store = memory_store
        self._memory_embedding = memory_embedding
        self._memory_vectors = memory_vectors
        self.stats = ConsumerStats()
        self._memory_failures = 0
        self._memory_warning_at = float('-inf')
        self.stats.set("memory_db_unavailable_consecutive", 0)
        self.stats.set("memory_db_alerts", 0)
        self._buffers: OrderedDict[str, _RoomBuffer] = OrderedDict()
        self._buf_lock = threading.Lock()

    @property
    def persona_ids(self) -> Tuple[str, ...]:
        """Configured persona IDs as an immutable snapshot."""
        return tuple(self._personas)

    def _get_buffer(self, room_id: str) -> _RoomBuffer:
        with self._buf_lock:
            if room_id not in self._buffers:
                self._buffers[room_id] = _RoomBuffer()
            self._buffers.move_to_end(room_id)
            while len(self._buffers) > MAX_ROOM_BUFFERS:
                self._buffers.popitem(last=False)
            return self._buffers[room_id]

    def _memory_unavailable(self, exc: Exception) -> MemoryDBUnavailable:
        self._memory_failures += 1
        self.stats.set("memory_db_unavailable_consecutive", self._memory_failures)
        if self._memory_failures == 5:
            self.stats.inc("memory_db_alerts")
        now = time.monotonic()
        if self._memory_failures >= 5 and now - self._memory_warning_at >= 3600:
            _LOG.warning("%s", type(exc).__name__)
            self._memory_warning_at = now
        return MemoryDBUnavailable(min(3600, 30 * 2 ** min(self._memory_failures - 1, 7)))

    def _memory_recovered(self) -> None:
        self._memory_failures = 0
        self.stats.set("memory_db_unavailable_consecutive", 0)

    def on_human_text(self, ctx: ConsumerContext) -> None:
        try:
            self._on_human_text(ctx)
        except MemoryDBUnavailable:
            raise
        except Exception:
            self._memory_recovered()
            raise
        else:
            self._memory_recovered()

    def _on_human_text(self, ctx: ConsumerContext) -> None:
        """per-event 入口：对 target_user_ids 里每个 persona 执行 evaluate→model→send。"""
        room_id = ctx.room_id or ""
        text = ctx.text
        event_id = ctx.event_id or ""

        # Persistent memory replaces the in-process buffer when enabled.
        if room_id and self._memory_store is None:
            buf = self._get_buffer(room_id)
            buf.append("user", truncate_text(text, _RING_TEXT_LIMIT), event_id)

        targets = list(ctx.target_user_ids) if ctx.target_user_ids else (
            [ctx.persona_user_id] if ctx.persona_user_id else []
        )
        if self._memory_store is not None and event_id:
            try:
                if self._memory_store.is_tombstoned(event_id):
                    return
            except Exception as exc:
                self.stats.inc("memory_read_errors")
                raise self._memory_unavailable(exc) from None
        if self._memory_store is not None:
            # In a group, retain human input for every confirmed local persona
            # even when it was not mentioned and will not answer this turn.
            record_ids = list(dict.fromkeys((*targets, *ctx.room_ai_user_ids)))
            for persona_user_id in record_ids:
                persona = self._personas.get(persona_user_id)
                if persona is not None:
                    self._record_memory(persona, ctx, "user", text, event_id)
        if not targets:
            return

        for persona_user_id in targets:
            persona = self._personas.get(persona_user_id)
            if persona is None:
                continue
            self._reply_as(persona, room_id, text, event_id, ctx)

    def _reply_as(
        self,
        persona: Persona,
        room_id: str,
        text: str,
        event_id: str,
        ctx: Optional[ConsumerContext] = None,
    ) -> None:
        """一个 persona 对一条消息的完整回复流程。"""
        # PR3a may install the hooks solely to persist layer-1 memory while no
        # model is configured.  Preserve PR2's record/count-only behaviour.
        if not self._client.config.configured:
            return
        idem_key = speech_idempotency_key(persona.user_id, event_id)

        # 去重：重放时已 mark 的键直接跳过。
        if self._dedupe is not None and self._dedupe.seen(idem_key):
            return

        # Evaluate policy before reading or embedding memory.  In particular a
        # rejected message must not reserve embedding budget or expose recall.
        system_text = SYSTEM_ZONE_DECLARATION + "\n" + persona.instruction_text()
        buf = self._get_buffer(room_id) if room_id and self._memory_store is None else None
        def evaluate(messages):
            return self._service.evaluate_policy({
                "persona_id": persona.user_id,
                "conversation_id": room_id,
                "content": text,
                "token_estimate": (estimate_tokens(system_text)
                    + sum(estimate_tokens(m["content"]) for m in messages)
                    + estimate_tokens("x" * persona.max_reply_chars)),
                "kind": "message",
            })

        # Persistent recall can trigger an embedding budget reservation.  A
        # cheap preflight prevents that work for any rejected user message.
        if self._memory_store is not None and ctx is not None:
            decision = evaluate(_build_messages([], text))
            if decision["decision"] != DECISION_ALLOW:
                if decision["decision"] == DECISION_DEFER:
                    raise self._deferred_until(decision, persona, room_id)
                self.stats.inc("precheck_blocked")
                return

        if self._memory_store is not None and ctx is not None:
            scope_kind, scope_key, counterpart = self._memory_scope(persona, ctx)
            try:
                from ..memory.recall import assemble_recall
                from ..memory.summary import StoreSummaryProvider
                vector_recall = None
                if self._memory_embedding is not None and self._memory_vectors is not None:
                    from ..memory.worker import budgeted_embed
                    from ..memory.vector import VectorRecall
                    try:
                        query = budgeted_embed(self._memory_embedding, self._service,
                                               persona.user_id, text,
                                               conversation_id=room_id)
                        vector_recall = VectorRecall(self._memory_vectors, query_vec=query,
                                                     model=self._memory_embedding.config.model,
                                                     dim=self._memory_embedding.dimension)
                    except Exception:
                        # Vector enrichment is optional; never defer a human reply.
                        self.stats.inc('memory_vector_skipped')
                messages = assemble_recall(
                    self._memory_store, persona_id=persona.user_id,
                    scope_kind=scope_kind, scope_key=scope_key,
                    counterpart=counterpart,
                    mentioned_counterparts=ctx.mentioned_user_ids,
                    budget=2000,
                    summary_provider=StoreSummaryProvider(self._memory_store),
                    vector_recall=vector_recall,
                )
                # The current event must be visible even when pinned/profile
                # memory consumes the recall allowance.  It remains untrusted.
                messages.extend(_build_messages([], text))
            except Exception as exc:
                self.stats.inc("memory_recall_errors")
                raise self._memory_unavailable(exc) from None
        else:
            history = buf.snapshot() if buf else []
            messages = _build_messages(history, text)

        # Keep the normal full-prompt budget gate: recalled context and room
        # history count towards the reply policy just as before.
        decision = evaluate(messages)
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
        if self._memory_store is not None and ctx is not None:
            self._record_memory(
                persona, ctx, "assistant", reply_text,
                str((receipt.get("delivery") or {}).get("matrix_event_id") or "spk:" + idem_key),
            )
        if buf is not None:
            buf.append("assistant", truncate_text(reply_text, _RING_TEXT_LIMIT))

    def on_redaction(self, ctx: ConsumerContext) -> None:
        if self._memory_store is None or not ctx.redacts_event_id:
            return
        try:
            self._memory_store.redact_event(ctx.redacts_event_id, room_id=ctx.room_id or None)
        except Exception as exc:
            self.stats.inc("memory_redaction_errors")
            raise self._memory_unavailable(exc) from None
        self._memory_recovered()

    @staticmethod
    def _memory_scope(persona: Persona, ctx: ConsumerContext) -> Tuple[str, str, str]:
        if ctx.room_members_confirmed and len(ctx.room_ai_user_ids) == 1 and ctx.sender:
            return "dm", ctx.sender, ctx.sender
        return "group", ctx.room_id or "", ctx.sender or ""

    def _record_memory(self, persona: Persona, ctx: ConsumerContext, role: str,
                       text: str, event_id: str) -> None:
        scope_kind, scope_key, counterpart = self._memory_scope(persona, ctx)
        try:
            self._memory_store.add_item(
                persona_id=persona.user_id, scope_kind=scope_kind, scope_key=scope_key,
                room_id=ctx.room_id or "", counterpart=counterpart,
                event_id=event_id or None,
                sender=ctx.sender if role == "user" else persona.user_id,
                role=role, trust=ctx.trust if role == "user" else "trusted",
                text=truncate_text(text, _RING_TEXT_LIMIT), source="event",
            )
        except Exception as exc:
            self.stats.inc("memory_write_errors")
            if role == "assistant":
                # Delivery already succeeded; memory must not trigger a resend.
                _LOG.warning("%s", type(exc).__name__)
                return
            raise self._memory_unavailable(exc) from None

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

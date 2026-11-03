# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""发言编排：评估策略 → 允许则经 transport 投递 → 返回 MessageReceipt 形态 dict。

契约要点（gateway/docs/contract.md §1.4）：
- 策略拦截是 **HTTP 200 + outcome=blocked**，不是 429/403；
- `retryable` 在本结构里始终为 false；
- `deferred` 必须带 `queue_id`，表示服务端排程，客户端不要自行重试；
- 被拦截要记 `usage_events kind=blocked`。

请求校验（保持零依赖，手写最小 schema 校验，对齐 OpenAPI）：
- `OutgoingMessageRequest.additionalProperties: false` → 未知字段拒绝；
- `OutgoingContent.type` ∈ {text, markdown}（必填）；
- `reply_to` 必须是 string；`mentions` 原样透传；
- `PolicyEvaluateRequest.kind` ∈ {message, proactive, town_action}。

幂等：同一 `Idempotency-Key` + 相同请求体返回缓存回执；同 key 不同请求体
抛 `IdempotencyConflict`（HTTP 层映射 409 `idempotency.conflict`）。
`queued` / `deferred` 同样进缓存（契约 §1.5）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .matrix import DeliveryError, MatrixTransport
from .policy import (
    DECISION_BLOCK,
    DECISION_DEFER,
    PolicyDecision,
    PolicyEngine,
)
from .store import EventKind, UsageEvent, UsageStore
from .timeutil import iso_utc, utcnow

CONTENT_TYPES = frozenset({"text", "markdown"})
EVALUATE_KINDS = frozenset({"message", "proactive", "town_action"})
# 与 OpenAPI OutgoingMessageRequest / OutgoingContent 的 properties 对齐。
_SEND_FIELDS = frozenset(
    {"sender_persona_id", "content", "reply_to", "token_estimate", "allow_deferred", "metadata"}
)
_CONTENT_FIELDS = frozenset({"type", "text", "mentions"})
_EVALUATE_FIELDS = frozenset(
    {"persona_id", "conversation_id", "kind", "content", "token_estimate"}
)


class IdempotencyConflict(Exception):
    """同一 Idempotency-Key 配了不同请求体。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"idempotency key {key!r} was reused with a different request")
        self.key = key


class ValidationError(Exception):
    """请求体不符合契约（映射为 400 problem+json）。"""

    def __init__(self, code: str, detail: str, field: Optional[str] = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.field = field


class GatewayDeliveryError(Exception):
    """transport 投递失败（映射为 500 internal.error，不是策略结论）。

    注意：契约状态码集合不含 502/`matrix.unavailable`；B6 选方案(a)，
    统一映射为 500 internal.error，不改契约状态码集合。
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class _QueueItem:
    """deferred 排程。B1.1 只记录，不实现定时投递（留 TODO 给 B1.2/B2.x）。"""

    queue_id: str
    room_id: str
    sender_id: str
    text: str
    deliver_after: datetime
    reply_to: Optional[str] = None


class GatewayService:
    def __init__(
        self,
        config: Config,
        transport: MatrixTransport,
        store: Optional[UsageStore] = None,
        *,
        now_fn=utcnow,
    ) -> None:
        self._config = config
        self._transport = transport
        self._store = store if store is not None else UsageStore()
        self._engine = PolicyEngine(config, self._store, now_fn=now_fn)
        self._now = now_fn
        self._lock = threading.RLock()
        self._idempotency: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        self._queue: Dict[str, _QueueItem] = {}
        self._started_at = now_fn()

    # ------------------------------------------------------------- accessors
    @property
    def store(self) -> UsageStore:
        return self._store

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def now(self) -> datetime:
        """当前时钟值（供 /health uptime 计算）。"""
        return self._now()

    def pending_queue(self) -> List[_QueueItem]:
        with self._lock:
            return list(self._queue.values())

    # ----------------------------------------------------------- policy only
    def evaluate_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # B5: 拒绝未知字段（additionalProperties: false）
        unknown = set(payload.keys()) - _EVALUATE_FIELDS
        if unknown:
            raise ValidationError(
                "request.invalid",
                f"unexpected fields: {', '.join(sorted(unknown))}",
            )
        persona_id = payload.get("persona_id")
        if not isinstance(persona_id, str) or not persona_id:
            raise ValidationError("request.invalid", "'persona_id' is required", "persona_id")
        text = payload.get("content") or ""
        if not isinstance(text, str):
            raise ValidationError("request.invalid", "'content' must be a string", "content")
        token_estimate = payload.get("token_estimate")
        if token_estimate is not None and (
            not isinstance(token_estimate, int) or isinstance(token_estimate, bool) or token_estimate < 0
        ):
            raise ValidationError(
                "request.invalid", "'token_estimate' must be a non-negative integer", "token_estimate"
            )
        kind = payload.get("kind", "message")
        if kind not in EVALUATE_KINDS:
            raise ValidationError(
                "request.invalid",
                f"'kind' must be one of {sorted(EVALUATE_KINDS)}, got {kind!r}",
                "kind",
            )
        decision = self._engine.evaluate(
            persona_id=persona_id,
            conversation_id=payload.get("conversation_id"),
            text=text,
            token_estimate=token_estimate,
            kind=kind,
        )
        return decision.to_dict()

    # ------------------------------------------------------------- send flow
    def send_ai_message(
        self,
        conversation_id: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValidationError("request.invalid", "conversation_id is required", "conversation_id")

        # B5: additionalProperties: false —— 未知字段直接拒绝
        unknown = set(payload.keys()) - _SEND_FIELDS
        if unknown:
            raise ValidationError(
                "request.invalid",
                f"unexpected fields: {', '.join(sorted(unknown))}",
            )

        sender = payload.get("sender_persona_id")
        if not isinstance(sender, str) or not sender:
            raise ValidationError(
                "request.invalid", "'sender_persona_id' is required", "sender_persona_id"
            )

        content = payload.get("content")
        if not isinstance(content, dict):
            raise ValidationError("request.invalid", "'content' must be an object", "content")
        unknown_content = set(content.keys()) - _CONTENT_FIELDS
        if unknown_content:
            raise ValidationError(
                "request.invalid",
                f"unexpected content fields: {', '.join(sorted(unknown_content))}",
                "content",
            )
        content_type = content.get("type")
        if content_type not in CONTENT_TYPES:
            raise ValidationError(
                "request.invalid",
                f"'content.type' must be one of {sorted(CONTENT_TYPES)}, got {content_type!r}",
                "content.type",
            )
        text = content.get("text")
        if not isinstance(text, str) or not text:
            raise ValidationError("request.invalid", "'content.text' is required", "content.text")
        if len(text) > self._config.max_message_chars:
            raise ValidationError(
                "request.invalid",
                f"'content.text' exceeds {self._config.max_message_chars} characters",
                "content.text",
            )
        mentions = content.get("mentions")
        if mentions is not None:
            if not isinstance(mentions, list) or not all(isinstance(m, str) for m in mentions):
                raise ValidationError(
                    "request.invalid",
                    "'content.mentions' must be an array of strings",
                    "content.mentions",
                )

        reply_to = payload.get("reply_to")
        if reply_to is not None and not isinstance(reply_to, str):
            raise ValidationError(
                "request.invalid", "'reply_to' must be a string", "reply_to"
            )

        metadata = payload.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError(
                "request.invalid", "'metadata' must be an object", "metadata"
            )

        allow_deferred = payload.get("allow_deferred", True)
        if not isinstance(allow_deferred, bool):
            raise ValidationError(
                "request.invalid", "'allow_deferred' must be a boolean", "allow_deferred"
            )

        token_estimate = payload.get("token_estimate")
        if token_estimate is not None and (
            not isinstance(token_estimate, int) or isinstance(token_estimate, bool) or token_estimate < 0
        ):
            raise ValidationError(
                "request.invalid", "'token_estimate' must be a non-negative integer", "token_estimate"
            )

        fingerprint = self._fingerprint(conversation_id, payload)
        # 幂等查缓存 → 评估 → 投递/记账 → 写缓存，全程持同一把可重入锁：
        # 否则两个同 key 并发请求会都 miss 缓存、都投递，幂等就失效了。
        with self._lock:
            if idempotency_key:
                entry = self._idempotency.get(idempotency_key)
                if entry is not None:
                    stored_fp, receipt = entry
                    if stored_fp != fingerprint:
                        raise IdempotencyConflict(idempotency_key)
                    return json.loads(json.dumps(receipt))  # 深拷贝，调用方改不坏缓存

            receipt = self._send_locked(
                conversation_id=conversation_id,
                sender=sender,
                text=text,
                reply_to=reply_to,
                mentions=list(mentions) if mentions else None,
                token_estimate=token_estimate,
                allow_deferred=allow_deferred,
            )

            if idempotency_key:
                self._idempotency[idempotency_key] = (fingerprint, receipt)
            return json.loads(json.dumps(receipt))

    # ------------------------------------------------------------ internals
    def _send_locked(
        self,
        *,
        conversation_id: str,
        sender: str,
        text: str,
        reply_to: Optional[str],
        mentions: Optional[List[str]] = None,
        token_estimate: Optional[int],
        allow_deferred: bool,
    ) -> Dict[str, Any]:
        """调用方必须已持有 `self._lock`（RLock，可重入）。"""
        now = self._now()
        decision = self._engine.evaluate(
            persona_id=sender,
            conversation_id=conversation_id,
            text=text,
            token_estimate=token_estimate,
            allow_deferred=allow_deferred,
            now=now,
        )

        if decision.decision == DECISION_BLOCK:
            self._store.append(
                UsageEvent.make(
                    EventKind.BLOCKED,
                    persona_id=sender,
                    conversation_id=conversation_id,
                    reason_code=decision.reasons[0].code if decision.reasons else None,
                    text=text,
                    at=now,
                )
            )
            return self._receipt(decision, outcome="blocked")

        if decision.decision == DECISION_DEFER:
            queue_id = f"queue_{uuid.uuid4().hex}"
            self._queue[queue_id] = _QueueItem(
                queue_id=queue_id,
                room_id=conversation_id,
                sender_id=sender,
                text=text,
                deliver_after=self._parse_utc(decision.may_clear_at),
                reply_to=reply_to,
            )
            self._store.append(
                UsageEvent.make(
                    EventKind.DEFERRED,
                    persona_id=sender,
                    conversation_id=conversation_id,
                    reason_code=decision.reasons[0].code if decision.reasons else None,
                    text=text,
                    at=now,
                )
            )
            return self._receipt(decision, outcome="deferred", queue_id=queue_id)

        # allow：先投递再记账。投递失败不占名额（名额只记实际送达的消息）。
        try:
            event = self._transport.send_text(
                room_id=conversation_id,
                sender_id=sender,
                text=text,
                reply_to=reply_to,
            )
        except DeliveryError as exc:
            # 传输失败是 5xx 传输层错误，不是策略结论；不记 usage，不占名额。
            raise GatewayDeliveryError(exc.code, exc.detail) from exc

        self._store.append(
            UsageEvent.make(
                EventKind.SENT,
                persona_id=sender,
                conversation_id=conversation_id,
                text=text,
                at=now,
            )
        )

        return self._receipt(
            decision,
            outcome="delivered",
            message=self._message_payload(
                conversation_id=conversation_id,
                sender=sender,
                text=text,
                reply_to=reply_to,
                mentions=mentions,
                event_id=event.event_id,
                created_at=event.sent_at,
            ),
            delivery={
                "matrix_event_id": event.event_id,
                "delivered_at": iso_utc(event.sent_at),
            },
            usage_delta={
                "tokens_in": 0,  # 骨架不接模型，无真实 token 账单；保留字段对齐契约
                "tokens_out": decision.estimated_tokens,
                "total_tokens": decision.estimated_tokens,
                "town_tokens": 0,
            },
        )

    def _receipt(
        self,
        decision: PolicyDecision,
        *,
        outcome: str,
        queue_id: Optional[str] = None,
        message: Optional[Dict[str, Any]] = None,
        delivery: Optional[Dict[str, Any]] = None,
        usage_delta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        receipt: Dict[str, Any] = {
            "outcome": outcome,
            "retryable": False,
            "retry_after_seconds": decision.retry_after_seconds,
            "may_clear_at": decision.may_clear_at,
            "same_content_allowed": decision.same_content_allowed,
            "queue_id": queue_id,
            "message": message,
            "delivery": delivery,
            "decision": decision.to_dict(),
        }
        if usage_delta is not None:
            receipt["usage_delta"] = usage_delta
        return receipt

    @staticmethod
    def _message_payload(
        *,
        conversation_id: str,
        sender: str,
        text: str,
        reply_to: Optional[str],
        mentions: Optional[List[str]] = None,
        event_id: str,
        created_at: datetime,
    ) -> Dict[str, Any]:
        content: Dict[str, Any] = {"type": "text", "text": text}
        if mentions:
            content["mentions"] = list(mentions)
        msg: Dict[str, Any] = {
            "id": f"msg_{uuid.uuid4().hex}",
            "matrix_event_id": event_id,
            "conversation_id": conversation_id,
            "sender": {
                "id": sender,
                "kind": "ai",
                "display_name": sender,
                "persona_id": sender,
            },
            "content": content,
            "trust": "trusted",
            "provenance": {"origin": "ai", "external": False},
            "created_at": iso_utc(created_at),
            "redacted": False,
        }
        if reply_to:
            msg["reply_to"] = reply_to
        return msg

    @staticmethod
    def _fingerprint(conversation_id: str, payload: Dict[str, Any]) -> str:
        canonical = json.dumps(
            {"conversation_id": conversation_id, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _parse_utc(self, value: Optional[str]) -> datetime:
        if not value:
            return self._now()
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

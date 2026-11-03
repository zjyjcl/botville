# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""发言编排 + 三层限额 / 用量账单的查询与写入。

契约要点（gateway/docs/contract.md §1.4）：
- 策略拦截是 **HTTP 200 + outcome=blocked**，不是 429/403；
- `retryable` 在本结构里始终为 false；
- `deferred` 必须带 `queue_id`，表示服务端排程，客户端不要自行重试；
- 被拦截要记 `usage_events kind=blocked`。

请求校验（保持零依赖，手写最小 schema 校验，对齐 OpenAPI）：
- `OutgoingMessageRequest.additionalProperties: false` → 未知字段拒绝；
- `OutgoingContent.type` ∈ {text, markdown}（必填）；
- `reply_to` 必须是 string；`mentions` / `metadata` 原样透传（#92）；
- `PolicyEvaluateRequest.kind` ∈ {message, proactive, town_action}。

幂等：同一 `Idempotency-Key` + 相同请求体返回缓存回执；同 key 不同请求体
抛 `IdempotencyConflict`（HTTP 层映射 409 `idempotency.conflict`）。
`queued` / `deferred` 同样进缓存（契约 §1.5）。同 key 的**并发**请求靠一把
按 key 拆分的互斥锁串行化（`_idempotency_guard`），不同 key / 无 key 的请求
互不阻塞。

**锁外投递（本提交）**：`send_ai_message` 拆成三段——

1. 锁内：幂等查缓存 → 策略判断 → 预留名额（消息数 / token 预扣 / 最小间隔，
   见 `policy.ReservationBook`）；
2. 释放锁：调用 `transport.send_text`，慢的 Matrix 调用不再阻塞其它请求；
3. 重新进锁：成功则提交（写 `UsageEvent`、计费、清预留），失败则回滚预留
   （投递失败不计费、不占名额），然后抛出 `GatewayDeliveryError`。

**容量上限**（#92）：幂等缓存 / deferred 队列 / 用量事件日志都有界
（`MAX_IDEMPOTENCY_ENTRIES` / `MAX_QUEUE_ENTRIES` / `MAX_USAGE_EVENTS`），
超出后按 LRU（缓存/队列）或最旧优先（用量日志）淘汰，避免长时间运行的
进程被无限增长的请求拖垮。幂等缓存另有 TTL（`IDEMPOTENCY_TTL_SECONDS`）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .errors import GatewayDeliveryError, IdempotencyConflict, LimitError, ValidationError
from .limits import LAYER_PLATFORM, LimitStore, PLATFORM_SCOPE_ID
from .matrix import DeliveryError, MatrixTransport, SentEvent
from .policy import (
    CODE_SILENT_HOURS,
    DECISION_BLOCK,
    DECISION_DEFER,
    PolicyDecision,
    PolicyEngine,
    Reservation,
    ReservationBook,
)
from .store import EventKind, UsageEvent, UsageStore
from .timeutil import iso_utc, utcnow

CONTENT_TYPES = frozenset({"text", "markdown"})
EVALUATE_KINDS = frozenset({"message", "proactive", "town_action"})
USAGE_GROUP_BY = frozenset({"conversation", "day", "capability", "model"})
# 与 OpenAPI OutgoingMessageRequest / OutgoingContent 的 properties 对齐。
_SEND_FIELDS = frozenset(
    {"sender_persona_id", "content", "reply_to", "token_estimate", "allow_deferred", "metadata"}
)
_CONTENT_FIELDS = frozenset({"type", "text", "mentions"})
_EVALUATE_FIELDS = frozenset(
    {"persona_id", "conversation_id", "kind", "content", "token_estimate", "peer_id"}
)
# 契约 metadata：maxProperties 8，值必须是 string
_MAX_METADATA_PROPERTIES = 8


@dataclass
class _QueueItem:
    """deferred 排程。B1.1 只记录，不实现定时投递（留 TODO 给 B1.2/B2.x）。"""

    queue_id: str
    room_id: str
    sender_id: str
    text: str
    deliver_after: datetime
    reply_to: Optional[str] = None


@dataclass
class _IdempotencyEntry:
    fingerprint: str
    receipt: Dict[str, Any]
    created_at: datetime


class _KeyGuard:
    """同一 Idempotency-Key 的互斥闸门。

    只有「同 key」的请求会被串行化（第二个请求等到第一个写完缓存后直接命中），
    投递本身不持有 service 全局锁，因此不同 key / 无 key 的请求照常并行。
    `waiters` 用于在最后一个请求离开时回收闸门，避免字典无界增长。
    """

    __slots__ = ("lock", "waiters")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiters = 0


@dataclass(frozen=True)
class _PreparedSend:
    """锁内判断的产物：要么已是终态回执（blocked/deferred），要么带着预留去投递。"""

    decision: PolicyDecision
    receipt: Optional[Dict[str, Any]] = None
    reservation: Optional[Reservation] = None
    tokens_out: int = 0

    @property
    def needs_delivery(self) -> bool:
        return self.reservation is not None


class GatewayService:
    def __init__(
        self,
        config: Config,
        transport: MatrixTransport,
        store: Optional[UsageStore] = None,
        limit_store: Optional[LimitStore] = None,
        *,
        now_fn=utcnow,
        reservations: Optional[ReservationBook] = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._store = store if store is not None else UsageStore(config.max_usage_events)
        self._limits = (
            limit_store if limit_store is not None else LimitStore(config)
        )
        self._reservations = (
            reservations if reservations is not None else ReservationBook()
        )
        self._engine = PolicyEngine(
            config,
            self._store,
            self._limits,
            now_fn=now_fn,
            reservations=self._reservations,
        )
        self._now = now_fn
        self._lock = threading.RLock()
        self._idempotency: "OrderedDict[str, _IdempotencyEntry]" = OrderedDict()
        self._idempotency_guards: Dict[str, _KeyGuard] = {}
        self._queue: "OrderedDict[str, _QueueItem]" = OrderedDict()
        self._started_at = now_fn()

    # ------------------------------------------------------------- accessors
    @property
    def store(self) -> UsageStore:
        return self._store

    @property
    def limits(self) -> LimitStore:
        return self._limits

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def pending_reservations(self) -> int:
        """当前在途（已预留、尚未提交/回滚）的名额数。"""
        return len(self._reservations)

    @property
    def now(self) -> datetime:
        """当前时钟值（供 /health uptime 计算）。"""
        return self._now()

    def pending_queue(self) -> List[_QueueItem]:
        with self._lock:
            return list(self._queue.values())

    # --------------------------------------------------------------- limits
    def get_limits(
        self,
        *,
        persona_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        peer_id: Optional[str] = None,
        include_platform: bool = True,
    ) -> Dict[str, Any]:
        for name, value in (
            ("persona_id", persona_id),
            ("conversation_id", conversation_id),
            ("peer_id", peer_id),
        ):
            if value is not None and not isinstance(value, str):
                raise ValidationError("request.invalid", f"'{name}' must be a string", name)
        # 契约里三个 query 参数都是可选的：都不给时只看平台底线层。
        effective = self._limits.effective(
            persona_id=persona_id,
            conversation_id=conversation_id,
            peer_id=peer_id,
            now=self._now(),
            include_platform=include_platform,
        )
        return effective.to_dict()

    def put_limits(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        layer = self._limits.put(payload, now=self._now())
        # 返回重新计算后的生效限额：以写入的 scope 为准。
        effective = self._limits.effective(
            persona_id=layer.scope_id if layer.scope == "persona" else None,
            conversation_id=layer.scope_id if layer.scope == "conversation" else None,
            peer_id=layer.scope_id if layer.scope == "peer" else None,
            now=self._now(),
        )
        return effective.to_dict()

    # ---------------------------------------------------------------- usage
    def get_usage(
        self,
        *,
        persona_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        from_: Optional[str] = None,
        to: Optional[str] = None,
        timezone_name: Optional[str] = None,
        group_by: str = "conversation",
    ) -> Dict[str, Any]:
        if group_by not in USAGE_GROUP_BY:
            raise ValidationError(
                "request.invalid",
                f"'group_by' must be one of {sorted(USAGE_GROUP_BY)}, got {group_by!r}",
                "group_by",
            )
        now = self._now()
        start = self._parse_query_time(from_, "from") or (now - timedelta(hours=24))
        end = self._parse_query_time(to, "to") or now
        if end < start:
            raise ValidationError("request.invalid", "'to' must not precede 'from'", "to")
        tz = _resolve_timezone(timezone_name)

        events = self._store.events_in_window(
            persona_id=persona_id,
            conversation_id=conversation_id,
            since=start,
            until=end,
        )
        counts: Dict[str, int] = {}
        for event in events:
            counts[event.contract_kind] = counts.get(event.contract_kind, 0) + 1
        totals = _totals(events)

        budget = self._budget_for(persona_id, conversation_id, now)

        buckets: Dict[str, Dict[str, int]] = {}
        for event in events:
            key = _bucket_key(event, group_by, tz)
            bucket = buckets.setdefault(
                key, {"messages_sent": 0, "tokens_total": 0, "town_tokens": 0}
            )
            if event.kind in (EventKind.SENT, EventKind.PROACTIVE):
                bucket["messages_sent"] += 1
            bucket["tokens_total"] += event.tokens_in + event.tokens_out
            bucket["town_tokens"] += event.town_tokens

        body: Dict[str, Any] = {
            "from": iso_utc(start),
            "to": iso_utc(end),
            "timezone": tz.key,
            "totals": totals,
            "budget": budget,
            "breakdown": [
                {"key": key, **value} for key, value in sorted(buckets.items())
            ],
        }
        if persona_id:
            body["persona_id"] = persona_id
        if conversation_id:
            body["conversation_id"] = conversation_id
        return body

    def list_usage_events(
        self,
        *,
        persona_id: Optional[str] = None,
        kind: Optional[str] = None,
        from_: Optional[str] = None,
        to: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """`GET /usage/events`：按时间倒序分页，不返回消息正文（#92）。"""
        if limit < 1 or limit > 100:
            raise ValidationError("request.invalid", "'limit' must be 1..100", "limit")
        now = self._now()
        start = self._parse_query_time(from_, "from") or (now - timedelta(days=30))
        end = self._parse_query_time(to, "to") or now
        if end < start:
            raise ValidationError("request.invalid", "'to' must not precede 'from'", "to")

        wanted_kinds: Optional[set] = None
        if kind:
            wanted_kinds = _contract_kinds_for(kind)

        events = [
            e
            for e in self._store.events_in_window(
                persona_id=persona_id, since=start, until=end
            )
            if wanted_kinds is None or e.contract_kind in wanted_kinds
        ]
        events.sort(key=lambda e: e.recorded_at, reverse=True)

        offset = 0
        if cursor:
            offset = _decode_cursor(cursor)
        page = events[offset : offset + limit]
        has_more = offset + limit < len(events)
        return {
            "items": [_usage_event_body(e) for e in page],
            "next_cursor": _encode_cursor(offset + limit) if has_more else None,
            "has_more": has_more,
        }

    def record_town_usage(
        self, *, persona_id: str, tokens: int, at: Optional[datetime] = None
    ) -> None:
        """小镇行为的 token 入账（B4.2 接入；此处只提供记账入口）。"""
        if tokens <= 0:
            return
        self._store.append(
            UsageEvent.make(
                EventKind.TOWN_ACTION,
                persona_id=persona_id,
                at=at or self._now(),
                town_tokens=tokens,
            )
        )

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
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise ValidationError(
                "request.invalid", "'conversation_id' must be a string", "conversation_id"
            )
        peer_id = payload.get("peer_id")
        if peer_id is not None and not isinstance(peer_id, str):
            raise ValidationError("request.invalid", "'peer_id' must be a string", "peer_id")
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
            conversation_id=conversation_id,
            peer_id=peer_id,
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

        validated = self._validate_send(payload)
        fingerprint = self._fingerprint(conversation_id, payload)

        # 同 key 的并发请求在这里排队（只按 key 串行，不影响其它请求）。
        guard = self._enter_idempotency_guard(idempotency_key)
        try:
            # ---- 第一段：锁内策略判断 + 预留名额 -------------------------
            with self._lock:
                if idempotency_key:
                    entry = self._idempotency_get(idempotency_key)
                    if entry is not None:
                        if entry.fingerprint != fingerprint:
                            raise IdempotencyConflict(idempotency_key)
                        return json.loads(json.dumps(entry.receipt))  # 深拷贝，调用方改不坏缓存

                prepared = self._prepare_send(
                    conversation_id=conversation_id, **validated
                )
                if not prepared.needs_delivery:
                    receipt = prepared.receipt
                    assert receipt is not None
                    if idempotency_key:
                        self._idempotency_put(idempotency_key, fingerprint, receipt)
                    return json.loads(json.dumps(receipt))

                reservation = prepared.reservation
                assert reservation is not None
                decision = prepared.decision

            # ---- 第二段：锁外投递（慢的 Matrix 调用不阻塞其它请求） ------
            try:
                event = self._transport.send_text(
                    room_id=conversation_id,
                    sender_id=validated["sender"],
                    text=validated["text"],
                    reply_to=validated["reply_to"],
                    content_type=validated["content_type"],
                )
            except DeliveryError as exc:
                # 投递失败：回滚预留，不记账、不占名额；再映射为 5xx 传输错误。
                self._rollback(reservation)
                raise GatewayDeliveryError(exc.code, exc.detail) from exc
            except BaseException:
                # 任何意外异常都不能把名额永久泄漏在账上。
                self._rollback(reservation)
                raise

            # ---- 第三段：重新进锁提交（写 usage、计费、清预留） ----------
            with self._lock:
                self._reservations.release(reservation)
                receipt = self._commit_send(
                    decision=decision,
                    reservation=reservation,
                    event=event,
                    conversation_id=conversation_id,
                    sender=validated["sender"],
                    text=validated["text"],
                    content_type=validated["content_type"],
                    reply_to=validated["reply_to"],
                    mentions=validated["mentions"],
                    metadata=validated["metadata"],
                )
                if idempotency_key:
                    self._idempotency_put(idempotency_key, fingerprint, receipt)
                return json.loads(json.dumps(receipt))
        finally:
            self._leave_idempotency_guard(idempotency_key, guard)

    # ---------------------------------------------------- idempotency guards
    def _enter_idempotency_guard(self, key: Optional[str]) -> Optional[_KeyGuard]:
        """按 key 取互斥闸门并加锁；无 key 时返回 None（不做串行化）。"""
        if not key:
            return None
        with self._lock:
            guard = self._idempotency_guards.get(key)
            if guard is None:
                guard = _KeyGuard()
                self._idempotency_guards[key] = guard
            guard.waiters += 1
        try:
            guard.lock.acquire()
        except BaseException:
            with self._lock:
                self._discard_guard_locked(key, guard)
            raise
        return guard

    def _leave_idempotency_guard(
        self, key: Optional[str], guard: Optional[_KeyGuard]
    ) -> None:
        if guard is None or key is None:
            return
        try:
            guard.lock.release()
        finally:
            with self._lock:
                self._discard_guard_locked(key, guard)

    def _discard_guard_locked(self, key: str, guard: _KeyGuard) -> None:
        """调用方须持有 `self._lock`；最后一个等待者离开时回收闸门。"""
        guard.waiters -= 1
        if guard.waiters <= 0 and self._idempotency_guards.get(key) is guard:
            self._idempotency_guards.pop(key, None)

    # ------------------------------------------------------------ internals
    def _validate_send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
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
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ValidationError(
                    "request.invalid", "'metadata' must be an object", "metadata"
                )
            if len(metadata) > _MAX_METADATA_PROPERTIES:
                raise ValidationError(
                    "request.invalid",
                    f"'metadata' must have at most {_MAX_METADATA_PROPERTIES} properties",
                    "metadata",
                )
            if not all(isinstance(v, str) for v in metadata.values()):
                raise ValidationError(
                    "request.invalid", "'metadata' values must be strings", "metadata"
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

        return {
            "sender": sender,
            "text": text,
            "content_type": content_type,
            "reply_to": reply_to,
            "mentions": list(mentions) if mentions else None,
            "metadata": dict(metadata) if metadata else None,
            "token_estimate": token_estimate,
            "allow_deferred": allow_deferred,
        }

    def _prepare_send(
        self,
        *,
        conversation_id: str,
        sender: str,
        text: str,
        content_type: str,
        reply_to: Optional[str],
        mentions: Optional[List[str]],
        metadata: Optional[Dict[str, str]],
        token_estimate: Optional[int],
        allow_deferred: bool,
    ) -> _PreparedSend:
        """锁内阶段：策略判断 + 预留名额。调用方必须已持有 `self._lock`。

        - block / defer 在这里直接产出终态回执（不需要投递，也就没有预留）；
        - allow 只**预留**名额并返回，真正的 `send_text` 必须在锁外调用，
          否则一条慢投递会把所有请求挡在锁后面。
        """
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
            return _PreparedSend(decision, receipt=self._receipt(decision, outcome="blocked"))

        if decision.decision == DECISION_DEFER:
            queue_id = f"queue_{uuid.uuid4().hex}"
            self._queue_put(
                queue_id,
                _QueueItem(
                    queue_id=queue_id,
                    room_id=conversation_id,
                    sender_id=sender,
                    text=text,
                    deliver_after=self._parse_utc(decision.may_clear_at),
                    reply_to=reply_to,
                ),
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
            return _PreparedSend(
                decision,
                receipt=self._receipt(decision, outcome="deferred", queue_id=queue_id),
            )

        # allow：先预留名额（消息数 / token 预扣 / 最小间隔），投递完成后再提交。
        reservation = self._reservations.reserve(
            persona_id=sender, tokens=decision.estimated_tokens, at=now
        )
        return _PreparedSend(
            decision, reservation=reservation, tokens_out=decision.estimated_tokens
        )

    def _rollback(self, reservation: Reservation) -> None:
        """锁外投递失败：丢弃预留（不写 usage、不占名额）。"""
        with self._lock:
            self._reservations.release(reservation)

    def _commit_send(
        self,
        *,
        decision: PolicyDecision,
        reservation: Reservation,
        event: SentEvent,
        conversation_id: str,
        sender: str,
        text: str,
        content_type: str,
        reply_to: Optional[str],
        mentions: Optional[List[str]],
        metadata: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """锁内提交：调用方须持锁，且已释放（或即将释放）该预留。

        以预留时刻 `reservation.at` 作为 `recorded_at`：策略判断发生在那一刻，
        投递耗时不应改变这条消息落在哪个滑动窗口里。
        """
        tokens_out = decision.estimated_tokens
        self._store.append(
            UsageEvent.make(
                EventKind.SENT,
                persona_id=sender,
                conversation_id=conversation_id,
                text=text,
                at=reservation.at,
                tokens_out=tokens_out,
            )
        )

        return self._receipt(
            decision,
            outcome="delivered",
            message=self._message_payload(
                conversation_id=conversation_id,
                sender=sender,
                text=text,
                content_type=content_type,
                reply_to=reply_to,
                mentions=mentions,
                metadata=metadata,
                event_id=event.event_id,
                created_at=event.sent_at,
            ),
            delivery={
                "matrix_event_id": event.event_id,
                "delivered_at": iso_utc(event.sent_at),
            },
            usage_delta={
                "tokens_in": 0,  # 骨架不接模型，无真实 token 账单；保留字段对齐契约
                "tokens_out": tokens_out,
                "total_tokens": tokens_out,
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
        content_type: str = "text",
        reply_to: Optional[str] = None,
        mentions: Optional[List[str]] = None,
        metadata: Optional[Dict[str, str]] = None,
        event_id: str,
        created_at: datetime,
    ) -> Dict[str, Any]:
        # 原样保留请求的 content.type：markdown 不得降级为 text（#92）。
        content: Dict[str, Any] = {"type": content_type, "text": text}
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
        if metadata:
            # 透传元数据（#92）：不进正文，只挂在 Message 上供调用方对账。
            msg["metadata"] = dict(metadata)
        return msg

    # ------------------------------------------------- bounds / idempotency
    def _idempotency_get(self, key: str) -> Optional[_IdempotencyEntry]:
        entry = self._idempotency.get(key)
        if entry is None:
            return None
        ttl = self._config.idempotency_ttl_seconds
        if ttl > 0 and (self._now() - entry.created_at).total_seconds() > ttl:
            # 过期即失效：删除后按 miss 处理（与「服务端建议保留 24 小时」一致）。
            self._idempotency.pop(key, None)
            return None
        self._idempotency.move_to_end(key)
        return entry

    def _idempotency_put(self, key: str, fingerprint: str, receipt: Dict[str, Any]) -> None:
        self._idempotency[key] = _IdempotencyEntry(
            fingerprint=fingerprint, receipt=receipt, created_at=self._now()
        )
        self._idempotency.move_to_end(key)
        while len(self._idempotency) > self._config.max_idempotency_entries:
            self._idempotency.popitem(last=False)  # 淘汰最久未使用

    def _queue_put(self, queue_id: str, item: _QueueItem) -> None:
        self._queue[queue_id] = item
        while len(self._queue) > self._config.max_queue_entries:
            self._queue.popitem(last=False)

    # -------------------------------------------------------------- budget
    def _budget_for(
        self, persona_id: Optional[str], conversation_id: Optional[str], now: datetime
    ) -> Dict[str, Any]:
        """`UsageSummary.budget`：生效 token 限额与窗口内已用量。"""
        if persona_id:
            effective = self._limits.effective(
                persona_id=persona_id, conversation_id=conversation_id, now=now
            )
        else:
            effective = self._limits.effective(conversation_id=conversation_id, now=now)

        token = effective.get("tokens_per_window")
        window = effective.get("window_seconds")
        window_seconds = int(window.value) if window is not None else self._config.rate_limit_window_seconds
        since = now - timedelta(seconds=window_seconds)
        events = self._store.events_in_window(
            persona_id=persona_id, conversation_id=conversation_id, since=since, until=now
        )
        used = sum(e.tokens_in + e.tokens_out for e in events)

        if token is None:
            return {
                "limit": None,
                "used": used,
                "remaining": None,
                "reset_at": None,
                "source_layer": None,
            }
        # reset_at：最早一条事件的过期时刻（窗口滑出）；无事件时为窗口结束时刻。
        reset_at = (
            min((e.recorded_at for e in events)) + timedelta(seconds=window_seconds)
            if events
            else now + timedelta(seconds=window_seconds)
        )
        return {
            "limit": token.value,
            "used": used,
            "remaining": max(0, token.value - used),
            "reset_at": iso_utc(reset_at),
            "source_layer": token.source_layer,
        }

    # ------------------------------------------------------------- helpers
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

    @staticmethod
    def _parse_query_time(value: Optional[str], field: str) -> Optional[datetime]:
        if value is None or value == "":
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(
                "request.invalid", f"'{field}' must be an RFC 3339 date-time", field
            ) from exc
        if parsed.tzinfo is None:
            raise ValidationError(
                "request.invalid", f"'{field}' must include a timezone (UTC Z)", field
            )
        return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# usage helpers
# --------------------------------------------------------------------------- #
def _resolve_timezone(name: Optional[str]) -> ZoneInfoLite:
    return ZoneInfoLite(name or "UTC")


class ZoneInfoLite:
    """包装 `ZoneInfo`，保留原始名称（契约 `UsageSummary.timezone` 回显询问值）。"""

    __slots__ = ("key", "_zone")

    def __init__(self, key: str) -> None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            self._zone = ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError(
                "request.invalid", f"unknown timezone {key!r}", "timezone"
            ) from exc
        self.key = key

    def local_day(self, moment: datetime) -> str:
        return moment.astimezone(self._zone).strftime("%Y-%m-%d")


def _totals(events: List[UsageEvent]) -> Dict[str, int]:
    totals = {
        "messages_sent": 0,
        "messages_blocked": 0,
        "messages_deferred": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_total": 0,
        "proactive_messages": 0,
        "model_calls": 0,
        "town_tokens": 0,
    }
    for event in events:
        if event.kind in (EventKind.SENT, EventKind.PROACTIVE):
            totals["messages_sent"] += 1
        if event.kind == EventKind.BLOCKED:
            totals["messages_blocked"] += 1
        if event.kind == EventKind.DEFERRED:
            totals["messages_deferred"] += 1
        if event.kind == EventKind.PROACTIVE:
            totals["proactive_messages"] += 1
        totals["tokens_in"] += event.tokens_in
        totals["tokens_out"] += event.tokens_out
        totals["tokens_total"] += event.tokens_in + event.tokens_out
        totals["town_tokens"] += event.town_tokens
    return totals


def _bucket_key(event: UsageEvent, group_by: str, tz: ZoneInfoLite) -> str:
    if group_by == "day":
        return tz.local_day(event.recorded_at)
    if group_by == "capability":
        # 当前 UsageEvent 不带 capability_id；统一归到 "gateway" 桶（B4 补来源字段）。
        return "gateway"
    if group_by == "model":
        return "unknown"
    return event.conversation_id or "(none)"


def _usage_event_body(event: UsageEvent) -> Dict[str, Any]:
    """契约 `UsageEvent`（`GET /usage/events` 的单条）。

    **不含消息正文**（#92）：只给长度与摘要，避免审计日志成为内容泄漏面。
    """
    body: Dict[str, Any] = {
        "id": event.id,
        "occurred_at": iso_utc(event.recorded_at),
        "kind": event.contract_kind,
        "persona_id": event.persona_id,
    }
    if event.conversation_id:
        body["conversation_id"] = event.conversation_id
    if event.reason_code:
        body["decision_code"] = event.reason_code
    if event.tokens_in:
        body["tokens_in"] = event.tokens_in
    if event.tokens_out:
        body["tokens_out"] = event.tokens_out
    if event.town_tokens:
        body["town_tokens"] = event.town_tokens
    if event.text_length:
        body["text_length"] = event.text_length
    if event.text_sha256:
        body["text_sha256"] = event.text_sha256
    return body


_CONTRACT_KIND_GROUPS: Dict[str, set] = {
    "message_sent": {"message_sent"},
    "blocked": {"blocked"},
    "message_blocked": {"blocked"},  # 0.1.0 旧别名
    "message_deferred": {"message_deferred"},
    "proactive_message": {"proactive_message"},
    "town_action": {"town_action"},
    "model_call": {"model_call"},
    "image_generation": {"image_generation"},
    "card_action": {"card_action"},
}


def _contract_kinds_for(kind: str) -> set:
    if kind not in _CONTRACT_KIND_GROUPS:
        raise ValidationError(
            "request.invalid", f"unknown usage event kind {kind!r}", "kind"
        )
    return _CONTRACT_KIND_GROUPS[kind]


def _encode_cursor(offset: int) -> str:
    return f"off_{offset}"


def _decode_cursor(cursor: str) -> int:
    if not cursor.startswith("off_"):
        raise ValidationError("request.invalid", "invalid cursor", "cursor")
    try:
        offset = int(cursor[4:])
    except ValueError as exc:
        raise ValidationError("request.invalid", "invalid cursor", "cursor") from exc
    if offset < 0:
        raise ValidationError("request.invalid", "invalid cursor", "cursor")
    return offset

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
import hmac
import json
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .errors import GatewayDeliveryError, IdempotencyConflict, LimitError, PeerValidationError, ValidationError
from .limits import LAYER_PLATFORM, LimitStore, PLATFORM_SCOPE_ID
from .matrix import DeliveryError, MatrixTransport, SentEvent
from .policy import (
    CODE_AI_LOOP_TRIPPED,
    CODE_ANTI_SPAM_CONSECUTIVE,
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
    {
        "sender_persona_id",
        "content",
        "reply_to",
        "token_estimate",
        "allow_deferred",
        "metadata",
        "peer_id",
    }
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
        self._store = store if store is not None else UsageStore(
            config.max_usage_events,
            config.usage_retention_seconds,
            secret=config.hmac_secret,
        )
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
        # #114 PR-a：txnId → event_id 的去重账本。幂等缓存被容量/TTL 淘汰后同 key
        # 重试会重新投递，HS 按 txnId 返回**同一个** event_id；据此不再重复记
        # SENT 用量（淘汰后重复计费）。容量取 max_idempotency_entries 的 2 倍，TTL 取
        # idempotency_ttl_seconds 的 2 倍（必须比幂等缓存活得久，否则淘汰后重试
        # 恰好落在账本过期窗口里仍会重复计费）。仅无 key（随机 txnId）时不写入。
        self._txn_events: "OrderedDict[str, Tuple[str, datetime]]" = OrderedDict()
        self._idempotency_guards: Dict[str, _KeyGuard] = {}
        self._queue: "OrderedDict[str, _QueueItem]" = OrderedDict()
        self._started_at = now_fn()
        # B2.2 peer_id 校验：最小内存会话注册表（内部 API，测试用）。
        self._conversation_registry: Dict[str, List[str]] = {}

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

    def register_conversation(self, conversation_id: str, persona_ids: List[str]) -> None:
        """注册会话成员列表（内部 API，测试用；不加 HTTP 端点）。

        用于 peer_id 校验：evaluate_policy / send_ai_message 时检查
        peer_id ∈ persona_ids 且 ≠ sender。
        """
        with self._lock:
            self._conversation_registry[conversation_id] = list(persona_ids)

    def record_human_message(
        self, conversation_id: str, at: Optional[datetime] = None
    ) -> None:
        """记录人类消息到达事件（内部 API，不加 HTTP 端点）。

        写 UsageEvent kind=HUMAN，用于 anti_spam / ai_loop 计数重置。
        同时提前解除该会话 persona 的 anti_spam 冷却（通过 EventKind.HUMAN 事件的存在，
        policy engine 在 _anti_spam_view 中检查 human_at 来决定计数起点）。
        """
        now = at or self._now()
        self._store.append(
            UsageEvent.make(
                EventKind.HUMAN,
                persona_id="__human__",
                conversation_id=conversation_id,
                at=now,
                secret=self._store.secret,
            )
        )
        # B6 (#117)：人类消息重置该会话的 blocked 去重记录（全部 persona）。
        self._engine.clear_blocked_records(None, conversation_id)

    def _check_peer_id(
        self,
        *,
        conversation_id: Optional[str],
        sender: str,
        peer_id: Optional[str],
    ) -> None:
        """peer_id 校验（B2.2 §6.4，审查 #117 B1 后退回 B2.1 兼容语义）：

        - 会话未注册（或 evaluate 无 conversation_id）：peer_id 原样采信，
          **不校验成员**；带 peer_id 时 peer 层生效并写入 peer 计数（B2.1 行为）。
        - 会话已注册 + 带 peer_id：peer_id ∈ 成员且 ≠ sender，否则 422。
        - 会话已注册 + 无 peer_id：若**对方**（排除 sender）有 peer 层规则 → 422。

        会话成员的真实来源（HTTP 注册 / 房间成员同步）尚未接入；首版只有内部
        API `register_conversation`，因此未注册会话按 B2.1 采信 peer_id。
        """
        registered = (
            self._conversation_registry.get(conversation_id)
            if conversation_id is not None
            else None
        )
        if registered is None:
            return
        if peer_id is not None:
            if peer_id not in registered or peer_id == sender:
                raise PeerValidationError(
                    f"peer_id {peer_id!r} must be a conversation member and differ from sender {sender!r}"
                )
        else:
            # 无 peer_id：仅当「对方」（排除 sender）有 peer 层规则时强制 422。
            others = [p for p in registered if p != sender]
            if self._limits.any_peer_rules(others):
                raise PeerValidationError(
                    "peer_id is required when a conversation peer has peer-layer rules configured"
                )

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

        events = [
            e
            for e in self._store.events_in_window(
                persona_id=persona_id,
                conversation_id=conversation_id,
                since=start,
                until=end,
            )
            if e.contract_kind  # filter out HUMAN events
        ]
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
            if e.contract_kind  # EventKind.HUMAN has empty contract_kind → filter out
            and (wanted_kinds is None or e.contract_kind in wanted_kinds)
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
                secret=self._store.secret,
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
        if peer_id == "":
            # 与 send 路径一致：空串不是合法 peer 标识 → 400 request.invalid（#117）。
            raise ValidationError("request.invalid", "'peer_id' must not be empty", "peer_id")
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
        self._check_peer_id(
            conversation_id=conversation_id,
            sender=persona_id,
            peer_id=peer_id,
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
                    txn_id=self._txn_id_for(idempotency_key),
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
                    peer_id=validated["peer_id"],
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

        peer_id = payload.get("peer_id")
        if peer_id is not None and not isinstance(peer_id, str):
            raise ValidationError("request.invalid", "'peer_id' must be a string", "peer_id")
        if peer_id == "":
            # 空串不是合法的 peer 标识：send 与 evaluate 两端统一按 400 拒绝（#117 2.10）。
            raise ValidationError("request.invalid", "'peer_id' must not be empty", "peer_id")

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
            "peer_id": peer_id,
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
        peer_id: Optional[str] = None,
    ) -> _PreparedSend:
        """锁内阶段：策略判断 + 预留名额。调用方必须已持有 `self._lock`。

        - block / defer 在这里直接产出终态回执（不需要投递，也就没有预留）；
        - allow 只**预留**名额并返回，真正的 `send_text` 必须在锁外调用，
          否则一条慢投递会把所有请求挡在锁后面。

        `peer_id` 必须传进 `evaluate()`（B2）：否则 peer 层的 messages/tokens
        限额只在预检里生效，实发会把三层退化成两层。
        """
        self._check_peer_id(
            conversation_id=conversation_id,
            sender=sender,
            peer_id=peer_id,
        )
        now = self._now()
        decision = self._engine.evaluate(
            persona_id=sender,
            conversation_id=conversation_id,
            peer_id=peer_id,
            text=text,
            token_estimate=token_estimate,
            allow_deferred=allow_deferred,
            now=now,
            commit_state=True,  # 实发路径：冷却到期清零等状态变更落库（#117 N4）
        )

        if decision.decision == DECISION_BLOCK:
            # B3/B5 (#117)：冷却锚点独立于事件表；由实发路径写入，不依赖 reasons 顺序。
            anti_spam_consecutive = any(
                check.key == CODE_ANTI_SPAM_CONSECUTIVE and not check.allowed
                for check in decision.checks
            )
            if anti_spam_consecutive:
                self._engine.note_anti_spam_trip(sender, conversation_id, now)
            reason_code = self._primary_reason_code(decision)
            # B6 (#117)：**所有** blocked 码都按 (persona, conv, reason_code) 去重，
            # 只有冷却期一条码去重时，其它码（message.length / ai_loop.tripped /
            # messages.window.exceeded …）的写放大仍能把 SENT 计数事件挤出容量上限，
            # 从而重置 rate/token/anti_spam/ai_loop 计数。不写事件时仍返回 blocked 回执。
            if not self._engine.should_record_blocked(
                sender, conversation_id, reason_code
            ):
                return _PreparedSend(
                    decision, receipt=self._receipt(decision, outcome="blocked")
                )
            self._store.append(
                UsageEvent.make(
                    EventKind.BLOCKED,
                    persona_id=sender,
                    conversation_id=conversation_id,
                    reason_code=reason_code,
                    text=text,
                    at=now,
                    peer_id=peer_id,
                    secret=self._store.secret,
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
                    reason_code=self._primary_reason_code(decision),
                    text=text,
                    at=now,
                    peer_id=peer_id,
                    secret=self._store.secret,
                )
            )
            return _PreparedSend(
                decision,
                receipt=self._receipt(decision, outcome="deferred", queue_id=queue_id),
            )

        # allow：先预留名额（消息数 / token 预扣 / 最小间隔），投递完成后再提交。
        reservation = self._reservations.reserve(
            persona_id=sender,
            tokens=decision.estimated_tokens,
            at=now,
            conversation_id=conversation_id,
            peer_id=peer_id,
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
        peer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """锁内提交：调用方须持锁，且已释放（或即将释放）该预留。

        以预留时刻 `reservation.at` 作为 `recorded_at`：策略判断发生在那一刻，
        投递耗时不应改变这条消息落在哪个滑动窗口里。

        #114 PR-a 淘汰后去重计费：幂等缓存被淘汰后同 key 重试，HS 按 txnId 返回
        同一个 event_id。此处按 txn_id 记账，若本次 event_id 与已记录的一致，
        说明是同一次投递的重放——不再 append SENT 事件、不重复扣 token，回执照常
        返回（`delivery.matrix_event_id` 即上次的同一事件；不新增字段）。
        """
        tokens_out = decision.estimated_tokens
        txn_key = event.txn_id
        deduped = False
        if txn_key:
            recorded = self._txn_events_get(txn_key)
            if recorded is not None and recorded == event.event_id:
                deduped = True
            else:
                self._txn_events_put(txn_key, event.event_id)

        if not deduped:
            self._store.append(
                UsageEvent.make(
                    EventKind.SENT,
                    persona_id=sender,
                    conversation_id=conversation_id,
                    text=text,
                    at=reservation.at,
                    tokens_out=tokens_out,
                    peer_id=peer_id,
                    secret=self._store.secret,
                )
            )
        # B6 (#117)：delivered 后清 blocked 去重记录，下次进入 blocked 状态可重新写一条。
        self._engine.clear_blocked_records(sender, conversation_id)

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
                # 淘汰后同 key 重试时这里就是 HS 上次返回的同一个 event_id（不重复计费），
                # 契约字段集合不变。
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
        # 窗口与 token 计数成对（B1）：用 token 速率对胜出层的窗口，而不是
        # 单独取过 min 的 `window_seconds`。
        window = effective.get("tokens.window_seconds")
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
    def _primary_reason_code(decision: PolicyDecision) -> Optional[str]:
        """blocked/deferred 事件的 reason_code（#117 B5）。

        优先取 anti_spam.* / ai_loop.* 的失败 check 码：这些检查的锚点/选码语义
        不能被其它同时失败的检查（rate/token/interval/silent）挤掉。否则退回
        `reasons[0].code`。不依赖 reasons 的顺序。
        """
        for check in decision.checks:
            if check.allowed or not check.code:
                continue
            if check.code.startswith("anti_spam.") or check.code.startswith("ai_loop."):
                return check.code
        return decision.reasons[0].code if decision.reasons else None

    def _txn_id_for(self, idempotency_key: Optional[str]) -> str:
        """Matrix 事务 ID：由幂等键 + 部署密钥 HMAC 稳定推导（#99 / #114 PR-a）。

        同一 `Idempotency-Key` 的重试必须打到同一个 Matrix 事务上，HS 才能按
        `(room, sender, txnId)` 去重；每次随机 uuid4 会让重试变成两条消息。
        无 key 时仍用 uuid4（本来就没有重试语义）。

        #114 PR-a：改 HMAC-SHA256(部署密钥, b"txn\\x00" + key)。裸 `sha256(key)`
        在 HS / 反代日志里可见，key 若是可猜的短串即可被字典还原、进而跨部署
        对账同一 key；带密钥的 HMAC 让外部无法反推。`\\x00` 域分隔前缀把 txnId
        与 `text_sha256`（`text\\x00` 前缀）隔开，两者不会同构。

        Matrix txnId 允许 `[A-Za-z0-9._=-]`；`hexdigest()[:64]` 正好 64 个
        `[0-9a-f]`，无需转义。
        """
        if not idempotency_key:
            return uuid.uuid4().hex
        secret = self._config.hmac_secret
        assert secret is not None, "Config guarantees hmac_secret is set"
        return hmac.new(secret, b"txn\x00" + idempotency_key.encode("utf-8"), hashlib.sha256).hexdigest()[:64]

    # ------------------------------------------- txnId → event_id dedupe book
    def _txn_events_get(self, txn_id: str) -> Optional[str]:
        """查该 txnId 上次投递的 event_id；过 TTL 即失效。调用方须持锁。"""
        entry = self._txn_events.get(txn_id)
        if entry is None:
            return None
        event_id, created_at = entry
        # TTL 取幂等缓存的两倍：账本必须比缓存活得久，否则缓存淘汰后的重试
        # 会落在账本过期窗口里、重复计费。
        ttl = self._config.idempotency_ttl_seconds * 2
        if ttl > 0 and (self._now() - created_at).total_seconds() > ttl:
            self._txn_events.pop(txn_id, None)
            return None
        self._txn_events.move_to_end(txn_id)
        return event_id

    def _txn_events_put(self, txn_id: str, event_id: str) -> None:
        """记录 txnId → event_id（容量复用幂等缓存上限，LRU 淘汰）。调用方须持锁。"""
        self._txn_events[txn_id] = (event_id, self._now())
        self._txn_events.move_to_end(txn_id)
        # 容量取幂等缓存上限的 2 倍：账本必须比缓存保留更多 key，否则缓存淘汰的
        # 那个 key 往往也已被账本淘汰，去重形同虚设。
        while len(self._txn_events) > 2 * max(1, self._config.max_idempotency_entries):
            self._txn_events.popitem(last=False)

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
    if event.peer_id:
        body["peer_id"] = event.peer_id
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

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""发言策略引擎：三层限额（platform / owner / peer）+ 深夜静默 + 最小发言间隔。

三层限额逐项取最严（B2.1）。评估从 `LimitStore` 取 `EffectiveLimits`，对
消息数窗口、token 窗口、最小发言间隔、静默时段分别检查，每项生成一个
`PolicyCheck`；`winner_layer` 记录导致 block/defer 的那一项的 `source_layer`。

**限速算法：滑动窗口（半开区间）**（#92）。
  窗口 = `[now - window_seconds, now)` 内该 persona 的 `kind=sent` 事件数。
  半开区间保证同一时刻（精确到秒）不会既算进上一个窗口又算进下一个窗口。
  `reset_at` 取**第 `used − limit + 1` 早的**事件的过期时刻（从 0 起数），
  即「足够让一个名额腾出来」的事件离开窗口的时刻；`used == limit` 时就是最早
  那条。时刻**向上取整到秒**（#92）：23:59:59.5 的释放时刻报 00:00:00，而不是
  23:59:59——否则调用方在下一整秒重试仍会被拦，形成差一秒的活锁。

**token 预算**：窗口内 `tokens_in + tokens_out` 之和 + 本次 `token_estimate`
（预扣）不得超过 `tokens_per_window`。实际入账由 service 在投递成功后写入。

**在途预留（锁外投递）**：send 流程在锁内判断并预留名额，释放锁调用 transport，
回到锁内提交（写 UsageEvent）或回滚（丢弃预留）。投递期间这条消息已经「占了
名额」，因此 `ReservationBook` 里的在途预留必须计入消息数、token 与最小间隔，
否则并发请求会同时通过同一条限额。没有 ReservationBook（单测旧路径）时行为不变。

**静默时段**按 IANA 时区把 `now` 转到本地时间，用分钟数判断是否落在
`[start, end)` 内；`start > end` 表示跨午夜。三层中任意一层开启即生效。

**优先级**：限速 / token / 最小间隔的 block 优于静默 defer。一条注定超限的
消息延后到静默结束仍然发不出去，直接 block 更诚实；所有未通过的检查都会出现在
`checks[]` 与 `reasons[]` 里。

**0 的语义**（易错点）：`LimitRules` 里的 `0` 是有效限额（一条都不允许）。
「不设该项限额」只能用**省略字段**表达；部署配置 `RATE_LIMIT_MESSAGES=0`
则是「平台关闭限速」的历史语义，由 `LimitStore` 在构造平台层时省略该键来区分。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import Config, parse_hhmm
from .limits import EffectiveLimits, LAYER_PEER, LimitStore, LAYER_PLATFORM
from .store import EventKind, UsageStore, text_digest
from .timeutil import iso_utc, seconds_until, utcnow

SOURCE_PLATFORM = "platform_default"

# 在途预留的自动回收 TTL（秒，#114-7.7）。传输层超时默认 30s、/health 探针
# 另有 2s 短超时；预留 TTL 取 300s，比任何单次投递的正常耗时都长一个量级，
# 只回收真正被遗弃的预留（投递线程异常终止）。
DEFAULT_RESERVATION_TTL_SECONDS = 300.0

# ---- 原因码（对外稳定标识；只增不改） ----
CODE_RATE_LIMIT = "messages.window.exceeded"
CODE_TOKEN_LIMIT = "tokens.window.exceeded"
CODE_SILENT_HOURS = "silent_hours.active"
CODE_MIN_INTERVAL = "min_interval.too_soon"
CODE_GROUP_REPLIES = "group.replies_per_hour.exceeded"
CODE_PROACTIVE_LIMIT = "proactive.per_day.exceeded"
CODE_TOWN_TOKEN_LIMIT = "town.tokens_per_day.exceeded"
# B2.2
CODE_ANTI_SPAM_CONSECUTIVE = "anti_spam.consecutive"
CODE_ANTI_SPAM_COOLDOWN = "anti_spam.cooldown"
CODE_AI_LOOP_TRIPPED = "ai_loop.tripped"
CODE_MESSAGE_LENGTH = "message.length"


def silent_hours_active(window: Dict[str, Any], now: datetime) -> bool:
    """Return whether a validated quiet-hours mapping is active at ``now``.

    This deliberately contains no policy-layer selection: persona proactive
    settings and regular limits can share the same wall-clock semantics.
    """
    if not isinstance(window, dict) or not window.get("enabled"):
        return False
    try:
        start = parse_hhmm(window["start"])
        end = parse_hhmm(window["end"])
        tz = ZoneInfo(window["timezone"])
    except (KeyError, TypeError, ValueError):
        return False
    if start == end:
        return False
    local = now.astimezone(tz)
    minute = local.hour * 60 + local.minute
    return start <= minute < end if start < end else minute >= start or minute < end

DECISION_ALLOW = "allow"
DECISION_DEFER = "defer"
DECISION_BLOCK = "block"


def _ceil_to_second(value: datetime) -> datetime:
    """向上取整到秒（#92）。"""
    if value.microsecond == 0:
        return value
    return value.replace(microsecond=0) + timedelta(seconds=1)


@dataclass(frozen=True)
class PolicyReason:
    code: str
    message: str
    layer: str

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "layer": self.layer}


@dataclass(frozen=True)
class PolicyCheck:
    key: str
    layer: str
    allowed: bool
    limit: Optional[float] = None
    used: Optional[float] = None
    remaining: Optional[float] = None
    reset_at: Optional[str] = None
    code: Optional[str] = None
    source_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """只输出契约声明过的字段，未设置的可选字段省略。"""
        out: Dict[str, Any] = {"key": self.key, "layer": self.layer, "allowed": self.allowed}
        for name in ("limit", "used", "remaining", "reset_at", "code", "source_id"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


@dataclass(frozen=True)
class PolicyDecision:
    """一次发言裁决。

    `may_clear_at` / `same_content_allowed` 是内部字段：契约把这两个字段放在
    `MessageReceipt` 而不是 `PolicyDecision` 上，所以 `to_dict()`（用于
    `POST /v1/policy/evaluate` 响应）不包含它们，由 service 层写进回执。
    """

    decision: str
    allowed: bool
    evaluated_at: str
    winner_layer: Optional[str]
    retryable: bool
    retry_after_seconds: Optional[int]
    estimated_tokens: int
    reasons: Tuple[PolicyReason, ...] = ()
    checks: Tuple[PolicyCheck, ...] = ()
    may_clear_at: Optional[str] = None
    same_content_allowed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "allowed": self.allowed,
            "evaluated_at": self.evaluated_at,
            "winner_layer": self.winner_layer,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "estimated_tokens": self.estimated_tokens,
            "reasons": [r.to_dict() for r in self.reasons],
            "checks": [c.to_dict() for c in self.checks],
        }


def estimate_tokens(text: str) -> int:
    """CJK scripts/forms cost about one token; other text costs about 1/4."""
    if not text:
        return 0
    ranges = ((0x1100, 0x11FF), (0x2E80, 0xA4CF), (0xA960, 0xA97F),
              (0xAC00, 0xD7FF), (0xF900, 0xFAFF), (0xFE10, 0xFE1F),
              (0xFE30, 0xFE4F), (0xFF00, 0xFFEF), (0x1B000, 0x1B16F),
              (0x20000, 0x2FA1F), (0x30000, 0x3FFFF))
    cjk = sum(any(start <= ord(char) <= end for start, end in ranges) for char in text)
    return cjk + (len(text) - cjk) // 4 + 1


@dataclass(frozen=True)
class _SilentView:
    active: bool
    ends_at: Optional[datetime]
    layer: str
    source_id: str


@dataclass(frozen=True)
class Reservation:
    """锁外投递期间占用的名额（一条在途消息）。

    `at` 是预留时刻：它同时充当「这条消息进入滑动窗口的时间」，提交时会成为
    UsageEvent 的 `recorded_at`，所以投递耗时不会被算进窗口起点之前。
    `conversation_id` 用于 ai_loop / anti_spam 按会话计数在途预留（B2.2）；
    `peer_id` 用于 peer 层按 (persona, peer_id) 计数在途预留（§6.4）。
    `expires_at` 是自动回收时刻（#114-7.7），见 `ReservationBook`；用单调时钟
    秒数（`time.monotonic()`）计量在途时长，不写进任何时间戳字段。
    """

    id: str
    persona_id: str
    tokens: int
    at: datetime
    conversation_id: Optional[str] = None
    peer_id: Optional[str] = None
    # Only proactive reservations consume the separate daily proactive budget.
    kind: str = "message"
    expires_at: Optional[float] = None


class ReservationBook:
    """在途预留的临时账本（线程安全）。

    service 在锁内 `reserve()`、锁外投递、回到锁内 `commit()` / `release()`。
    评估时把在途预留当作已用额度，因此：
    - 消息数：`limit=N` 时最多 N 条同时在途/已送达；
    - token：在途的 `tokens` 先占预算，失败回滚后额度立刻可用；
    - 最小间隔：在途消息的 `at` 也算「最近一次发言」，避免并发击穿间隔限制。

    预留条目是内存里的临时状态，不写 UsageEvent、不进账单（投递失败不计费）。

    **TTL 自动回收（#114-7.7）**：预留只在 `send_text` 返回后提交/回滚。若投递
    线程被异常终止（进程内未捕获的 `BaseException`、线程被杀、或调用方忘记
    release），名额会永久留在账上——消息数/ token 预算/最小间隔就此被一条
    幽灵消息卡死。因此每条预留带 `expires_at`，任何一次读写账本时先回收过期
    条目。TTL 由 `ReservationBook(ttl_seconds=…)` 注入，默认 300s（远大于传输
    超时 30s 的两倍），保证正常慢投递不会被误回收。
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._ttl = ttl_seconds
        self._items: Dict[str, Reservation] = {}
        self._lock = threading.Lock()

    def _purge_expired_locked(self) -> None:
        """回收已过 TTL 的预留（调用方须持锁）。

        用**单调时钟**计量在途时长，不读墙钟：一是预留问的是「这条消息占位
        多久了」，与日历时间无关；二是测试与回放会注入过去的假时钟（策略评估
        用），若拿墙钟比对 `at`，所有预留都会瞬间被判过期，去重/限额全线失效。
        """
        now = time.monotonic()
        expired = [
            rid
            for rid, item in self._items.items()
            if item.expires_at is not None and now >= item.expires_at
        ]
        for rid in expired:
            del self._items[rid]

    def reserve(
        self,
        *,
        persona_id: str,
        tokens: int,
        at: datetime,
        conversation_id: Optional[str] = None,
        peer_id: Optional[str] = None,
        kind: str = "message",
    ) -> Reservation:
        reservation = Reservation(
            id=f"res_{uuid.uuid4().hex}",
            persona_id=persona_id,
            tokens=tokens,
            at=at,
            expires_at=time.monotonic() + self._ttl,
            conversation_id=conversation_id,
            peer_id=peer_id,
            kind=kind,
        )
        with self._lock:
            self._purge_expired_locked()
            self._items[reservation.id] = reservation
        return reservation

    def release(self, reservation: Reservation) -> bool:
        """回滚预留；返回该预留是否仍在账上（重复回滚返回 False）。"""
        with self._lock:
            self._purge_expired_locked()
            # Do not let a reconstructed Reservation with a copied ID release
            # the live slot owned by another caller.
            if self._items.get(reservation.id) is not reservation:
                return False
            del self._items[reservation.id]
            return True

    def contains(self, reservation: Reservation) -> bool:
        """Whether this exact, unexpired reservation is still owned by us."""
        with self._lock:
            self._purge_expired_locked()
            return self._items.get(reservation.id) is reservation

    def for_persona(self, persona_id: str) -> List[Reservation]:
        with self._lock:
            self._purge_expired_locked()
            return [r for r in self._items.values() if r.persona_id == persona_id]

    def for_conversation(self, conversation_id: str) -> List[Reservation]:
        """按会话过滤在途预留（B2.2 ai_loop 计数用）。"""
        with self._lock:
            self._purge_expired_locked()
            return [
                r for r in self._items.values() if r.conversation_id == conversation_id
            ]

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired_locked()
            return len(self._items)


class PolicyEngine:
    def __init__(
        self,
        config: Config,
        store: UsageStore,
        limit_store: Optional[LimitStore] = None,
        *,
        now_fn=utcnow,
        reservations: Optional[ReservationBook] = None,
    ) -> None:
        self.group_invites = None
        self._group_sends = OrderedDict()
        self._config = config
        self._store = store
        self._limit_store = limit_store
        self._now = now_fn
        # 锁外投递时的在途预留；None 时走旧路径（不把在途算进已用额度）。
        self._reservations = reservations
        # B2.2 §6.3 冷却状态：与容量有界的事件表解耦（#117 B3/B5）。
        # - `_cooldowns[(persona, conv)]` = 首次触发 anti_spam.consecutive 的时刻；
        # - `_count_since[(persona, conv)]` = 冷却结束/人类消息之后的连续计数起点；
        # - `_blocked_recorded[(persona, conv, reason_code)]` = 该组合是否已写过
        #   blocked 事件（**按 reason_code** 去重，防洪泛写放大挤掉计数事件，#117 B6）。
        self._state_lock = threading.Lock()
        self._cooldowns: "OrderedDict[Tuple[str, str], datetime]" = OrderedDict()
        self._count_since: "OrderedDict[Tuple[str, str], datetime]" = OrderedDict()
        self._blocked_recorded: "OrderedDict[Tuple[str, str, str], bool]" = OrderedDict()

    # ------------------------------------------------------------------ API
    def evaluate(
        self,
        *,
        persona_id: str,
        conversation_id: Optional[str] = None,
        peer_id: Optional[str] = None,
        text: str = "",
        token_estimate: Optional[int] = None,
        allow_deferred: bool = True,
        kind: str = "message",
        now: Optional[datetime] = None,
        commit_state: bool = False,
    ) -> PolicyDecision:
        """策略裁决。

        `commit_state=False`（默认）为**只读预检**：不改任何冷却/计数状态，
        只回答「若现在发送会怎样」（`/policy/evaluate` 走这条，#117 N4）。
        `commit_state=True` 仅由 service 的实发路径（`_prepare_send`）传入：
        冷却到期清零等状态变更才会落库（该调用在 `service._lock` 内）。
        """
        persona_id, conversation_id = self.normalize_group_target(persona_id, conversation_id)
        now = now or self._now()
        # 计量取 max（B3）：客户端给的 token_estimate 只是**下限**，不能低于按
        # 正文估算的值；否则 `token_estimate: 0` 可以完全绕过 token 预算
        # （`tokens_per_window: 0` 也挡不住非空正文）。
        estimated = max(
            token_estimate if token_estimate is not None else 0,
            estimate_tokens(text),
        )

        effective: Optional[EffectiveLimits] = None
        if self._limit_store is not None:
            effective = self._limit_store.effective(
                persona_id=persona_id,
                conversation_id=conversation_id,
                peer_id=peer_id,
                now=now,
            )

        checks: List[PolicyCheck] = []
        reasons: List[PolicyReason] = []

        # 1) 消息数窗口
        rate = self._rate_view(persona_id, now, effective, peer_id=peer_id)
        checks.append(rate)
        if not rate.allowed:
            reasons.append(
                PolicyReason(
                    code=CODE_RATE_LIMIT,
                    message=(
                        f"Message window limit reached ({rate.limit} per window); "
                        f"a slot frees at {rate.reset_at}."
                    ),
                    layer=rate.layer,
                )
            )

        # 2) token 窗口（预扣本次估算）
        token = self._token_view(persona_id, now, estimated, effective, peer_id=peer_id)
        if token is not None:
            checks.append(token)
            if not token.allowed:
                reasons.append(
                    PolicyReason(
                        code=CODE_TOKEN_LIMIT,
                        message=(
                            f"Token budget exhausted (limit {token.limit}, "
                            f"used {token.used}, requested {estimated})."
                        ),
                        layer=token.layer,
                    )
                )

        # 3) 最小发言间隔
        interval = self._interval_view(persona_id, now, effective, peer_id=peer_id)
        if interval is not None:
            checks.append(interval)
            if not interval.allowed:
                reasons.append(
                    PolicyReason(
                        code=CODE_MIN_INTERVAL,
                        message=(
                            f"At least {interval.limit}s must pass between messages; "
                            f"retry at {interval.reset_at}."
                        ),
                        layer=interval.layer,
                    )
                )

        group = bool(conversation_id and self.group_invites is not None
                     and self.group_invites.is_throttled(persona_id, conversation_id))
        if group:
            for check in self._group_views(persona_id, conversation_id, now):
                checks.append(check)
                if not check.allowed:
                    reasons.append(PolicyReason(check.code, "Group send limit reached.", check.layer))

        # 4) 主动发言 / 日（仅 kind=proactive 参与）
        if kind == "proactive":
            proactive = self._proactive_view(persona_id, now, effective)
            if proactive is not None:
                checks.append(proactive)
                if not proactive.allowed:
                    reasons.append(
                        PolicyReason(
                            code=CODE_PROACTIVE_LIMIT,
                            message=(
                                f"Daily proactive budget exhausted "
                                f"(limit {proactive.limit}, used {proactive.used})."
                            ),
                            layer=proactive.layer,
                        )
                    )

        # 5) 小镇 token / 日（仅 kind=town_action 参与）
        if kind == "town_action":
            town = self._town_view(now, estimated, effective)
            if town is not None:
                checks.append(town)
                if not town.allowed:
                    reasons.append(
                        PolicyReason(
                            code=CODE_TOWN_TOKEN_LIMIT,
                            message=(
                                f"Daily town token budget exhausted "
                                f"(limit {town.limit}, used {town.used}, requested {estimated})."
                            ),
                            layer=town.layer,
                        )
                    )

        # 6) 静默时段
        silent = self._silent_view(now, effective)
        checks.append(
            PolicyCheck(
                key="silent_hours",
                layer=silent.layer,
                allowed=not silent.active,
                reset_at=iso_utc(silent.ends_at) if silent.ends_at else None,
                code=CODE_SILENT_HOURS if silent.active else None,
                source_id=silent.source_id,
            )
        )
        if silent.active and silent.ends_at is not None:
            reasons.append(
                PolicyReason(
                    code=CODE_SILENT_HOURS,
                    message=f"Quiet hours are active until {iso_utc(silent.ends_at)}.",
                    layer=silent.layer,
                )
            )

        # 7) 防刷屏 anti_spam（B2.2）
        if conversation_id:
            anti_spam = self._anti_spam_view(
                persona_id=persona_id,
                conversation_id=conversation_id,
                now=now,
                effective=effective,
                commit_state=commit_state,
                group=group,
            )
            if anti_spam is not None:
                checks.append(anti_spam)
                if not anti_spam.allowed:
                    reasons.append(
                        PolicyReason(
                            code=anti_spam.code or CODE_ANTI_SPAM_CONSECUTIVE,
                            message=(
                                f"Anti-spam triggered: {anti_spam.code}; "
                                f"may clear at {anti_spam.reset_at}."
                            ),
                            layer=anti_spam.layer,
                        )
                    )

        # 8) 互聊熔断 ai_loop（B2.2）
        if conversation_id:
            ai_loop = self._ai_loop_view(
                persona_id=persona_id,
                conversation_id=conversation_id,
                now=now,
                effective=effective,
                group=group,
            )
            if ai_loop is not None:
                checks.append(ai_loop)
                if not ai_loop.allowed:
                    reasons.append(
                        PolicyReason(
                            code=CODE_AI_LOOP_TRIPPED,
                            message=(
                                f"AI-to-AI conversation hit the hourly circuit breaker; "
                                f"may clear at {ai_loop.reset_at}."
                            ),
                            layer=ai_loop.layer,
                        )
                    )

        # 9) 分层 max_message_chars（B2.2 #114-7.2）
        # 平台硬上限已在 _validate_send 里做 400；这里只处理 owner/peer 更严的配置。
        msg_len = self._message_length_view(text=text, now=now, effective=effective)
        if msg_len is not None:
            checks.append(msg_len)
            if not msg_len.allowed:
                reasons.append(
                    PolicyReason(
                        code=CODE_MESSAGE_LENGTH,
                        message=(
                            f"Message length {len(text)} exceeds the effective limit "
                            f"{msg_len.limit} set by layer {msg_len.layer}."
                        ),
                        layer=msg_len.layer,
                    )
                )

        return self._resolve(
            now=now,
            estimated=estimated,
            checks=tuple(checks),
            reasons=tuple(reasons),
            silent=silent,
            allow_deferred=allow_deferred,
        )

    @staticmethod
    def _group_floor(effective, key, floor):
        # Group-only 0 = unset. Inspect all layers so a zero cannot hide a
        # stricter positive owner/peer value selected out by the legacy merge.
        winner = (floor, LAYER_PLATFORM, SOURCE_PLATFORM)
        block, field = key.split(".")
        for layer in effective.layers if effective is not None else ():
            value = (layer.rules.get(block) or {}).get(field, 0)
            if value > 0 and value < winner[0]:
                winner = (value, layer.layer, layer.scope_id)
        return winner

    def normalize_group_target(self, persona, room):
        """Resolve before group classification; unknown aliases fail closed.

        Reuse the transport resolver/cache, never a separate lookup path.
        Preserve legacy non-group accounting identifiers.
        """
        groups = self.group_invites
        if not room or groups is None or not groups.enabled:
            return persona, room
        canonical = persona
        sender_resolver = getattr(groups.transport, 'resolve_sender', None)
        if sender_resolver is not None:
            canonical = sender_resolver(persona)
        elif not persona.startswith('@') and groups.server_name:
            local = persona if persona.startswith('ai_') else 'ai_' + persona
            canonical = '@' + local + ':' + groups.server_name
        resolver = getattr(groups.transport, 'resolve_room', None)
        if resolver is not None:
            room_id = resolver(room)
        elif room.startswith('!'):
            room_id = room
        else:
            from .matrix import DeliveryError
            raise DeliveryError('conversation.not_found', 'Conversation could not be resolved')
        if groups.is_throttled(canonical, room_id) or groups.is_disabled(canonical, room_id):
            return canonical, room_id
        return persona, room

    def _group_key(self, persona, room):
        # No raw identifiers in the new bounded counter keys.
        return (text_digest("group-persona:" + persona, self._config.hmac_secret),
                text_digest("group-room:" + room, self._config.hmac_secret))

    def record_group_send(self, persona, room, at):
        # Called only by service's successful, deduplicated commit under its lock.
        persona, room = self.normalize_group_target(persona, room)
        if self.group_invites is None or not self.group_invites.is_throttled(persona, room):
            return
        key = self._group_key(persona, room)
        with self._state_lock:
            times = [t for t in self._group_sends.get(key, ()) if t > at - timedelta(hours=1)]
            self._group_sends[key] = (times + [at])[-200:]
            self._group_sends.move_to_end(key)
            while len(self._group_sends) > 4096:
                self._group_sends.popitem(last=False)

    def _group_views(self, persona, room, now):
        since = now - timedelta(hours=1)
        with self._state_lock:
            times = [t for t in self._group_sends.get(self._group_key(persona, room), ()) if t > since]
        if self._reservations is not None:
            times.extend(r.at for r in self._reservations.for_persona(persona)
                         if r.conversation_id == room)
        times.sort()
        cooldown = self._config.group_cooldown_seconds
        if cooldown:
            elapsed = (now - times[-1]).total_seconds() if times else cooldown
            allowed = elapsed >= cooldown
            yield PolicyCheck(key="group.min_interval", layer=LAYER_PLATFORM,
                allowed=allowed, limit=cooldown, used=elapsed,
                remaining=max(0, cooldown - elapsed),
                reset_at=iso_utc(_ceil_to_second(times[-1] + timedelta(seconds=cooldown))) if times else None,
                code=None if allowed else CODE_MIN_INTERVAL, source_id=SOURCE_PLATFORM)
        limit = self._config.group_max_replies_per_hour
        used = len(times)
        allowed = used < limit
        yield PolicyCheck(key="group.replies_per_hour", layer=LAYER_PLATFORM,
            allowed=allowed, limit=limit, used=used, remaining=max(0, limit - used),
            reset_at=iso_utc(_ceil_to_second(times[max(0, used - limit)] + timedelta(hours=1))) if times else None,
            code=None if allowed else CODE_GROUP_REPLIES, source_id=SOURCE_PLATFORM)

    # ------------------------------------------------------- decision assembly
    def _resolve(
        self,
        *,
        now: datetime,
        estimated: int,
        checks: Tuple[PolicyCheck, ...],
        reasons: Tuple[PolicyReason, ...],
        silent: _SilentView,
        allow_deferred: bool,
    ) -> PolicyDecision:
        # 除静默外的任一检查未通过 → block（静默本身不阻止 block 的优先级）
        blocking = [c for c in checks if c.key != "silent_hours" and not c.allowed]
        if blocking:
            return PolicyDecision(
                decision=DECISION_BLOCK,
                allowed=False,
                evaluated_at=iso_utc(now),
                winner_layer=blocking[0].layer,
                retryable=False,
                retry_after_seconds=None,
                estimated_tokens=estimated,
                reasons=reasons,
                checks=checks,
                may_clear_at=next((c.reset_at for c in blocking if c.reset_at), None),
                same_content_allowed=True,
            )

        if silent.active and silent.ends_at is not None:
            ends_at = iso_utc(silent.ends_at)
            return PolicyDecision(
                decision=DECISION_DEFER if allow_deferred else DECISION_BLOCK,
                allowed=False,
                evaluated_at=iso_utc(now),
                winner_layer=silent.layer,
                retryable=False,
                retry_after_seconds=(
                    seconds_until(silent.ends_at, now=now) if allow_deferred else None
                ),
                estimated_tokens=estimated,
                reasons=reasons,
                checks=checks,
                may_clear_at=ends_at,
                same_content_allowed=True,
            )

        return PolicyDecision(
            decision=DECISION_ALLOW,
            allowed=True,
            evaluated_at=iso_utc(now),
            winner_layer=None,
            retryable=False,
            retry_after_seconds=None,
            estimated_tokens=estimated,
            reasons=(),
            checks=checks,
        )

    # ------------------------------------------------------------ rate limit
    def _limits_for(self, effective: Optional[EffectiveLimits], key: str) -> Optional[Tuple[Any, str, str]]:
        """返回 `(value, source_layer, source_id)`；未设置该项时返回 None。

        有 LimitStore 时以生效限额为准（省略即「该层不限制」）；
        没有 LimitStore（旧路径/单测）时回退到 Config。
        """
        if effective is not None:
            item = effective.get(key)
            if item is None:
                return None
            return item.value, item.source_layer, item.source_id
        return None

    def _window_seconds(
        self, effective: Optional[EffectiveLimits], *, pair: str
    ) -> int:
        """速率对的生效窗口（B1）：消息数与 token 各自用胜出层的窗口。

        `effective` 里由 `LimitStore` 分别写下 `messages.window_seconds` /
        `tokens.window_seconds`；没有该键时回退部署配置的窗口。
        """
        item = self._limits_for(effective, f"{pair}.window_seconds")
        if item is not None:
            return int(item[0])
        return self._config.rate_limit_window_seconds

    def _rate_view(
        self,
        persona_id: str,
        now: datetime,
        effective: Optional[EffectiveLimits],
        *,
        peer_id: Optional[str] = None,
    ) -> PolicyCheck:
        window = self._window_seconds(effective, pair="messages")
        if effective is not None:
            item = self._limits_for(effective, "messages_per_window")
            if item is None:
                return PolicyCheck(
                    key="messages.window",
                    layer=LAYER_PLATFORM,
                    allowed=True,
                    source_id=SOURCE_PLATFORM,
                )
            limit, layer, source_id = item
        else:
            if self._config.rate_limit_messages <= 0:
                return PolicyCheck(
                    key="messages.window",
                    layer=LAYER_PLATFORM,
                    allowed=True,
                    source_id=SOURCE_PLATFORM,
                )
            limit = self._config.rate_limit_messages
            layer, source_id = LAYER_PLATFORM, SOURCE_PLATFORM

        since = now - timedelta(seconds=window)
        # B2.2 §6.4: peer 层按 (persona, peer_id) 计数；其他层仍为 persona 全局。
        use_peer_filter = (layer == LAYER_PEER and peer_id is not None)
        events = sorted(
            (event for event in self._store.events_in_window(
                persona_id=persona_id, since=since,
                peer_id=peer_id if use_peer_filter else None,
            ) if event.kind in (EventKind.SENT, EventKind.PROACTIVE)),
            key=lambda e: e.recorded_at,
        )
        used = len(events)

        # 把在途预留（锁外投递中）也算作已用名额，防止并发击穿。
        inflight_ats: List[datetime] = []
        if self._reservations is not None:
            # B2.2 §6.4: peer 层在途只计同 peer_id 的；其他层计全部该 persona 在途。
            inflight = [
                r
                for r in self._reservations.for_persona(persona_id)
                if not use_peer_filter or r.peer_id == peer_id
            ]
            used += len(inflight)
            inflight_ats = [r.at for r in inflight]

        allowed = used < limit
        remaining = max(0, limit - used)
        if events:
            release_index = min(len(events) - 1, max(0, used - limit))
            release_at = events[release_index].recorded_at + timedelta(seconds=window)
        elif inflight_ats:
            # 仅由在途预留阻塞：reset_at = 最早预留 at + window（不得为 now/None，#114-7.1）。
            release_at = min(inflight_ats) + timedelta(seconds=window)
        else:
            release_at = since + timedelta(seconds=window)
        return PolicyCheck(
            key="messages.window",
            layer=layer,
            allowed=allowed,
            limit=limit,
            used=used,
            remaining=remaining,
            reset_at=iso_utc(_ceil_to_second(release_at)),
            code=None if allowed else CODE_RATE_LIMIT,
            source_id=source_id,
        )

    # ------------------------------------------------------------ token view
    def _token_view(
        self,
        persona_id: str,
        now: datetime,
        estimated: int,
        effective: Optional[EffectiveLimits],
        *,
        peer_id: Optional[str] = None,
    ) -> Optional[PolicyCheck]:
        window = self._window_seconds(effective, pair="tokens")
        if effective is not None:
            item = self._limits_for(effective, "tokens_per_window")
            if item is None:
                return None  # 平台层省略 = 不设 token 预算
            budget, layer, source_id = item
        else:
            if self._config.platform_token_budget <= 0:
                return None
            budget, layer, source_id = (
                self._config.platform_token_budget,
                LAYER_PLATFORM,
                SOURCE_PLATFORM,
            )

        since = now - timedelta(seconds=window)
        use_peer_filter = (layer == LAYER_PEER and peer_id is not None)
        events = sorted(
            [e for e in self._store.events_in_window(
                persona_id=persona_id,
                since=since,
                peer_id=peer_id if use_peer_filter else None,
            ) if e.kind in (EventKind.SENT, EventKind.PROACTIVE, EventKind.MEMORY)],
            key=lambda e: e.recorded_at,
        )
        used = sum(e.tokens_in + e.tokens_out for e in events)
        # 在途预留先占预算：并发请求不能各自看到同一份剩余额度。
        # peer 层只计同 peer_id 的在途（§6.4）。
        inflight_ats: List[datetime] = []
        if self._reservations is not None:
            inflight = [
                r
                for r in self._reservations.for_persona(persona_id)
                if not use_peer_filter or r.peer_id == peer_id
            ]
            used += sum(r.tokens for r in inflight)
            inflight_ats = [r.at for r in inflight]
        allowed = used + estimated <= budget
        remaining = max(0, budget - used)

        if allowed:
            # 窗口清空时刻（参考值）；无事件时就是窗口滑过 now 的时刻。
            release_at = (
                events[0].recorded_at + timedelta(seconds=window)
                if events
                else now + timedelta(seconds=window)
            )
            reset_at: Optional[str] = iso_utc(_ceil_to_second(release_at))
        else:
            # 找最早让「剩余额度 ≥ estimated」的事件，其过期时刻即恢复时刻。
            residual = used
            reset_at = None
            for event in events:
                residual -= event.tokens_in + event.tokens_out
                if residual + estimated <= budget:
                    reset_at = iso_utc(
                        _ceil_to_second(event.recorded_at + timedelta(seconds=window))
                    )
                    break
            # 单次请求就超过总预算：等多久都不会恢复，reset_at 省略。
            if reset_at is None and not events and inflight_ats:
                # 仅由在途预留阻塞：最早预留释放时刻（不得为 None，#114-7.1）。
                reset_at = iso_utc(
                    _ceil_to_second(
                        min(inflight_ats) + timedelta(seconds=window)
                    )
                )

        return PolicyCheck(
            key="tokens.window",
            layer=layer,
            allowed=allowed,
            limit=budget,
            used=used,
            remaining=remaining,
            reset_at=reset_at,
            code=None if allowed else CODE_TOKEN_LIMIT,
            source_id=source_id,
        )

    # --------------------------------------------------------- interval view
    def _interval_view(
        self,
        persona_id: str,
        now: datetime,
        effective: Optional[EffectiveLimits],
        *,
        peer_id: Optional[str] = None,
    ) -> Optional[PolicyCheck]:
        if effective is None:
            return None
        item = self._limits_for(effective, "min_seconds_between_messages")
        if item is None:
            return None
        min_seconds, layer, source_id = item
        if min_seconds <= 0:
            return PolicyCheck(
                key="min_interval",
                layer=layer,
                allowed=True,
                limit=min_seconds,
                source_id=source_id,
            )
        since = now - timedelta(seconds=min_seconds)
        use_peer_filter = (layer == LAYER_PEER and peer_id is not None)
        events = [event for event in self._store.events_in_window(
            persona_id=persona_id, since=since,
            peer_id=peer_id if use_peer_filter else None,
        ) if event.kind in (EventKind.SENT, EventKind.PROACTIVE)]
        # 在途预留同样算「最近一次发言」：并发请求不能在间隔窗口内同时放行。
        latest_at: Optional[datetime] = None
        if events:
            latest_at = max(e.recorded_at for e in events)
        if self._reservations is not None:
            for reservation in self._reservations.for_persona(persona_id):
                if use_peer_filter and reservation.peer_id != peer_id:
                    continue
                if reservation.at > since and (
                    latest_at is None or reservation.at > latest_at
                ):
                    latest_at = reservation.at
        if latest_at is None:
            return PolicyCheck(
                key="min_interval",
                layer=layer,
                allowed=True,
                limit=min_seconds,
                source_id=source_id,
            )
        release_at = latest_at + timedelta(seconds=min_seconds)
        return PolicyCheck(
            key="min_interval",
            layer=layer,
            allowed=False,
            limit=min_seconds,
            reset_at=iso_utc(_ceil_to_second(release_at)),
            code=CODE_MIN_INTERVAL,
            source_id=source_id,
        )

    # ------------------------------------------------------- daily windows
    def _day_bounds(self, now: datetime, effective: Optional[EffectiveLimits]) -> Tuple[datetime, datetime, ZoneInfo]:
        """`(当日开始, 次日开始, 时区)`，按生效静默时区的日历日计算。"""
        if effective is not None and self._limit_store is not None:
            window = self._limit_store.silent_window(effective)
            tz = ZoneInfo(window.get("timezone") or self._config.silent_hours_timezone)
        else:
            tz = ZoneInfo(self._config.silent_hours_timezone)
        local = now.astimezone(tz)
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = (start_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), tz

    def _proactive_view(
        self, persona_id: str, now: datetime, effective: Optional[EffectiveLimits]
    ) -> Optional[PolicyCheck]:
        if effective is None:
            return None
        item = self._limits_for(effective, "proactive_per_day")
        if item is None:
            return None
        limit, layer, source_id = item
        day_start, day_end, _ = self._day_bounds(now, effective)
        events = self._store.events_in_window(
            persona_id=persona_id, kind=EventKind.PROACTIVE,
            since=day_start - timedelta(microseconds=1)
        )
        used = len(events)
        if self._reservations is not None:
            # Pre-model proactive reservations are included so concurrent ticks
            # cannot both pass a single daily slot.
            used += sum(
                1 for reservation in self._reservations.for_persona(persona_id)
                if reservation.kind == "proactive" and reservation.at >= day_start
            )
        allowed = used < limit
        return PolicyCheck(
            key="proactive.per_day",
            layer=layer,
            allowed=allowed,
            limit=limit,
            used=used,
            remaining=max(0, limit - used),
            reset_at=iso_utc(day_end),
            code=None if allowed else CODE_PROACTIVE_LIMIT,
            source_id=source_id,
        )

    def _town_view(
        self, now: datetime, estimated: int, effective: Optional[EffectiveLimits]
    ) -> Optional[PolicyCheck]:
        if effective is None:
            return None
        item = self._limits_for(effective, "town_tokens_per_day")
        if item is None:
            return None
        limit, layer, source_id = item
        day_start, day_end, _ = self._day_bounds(now, effective)
        events = self._store.events_in_window(
            kind=EventKind.TOWN_ACTION, since=day_start
        )
        used = sum(e.town_tokens for e in events)
        allowed = used + estimated <= limit
        return PolicyCheck(
            key="town.tokens_per_day",
            layer=layer,
            allowed=allowed,
            limit=limit,
            used=used,
            remaining=max(0, limit - used),
            reset_at=iso_utc(day_end),
            code=None if allowed else CODE_TOWN_TOKEN_LIMIT,
            source_id=source_id,
        )

    # ---------------------------------------------------------- silent hours
    def _silent_view(
        self, now: datetime, effective: Optional[EffectiveLimits]
    ) -> _SilentView:
        if effective is not None and self._limit_store is not None:
            window = self._limit_store.silent_window(effective)
            if not window.get("enabled"):
                return _SilentView(False, None, LAYER_PLATFORM, SOURCE_PLATFORM)
            start = parse_hhmm(window["start"])
            end = parse_hhmm(window["end"])
            if start == end:
                # 零长度窗口 = 不静默（`silent_window` 已排除这类层，这里是兜底）。
                return _SilentView(False, None, LAYER_PLATFORM, SOURCE_PLATFORM)
            tz = ZoneInfo(window["timezone"])
            item = effective.get("silent_hours")
            layer = item.source_layer if item is not None else LAYER_PLATFORM
            source_id = item.source_id if item is not None else SOURCE_PLATFORM
        else:
            cfg = self._config
            if not cfg.silent_hours_enabled:
                return _SilentView(False, None, LAYER_PLATFORM, SOURCE_PLATFORM)
            start = cfg.silent_start_minute
            end = cfg.silent_end_minute
            if start == end:
                return _SilentView(False, None, LAYER_PLATFORM, SOURCE_PLATFORM)
            tz = ZoneInfo(cfg.silent_hours_timezone)
            layer, source_id = LAYER_PLATFORM, SOURCE_PLATFORM

        local_now = now.astimezone(tz)
        active = silent_hours_active(
            {"enabled": True, "start": f"{start // 60:02d}:{start % 60:02d}",
             "end": f"{end // 60:02d}:{end % 60:02d}", "timezone": str(tz)}, now
        )
        if not active:
            return _SilentView(False, None, layer, source_id)
        return _SilentView(True, self._next_local_time(local_now, end), layer, source_id)

    def _next_local_time(self, local_now: datetime, minute_of_day: int) -> datetime:
        """本次静默结束的 UTC 时刻（严格晚于 `local_now`）。

        DST 跳变日的墙钟时间可能不存在或重复，`ZoneInfo` 按 PEP 495 折衷解释；
        Asia/Shanghai 无 DST，主流时区一年最多偏差一小时，对本功能可接受。
        """
        target = local_now.replace(
            hour=minute_of_day // 60,
            minute=minute_of_day % 60,
            second=0,
            microsecond=0,
        )
        if target <= local_now:
            target = (local_now + timedelta(days=1)).replace(
                hour=minute_of_day // 60,
                minute=minute_of_day % 60,
                second=0,
                microsecond=0,
            )
        return target.astimezone(timezone.utc)

    # ---------------------------------------------- B2.2 anti_spam / ai_loop
    def _anti_spam_key(self, persona_id: str, conversation_id: str) -> Tuple[str, str]:
        return self._group_key(persona_id, conversation_id)

    def _trim_state_locked(self) -> None:
        """冷却/计数起点/去重表按 max_usage_events 设上限（最旧优先淘汰，#117 N3）。"""
        limit = self._config.max_usage_events
        if limit <= 0:
            return
        for book in (self._cooldowns, self._count_since, self._blocked_recorded):
            while len(book) > limit:
                book.popitem(last=False)

    def note_anti_spam_trip(
        self, persona_id: str, conversation_id: str, at: datetime
    ) -> None:
        """记录 anti_spam 冷却锚点（#117 B3/B5）。

        只由 service 在**实发路径**、且 decision 的 checks 中出现
        `anti_spam.consecutive` + `allowed=False` 时调用；预检（evaluate）不落锚点。
        冷却状态存在 engine 内，独立于容量有界的事件表：blocked 事件被淘汰不会提前
        解除冷却（否则客户端可用被拒请求洪泛绕过冷却）。
        """
        key = self._anti_spam_key(persona_id, conversation_id)
        with self._state_lock:
            self._cooldowns[key] = at
            self._cooldowns.move_to_end(key)
            # 冷却结束后的计数起点由冷却结束时决定，旧起点作废。
            self._count_since.pop(key, None)
            # 新冷却开始：清掉该 (persona,conv) 的所有 blocked 去重记录，
            # 这样冷却期内的首次 blocked 会重新写一条事件。
            for k in [k for k in self._blocked_recorded if k[:2] == key]:
                del self._blocked_recorded[k]
            self._trim_state_locked()

    def should_record_blocked(
        self, persona_id: str, conversation_id: str, reason_code: Optional[str]
    ) -> bool:
        """同 (persona, conv, reason_code) 的 blocked 事件只在首次写一条（#117 B6）。

        返回 True 表示应写 UsageEvent；之后的重试返回 False（不写），
        防止被拒请求的写放大把计数事件挤出容量上限。`reason_code=None` 时
        退化为按 (persona, conv, "") 去重。
        """
        key = self._anti_spam_key(persona_id, conversation_id)
        rec_key = (key[0], key[1], reason_code or "")
        with self._state_lock:
            if self._blocked_recorded.get(rec_key):
                return False
            self._blocked_recorded[rec_key] = True
            self._blocked_recorded.move_to_end(rec_key)
            self._trim_state_locked()
            return True

    def clear_blocked_records(
        self, persona_id: Optional[str], conversation_id: str
    ) -> None:
        """清掉该会话（或该 persona×会话）的全部 blocked 去重记录（#117 B6）。

        成功 delivered（`_commit_send`）或 `record_human_message` 时调用：
        计数事件已落库，后续若再进入 blocked 状态应允许重新写一条。
        `persona_id=None` 表示清该会话下所有 persona 的记录（人类消息场景）。
        """
        persona_key, room_key = self._group_key(persona_id or "", conversation_id)
        with self._state_lock:
            for k in [
                k
                for k in self._blocked_recorded
                if k[1] == room_key
                and (persona_id is None or k[0] == persona_key)
            ]:
                del self._blocked_recorded[k]

    def should_record_cooldown_block(
        self, persona_id: str, conversation_id: str
    ) -> bool:
        """冷却期内同 (persona, conversation) 的 blocked 事件只在首次写一条。

        旧入口，保留以兼容现有调用点；新代码用 `should_record_blocked`。
        """
        return self.should_record_blocked(
            persona_id, conversation_id, CODE_ANTI_SPAM_COOLDOWN
        )

    def _last_human_at(
        self, conversation_id: str, now: datetime
    ) -> Optional[datetime]:
        """该会话最近一条人类消息时刻（不晚于 now）；无人类消息时返回 None。"""
        events = self._store.events_in_window(
            conversation_id=conversation_id,
            kind=EventKind.HUMAN,
            until=now,
        )
        if not events:
            return None
        return max(e.recorded_at for e in events)

    def _anti_spam_view(
        self,
        *,
        persona_id: str,
        conversation_id: str,
        now: datetime,
        effective: Optional[EffectiveLimits],
        commit_state: bool = False,
        group: bool = False,
    ) -> Optional[PolicyCheck]:
        """防刷屏：同 persona、同会话连续 AI 消息数 + 冷却（B2.2 §6.3）。

        连续数 = 最后一条人类消息（或冷却结束）之后该 persona 的 sent/deferred
        事件数 + 同会话在途预留数。
        达到 max_consecutive_ai_messages 后的下一条 → blocked（consecutive），
        进入冷却；冷却锚定首次触发时刻，冷却期内再请求 → blocked（cooldown），
        **不延长冷却**。冷却结束 → 连续数清零（以冷却结束时刻作为计数起点）。
        blocked 事件不计入连续数。

        `commit_state=False`（预检）时只读：**不** pop `_cooldowns`、不写
        `_count_since`，只按内存中已有状态推算「若现在发送会怎样」（#117 N4）。
        """

        max_item = self._group_floor(effective, "anti_spam.max_consecutive_ai_messages",
            self._config.group_max_consecutive_ai_messages) if group else self._limits_for(
                effective, "anti_spam.max_consecutive_ai_messages")
        cooldown_item = self._limits_for(effective, "anti_spam.cooldown_seconds")
        if max_item is None:
            return None
        max_consecutive, layer, source_id = max_item
        cooldown_seconds = int(cooldown_item[0]) if cooldown_item is not None else 0

        key = self._anti_spam_key(persona_id, conversation_id)
        with self._state_lock:
            anchor = self._cooldowns.get(key)
            stored_since = self._count_since.get(key)

        # 人类消息提前解除冷却：锚点之后出现经网关人类消息 → 冷却失效，
        # 计数起点直接取该人类消息时刻（契约 §6.3）。
        human_at = self._last_human_at(conversation_id, now)
        if anchor is not None and human_at is not None and human_at > anchor:
            anchor = None
            if commit_state:
                with self._state_lock:
                    self._cooldowns.pop(key, None)
                self.clear_blocked_records(persona_id, conversation_id)
            stored_since = human_at

        if anchor is not None:
            cooldown_end = anchor + timedelta(seconds=cooldown_seconds)
            if cooldown_seconds > 0 and now < cooldown_end:
                # 冷却期内：blocked(cooldown)，may_clear_at 不变（不延长）。
                used_seconds = (now - anchor).total_seconds()
                remaining_seconds = max(0, cooldown_seconds - used_seconds)
                return PolicyCheck(
                    key="anti_spam.cooldown",
                    layer=layer,
                    allowed=False,
                    limit=cooldown_seconds,
                    used=used_seconds,
                    remaining=remaining_seconds,
                    reset_at=iso_utc(_ceil_to_second(cooldown_end)),
                    code=CODE_ANTI_SPAM_COOLDOWN,
                    source_id=source_id,
                )
            # 冷却已结束（cooldown_seconds=0 时触发即结束）：计数起点 = 冷却**结束**
            # 时刻（= anchor + cooldown_seconds）。0 秒冷却下即触发时刻本身，
            # 不取 now：否则触发那条也会被算进下一周期，导致每周期放行 max+1 条
            # （#117 N2）。
            stored_since = cooldown_end
            if commit_state:
                with self._state_lock:
                    self._cooldowns.pop(key, None)
                    self._count_since[key] = stored_since
                    self._count_since.move_to_end(key)
                    self._trim_state_locked()
                self.clear_blocked_records(persona_id, conversation_id)

        count_since = stored_since
        if human_at is not None and (
            count_since is None or human_at > count_since
        ):
            count_since = human_at

        # 统计 count_since 之后的 SENT / DEFERRED 事件（该 persona、该会话）。
        events = sorted(
            (
                e
                for e in self._store.events_in_window(
                    persona_id=persona_id,
                    conversation_id=conversation_id,
                    since=count_since,
                )
                if e.kind in (EventKind.SENT, EventKind.PROACTIVE, EventKind.DEFERRED)
            ),
            key=lambda e: e.recorded_at,
        )
        used = len(events)
        # 在途预留也计入连续数，但只计**同会话**的（#117 B2）。
        if self._reservations is not None:
            used += sum(
                1
                for r in self._reservations.for_persona(persona_id)
                if r.conversation_id == conversation_id
            )

        if used >= max_consecutive:
            # 触发：blocked(consecutive)，冷却锚点 = now（首次触发时刻）。
            may_clear = now + timedelta(seconds=cooldown_seconds)
            return PolicyCheck(
                key="anti_spam.consecutive",
                layer=layer,
                allowed=False,
                limit=max_consecutive,
                used=used,
                remaining=0,
                reset_at=iso_utc(_ceil_to_second(may_clear)),
                code=CODE_ANTI_SPAM_CONSECUTIVE,
                source_id=source_id,
            )

        return PolicyCheck(
            key="anti_spam.consecutive",
            layer=layer,
            allowed=True,
            limit=max_consecutive,
            used=used,
            remaining=max(0, max_consecutive - used),
            code=None,
            source_id=source_id,
        )

    def _ai_loop_view(
        self,
        *,
        persona_id: str,
        conversation_id: str,
        now: datetime,
        effective: Optional[EffectiveLimits],
        group: bool = False,
    ) -> Optional[PolicyCheck]:
        """互聊熔断：同会话任意 AI 在 1 小时滑动窗口内的消息数（B2.2 §6.3, N8）。

        计入 `count`（即 `used`）的项目 = 窗口内、最后一条人类消息之后的
        `kind=sent` / `kind=deferred` 事件，**加上**未超 TTL 的在途预留
        （`for_conversation` 已先回收过期条目；超 TTL 的不计入、不参与排序）。
        `deferred` 已是事件，不是在途预留。限额 = 被评估 persona 的生效
        max_ai_messages_per_hour（三层取最严）。超限 → blocked(ai_loop.tripped)；
        on_break=notify 按 stop 处理。

        `may_clear_at`（N8）：
        - `limit = 0`（有效限额「一条都不允许」）→ **null**：永不因滑窗恢复，
          不套出窗公式、不套下界。
        - `limit > 0` 且 `used >= limit`：事件与在途预留放进**同一排序**
          （事件的入窗时刻 = `recorded_at`，预留 = `at`；出窗 = `t + 3600`），
          取第 `used − limit + 1` 早的计入项（`used == limit` 即第 1 早），
          `may_clear_at = ceil_to_second(t + 3600)`。
        - 取值下界：最终 `may_clear_at = max(计算值, ceil_to_second(now) + 1s)`，
          永不返回过去或当前时刻（避免「到点重试仍被拦」的活锁）。
        预留**不受**人类消息重置：过滤预留用窗口起点，而不是人类消息时刻。
        """
        item = self._group_floor(effective, "ai_loop.max_ai_messages_per_hour",
            self._config.group_ai_loop_max_per_hour) if group else self._limits_for(
                effective, "ai_loop.max_ai_messages_per_hour")
        if item is None:
            return None
        max_per_hour, layer, source_id = item
        window_seconds = 3600

        # 计数起点 = 最后一条人类消息时刻（无人类消息则取窗口起点）。
        human_at = None if group else self._last_human_at(conversation_id, now)
        window_start = now - timedelta(seconds=window_seconds)
        if human_at is not None and human_at > window_start:
            count_since = human_at
        else:
            count_since = window_start

        events = sorted(
            (
                e
                for e in self._store.events_in_window(
                    conversation_id=conversation_id,
                    since=count_since,
                )
                if e.kind in (EventKind.SENT, EventKind.PROACTIVE, EventKind.DEFERRED)
            ),
            key=lambda e: e.recorded_at,
        )
        # 事件与未超 TTL 的在途预留放进同一排序（N8）：预留按 `at + 3600` 出窗，
        # 窗口起点取 `at`；人类消息只重置事件计数，不回收预留，因此用
        # `window_start` 而不是 `count_since` 过滤。
        entry_times: List[datetime] = [e.recorded_at for e in events]
        if self._reservations is not None:
            entry_times.extend(
                r.at
                for r in self._reservations.for_conversation(conversation_id)
                if r.at > window_start
            )
        entry_times.sort()
        used = len(entry_times)

        if used >= max_per_hour:
            if max_per_hour == 0:
                # API 的 0 是有效限额「一条都不允许」：恢复只能靠调整限额，
                # 没有任何「额度恢复时间」可报，故为 null。
                reset_at: Optional[str] = None
            else:
                # 第 `used − limit + 1` 早的计入项出窗（0 基索引就是 used - limit）。
                anchor = entry_times[used - max_per_hour]
                computed = _ceil_to_second(anchor + timedelta(seconds=window_seconds))
                # 下界：永不返回过去或当前时刻。
                floor = _ceil_to_second(now) + timedelta(seconds=1)
                reset_at = iso_utc(max(computed, floor))
            return PolicyCheck(
                key="ai_loop.per_hour",
                layer=layer,
                allowed=False,
                limit=max_per_hour,
                used=used,
                remaining=0,
                reset_at=reset_at,
                code=CODE_AI_LOOP_TRIPPED,
                source_id=source_id,
            )

        return PolicyCheck(
            key="ai_loop.per_hour",
            layer=layer,
            allowed=True,
            limit=max_per_hour,
            used=used,
            remaining=max(0, max_per_hour - used),
            code=None,
            source_id=source_id,
        )

    def _message_length_view(
        self,
        *,
        text: str,
        now: datetime,
        effective: Optional[EffectiveLimits],
    ) -> Optional[PolicyCheck]:
        """分层 max_message_chars（B2.2 #114-7.2）。

        平台硬上限 config.max_message_chars 仍由请求解析层 400 拦截；
        这里只处理 owner/peer 分层中更严的 max_message_chars，返回 200 blocked +
        code=message.length。若生效值不严于平台层（或未配置），返回 None。
        """
        if effective is None or self._limit_store is None:
            return None
        item = self._limits_for(effective, "max_message_chars")
        if item is None:
            return None
        limit, layer, source_id = item
        platform_chars = self._config.max_message_chars
        # 只在生效值比平台硬上限更严时才走策略路径。
        if limit >= platform_chars:
            return None
        length = len(text)
        allowed = length <= limit
        return PolicyCheck(
            key="message.length",
            layer=layer,
            allowed=allowed,
            limit=limit,
            used=length,
            remaining=max(0, limit - length),
            code=None if allowed else CODE_MESSAGE_LENGTH,
            source_id=source_id,
        )

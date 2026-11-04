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

import math
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import Config, parse_hhmm
from .limits import EffectiveLimits, LAYER_PEER, LimitStore, LAYER_PLATFORM
from .store import EventKind, UsageStore
from .timeutil import iso_utc, seconds_until, utcnow

SOURCE_PLATFORM = "platform_default"

# ---- 原因码（对外稳定标识；只增不改） ----
CODE_RATE_LIMIT = "messages.window.exceeded"
CODE_TOKEN_LIMIT = "tokens.window.exceeded"
CODE_SILENT_HOURS = "silent_hours.active"
CODE_MIN_INTERVAL = "min_interval.too_soon"
CODE_PROACTIVE_LIMIT = "proactive.per_day.exceeded"
CODE_TOWN_TOKEN_LIMIT = "town.tokens_per_day.exceeded"
# B2.2
CODE_ANTI_SPAM_CONSECUTIVE = "anti_spam.consecutive"
CODE_ANTI_SPAM_COOLDOWN = "anti_spam.cooldown"
CODE_AI_LOOP_TRIPPED = "ai_loop.tripped"
CODE_MESSAGE_LENGTH = "message.length"

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
    """粗略 token 估算（≈4 字符/token，最少 1）。

    这是**下限**估算，不是精确计数：对中文（常见 1–2 字符/token）会低估，
    但 B3 起它是客户端 `token_estimate` 的地板，保证 `token_estimate: 0` 无法
    把非空正文计成 0 token。接真实模型后应改用实际 token 计数。
    """
    if not text:
        return 0
    return max(1, len(text) // 4 + 1)


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
    """

    id: str
    persona_id: str
    tokens: int
    at: datetime
    conversation_id: Optional[str] = None
    peer_id: Optional[str] = None


class ReservationBook:
    """在途预留的临时账本（线程安全）。

    service 在锁内 `reserve()`、锁外投递、回到锁内 `commit()` / `release()`。
    评估时把在途预留当作已用额度，因此：
    - 消息数：`limit=N` 时最多 N 条同时在途/已送达；
    - token：在途的 `tokens` 先占预算，失败回滚后额度立刻可用；
    - 最小间隔：在途消息的 `at` 也算「最近一次发言」，避免并发击穿间隔限制。

    预留条目是内存里的临时状态，不写 UsageEvent、不进账单（投递失败不计费）。
    """

    def __init__(self) -> None:
        self._items: Dict[str, Reservation] = {}
        self._lock = threading.Lock()

    def reserve(
        self,
        *,
        persona_id: str,
        tokens: int,
        at: datetime,
        conversation_id: Optional[str] = None,
        peer_id: Optional[str] = None,
    ) -> Reservation:
        reservation = Reservation(
            id=f"res_{uuid.uuid4().hex}",
            persona_id=persona_id,
            tokens=tokens,
            at=at,
            conversation_id=conversation_id,
            peer_id=peer_id,
        )
        with self._lock:
            self._items[reservation.id] = reservation
        return reservation

    def release(self, reservation: Reservation) -> bool:
        """回滚预留；返回该预留是否仍在账上（重复回滚返回 False）。"""
        with self._lock:
            return self._items.pop(reservation.id, None) is not None

    def for_persona(self, persona_id: str) -> List[Reservation]:
        with self._lock:
            return [r for r in self._items.values() if r.persona_id == persona_id]

    def for_conversation(self, conversation_id: str) -> List[Reservation]:
        """按会话过滤在途预留（B2.2 ai_loop 计数用）。"""
        with self._lock:
            return [r for r in self._items.values() if r.conversation_id == conversation_id]

    def __len__(self) -> int:
        with self._lock:
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
        self._config = config
        self._store = store
        self._limit_store = limit_store
        self._now = now_fn
        # 锁外投递时的在途预留；None 时走旧路径（不把在途算进已用额度）。
        self._reservations = reservations
        # B2.2 §6.3 冷却状态：与容量有界的事件表解耦（#117 B3/B5）。
        # - `_cooldowns[(persona, conv)]` = 首次触发 anti_spam.consecutive 的时刻；
        # - `_count_since[(persona, conv)]` = 冷却结束/人类消息之后的连续计数起点；
        # - `_cooldown_notified` = 本次冷却是否已写过一条 blocked 事件（去重，防洪泛）。
        self._state_lock = threading.Lock()
        self._cooldowns: "OrderedDict[Tuple[str, str], datetime]" = OrderedDict()
        self._count_since: "OrderedDict[Tuple[str, str], datetime]" = OrderedDict()
        self._cooldown_notified: Dict[Tuple[str, str], bool] = {}

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
    ) -> PolicyDecision:
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
            self._store.events_in_window(
                persona_id=persona_id,
                kind=EventKind.SENT,
                since=since,
                peer_id=peer_id if use_peer_filter else None,
            ),
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
            self._store.events_in_window(
                persona_id=persona_id,
                kind=EventKind.SENT,
                since=since,
                peer_id=peer_id if use_peer_filter else None,
            ),
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
        events = self._store.events_in_window(
            persona_id=persona_id,
            kind=EventKind.SENT,
            since=since,
            peer_id=peer_id if use_peer_filter else None,
        )
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
            persona_id=persona_id, kind=EventKind.PROACTIVE, since=day_start
        )
        used = len(events)
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
        minute_of_day = local_now.hour * 60 + local_now.minute
        if start < end:
            active = start <= minute_of_day < end
        else:  # 跨午夜
            active = minute_of_day >= start or minute_of_day < end
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
        return (persona_id, conversation_id)

    def _trim_state_locked(self) -> None:
        """冷却/计数起点表按 max_usage_events 设上限（最旧优先淘汰）。"""
        limit = self._config.max_usage_events
        if limit <= 0:
            return
        for book in (self._cooldowns, self._count_since):
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
            self._cooldown_notified[key] = False
            self._trim_state_locked()

    def should_record_cooldown_block(
        self, persona_id: str, conversation_id: str
    ) -> bool:
        """冷却期内同 (persona, conversation) 的 blocked 事件只在首次写一条。

        返回 True 表示应写 UsageEvent；之后的重试返回 False（不写），
        避免被拒请求的写放大把计数事件挤出容量上限。
        """
        key = self._anti_spam_key(persona_id, conversation_id)
        with self._state_lock:
            if key not in self._cooldowns:
                return True
            if self._cooldown_notified.get(key):
                return False
            self._cooldown_notified[key] = True
            return True

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
    ) -> Optional[PolicyCheck]:
        """防刷屏：同 persona、同会话连续 AI 消息数 + 冷却（B2.2 §6.3）。

        连续数 = 最后一条人类消息（或冷却结束）之后该 persona 的 sent/deferred
        事件数 + 同会话在途预留数。
        达到 max_consecutive_ai_messages 后的下一条 → blocked（consecutive），
        进入冷却；冷却锚定首次触发时刻，冷却期内再请求 → blocked（cooldown），
        **不延长冷却**。冷却结束 → 连续数清零（以冷却结束时刻作为计数起点）。
        blocked 事件不计入连续数。
        """
        if effective is None:
            return None
        max_item = self._limits_for(effective, "anti_spam.max_consecutive_ai_messages")
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
            with self._state_lock:
                self._cooldowns.pop(key, None)
                self._cooldown_notified.pop(key, None)
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
            # 冷却已结束（cooldown_seconds=0 时触发即结束）：计数起点 = 冷却结束
            # 时刻。0 秒冷却取 now：触发那条之后的请求立刻重新从 0 起计，不活锁。
            stored_since = now if cooldown_seconds <= 0 else cooldown_end
            with self._state_lock:
                self._cooldowns.pop(key, None)
                self._cooldown_notified.pop(key, None)
                self._count_since[key] = stored_since
                self._count_since.move_to_end(key)
                self._trim_state_locked()

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
                if e.kind in (EventKind.SENT, EventKind.DEFERRED)
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
    ) -> Optional[PolicyCheck]:
        """互聊熔断：同会话任意 AI 在 1 小时滑动窗口内的消息数（B2.2 §6.3）。

        count = 每小时滑动窗口内、最后一条人类消息之后的**任意 AI** sent 事件数
        + 该会话在途预留。限额 = 被评估 persona 的生效 max_ai_messages_per_hour
        （三层取最严）。超限 → blocked(ai_loop.tripped)；on_break=notify 按 stop 处理。
        may_clear_at = 被计入 count 的最早一条事件出窗时刻（+3600s，向上取整到秒）；
        若阻塞来自在途预留则给保守值（预留 at + 3600）。
        """
        if effective is None:
            return None
        item = self._limits_for(effective, "ai_loop.max_ai_messages_per_hour")
        if item is None:
            return None
        max_per_hour, layer, source_id = item
        window_seconds = 3600

        # 计数起点 = 最后一条人类消息时刻（无人类消息则取窗口起点）。
        human_at = self._last_human_at(conversation_id, now)
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
                if e.kind in (EventKind.SENT, EventKind.DEFERRED)
            ),
            key=lambda e: e.recorded_at,
        )
        used = len(events)
        # 在途预留：按会话过滤（任意 AI 的预留都计入同一会话的熔断计数）。
        if self._reservations is not None:
            inflight_conv = self._reservations.for_conversation(conversation_id)
            used += len(inflight_conv)

        if used >= max_per_hour:
            # may_clear_at = 被计入 count 的最早一条事件出窗时刻。
            if events:
                earliest = events[0].recorded_at
                may_clear = earliest + timedelta(seconds=window_seconds)
            else:
                # 阻塞来自在途预留：给保守值（now + 3600）。
                may_clear = now + timedelta(seconds=window_seconds)
            return PolicyCheck(
                key="ai_loop.per_hour",
                layer=layer,
                allowed=False,
                limit=max_per_hour,
                used=used,
                remaining=0,
                reset_at=iso_utc(_ceil_to_second(may_clear)),
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

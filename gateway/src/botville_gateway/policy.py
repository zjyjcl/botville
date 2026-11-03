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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import Config, parse_hhmm
from .limits import EffectiveLimits, LimitStore, LAYER_PLATFORM
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
    """粗略 token 估算（≈4 字符/token，中文按字算更保守取 max）。

    仅供预算展示；接真实模型后应改用实际 token 计数。
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


class PolicyEngine:
    def __init__(
        self,
        config: Config,
        store: UsageStore,
        limit_store: Optional[LimitStore] = None,
        *,
        now_fn=utcnow,
    ) -> None:
        self._config = config
        self._store = store
        self._limit_store = limit_store
        self._now = now_fn

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
        estimated = token_estimate if token_estimate is not None else estimate_tokens(text)

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
        rate = self._rate_view(persona_id, now, effective)
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
        token = self._token_view(persona_id, now, estimated, effective)
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
        interval = self._interval_view(persona_id, now, effective)
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

    def _window_seconds(self, effective: Optional[EffectiveLimits]) -> int:
        item = self._limits_for(effective, "window_seconds")
        if item is not None:
            return int(item[0])
        return self._config.rate_limit_window_seconds

    def _rate_view(
        self, persona_id: str, now: datetime, effective: Optional[EffectiveLimits]
    ) -> PolicyCheck:
        window = self._window_seconds(effective)
        if effective is not None:
            item = self._limits_for(effective, "messages_per_window")
            if item is None:
                # 平台层省略 = 不限；owner/peer 不可能放宽，故整体不限。
                return PolicyCheck(
                    key="messages.window",
                    layer=LAYER_PLATFORM,
                    allowed=True,
                    source_id=SOURCE_PLATFORM,
                )
            limit, layer, source_id = item
        else:
            # 无 LimitStore：沿用 Config 语义（0 = 关闭限速）
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
        events = sorted(
            self._store.events_in_window(
                persona_id=persona_id, kind=EventKind.SENT, since=since
            ),
            key=lambda e: e.recorded_at,
        )
        used = len(events)
        allowed = used < limit
        remaining = max(0, limit - used)
        if events:
            # 第 used−limit+1 早的事件离开窗口时，名额才腾出来（used>limit 时跳过前几条）
            release_index = min(len(events) - 1, max(0, used - limit))
            release_at = events[release_index].recorded_at + timedelta(seconds=window)
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
    ) -> Optional[PolicyCheck]:
        window = self._window_seconds(effective)
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
        events = sorted(
            self._store.events_in_window(
                persona_id=persona_id, kind=EventKind.SENT, since=since
            ),
            key=lambda e: e.recorded_at,
        )
        used = sum(e.tokens_in + e.tokens_out for e in events)
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
        self, persona_id: str, now: datetime, effective: Optional[EffectiveLimits]
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
        events = self._store.events_in_window(
            persona_id=persona_id,
            kind=EventKind.SENT,
            since=now - timedelta(seconds=min_seconds),
        )
        if not events:
            return PolicyCheck(
                key="min_interval",
                layer=layer,
                allowed=True,
                limit=min_seconds,
                source_id=source_id,
            )
        latest = max(events, key=lambda e: e.recorded_at)
        release_at = latest.recorded_at + timedelta(seconds=min_seconds)
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

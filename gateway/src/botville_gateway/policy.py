# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""发言策略引擎：平台级消息限速 + 深夜静默。

本骨架只实现 **平台层**（deployment 级）两项规则，因为 B2.1 的三层限额
（platform / owner / peer）尚未开工。`PolicyCheck.layer` 与 `source_id`
已按契约带出，接入 owner/peer 层时只需补充检查项，无需改数据结构。

限速算法：**滑动窗口（sliding window）**。
  窗口 = [now - window_seconds, now] 内 `kind=sent` 的事件数。
  不按固定边界重置，因此不会出现「整点前一秒狂发、整点后又能狂发」的
  边界双发问题。`reset_at` 取窗口内最早一条事件的过期时刻，即「下一个
  名额何时释放」。

静默算法：按 IANA 时区把 `now` 转到本地时间，用分钟数判断是否落在
`[start, end)` 内；`start > end` 表示跨午夜。
  - `allow_deferred=true`  → decision=defer（服务端排程，静默结束投递）
  - `allow_deferred=false` → decision=block
  两者都是 `retryable: false` 且 `same_content_allowed: true`。

优先级：**限速 block 优于静默 defer**。一条注定超限的消息延后到静默结束
仍然发不出去，直接 block 更诚实；所有未通过的检查项都会出现在 `checks` 里。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import Config
from .store import EventKind, UsageStore
from .timeutil import iso_utc, seconds_until, utcnow

LAYER_PLATFORM = "platform"
SOURCE_PLATFORM = "platform_default"

# ---- 原因码（对外稳定标识；只增不改） ----
CODE_RATE_LIMIT = "messages.window.exceeded"
CODE_SILENT_HOURS = "silent_hours.active"

DECISION_ALLOW = "allow"
DECISION_DEFER = "defer"
DECISION_BLOCK = "block"


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


@dataclass(frozen=True)
class _RateLimitView:
    enabled: bool
    allowed: bool
    limit: int
    used: int
    remaining: int
    reset_at: datetime


@dataclass(frozen=True)
class _SilentHoursView:
    active: bool
    ends_at: Optional[datetime]


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（≈4 字符/token，中文按字算更保守取 max）。

    仅供预算展示；B1.1 不做 token 限额校验。
    """
    if not text:
        return 0
    return max(1, len(text) // 4 + 1)


class PolicyEngine:
    def __init__(self, config: Config, store: UsageStore, *, now_fn=utcnow) -> None:
        self._config = config
        self._store = store
        self._now = now_fn
        self._tz = ZoneInfo(config.silent_hours_timezone)

    # ------------------------------------------------------------------ API
    def evaluate(
        self,
        *,
        persona_id: str,
        conversation_id: Optional[str] = None,
        text: str = "",
        token_estimate: Optional[int] = None,
        allow_deferred: bool = True,
        kind: str = "message",
        now: Optional[datetime] = None,
    ) -> PolicyDecision:
        now = now or self._now()
        estimated = token_estimate if token_estimate is not None else estimate_tokens(text)

        checks: List[PolicyCheck] = []
        reasons: List[PolicyReason] = []

        rate = self._rate_view(persona_id, now)
        if rate.enabled:
            checks.append(
                PolicyCheck(
                    key="messages.window",
                    layer=LAYER_PLATFORM,
                    allowed=rate.allowed,
                    limit=rate.limit,
                    used=rate.used,
                    remaining=rate.remaining,
                    reset_at=iso_utc(rate.reset_at),
                    code=None if rate.allowed else CODE_RATE_LIMIT,
                    source_id=SOURCE_PLATFORM,
                )
            )
        else:
            # 平台限速被关闭（RATE_LIMIT_MESSAGES=0）：只报告「未限制」，不给数字。
            checks.append(
                PolicyCheck(
                    key="messages.window",
                    layer=LAYER_PLATFORM,
                    allowed=True,
                    source_id=SOURCE_PLATFORM,
                )
            )
        if not rate.allowed:
            reasons.append(
                PolicyReason(
                    code=CODE_RATE_LIMIT,
                    message=(
                        f"Platform message window reached "
                        f"({rate.limit} per {self._config.rate_limit_window_seconds}s); "
                        f"a slot frees at {iso_utc(rate.reset_at)}."
                    ),
                    layer=LAYER_PLATFORM,
                )
            )

        silent = self._silent_view(now)
        checks.append(
            PolicyCheck(
                key="silent_hours",
                layer=LAYER_PLATFORM,
                allowed=not silent.active,
                reset_at=iso_utc(silent.ends_at) if silent.ends_at else None,
                code=CODE_SILENT_HOURS if silent.active else None,
                source_id=SOURCE_PLATFORM,
            )
        )
        if silent.active and silent.ends_at is not None:
            reasons.append(
                PolicyReason(
                    code=CODE_SILENT_HOURS,
                    message=(
                        f"Quiet hours are active until {self._config.silent_hours_end} "
                        f"{self._config.silent_hours_timezone} "
                        f"({iso_utc(silent.ends_at)})."
                    ),
                    layer=LAYER_PLATFORM,
                )
            )

        return self._resolve(
            now=now,
            estimated=estimated,
            rate=rate,
            silent=silent,
            allow_deferred=allow_deferred,
            reasons=tuple(reasons),
            checks=tuple(checks),
        )

    # ------------------------------------------------------- decision assembly
    def _resolve(
        self,
        *,
        now: datetime,
        estimated: int,
        rate: _RateLimitView,
        silent: _SilentHoursView,
        allow_deferred: bool,
        reasons: Tuple[PolicyReason, ...],
        checks: Tuple[PolicyCheck, ...],
    ) -> PolicyDecision:
        if not rate.allowed:
            # 限速优先：即使处于静默时段也直接 block（见模块文档）。
            return PolicyDecision(
                decision=DECISION_BLOCK,
                allowed=False,
                evaluated_at=iso_utc(now),
                winner_layer=LAYER_PLATFORM,
                retryable=False,
                retry_after_seconds=None,
                estimated_tokens=estimated,
                reasons=reasons,
                checks=checks,
                may_clear_at=iso_utc(rate.reset_at),
                same_content_allowed=True,
            )

        if silent.active and silent.ends_at is not None:
            if allow_deferred:
                return PolicyDecision(
                    decision=DECISION_DEFER,
                    allowed=False,
                    evaluated_at=iso_utc(now),
                    winner_layer=LAYER_PLATFORM,
                    retryable=False,
                    retry_after_seconds=seconds_until(silent.ends_at, now=now),
                    estimated_tokens=estimated,
                    reasons=reasons,
                    checks=checks,
                    may_clear_at=iso_utc(silent.ends_at),
                    same_content_allowed=True,
                )
            return PolicyDecision(
                decision=DECISION_BLOCK,
                allowed=False,
                evaluated_at=iso_utc(now),
                winner_layer=LAYER_PLATFORM,
                retryable=False,
                retry_after_seconds=None,
                estimated_tokens=estimated,
                reasons=reasons,
                checks=checks,
                may_clear_at=iso_utc(silent.ends_at),
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
    def _rate_view(self, persona_id: str, now: datetime) -> _RateLimitView:
        limit = self._config.rate_limit_messages
        window = self._config.rate_limit_window_seconds
        # limit == 0 视为关闭平台限速（见 README 配置表）。
        if limit <= 0:
            return _RateLimitView(
                enabled=False,
                allowed=True,
                limit=0,
                used=0,
                remaining=0,
                reset_at=now + timedelta(seconds=window),
            )

        since = now - timedelta(seconds=window)
        events = self._store.events_in_window(
            persona_id=persona_id, kind=EventKind.SENT, since=since
        )
        used = len(events)
        allowed = used < limit
        remaining = max(0, limit - used)

        if events:
            earliest = min(e.recorded_at for e in events)
            reset_at = earliest + timedelta(seconds=window)
        else:
            reset_at = since + timedelta(seconds=window)
        return _RateLimitView(
            enabled=True,
            allowed=allowed,
            limit=limit,
            used=used,
            remaining=remaining,
            reset_at=reset_at,
        )

    # ---------------------------------------------------------- silent hours
    def _silent_view(self, now: datetime) -> _SilentHoursView:
        cfg = self._config
        if not cfg.silent_hours_enabled:
            return _SilentHoursView(active=False, ends_at=None)

        start = cfg.silent_start_minute
        end = cfg.silent_end_minute
        if start == end:
            # 零长度窗口 = 不静默（文档化行为，避免「全天静默」的意外）。
            return _SilentHoursView(active=False, ends_at=None)

        local_now = now.astimezone(self._tz)
        minute_of_day = local_now.hour * 60 + local_now.minute

        if start < end:
            active = start <= minute_of_day < end
        else:  # 跨午夜
            active = minute_of_day >= start or minute_of_day < end
        if not active:
            return _SilentHoursView(active=False, ends_at=None)

        return _SilentHoursView(active=True, ends_at=self._next_local_time(local_now, end))

    def _next_local_time(self, local_now: datetime, minute_of_day: int) -> datetime:
        """本次静默结束的 UTC 时刻（严格晚于 `local_now`）。

        用本地墙钟时间构造再回 UTC。注意：DST 跳变日的墙钟时间可能不存在或
        重复，`ZoneInfo` 会按 PEP 495 折衷解释；Asia/Shanghai 无 DST，主流时区
        一年最多偏差一小时，对本功能可接受。
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

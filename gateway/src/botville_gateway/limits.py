# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""三层限额（platform / owner / peer）：规则校验、存储、逐项取最严。

契约：`LimitRules` / `LimitPolicy` / `LimitLayer` / `EffectiveLimit` / `EffectiveLimits`
（`openapi/gateway.v1.yaml`），规则介绍见 `docs/contract.md` §6.1。

三条不变量：

1. **逐项取最严**，不是整层覆盖：消息数可能由 owner 胜出，token 可能由 platform
   胜出。每一项的 `source_layer` 单独记录。同值时按 platform → owner → peer 的
   顺序归属（平台底线优先，结果稳定且可复现）。

   **窗口类限额按「速率对」比较（B1）**：`messages_per_window` 与
   `tokens_per_window` 各自和它的 `window_seconds` 是一个整体——比较的是
   `count / window`（交叉相乘，整数运算），胜出层**成对采用**自己的 count 与
   window；该层省略 `window_seconds` 时回退平台窗口。`window_seconds` 本身
   **不再单独取 min**：否则单写一个 `window_seconds: 60` 就能把平台
   `2 条/3600s` 放大成 `2 条/60s`（评审 B1）。生效结果里分别记录
   `messages.window_seconds` / `tokens.window_seconds`，因为两个速率对的胜出层
   可能不同、窗口也就可能不同。
2. **平台层是底线，只读**：`PUT /limits` 只能写 owner / peer；owner/peer 的任何
   一项比平台层更宽松 → 422 `limit.out_of_range`。窗口类按速率对校验：
   `count/window` 不得大于平台的 `count/window`，**只缩短窗口**同样算放宽。
3. **0 就是 0**：`messages_per_window: 0` 表示一条都不允许，不是「不限」。
   「该层不额外限制」用**省略字段**表达（契约 LimitRules 描述）。

`silent_hours` 不是数值比较：只要有任一层 `enabled: true` 就生效（平台可开启，
owner/peer 不得关闭平台已开启的静默），生效值取第一个开启层的窗口配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config, parse_hhmm
from .errors import LimitError, ValidationError
from .timeutil import iso_utc

LAYER_PLATFORM = "platform"
LAYER_OWNER = "owner"
LAYER_PEER = "peer"
LAYERS = (LAYER_PLATFORM, LAYER_OWNER, LAYER_PEER)

SCOPE_PERSONA = "persona"
SCOPE_CONVERSATION = "conversation"
SCOPE_PEER = "peer"
SCOPES = (SCOPE_PERSONA, SCOPE_CONVERSATION, SCOPE_PEER)

PLATFORM_SCOPE_ID = "platform_default"

CODE_OUT_OF_RANGE = "limit.out_of_range"

# key → (unit, 方向)。方向 min = 数值越小越严；max = 数值越大越严。
# `window_seconds` **不在**这里：它不与任何计数独立比较，而是作为速率对的
# 一部分（见 `RATE_PAIRS`）。单独取 min 会让「2 条/3600s」被一个
# `window_seconds: 60` 放大成「2 条/60s」（评审 B1）。
# messages_per_window / tokens_per_window **不在** SCALAR_RULES：它们只走
# RATE_PAIRS（校验与 effective[]），否则会出现重复 key、source_layer 自相矛盾，
# 且 {count↑, window↑} 合法更严速率会被旧标量规则误 422（复审 B1-残）。
SCALAR_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("proactive_per_day", "count", "min"),
    ("town_tokens_per_day", "tokens", "min"),
    ("max_message_chars", "chars", "min"),
    ("min_seconds_between_messages", "seconds", "max"),
)

# 速率对：每个计数项与其窗口是一个整体，按 `count / window` 比较（交叉相乘）。
# 胜出层成对采用自己的 `count` + `window`；该层省略 `window_seconds` 时回退平台窗口。
RATE_PAIRS: Tuple[Tuple[str, str, str], ...] = (
    ("messages_per_window", "messages", "count"),
    ("tokens_per_window", "tokens", "tokens"),
)
_WINDOW_KEY = "window_seconds"

# key → (minimum, maximum or None)
_SCALAR_RANGES: Dict[str, Tuple[int, Optional[int]]] = {
    "window_seconds": (60, 2_592_000),
    "messages_per_window": (0, None),
    "tokens_per_window": (0, None),
    "proactive_per_day": (0, None),
    "town_tokens_per_day": (0, None),
    "max_message_chars": (1, 8000),
    "min_seconds_between_messages": (0, 86_400),
}

_ANTI_SPAM_RANGES: Dict[str, Tuple[int, Optional[int]]] = {
    "max_consecutive_ai_messages": (1, 50),
    "cooldown_seconds": (0, 86_400),
}
_AI_LOOP_RANGES: Dict[str, Tuple[int, Optional[int]]] = {
    "max_ai_messages_per_hour": (0, 1000),
}
_AI_LOOP_ON_BREAK = frozenset({"stop", "notify"})

_RULES_FIELDS = frozenset(
    {name for name, _, _ in SCALAR_RULES}
    | {count_key for count_key, _, _ in RATE_PAIRS}
    | {_WINDOW_KEY, "silent_hours", "anti_spam", "ai_loop"}
)
_SILENT_FIELDS = frozenset({"enabled", "start", "end", "timezone"})
_POLICY_FIELDS = frozenset({"layer", "scope", "scope_id", "rules"})


def _range_error(field_path: str, message: str) -> LimitError:
    return LimitError(
        CODE_OUT_OF_RANGE,
        message,
        errors=[{"field": field_path, "code": "out_of_range", "message": message}],
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_int(
    field_path: str, value: Any, minimum: int, maximum: Optional[int]
) -> int:
    if not _is_int(value):
        raise ValidationError(
            "request.invalid", f"{field_path} must be an integer", field_path
        )
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise _range_error(field_path, f"{field_path} must be {bound}, got {value}")
    return value


def _silent_is_effective(silent: Any) -> bool:
    """该层的 silent_hours 是否真的会静默（#114-7.3）。

    `enabled: true` 但 `start == end` 是**零长度窗口**，语义上等于「不静默」。
    平台层这样配置时不能把更严格的 owner 层窗口一起挡掉：否则平台一个
    `23:00–23:00` 的配置事故会悄悄关掉所有 owner 的静默设置。
    """
    if not isinstance(silent, dict) or not silent.get("enabled"):
        return False
    try:
        return parse_hhmm(silent["start"]) != parse_hhmm(silent["end"])
    except Exception:  # ConfigError / KeyError / TypeError：视为不可生效
        return False


def validate_silent_hours(value: Any, field_path: str = "rules.silent_hours") -> Dict[str, Any]:
    """校验并规范化 `SilentHours`。省略 enabled 时按契约默认 true。"""
    if not isinstance(value, dict):
        raise ValidationError("request.invalid", f"{field_path} must be an object", field_path)
    unknown = set(value) - _SILENT_FIELDS
    if unknown:
        raise ValidationError(
            "request.invalid",
            f"unexpected {field_path} fields: {', '.join(sorted(unknown))}",
            field_path,
        )
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValidationError(
            "request.invalid", f"{field_path}.enabled must be a boolean", field_path
        )
    out: Dict[str, Any] = {"enabled": enabled}
    for name in ("start", "end", "timezone"):
        raw = value.get(name)
        if raw is None:
            raise ValidationError(
                "request.invalid", f"{field_path}.{name} is required", f"{field_path}.{name}"
            )
        if not isinstance(raw, str):
            raise ValidationError(
                "request.invalid", f"{field_path}.{name} must be a string", field_path
            )
        out[name] = raw
    # HH:MM 格式在 parse_hhmm 里校验（抛 ConfigError，转成 422 更贴切）。
    # 逐字段校验：错误必须指向真正出错的那个字段（#114-7.8a），否则调用方
    # 按 errors[].field 定位时会去改没问题的 start。
    for name in ("start", "end"):
        try:
            parse_hhmm(out[name])
        except Exception as exc:  # ConfigError
            raise _range_error(f"{field_path}.{name}", str(exc)) from exc
    try:
        ZoneInfo(out["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _range_error(
            f"{field_path}.timezone", f"unknown timezone {out['timezone']!r}"
        ) from exc
    return out


def validate_rules(raw: Any) -> Dict[str, Any]:
    """校验 `LimitRules`（严格拒绝未知字段），返回只含出现字段的规范化 dict。"""
    if not isinstance(raw, dict):
        raise ValidationError("request.invalid", "'rules' must be an object", "rules")
    unknown = set(raw) - _RULES_FIELDS
    if unknown:
        raise ValidationError(
            "request.invalid",
            f"unexpected rules fields: {', '.join(sorted(unknown))}",
            "rules",
        )
    out: Dict[str, Any] = {}
    for name, _, _ in SCALAR_RULES:
        if name in raw:
            minimum, maximum = _SCALAR_RANGES[name]
            out[name] = _check_int(f"rules.{name}", raw[name], minimum, maximum)
    # 速率对计数项：校验范围，但不进 SCALAR_RULES 的独立取严。
    for count_key, _, _ in RATE_PAIRS:
        if count_key in raw:
            minimum, maximum = _SCALAR_RANGES[count_key]
            out[count_key] = _check_int(
                f"rules.{count_key}", raw[count_key], minimum, maximum
            )
    if _WINDOW_KEY in raw:
        # #114-4：window_seconds 只能与 messages_per_window 或 tokens_per_window 成对出现。
        # 单独写 window_seconds 不生效且语义不合法，422 limit.out_of_range。
        has_count = any(count_key in raw for count_key, _, _ in RATE_PAIRS)
        if not has_count:
            raise _range_error(
                f"rules.{_WINDOW_KEY}",
                f"rules.{_WINDOW_KEY} must be paired with messages_per_window or tokens_per_window; "
                "writing window_seconds alone has no effect and is rejected to prevent silent misconfiguration",
            )
        # 仍然接受该字段（契约声明了它），只是不再独立参与「取最严」。
        minimum, maximum = _SCALAR_RANGES[_WINDOW_KEY]
        out[_WINDOW_KEY] = _check_int(
            f"rules.{_WINDOW_KEY}", raw[_WINDOW_KEY], minimum, maximum
        )

    if "silent_hours" in raw:
        out["silent_hours"] = validate_silent_hours(raw["silent_hours"])

    if "anti_spam" in raw:
        block = raw["anti_spam"]
        if not isinstance(block, dict):
            raise ValidationError("request.invalid", "'rules.anti_spam' must be an object", "rules.anti_spam")
        unknown = set(block) - set(_ANTI_SPAM_RANGES)
        if unknown:
            raise ValidationError(
                "request.invalid",
                f"unexpected rules.anti_spam fields: {', '.join(sorted(unknown))}",
                "rules.anti_spam",
            )
        normalized: Dict[str, Any] = {}
        for name, (minimum, maximum) in _ANTI_SPAM_RANGES.items():
            if name in block:
                normalized[name] = _check_int(
                    f"rules.anti_spam.{name}", block[name], minimum, maximum
                )
        out["anti_spam"] = normalized

    if "ai_loop" in raw:
        block = raw["ai_loop"]
        if not isinstance(block, dict):
            raise ValidationError("request.invalid", "'rules.ai_loop' must be an object", "rules.ai_loop")
        unknown = set(block) - set(_AI_LOOP_RANGES) - {"on_break"}
        if unknown:
            raise ValidationError(
                "request.invalid",
                f"unexpected rules.ai_loop fields: {', '.join(sorted(unknown))}",
                "rules.ai_loop",
            )
        normalized = {}
        for name, (minimum, maximum) in _AI_LOOP_RANGES.items():
            if name in block:
                normalized[name] = _check_int(
                    f"rules.ai_loop.{name}", block[name], minimum, maximum
                )
        if "on_break" in block:
            on_break = block["on_break"]
            if on_break not in _AI_LOOP_ON_BREAK:
                raise _range_error(
                    "rules.ai_loop.on_break",
                    f"rules.ai_loop.on_break must be one of {sorted(_AI_LOOP_ON_BREAK)}",
                )
            normalized["on_break"] = on_break
        out["ai_loop"] = normalized
    return out


# --------------------------------------------------------------------------- #
# 层存储
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LimitLayer:
    layer: str
    scope: str
    scope_id: str
    rules: Dict[str, Any]
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "layer": self.layer,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "rules": _copy_rules(self.rules),
        }
        if self.updated_at is not None:
            body["updated_at"] = iso_utc(self.updated_at)
        return body


def _layer_rate(
    layer: LimitLayer, count_key: str, fallback_window: int
) -> Optional[Tuple[int, int]]:
    """该层的 `(count, window)`；该层没写该计数项时返回 None。

    `window_seconds` 省略时回退 `fallback_window`（通常是平台窗口）——这正是
    「窗口与计数成对」的含义：窗口只对同一层写下的计数项生效。
    """
    count = layer.rules.get(count_key)
    if count is None:
        return None
    window = layer.rules.get(_WINDOW_KEY, fallback_window)
    return int(count), int(window)


def rate_is_looser(
    count: int, window: int, floor_count: int, floor_window: int
) -> bool:
    """`count/window` 是否比 `floor_count/floor_window` 更宽松（交叉相乘，无浮点）。"""
    return count * floor_window > floor_count * window


def _copy_rules(rules: Dict[str, Any]) -> Dict[str, Any]:
    """浅拷贝 + 嵌套 dict 拷贝，避免调用方改坏存储里的规则。"""
    out: Dict[str, Any] = {}
    for key, value in rules.items():
        out[key] = dict(value) if isinstance(value, dict) else value
    return out


@dataclass(frozen=True)
class EffectiveValue:
    key: str
    value: Any
    unit: str
    source_layer: str
    source_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "unit": self.unit,
            "source_layer": self.source_layer,
            "source_id": self.source_id,
        }


@dataclass(frozen=True)
class EffectiveLimits:
    """一次计算的结果（服务内部用；`to_dict()` 输出契约 `EffectiveLimits`）。"""

    scope: str
    scope_id: str
    generated_at: datetime
    values: Tuple[EffectiveValue, ...]
    layers: Tuple[LimitLayer, ...]

    def get(self, key: str) -> Optional[EffectiveValue]:
        for item in self.values:
            if item.key == key:
                return item
        return None

    def value(self, key: str, default: Any = None) -> Any:
        item = self.get(key)
        return default if item is None else item.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "generated_at": iso_utc(self.generated_at),
            "effective": [item.to_dict() for item in self.values],
            "layers": [layer.to_dict() for layer in self.layers],
        }


def _strictness_layers(cfg: Config) -> LimitLayer:
    """部署配置构成的平台底线层（只读）。

    配置里的 0 表示「平台不设该限额」（与 `RATE_LIMIT_MESSAGES=0` 的既有语义一致），
    因此不输出该键；API 写入的 0 则真的是「一条都不允许」。
    """
    rules: Dict[str, Any] = {"window_seconds": cfg.rate_limit_window_seconds}
    if cfg.rate_limit_messages > 0:
        rules["messages_per_window"] = cfg.rate_limit_messages
    if cfg.platform_token_budget > 0:
        rules["tokens_per_window"] = cfg.platform_token_budget
    if cfg.platform_proactive_per_day > 0:
        rules["proactive_per_day"] = cfg.platform_proactive_per_day
    rules["max_message_chars"] = cfg.max_message_chars
    rules["silent_hours"] = {
        "enabled": bool(cfg.silent_hours_enabled),
        "start": cfg.silent_hours_start,
        "end": cfg.silent_hours_end,
        "timezone": cfg.silent_hours_timezone,
    }
    # N7（契约 §6.3）：三个数值项 0 = 「平台不设该项」→ 省略键（与 RATE_LIMIT_MESSAGES=0 一致）。
    # 非 0 时写入平台层底线；on_break 始终写入（默认 stop，即平台底线）。
    anti_spam: Dict[str, Any] = {}
    if cfg.platform_max_consecutive_ai_messages != 0:
        anti_spam["max_consecutive_ai_messages"] = cfg.platform_max_consecutive_ai_messages
    if cfg.platform_anti_spam_cooldown_seconds != 0:
        anti_spam["cooldown_seconds"] = cfg.platform_anti_spam_cooldown_seconds
    if anti_spam:
        rules["anti_spam"] = anti_spam

    ai_loop: Dict[str, Any] = {"on_break": cfg.platform_ai_loop_on_break}
    if cfg.platform_max_ai_messages_per_hour != 0:
        ai_loop["max_ai_messages_per_hour"] = cfg.platform_max_ai_messages_per_hour
    rules["ai_loop"] = ai_loop
    return LimitLayer(
        layer=LAYER_PLATFORM,
        scope=SCOPE_PEER,
        scope_id=PLATFORM_SCOPE_ID,
        rules=rules,
    )


class LimitStore:
    """线程安全的三层限额存储（platform 只读，owner/peer 可写）。"""

    def __init__(self, config: Config) -> None:
        import threading

        self._config = config
        self._platform = _strictness_layers(config)
        self._layers: Dict[Tuple[str, str, str], LimitLayer] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ read
    @property
    def platform(self) -> LimitLayer:
        return self._platform

    def get_layer(self, layer: str, scope: str, scope_id: str) -> Optional[LimitLayer]:
        if layer == LAYER_PLATFORM:
            return self._platform
        with self._lock:
            return self._layers.get((layer, scope, scope_id))

    def layers_for(
        self, *, persona_id: Optional[str], conversation_id: Optional[str], peer_id: Optional[str]
    ) -> List[LimitLayer]:
        """返回参与本次计算的所有层（platform 恒在首位）。"""
        found: List[LimitLayer] = [self._platform]
        with self._lock:
            if persona_id:
                layer = self._layers.get((LAYER_OWNER, SCOPE_PERSONA, persona_id))
                if layer is not None:
                    found.append(layer)
            if conversation_id:
                layer = self._layers.get((LAYER_OWNER, SCOPE_CONVERSATION, conversation_id))
                if layer is not None:
                    found.append(layer)
            if peer_id:
                layer = self._layers.get((LAYER_PEER, SCOPE_PEER, peer_id))
                if layer is not None:
                    found.append(layer)
        return found

    # ----------------------------------------------------------------- write
    def put(self, payload: Any, *, now: datetime) -> LimitLayer:
        """校验并写入 owner / peer 层，返回写入后的层。"""
        if not isinstance(payload, dict):
            raise ValidationError("request.invalid", "request body must be an object")
        unknown = set(payload) - _POLICY_FIELDS
        if unknown:
            raise ValidationError(
                "request.invalid", f"unexpected fields: {', '.join(sorted(unknown))}"
            )
        for name in ("layer", "scope", "scope_id", "rules"):
            if name not in payload:
                raise ValidationError(
                    "request.invalid", f"'{name}' is required", name
                )
        layer = payload["layer"]
        if layer == LAYER_PLATFORM:
            raise ValidationError(
                "request.invalid",
                "the platform layer is read-only; set it through deployment configuration",
                "layer",
            )
        if layer not in (LAYER_OWNER, LAYER_PEER):
            raise ValidationError(
                "request.invalid", f"'layer' must be one of ['owner', 'peer'], got {layer!r}", "layer"
            )
        scope = payload["scope"]
        if scope not in SCOPES:
            raise ValidationError(
                "request.invalid",
                f"'scope' must be one of {sorted(SCOPES)}, got {scope!r}",
                "scope",
            )
        if layer == LAYER_PEER and scope != SCOPE_PEER:
            raise _range_error("scope", "layer 'peer' requires scope 'peer'")
        if layer == LAYER_OWNER and scope == SCOPE_PEER:
            raise _range_error("scope", "layer 'owner' cannot use scope 'peer'")
        scope_id = payload["scope_id"]
        if not isinstance(scope_id, str) or not scope_id:
            raise ValidationError(
                "request.invalid", "'scope_id' must be a non-empty string", "scope_id"
            )
        rules = validate_rules(payload["rules"])
        self._check_not_looser(rules, scope=scope, scope_id=scope_id)

        stored = LimitLayer(
            layer=layer, scope=scope, scope_id=scope_id, rules=rules, updated_at=now
        )
        with self._lock:
            # 合并写：PUT 的语义是「该层的这一份规则」，因此整体替换该层规则。
            self._layers[(layer, scope, scope_id)] = stored
        return stored

    def _check_not_looser(self, rules: Dict[str, Any], *, scope: str, scope_id: str) -> None:
        """owner/peer 的任何一项都不得比平台层更宽松（422 limit.out_of_range）。

        窗口类（messages / tokens）按**速率对**校验：`count / window` 不得大于
        平台的 `count / window`。因此 `messages_per_window: 1` 配平台窗口是收紧，
        而只写 `window_seconds: 60`（计数沿用平台值、窗口变短）会被判为放宽。
        """
        platform_rules = self._platform.rules
        errors: List[Dict[str, Any]] = []

        # 窗口类：先算出该层两个速率对的窗口（缺省回退平台窗口）。
        platform_window = int(
            platform_rules.get(_WINDOW_KEY, self._config.rate_limit_window_seconds)
        )
        candidate_window = int(rules.get(_WINDOW_KEY, platform_window))
        for count_key, label, _unit in RATE_PAIRS:
            platform_count = platform_rules.get(count_key)
            if platform_count is None:
                continue
            # 如果 owner/peer 既没写 count_key 也没写 window_seconds → 没变化 → 跳过。
            if count_key not in rules and _WINDOW_KEY not in rules:
                continue
            # 否则，「缺省用平台值」：该层省略 count 时继承平台 count，
            # 省略 window 时继承平台 window（已在 candidate_window 里处理）。
            candidate_count = int(rules.get(count_key, platform_count))
            if rate_is_looser(candidate_count, candidate_window, platform_count, platform_window):
                errors.append(
                    {
                        "field": f"rules.{count_key}",
                        "code": "out_of_range",
                        "message": (
                            f"{label} rate {candidate_count}/{candidate_window}s is looser than "
                            f"the platform floor {platform_count}/{platform_window}s; "
                            f"the pair must not exceed it"
                        ),
                    }
                )

        for key, _, direction in SCALAR_RULES:
            if key not in rules or key not in platform_rules:
                continue
            candidate = rules[key]
            floor = platform_rules[key]
            looser = candidate > floor if direction == "min" else candidate < floor
            if looser:
                rel = "<=" if direction == "min" else ">="
                errors.append(
                    {
                        "field": f"rules.{key}",
                        "code": "out_of_range",
                        "message": (
                            f"{key}={candidate} is looser than the platform floor "
                            f"{floor}; it must be {rel} {floor}"
                        ),
                    }
                )
        # 嵌套规则：max_consecutive / ai_loop 越小越严，cooldown 越大越严。
        for key, sub, direction in (
            ("anti_spam", "max_consecutive_ai_messages", "min"),
            ("anti_spam", "cooldown_seconds", "max"),
            ("ai_loop", "max_ai_messages_per_hour", "min"),
        ):
            block = rules.get(key) or {}
            floor_block = platform_rules.get(key) or {}
            if sub not in block or sub not in floor_block:
                continue
            candidate, floor = block[sub], floor_block[sub]
            looser = candidate > floor if direction == "min" else candidate < floor
            if looser:
                rel = "<=" if direction == "min" else ">="
                errors.append(
                    {
                        "field": f"rules.{key}.{sub}",
                        "code": "out_of_range",
                        "message": (
                            f"{key}.{sub}={candidate} is looser than the platform floor "
                            f"{floor}; it must be {rel} {floor}"
                        ),
                    }
                )
        silent = rules.get("silent_hours")
        platform_silent = platform_rules.get("silent_hours") or {}
        if (
            isinstance(silent, dict)
            and silent.get("enabled") is False
            and _silent_is_effective(platform_silent)
        ):
            errors.append(
                {
                    "field": "rules.silent_hours.enabled",
                    "code": "out_of_range",
                    "message": "the platform layer enables silent hours; owner/peer cannot disable them",
                }
            )
        if errors:
            raise LimitError(
                CODE_OUT_OF_RANGE,
                "one or more rules are looser than the platform floor",
                errors=errors,
            )

    def any_peer_rules(self, persona_ids: List[str]) -> bool:
        """返回 persona_ids 中是否有任一 persona 配置了 peer 层规则（B2.2 §6.4）。

        用于校验请求是否需要强制携带 peer_id：会话成员中存在 peer 层规则 →
        未带 peer_id 的请求应返回 422 request.invalid。
        """
        with self._lock:
            for pid in persona_ids:
                if self._layers.get((LAYER_PEER, SCOPE_PEER, pid)) is not None:
                    return True
        return False

    # -------------------------------------------------------------- effective
    def effective(
        self,
        *,
        persona_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        peer_id: Optional[str] = None,
        now: datetime,
        include_platform: bool = True,
    ) -> EffectiveLimits:
        layers = self.layers_for(
            persona_id=persona_id, conversation_id=conversation_id, peer_id=peer_id
        )
        if conversation_id:
            scope, scope_id = SCOPE_CONVERSATION, conversation_id
        elif peer_id:
            scope, scope_id = SCOPE_PEER, peer_id
        elif persona_id:
            scope, scope_id = SCOPE_PERSONA, persona_id
        else:
            scope, scope_id = SCOPE_PEER, PLATFORM_SCOPE_ID

        values: List[EffectiveValue] = []

        platform_window = int(
            self._platform.rules.get(
                _WINDOW_KEY, self._config.rate_limit_window_seconds
            )
        )

        # 速率对：按 `count / window` 取最小 rate 的层，成对采用该层 count + window。
        # 同 rate 时保留先出现（更靠平台）的层，与其它标量的归属顺序一致。
        for count_key, label, unit in RATE_PAIRS:
            winner: Optional[Tuple[int, int, LimitLayer]] = None
            for layer in layers:
                pair = _layer_rate(layer, count_key, platform_window)
                if pair is None:
                    continue
                count, window = pair
                if winner is None or rate_is_looser(winner[0], winner[1], count, window):
                    # 候选层的 rate 比当前胜出者更严格 → 整对换层。
                    winner = (count, window, layer)
            if winner is None:
                continue
            count, window, layer = winner
            values.append(
                EffectiveValue(count_key, count, unit, layer.layer, layer.scope_id)
            )
            # 窗口与计数分开暴露：两个速率对的胜出层可能不同，窗口也可能不同。
            values.append(
                EffectiveValue(
                    f"{label}.{_WINDOW_KEY}", window, "seconds", layer.layer, layer.scope_id
                )
            )

        for key, unit, direction in SCALAR_RULES:
            winner = None
            for layer in layers:
                if key not in layer.rules:
                    continue
                candidate = layer.rules[key]
                if winner is None:
                    winner = EffectiveValue(key, candidate, unit, layer.layer, layer.scope_id)
                    continue
                if direction == "min" and candidate < winner.value:
                    winner = EffectiveValue(key, candidate, unit, layer.layer, layer.scope_id)
                elif direction == "max" and candidate > winner.value:
                    winner = EffectiveValue(key, candidate, unit, layer.layer, layer.scope_id)
            if winner is not None:
                values.append(winner)

        # 静默：任一层开启即生效；生效窗口取第一个**真正生效**的开启层
        # （enabled 但 start == end 的零长度窗口不算，见 `_silent_is_effective`）。
        silent_winner: Optional[EffectiveValue] = None
        for layer in layers:
            silent = layer.rules.get("silent_hours")
            if _silent_is_effective(silent):
                silent_winner = EffectiveValue(
                    "silent_hours", True, "boolean", layer.layer, layer.scope_id
                )
                break
        if silent_winner is None:
            for layer in layers:
                if isinstance(layer.rules.get("silent_hours"), dict):
                    silent_winner = EffectiveValue(
                        "silent_hours", False, "boolean", layer.layer, layer.scope_id
                    )
                    break
        if silent_winner is not None:
            values.append(silent_winner)

        # anti_spam / ai_loop：同样逐项取最严（B2.2 才真正执行，这里只计算生效值）。
        for key, sub, unit, direction in (
            ("anti_spam", "max_consecutive_ai_messages", "count", "min"),
            ("anti_spam", "cooldown_seconds", "seconds", "max"),
            ("ai_loop", "max_ai_messages_per_hour", "count", "min"),
        ):
            winner = None
            for layer in layers:
                block = layer.rules.get(key)
                if not isinstance(block, dict) or sub not in block:
                    continue
                candidate = block[sub]
                if winner is None or (
                    direction == "min" and candidate < winner.value
                ) or (direction == "max" and candidate > winner.value):
                    winner = EffectiveValue(
                        f"{key}.{sub}", candidate, unit, layer.layer, layer.scope_id
                    )
            if winner is not None:
                values.append(winner)

        returned = layers if include_platform else [
            layer for layer in layers if layer.layer != LAYER_PLATFORM
        ]
        return EffectiveLimits(
            scope=scope,
            scope_id=scope_id,
            generated_at=now,
            values=tuple(values),
            layers=tuple(returned),
        )

    def silent_window(self, effective: EffectiveLimits) -> Dict[str, Any]:
        """生效静默窗口的完整配置（`effective` 里只暴露布尔值）。"""
        winner_layer, winner_scope = None, None
        for layer in effective.layers:
            silent = layer.rules.get("silent_hours")
            if _silent_is_effective(silent):
                winner_layer, winner_scope = layer, silent
                break
        if winner_layer is None:
            return {"enabled": False, "start": "00:00", "end": "00:00", "timezone": "UTC"}
        merged = dict(winner_scope)
        merged["enabled"] = True
        return merged

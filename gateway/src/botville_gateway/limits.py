# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""三层限额（platform / owner / peer）：规则校验、存储、逐项取最严。

契约：`LimitRules` / `LimitPolicy` / `LimitLayer` / `EffectiveLimit` / `EffectiveLimits`
（`openapi/gateway.v1.yaml`），规则介绍见 `docs/contract.md` §6.1。

三条不变量：

1. **逐项取最严**，不是整层覆盖：消息数可能由 owner 胜出，token 可能由 platform
   胜出。每一项的 `source_layer` 单独记录。同值时按 platform → owner → peer 的
   顺序归属（平台底线优先，结果稳定且可复现）。
2. **平台层是底线，只读**：`PUT /limits` 只能写 owner / peer；owner/peer 的任何
   一项比平台层更宽松 → 422 `limit.out_of_range`。
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
SCALAR_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("window_seconds", "seconds", "min"),
    ("messages_per_window", "count", "min"),
    ("tokens_per_window", "tokens", "min"),
    ("proactive_per_day", "count", "min"),
    ("town_tokens_per_day", "tokens", "min"),
    ("max_message_chars", "chars", "min"),
    ("min_seconds_between_messages", "seconds", "max"),
)

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
    | {"silent_hours", "anti_spam", "ai_loop"}
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
    # HH:MM 格式在 parse_hhmm 里校验（抛 ConfigError，转成 422 更贴切）
    try:
        parse_hhmm(out["start"])
        parse_hhmm(out["end"])
    except Exception as exc:  # ConfigError
        raise _range_error(f"{field_path}.start", str(exc)) from exc
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
        """owner/peer 的任何一项都不得比平台层更宽松（422 limit.out_of_range）。"""
        platform_rules = self._platform.rules
        errors: List[Dict[str, Any]] = []
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
            and platform_silent.get("enabled")
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
        for key, unit, direction in SCALAR_RULES:
            winner: Optional[EffectiveValue] = None
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

        # 静默：任一层开启即生效；生效窗口取第一个开启层。
        silent_winner: Optional[EffectiveValue] = None
        for layer in layers:
            silent = layer.rules.get("silent_hours")
            if isinstance(silent, dict) and silent.get("enabled"):
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
            if isinstance(silent, dict) and silent.get("enabled"):
                winner_layer, winner_scope = layer, silent
                break
        if winner_layer is None:
            return {"enabled": False, "start": "00:00", "end": "00:00", "timezone": "UTC"}
        merged = dict(winner_scope)
        merged["enabled"] = True
        return merged

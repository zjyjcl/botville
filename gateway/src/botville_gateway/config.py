# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""环境变量配置。

所有值都有安全默认值；生产部署通过环境变量覆盖。这里不读取 .env 文件本身
（避免引入依赖），由部署侧（systemd / docker compose / shell）注入环境变量。
`.env.example` 只列占位值。
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GATEWAY_VERSION = "0.1.0"
CONTRACT_VERSION = "0.1.0"

# Token values that mean "no auth configured"; starting with these is unsafe
# unless GATEWAY_DEV_MODE=true is explicitly set.
_INSECURE_TOKENS: frozenset = frozenset({"", "<change-me>"})

# dev 模式（免鉴权）只允许绑定这些本机地址，否则等于把无鉴权网关暴露到网络上。
LOOPBACK_HOSTS: frozenset = frozenset({"127.0.0.1", "::1", "localhost"})

# Matrix 传输选择（A1.3）：memory = 内存假后端（B1.1 现状），appservice = 以
# 部署层注册的 application service 身份代发（真实 homeserver）。
MATRIX_TRANSPORTS = frozenset({"memory", "appservice"})

# conversation 房间别名 localpart 前缀（GATEWAY_ROOM_ALIAS_PREFIX 可覆盖）。
DEFAULT_ROOM_ALIAS_PREFIX = "botville_conv_"
# 别名 localpart 的安全字符集（与 persona/conversation id 一致）。
_ROOM_ALIAS_PREFIX_RE = re.compile(r"^[a-z0-9._=-]{1,64}$")

# appservice 模式下 as_token 的最低强度（长度下限；且不得含任何空白）。
AS_TOKEN_MIN_LENGTH = 32

# 明文 http:// 的 homeserver 只允许这些主机（或无点的单标签容器名，如
# compose 网络里的 synapse/tuwunel）；其余必须 https 或显式放开。
_LOCAL_HOMESERVER_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

DEFAULT_SILENT_START = "23:00"
DEFAULT_SILENT_END = "07:30"
DEFAULT_TIMEZONE = "Asia/Shanghai"

# 平台层 token 预算默认值（窗口由 RATE_LIMIT_WINDOW_SECONDS 决定）。
# 与 RATE_LIMIT_MESSAGES 一致：配置里的 0 表示「平台不设该限额」，
# 而 `/limits` API 里的 0 表示「一个都不允许」（契约规则语义）。
DEFAULT_TOKEN_BUDGET = 200_000
DEFAULT_PROACTIVE_PER_DAY = 6
DEFAULT_MAX_USAGE_EVENTS = 10_000
DEFAULT_MAX_IDEMPOTENCY_ENTRIES = 2_000
DEFAULT_IDEMPOTENCY_TTL = 86_400
DEFAULT_MAX_QUEUE_ENTRIES = 1_000


class ConfigError(ValueError):
    """配置值非法（启动时应直接失败，而不是带病运行）。"""


def _env_int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _env_str(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip()


_DNS_LABEL_RE = re.compile(r"^[0-9a-z]([0-9a-z-]*[0-9a-z])?$")
_IPV6_LITERAL_RE = re.compile(r"^\[[0-9a-f:.]+\]$")


def normalize_matrix_server_name(name: str) -> str:
    """校验 Matrix server_name（主机名或 IP，可带 `:端口`）并归一为小写。

    Matrix server_name 大小写不敏感，homeserver 侧一律小写（Synapse 自己
    会 lower）；这里归一成小写，避免网关与 HS 两侧的 @ai_* 命名空间因大小
    写漂移对不上。形状非法抛 ConfigError。
    """
    lowered = name.strip().lower()

    def _bad() -> None:
        raise ConfigError(
            f"matrix server name must be a hostname or IP, optionally with :port, "
            f"got {name!r}"
        )

    port: Optional[int] = None
    host = lowered
    if lowered.startswith("["):
        end = lowered.find("]")
        if end < 0:
            _bad()
        host = lowered[: end + 1]
        rest = lowered[end + 1:]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                _bad()
            port = int(rest[1:])
        if not _IPV6_LITERAL_RE.fullmatch(host):
            _bad()
    else:
        left, sep, maybe_port = lowered.rpartition(":")
        if sep:
            host = left
            if not maybe_port.isdigit():
                _bad()
            port = int(maybe_port)
        if not host or len(host) > 253:
            _bad()
        for label in host.split("."):
            # 同时覆盖 DNS 标签与 IPv4 段（纯数字段也是合法标签）。
            if not label or len(label) > 63 or not _DNS_LABEL_RE.fullmatch(label):
                _bad()
    if port is not None and not 1 <= port <= 65535:
        _bad()
    return lowered


def _env_int_range(env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = _env_int(env, name, default, minimum=minimum)
    if value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def parse_hhmm(value: str) -> int:
    """把 `HH:MM` 解析为自午夜起的分钟数。格式非法时抛 ConfigError。"""
    parts = value.split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        raise ConfigError(f"time must be HH:MM, got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ConfigError(f"time must be HH:MM, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"time out of range, got {value!r}")
    return hour * 60 + minute


@dataclass(frozen=True)
class Config:
    """网关运行配置（不可变，便于测试构造）。"""

    host: str = "127.0.0.1"
    port: int = 8080
    auth_token: Optional[str] = None
    dev_mode: bool = False
    rate_limit_messages: int = 60
    rate_limit_window_seconds: int = 3600
    silent_hours_enabled: bool = True
    silent_hours_start: str = DEFAULT_SILENT_START
    silent_hours_end: str = DEFAULT_SILENT_END
    silent_hours_timezone: str = DEFAULT_TIMEZONE
    max_message_chars: int = 8000
    access_log: bool = False
    version: str = GATEWAY_VERSION
    contract_version: str = CONTRACT_VERSION
    # Matrix 传输（A1.3）。memory 保持 B1.1 现状；appservice 需要下面的值。
    matrix_transport: str = "memory"
    homeserver_url: Optional[str] = None
    matrix_server_name: Optional[str] = None
    # as_token 等同 @ai_* 命名空间管理员凭据：不进 repr（日志/断言消息不泄露）。
    as_token: Optional[str] = field(default=None, repr=False)
    # conversation → 房间别名 localpart 的前缀（#<prefix><conversation_id>:<server>）。
    room_alias_prefix: str = DEFAULT_ROOM_ALIAS_PREFIX
    # 显式放行明文 http:// 的非本机 homeserver（跨主机部署应使用 https）。
    allow_insecure_homeserver: bool = False
    # B2.1：平台层 token 预算（0 = 不限）
    platform_token_budget: int = DEFAULT_TOKEN_BUDGET
    # B2.1：平台层主动发言/日上限（0 = 不限）
    platform_proactive_per_day: int = DEFAULT_PROACTIVE_PER_DAY
    # B2.1：内存容量上限（容量达到上限时按 LRU 淘汰）
    max_usage_events: int = DEFAULT_MAX_USAGE_EVENTS
    max_idempotency_entries: int = DEFAULT_MAX_IDEMPOTENCY_ENTRIES
    idempotency_ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL
    max_queue_entries: int = DEFAULT_MAX_QUEUE_ENTRIES

    def __post_init__(self) -> None:
        # 构造时校验，避免明显错误的配置拖到第一次请求才炸。
        parse_hhmm(self.silent_hours_start)
        parse_hhmm(self.silent_hours_end)
        if self.port < 0 or self.port > 65535:
            # 0 是合法值（测试用随机端口）；`load_config` 拒绝环境里的 0。
            raise ConfigError(f"port must be 0–65535, got {self.port}")
        if self.rate_limit_messages < 0:
            raise ConfigError("rate_limit_messages must be >= 0")
        if self.rate_limit_window_seconds < 1:
            raise ConfigError("rate_limit_window_seconds must be >= 1")
        if self.max_message_chars < 1:
            raise ConfigError("max_message_chars must be >= 1")
        if self.platform_token_budget < 0:
            raise ConfigError("platform_token_budget must be >= 0")
        if self.platform_proactive_per_day < 0:
            raise ConfigError("platform_proactive_per_day must be >= 0")
        # 容量上限必须 ≥ 1：0 会让 store 立刻清空，滑动窗口永远看不到历史事件。
        if self.max_usage_events < 1:
            raise ConfigError("max_usage_events must be >= 1")
        if self.max_idempotency_entries < 1:
            raise ConfigError("max_idempotency_entries must be >= 1")
        if self.idempotency_ttl_seconds < 0:
            raise ConfigError("idempotency_ttl_seconds must be >= 0")
        if self.max_queue_entries < 1:
            raise ConfigError("max_queue_entries must be >= 1")
        try:
            ZoneInfo(self.silent_hours_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"unknown timezone {self.silent_hours_timezone!r} "
                "(install tzdata on platforms without a system tz database)"
            ) from exc
        # B7/建议7: 默认/空/<change-me> token 必须拒绝启动，除非显式 dev 模式
        if self.auth_token is None or self.auth_token in _INSECURE_TOKENS:
            if not self.dev_mode:
                raise ConfigError(
                    "GATEWAY_AUTH_TOKEN is unset or uses the default placeholder '<change-me>'. "
                    "Set a strong random token, or set GATEWAY_DEV_MODE=true to run without auth "
                    "(dev mode only; prints a WARNING on startup)."
                )
        # #92: dev 模式只允许绑定 loopback；绑定非 loopback 时拒绝启动（ConfigError）。
        if self.dev_mode and self.host not in LOOPBACK_HOSTS:
            raise ConfigError(
                f"GATEWAY_DEV_MODE=true requires GATEWAY_HOST to be a loopback address "
                f"(127.0.0.1, ::1, or localhost), got {self.host!r}. "
                "Set a real GATEWAY_AUTH_TOKEN and disable dev mode for non-loopback deployments."
            )
        # A1.3：transport=appservice 时必须带齐全的三件套（fail closed），
        # 否则启动即失败，而不是回落到内存假后端悄悄"成功"。
        if self.matrix_transport not in MATRIX_TRANSPORTS:
            raise ConfigError(
                f"GATEWAY_MATRIX_TRANSPORT must be one of {sorted(MATRIX_TRANSPORTS)}, "
                f"got {self.matrix_transport!r}"
            )
        if self.matrix_transport == "appservice":
            if not self.homeserver_url or not self.homeserver_url.startswith(
                ("http://", "https://")
            ):
                raise ConfigError(
                    "GATEWAY_MATRIX_TRANSPORT=appservice requires GATEWAY_HOMESERVER_URL "
                    "to be an http:// or https:// URL of the homeserver Client-Server API"
                )
            # as_token 会以 Authorization 头明文发出去：明文 http:// 只允许
            # loopback / 单标签容器名（compose 网络内），跨主机必须 https，
            # 或显式 GATEWAY_ALLOW_INSECURE_HS=true 自担风险。
            if (
                not self.allow_insecure_homeserver
                and not self.homeserver_url.startswith("https://")
            ):
                host = (urllib.parse.urlparse(self.homeserver_url).hostname or "").lower()
                if host not in _LOCAL_HOMESERVER_HOSTS and "." in host:
                    raise ConfigError(
                        f"GATEWAY_HOMESERVER_URL {self.homeserver_url!r} sends the "
                        "appservice token over plaintext http:// to a non-local host; "
                        "use https://, point at a loopback/compose-internal host, or "
                        "set GATEWAY_ALLOW_INSECURE_HS=true to accept the risk"
                    )
            if self.as_token is None:
                raise ConfigError(
                    "GATEWAY_MATRIX_TRANSPORT=appservice requires a real GATEWAY_AS_TOKEN "
                    "(unset, empty or '<change-me>' is rejected; it must match "
                    "APPSERVICE_AS_TOKEN in the homeserver's rendered registration file)"
                )
            if len(self.as_token) < AS_TOKEN_MIN_LENGTH:
                raise ConfigError(
                    f"GATEWAY_MATRIX_TRANSPORT=appservice requires GATEWAY_AS_TOKEN to be "
                    f"at least {AS_TOKEN_MIN_LENGTH} characters (it guards the whole @ai_* "
                    "namespace); deploy with the 64-hex value init.sh generates"
                )
            if any(ch.isspace() for ch in self.as_token):
                raise ConfigError(
                    "GATEWAY_AS_TOKEN must not contain whitespace "
                    "(it would not survive env-file/HTTP-header round trips)"
                )
            if not self.matrix_server_name:
                raise ConfigError(
                    "GATEWAY_MATRIX_TRANSPORT=appservice requires GATEWAY_MATRIX_SERVER_NAME "
                    "(the homeserver's server_name, e.g. 'localhost') to validate the @ai_* "
                    "namespace"
                )
            # load_config 已归一化小写；直接构造 Config 也必须给小写合法的
            # server_name，否则两侧命名空间会因大小写对不上。
            try:
                normalized = normalize_matrix_server_name(self.matrix_server_name)
            except ConfigError as exc:
                raise ConfigError(f"GATEWAY_MATRIX_SERVER_NAME: {exc}") from exc
            if normalized != self.matrix_server_name:
                raise ConfigError(
                    f"GATEWAY_MATRIX_SERVER_NAME must be a valid lowercase Matrix "
                    f"server name (hostname or IP, optional :port), got "
                    f"{self.matrix_server_name!r}"
                )
            if not _ROOM_ALIAS_PREFIX_RE.fullmatch(self.room_alias_prefix):
                raise ConfigError(
                    "GATEWAY_ROOM_ALIAS_PREFIX must be 1-64 characters of "
                    f"[a-z0-9._=-], got {self.room_alias_prefix!r}"
                )

    @property
    def silent_start_minute(self) -> int:
        return parse_hhmm(self.silent_hours_start)

    @property
    def silent_end_minute(self) -> int:
        return parse_hhmm(self.silent_hours_end)


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    """从环境变量读取配置；`env` 参数便于测试注入。"""
    source: Mapping[str, str] = os.environ if env is None else env
    token = _env_str(source, "GATEWAY_AUTH_TOKEN", "")
    dev_mode = _env_bool(source, "GATEWAY_DEV_MODE", False)
    as_token = _env_str(source, "GATEWAY_AS_TOKEN", "")
    server_name = _env_str(source, "GATEWAY_MATRIX_SERVER_NAME", "") or None
    if server_name is not None:
        # 统一小写 + 形状校验（大小写漂移会让 @ai_* 命名空间两侧对不上）。
        server_name = normalize_matrix_server_name(server_name)
    return Config(
        host=_env_str(source, "GATEWAY_HOST", "127.0.0.1"),
        # 环境变量里的 0 会随机选端口，对部署是事故；仅测试直接构造 Config(port=0)。
        port=_env_int_range(
            source, "GATEWAY_PORT", 8080, minimum=1, maximum=65535
        ),
        # 空字符串 / <change-me> 表示未配置：默认拒绝启动，只有显式 dev 模式放行。
        # 注意：占位符即使被保留也不能当作真实 token 去校验；必须在 load_config
        # 阶段就降级为 None，否则 dev 模式下会拿公开已知值当口令。
        auth_token=None if token in _INSECURE_TOKENS else token,
        dev_mode=dev_mode,
        rate_limit_messages=_env_int(source, "RATE_LIMIT_MESSAGES", 60),
        rate_limit_window_seconds=_env_int(
            source, "RATE_LIMIT_WINDOW_SECONDS", 3600, minimum=1
        ),
        silent_hours_enabled=_env_bool(source, "SILENT_HOURS_ENABLED", True),
        silent_hours_start=_env_str(source, "SILENT_HOURS_START", DEFAULT_SILENT_START),
        silent_hours_end=_env_str(source, "SILENT_HOURS_END", DEFAULT_SILENT_END),
        silent_hours_timezone=_env_str(
            source, "SILENT_HOURS_TIMEZONE", DEFAULT_TIMEZONE
        ),
        max_message_chars=_env_int(source, "MAX_MESSAGE_CHARS", 8000, minimum=1),
        access_log=_env_bool(source, "GATEWAY_ACCESS_LOG", False),
        matrix_transport=_env_str(source, "GATEWAY_MATRIX_TRANSPORT", "memory") or "memory",
        # 空串统一归一为 None；占位符 token 与 GATEWAY_AUTH_TOKEN 同规则降级，
        # 由 Config 校验在 appservice 模式下拒绝启动（fail closed）。
        homeserver_url=_env_str(source, "GATEWAY_HOMESERVER_URL", "") or None,
        matrix_server_name=server_name,
        as_token=None if as_token in _INSECURE_TOKENS else as_token,
        room_alias_prefix=(
            _env_str(source, "GATEWAY_ROOM_ALIAS_PREFIX", DEFAULT_ROOM_ALIAS_PREFIX)
            or DEFAULT_ROOM_ALIAS_PREFIX
        ),
        allow_insecure_homeserver=_env_bool(source, "GATEWAY_ALLOW_INSECURE_HS", False),
        platform_token_budget=_env_int(
            source, "PLATFORM_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET
        ),
        platform_proactive_per_day=_env_int(
            source, "PLATFORM_PROACTIVE_PER_DAY", DEFAULT_PROACTIVE_PER_DAY
        ),
        max_usage_events=_env_int(
            source, "MAX_USAGE_EVENTS", DEFAULT_MAX_USAGE_EVENTS, minimum=1
        ),
        max_idempotency_entries=_env_int(
            source, "MAX_IDEMPOTENCY_ENTRIES", DEFAULT_MAX_IDEMPOTENCY_ENTRIES, minimum=1
        ),
        idempotency_ttl_seconds=_env_int(
            source, "IDEMPOTENCY_TTL_SECONDS", DEFAULT_IDEMPOTENCY_TTL
        ),
        max_queue_entries=_env_int(
            source, "MAX_QUEUE_ENTRIES", DEFAULT_MAX_QUEUE_ENTRIES, minimum=1
        ),
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""环境变量配置。

所有值都有安全默认值；生产部署通过环境变量覆盖。这里不读取 .env 文件本身
（避免引入依赖），由部署侧（systemd / docker compose / shell）注入环境变量。
`.env.example` 只列占位值。
"""

from __future__ import annotations

import os
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

DEFAULT_SILENT_START = "23:00"
DEFAULT_SILENT_END = "07:30"
DEFAULT_TIMEZONE = "Asia/Shanghai"


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
    # Matrix 传输（A1.3）。memory 保持 B1.1 现状；appservice 需要下面三个值。
    matrix_transport: str = "memory"
    homeserver_url: Optional[str] = None
    matrix_server_name: Optional[str] = None
    # as_token 等同 @ai_* 命名空间管理员凭据：不进 repr（日志/断言消息不泄露）。
    as_token: Optional[str] = field(default=None, repr=False)

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
            if self.as_token is None:
                raise ConfigError(
                    "GATEWAY_MATRIX_TRANSPORT=appservice requires a real GATEWAY_AS_TOKEN "
                    "(unset or '<change-me>' is rejected; it must match APPSERVICE_AS_TOKEN "
                    "in the homeserver's rendered registration file)"
                )
            if not self.matrix_server_name:
                raise ConfigError(
                    "GATEWAY_MATRIX_TRANSPORT=appservice requires GATEWAY_MATRIX_SERVER_NAME "
                    "(the homeserver's server_name, e.g. 'localhost') to validate the @ai_* "
                    "namespace"
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
        matrix_server_name=_env_str(source, "GATEWAY_MATRIX_SERVER_NAME", "") or None,
        as_token=None if as_token in _INSECURE_TOKENS else as_token,
    )

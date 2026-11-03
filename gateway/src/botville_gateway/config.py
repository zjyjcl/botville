# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""环境变量配置。

所有值都有安全默认值；生产部署通过环境变量覆盖。这里不读取 .env 文件本身
（避免引入依赖），由部署侧（systemd / docker compose / shell）注入环境变量。
`.env.example` 只列占位值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GATEWAY_VERSION = "0.1.0"
CONTRACT_VERSION = "0.1.0"

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

    def __post_init__(self) -> None:
        # 构造时校验，避免明显错误的配置拖到第一次请求才炸。
        parse_hhmm(self.silent_hours_start)
        parse_hhmm(self.silent_hours_end)
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
    return Config(
        host=_env_str(source, "GATEWAY_HOST", "127.0.0.1"),
        port=_env_int(source, "GATEWAY_PORT", 8080),
        # 空字符串表示开发模式：不校验 Authorization。生产必须设置。
        auth_token=token or None,
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
    )

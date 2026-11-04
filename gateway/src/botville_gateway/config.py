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

# 别名 → room_id 进程内缓存的默认 TTL 秒数（GATEWAY_ROOM_ALIAS_CACHE_TTL
# 可覆盖；0 = 不缓存）。与 matrix_appservice.DEFAULT_ROOM_CACHE_TTL_SECONDS
# 保持一致（各自定义避免 config 反向依赖传输模块）。
DEFAULT_ROOM_ALIAS_CACHE_TTL = 60

# appservice 模式下 as_token 的最低强度（长度下限；且不得含任何空白）。
AS_TOKEN_MIN_LENGTH = 32

# 明文 http:// 的 homeserver 只允许这些 loopback 主机，加上无点的单标签
# 主机名（compose 网络里的服务名，如 synapse/tuwunel，见下方正则）；其余
# （带点域名、非 ::1 的 IPv6、链路本地/ULA 等）必须 https 或显式放开。
# urlparse 对 http://[::1]/ 的 hostname 就是 ::1（已去掉方括号）。
_LOCAL_HOMESERVER_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# 单标签主机名形状（#107）：字母数字开头结尾，中间可含连字符；字符集不含
# "." 与 ":"，天然排除带点域名和一切 IPv6 字面量。
_PLAINTEXT_HS_SINGLE_LABEL_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$"
)

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


def _plaintext_homeserver_host(url: str) -> Optional[str]:
    """明文 homeserver URL → 白名单判断用的 host（小写）；解析不出 → None。

    覆盖 #107 的两类畸形输入：解析不出非空 host（仅 scheme / 空 host），以及
    未加方括号的 IPv6（如 `http://fe80::1`，urlparse 会把它截成 host='fe80'、
    端口段 ':1'）——后者按 netloc 复核主机段没有残留冒号，避免 IPv6 字面量
    被误当成单标签服务名放行。
    """
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.rpartition("@")[2]
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if not netloc.startswith("[") and ":" in netloc.rpartition(":")[0]:
        # 主机段里还有冒号：未加方括号的 IPv6 / 畸形端口，不是单标签主机名。
        return None
    return host


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
    # 别名 → room_id 缓存 TTL 秒数（GATEWAY_ROOM_ALIAS_CACHE_TTL；0 = 不缓存）。
    room_alias_cache_ttl_seconds: int = DEFAULT_ROOM_ALIAS_CACHE_TTL
    # 显式放行明文 http:// 的非本机 homeserver（跨主机部署应使用 https）。
    allow_insecure_homeserver: bool = False

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
            # as_token 会以 Authorization 头明文发出去：明文 http:// 只允许
            # loopback / 单标签容器名（compose 网络内），跨主机必须 https，
            # 或显式 GATEWAY_ALLOW_INSECURE_HS=true 自担风险。#107 收紧：
            # 空 host 与未加方括号的 IPv6 不再被误放行，允许集合之外的
            # 一律拒绝（含非 ::1 的 IPv6、带点域名、链路本地/ULA）。
            if (
                not self.allow_insecure_homeserver
                and not self.homeserver_url.startswith("https://")
            ):
                host = _plaintext_homeserver_host(self.homeserver_url)
                if host is None or (
                    host not in _LOCAL_HOMESERVER_HOSTS
                    and not _PLAINTEXT_HS_SINGLE_LABEL_RE.fullmatch(host)
                ):
                    raise ConfigError(
                        f"GATEWAY_HOMESERVER_URL {self.homeserver_url!r} would send "
                        "the appservice token over plaintext http:// to a host outside "
                        "the allowlist (loopback localhost/127.0.0.1/::1 or a dotless "
                        "single-label compose service name; the URL must carry a "
                        "non-empty host); use https://, point at a loopback or "
                        "compose-internal host, or set GATEWAY_ALLOW_INSECURE_HS=true "
                        "to accept the risk"
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
        # 别名缓存 TTL 只在 appservice 模式生效，但负值任何模式下都是配置
        # 错误，直接在构造时拒绝（load_config 的 >=0 校验之外的兜底）。
        if self.room_alias_cache_ttl_seconds < 0:
            raise ConfigError(
                "GATEWAY_ROOM_ALIAS_CACHE_TTL must be >= 0 seconds "
                "(0 disables the alias->room cache), got "
                f"{self.room_alias_cache_ttl_seconds}"
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
        room_alias_cache_ttl_seconds=_env_int(
            source, "GATEWAY_ROOM_ALIAS_CACHE_TTL", DEFAULT_ROOM_ALIAS_CACHE_TTL
        ),
        allow_insecure_homeserver=_env_bool(source, "GATEWAY_ALLOW_INSECURE_HS", False),
    )

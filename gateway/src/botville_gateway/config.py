# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""环境变量配置。

所有值都有安全默认值；生产部署通过环境变量覆盖。这里不读取 .env 文件本身
（避免引入依赖），由部署侧（systemd / docker compose / shell）注入环境变量。
`.env.example` 只列占位值。
"""

from __future__ import annotations

import logging
import os
import re
import secrets as _secrets
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GATEWAY_VERSION = "0.1.0"
CONTRACT_VERSION = "0.1.0"

# Token values that mean "no auth configured"; starting with these is unsafe
# unless GATEWAY_DEV_MODE=true is explicitly set.
_INSECURE_TOKENS: frozenset = frozenset({"", "<change-me>"})

# 速率窗口的合法下界（#114-7.8d）：OpenAPI `window_seconds` 声明 `minimum: 60`，
# 部署配置用更小的窗口会造出契约里不存在的速率对。
MIN_RATE_LIMIT_WINDOW_SECONDS = 60

# 部署密钥（GATEWAY_HMAC_SECRET）的格式与最低强度（#114 N10）：HMAC 的保密性
# 全部押在密钥上，32 字节（256 bit）与 SHA-256 的输出宽度对齐；只接受 hex，
# 32 字节恰好是 64 个 hex 字符。全零/全相同字符是明显的低熵占位，拒绝。
HMAC_SECRET_MIN_LENGTH = 32
HMAC_SECRET_MIN_HEX_CHARS = HMAC_SECRET_MIN_LENGTH * 2
_HMAC_SECRET_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
HMAC_SECRET_HINT = "python3 -c 'import secrets;print(secrets.token_hex(32))'"
# 与 _INSECURE_TOKENS 同语义的字节形态（直接构造 Config 时也按占位处理）。
_INSECURE_SECRET_BYTES: frozenset = frozenset({b"", b"<change-me>"})
# dev 模式自动生成密钥只警告一次，避免每次构造 Config（测试）都刷屏。
_hmac_dev_warning_emitted = False

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

# 平台层 token 预算默认值（窗口由 RATE_LIMIT_WINDOW_SECONDS 决定）。
# 与 RATE_LIMIT_MESSAGES 一致：配置里的 0 表示「平台不设该限额」，
# 而 `/limits` API 里的 0 表示「一个都不允许」（契约规则语义）。
DEFAULT_TOKEN_BUDGET = 200_000
DEFAULT_PROACTIVE_PER_DAY = 6
DEFAULT_MAX_USAGE_EVENTS = 10_000
# B2.2 #114-7 保留期：事件超过该时长即优先淘汰（默认 30 天）。
DEFAULT_USAGE_RETENTION_SECONDS = 2_592_000
DEFAULT_MAX_IDEMPOTENCY_ENTRIES = 2_000
DEFAULT_IDEMPOTENCY_TTL = 86_400
DEFAULT_MAX_QUEUE_ENTRIES = 1_000

# 终端用户身份（#124 §1.2.3）：OpenID userinfo 结果缓存 TTL。
# 默认 60 秒；0 = 不缓存；负数或 > 60 拒绝启动（契约写死上限 60）。
DEFAULT_OPENID_CACHE_TTL_SECONDS = 60
OPENID_CACHE_TTL_MAX_SECONDS = 60

# A2.2（issue #7）动态模块保留天数（moments.md §7）：软删行硬删 / 审计清理。
DEFAULT_MOMENTS_SOFT_DELETE_RETENTION_DAYS = 30
DEFAULT_MOMENTS_AUDIT_RETENTION_DAYS = 90

# N7（契约 §6.3）：平台层 anti_spam / ai_loop 的部署配置。
# 三个数值项默认 0 = 「平台不设该项」（与 RATE_LIMIT_MESSAGES=0 同语义），
# 非 0 时必须落在与 API 相同的取值范围内（上限见下）。**注意** env 的 0 与
# API 的 0 含义不同：API 的 0 是有效限额「一条都不允许」。
PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES = 0
PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS = 0
PLATFORM_MAX_AI_MESSAGES_PER_HOUR = 0
DEFAULT_AI_LOOP_ON_BREAK = "stop"

# 非 0 时的取值范围（与 limits._ANTI_SPAM_RANGES / _AI_LOOP_RANGES 一致；
# 各自定义避免 config 反向依赖 limits）。
PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES_RANGE = (1, 50)
PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS_RANGE = (1, 86_400)
PLATFORM_MAX_AI_MESSAGES_PER_HOUR_RANGE = (1, 1_000)
# 枚举项没有「0 = 不设」语义；极限序 stop 严于 notify（见契约 §6.3）。
AI_LOOP_ON_BREAK_VALUES = frozenset({"stop", "notify"})

# 部署路径前缀（GATEWAY_HTTP_PATH_PREFIX）的禁用片段：这些要么改变路径语义
# （`//`、`..`），要么在拼 URL / 反代重写时会被提前截断或解码（`?` `#` `%`），
# 都会让「配置里的前缀」和「实际到达的路径」静默不一致。
_HTTP_PATH_PREFIX_FORBIDDEN = ("//", "..", "?", "#", "%")


# 调用方身份（#124 / 契约 §1.2.2）。
# caller token 的最低强度：32 字节随机值。
CALLER_TOKEN_MIN_BYTES = 32
# caller token 条目里的字段分隔符与条目分隔符（条目分隔符 `;` 或换行等价，可混用）。
_CALLER_ENTRY_SEP = ";"
_CALLER_FIELD_SEP = ":"
# §1.2.2：字段值不得含 `:`（字段分隔符）、`;`（条目分隔符）或换行；缺字段、空字段、
# 字段数不为 3 → 拒绝启动（dev 也拒绝）。
_CALLER_FIELD_FORBIDDEN_RE = re.compile(r"[:;\r\n]")


class ConfigError(ValueError):
    """配置值非法（启动时应直接失败，而不是带病运行）。"""


def _dev_hmac_secret() -> bytes:
    """dev 模式未配置密钥时**每进程**生成一次的随机密钥（模块级缓存）。"""
    global _DEV_HMAC_SECRET
    if _DEV_HMAC_SECRET is None:
        _DEV_HMAC_SECRET = _secrets.token_bytes(HMAC_SECRET_MIN_LENGTH)
    return _DEV_HMAC_SECRET


_DEV_HMAC_SECRET: Optional[bytes] = None


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


def normalize_http_path_prefix(prefix: str) -> str:
    """归一化网关部署路径前缀（`GATEWAY_HTTP_PATH_PREFIX`）。

    空串与 `/` 都表示「没有前缀」（不改变现有路由）；非空时去掉尾部 `/`，
    并要求形如 `/a/b`：以 `/` 开头、不含 `//`、`..`、`?`、`#`、`%`、空白与
    控制字符。形状非法抛 ConfigError（启动即失败，而不是让请求路径在运行
    时静默错配）。
    """
    # 先归一（去尾部 `/`）再校验：`/a/` 与 `/a///` 都归一为 `/a`，`/` 归一为空。
    prefix = prefix.rstrip("/")
    if not prefix:
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in prefix):
        raise ConfigError(
            f"GATEWAY_HTTP_PATH_PREFIX must not contain whitespace or control "
            f"characters, got {prefix!r}"
        )
    if not prefix.startswith("/"):
        raise ConfigError(
            f"GATEWAY_HTTP_PATH_PREFIX must start with '/', got {prefix!r}"
        )
    for bad in _HTTP_PATH_PREFIX_FORBIDDEN:
        if bad in prefix:
            raise ConfigError(
                f"GATEWAY_HTTP_PATH_PREFIX must not contain {bad!r}, got {prefix!r}"
            )
    # 去掉尾部 `/` 后纯斜杠即空：`/` 与 `/a/` 视为「空前缀」与「/a」。
    return prefix.rstrip("/")


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
class CallerToken:
    """一条 per-caller 凭据（`GATEWAY_CALLER_TOKENS` 的一项，#124 §1.2.2）。

    - `caller_id`：日志标识（不可逆），日志里只出现它；
    - `token`：≥32 字节随机值，**不进 repr**（日志/断言消息不泄露）；
    - `owner_id`：该调用方拥有的 persona 集合的所有者 ID，用于
      `/context/assemble` 的 persona 区鉴权。
    """

    caller_id: str
    token: str = field(repr=False)
    owner_id: str


def parse_caller_tokens(raw: str) -> Tuple[CallerToken, ...]:
    """解析 `GATEWAY_CALLER_TOKENS`（#124 §1.2.2）。

    条目用 `;` **或换行**分隔（两种等价，可混用）；空条目/纯空白条目忽略。
    每条按 `:` 切分必须**恰好 3 段**，各段 trim 后非空，且任何字段都不得含
    `:` / `;` / 换行；否则抛 `ConfigError`（**dev 模式也拒绝启动**——这是结构性
    错误，不是强度问题）。错误信息不含 token 值。
    """
    entries: Tuple[CallerToken, ...] = ()
    parsed = []
    # 先按 `;` 与换行统一切分（二者等价）。
    for chunk in re.split(r"[;\r\n]", raw):
        entry = chunk.strip()
        if not entry:
            continue
        parts = entry.split(_CALLER_FIELD_SEP)
        if len(parts) != 3:
            raise ConfigError(
                "GATEWAY_CALLER_TOKENS entry must have exactly 3 ':'-separated fields "
                f"(caller_id:token:owner_id), got {len(parts)} field(s)"
            )
        caller_id, token, owner_id = (part.strip() for part in parts)
        for name, value in (
            ("caller_id", caller_id),
            ("token", token),
            ("owner_id", owner_id),
        ):
            if not value:
                raise ConfigError(
                    f"GATEWAY_CALLER_TOKENS entry has an empty '{name}' field "
                    "(all of caller_id:token:owner_id are required)"
                )
            # trim 之后仍含分隔符 = 字段里藏了分隔符（如 token 内含 ':'），拒绝。
            if _CALLER_FIELD_FORBIDDEN_RE.search(value):
                raise ConfigError(
                    f"GATEWAY_CALLER_TOKENS field '{name}' must not contain ':', ';' "
                    "or a newline (they separate entries/fields)"
                )
        parsed.append(CallerToken(caller_id=caller_id, token=token, owner_id=owner_id))
    entries = tuple(parsed)
    return entries


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
    # #114 PR-a：部署密钥，用于 txnId / text_sha256 的 HMAC。同样不进 repr。
    # 非 dev 模式必须显式配置且 >= HMAC_SECRET_MIN_LENGTH 字节；dev 模式未配置
    # 时由 __post_init__ 每进程随机生成（重启后 txnId/hash 不稳定）。
    hmac_secret: Optional[bytes] = field(default=None, repr=False)
    # conversation → 房间别名 localpart 的前缀（#<prefix><conversation_id>:<server>）。
    room_alias_prefix: str = DEFAULT_ROOM_ALIAS_PREFIX
    # 别名 → room_id 缓存 TTL 秒数（GATEWAY_ROOM_ALIAS_CACHE_TTL；0 = 不缓存）。
    room_alias_cache_ttl_seconds: int = DEFAULT_ROOM_ALIAS_CACHE_TTL
    # 显式放行明文 http:// 的非本机 homeserver（跨主机部署应使用 https）。
    allow_insecure_homeserver: bool = False
    # B2.1：平台层 token 预算（0 = 不限）
    platform_token_budget: int = DEFAULT_TOKEN_BUDGET
    # B2.1：平台层主动发言/日上限（0 = 不限）
    platform_proactive_per_day: int = DEFAULT_PROACTIVE_PER_DAY
    # N7（契约 §6.3）：平台层 anti_spam / ai_loop。三个数值项 0 = 平台不设该项
    # （平台层省略对应键），非 0 时即平台底线，owner/peer 更宽松会被 422 拒绝。
    platform_max_consecutive_ai_messages: int = PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES
    platform_anti_spam_cooldown_seconds: int = PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS
    platform_max_ai_messages_per_hour: int = PLATFORM_MAX_AI_MESSAGES_PER_HOUR
    # 枚举项：default stop 即平台底线，没有「不设」取值。
    platform_ai_loop_on_break: str = DEFAULT_AI_LOOP_ON_BREAK
    # B2.1：内存容量上限（容量达到上限时按 LRU 淘汰）
    max_usage_events: int = DEFAULT_MAX_USAGE_EVENTS
    # B2.2：事件保留期（秒）；早于该时长的事件在 append 时优先淘汰。
    usage_retention_seconds: int = DEFAULT_USAGE_RETENTION_SECONDS
    max_idempotency_entries: int = DEFAULT_MAX_IDEMPOTENCY_ENTRIES
    idempotency_ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL
    max_queue_entries: int = DEFAULT_MAX_QUEUE_ENTRIES
    # #124 调用方身份（契约 §1.2.2）。token 值不进 repr（日志不泄露）。
    caller_tokens: Tuple[CallerToken, ...] = field(default=(), repr=False)
    # 是否仍接受站点级 GATEWAY_AUTH_TOKEN；关闭后只有 caller token 可用。
    legacy_token_enabled: bool = True
    # #124 §1.2.3：终端用户身份 OpenID。
    # federation userinfo 专用基址（空则回落 homeserver_url）；
    # 只有这一条 userinfo 走直连，不影响其他联邦流量。
    homeserver_federation_url: Optional[str] = None
    # OpenID userinfo 结果缓存 TTL（秒）；0 = 不缓存；>60 拒绝启动。
    openid_cache_ttl_seconds: int = DEFAULT_OPENID_CACHE_TTL_SECONDS
    # 部署路径前缀（GATEWAY_HTTP_PATH_PREFIX，默认空 = 不改变现有路由）。
    # 空串表示「无前缀」；`/` 与尾部 `/` 由 normalize_http_path_prefix 归一为空。
    # 非空时形如 `/a/b`：以 `/` 开头，不含 `//` `..` `?` `#` `%` 空白控制字符。
    http_path_prefix: str = ""
    # A2.2（issue #7）动态模块：SQLite 库路径。空/None = 模块关闭
    # （/moments* 鉴权后仍 404/405，与合入前一致）。
    moments_db_path: Optional[str] = None
    # 临时好友图文件（A2.3 前的可测试实现，JSON {"friends": [[a, b], ...]}）；
    # 空 = 空图（fail-closed：friends 档下除作者/所有者无人可见）。
    moments_friends_file: Optional[str] = None
    # 软删行动硬删保留天数（purge；默认 30）/ moments_events 审计保留天数（默认 90）。
    moments_soft_delete_retention_days: int = DEFAULT_MOMENTS_SOFT_DELETE_RETENTION_DAYS
    moments_audit_retention_days: int = DEFAULT_MOMENTS_AUDIT_RETENTION_DAYS

    def __post_init__(self) -> None:
        # 构造时校验，避免明显错误的配置拖到第一次请求才炸。
        parse_hhmm(self.silent_hours_start)
        parse_hhmm(self.silent_hours_end)
        if self.port < 0 or self.port > 65535:
            # 0 是合法值（测试用随机端口）；`load_config` 拒绝环境里的 0。
            raise ConfigError(f"port must be 0–65535, got {self.port}")
        if self.rate_limit_messages < 0:
            raise ConfigError("rate_limit_messages must be >= 0")
        if self.rate_limit_window_seconds < MIN_RATE_LIMIT_WINDOW_SECONDS:
            raise ConfigError(
                f"rate_limit_window_seconds must be >= {MIN_RATE_LIMIT_WINDOW_SECONDS} "
                "(matches the OpenAPI window_seconds minimum)"
            )
        if self.max_message_chars < 1:
            raise ConfigError("max_message_chars must be >= 1")
        if self.platform_token_budget < 0:
            raise ConfigError("platform_token_budget must be >= 0")
        if self.platform_proactive_per_day < 0:
            raise ConfigError("platform_proactive_per_day must be >= 0")
        # 容量上限必须 ≥ 1：0 会让 store 立刻清空，滑动窗口永远看不到历史事件。
        if self.max_usage_events < 1:
            raise ConfigError("max_usage_events must be >= 1")
        if self.usage_retention_seconds < 3600:
            raise ConfigError("usage_retention_seconds must be >= 3600")
        # N7（契约 §6.3）：平台层 anti_spam / ai_loop 值校验（fail-closed）。
        # 三个数值项：必须 >= 0；非 0 时必须落在与 API 相同的范围内。
        # 0 表示「平台不设该项」，不能解释成 API 的「一条都不允许」。
        def _check_platform_int(val: int, name: str, lo: int, hi: int) -> None:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"{name} must be an integer, got {val!r}")
            if val < 0:
                raise ConfigError(f"{name} must be >= 0, got {val}")
            if val != 0 and not (lo <= val <= hi):
                raise ConfigError(
                    f"{name} must be 0 (platform floor not set) or {lo}..{hi}, got {val}"
                )
        _check_platform_int(
            self.platform_max_consecutive_ai_messages,
            "platform_max_consecutive_ai_messages",
            *PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES_RANGE,
        )
        _check_platform_int(
            self.platform_anti_spam_cooldown_seconds,
            "platform_anti_spam_cooldown_seconds",
            *PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS_RANGE,
        )
        _check_platform_int(
            self.platform_max_ai_messages_per_hour,
            "platform_max_ai_messages_per_hour",
            *PLATFORM_MAX_AI_MESSAGES_PER_HOUR_RANGE,
        )
        if self.platform_ai_loop_on_break not in AI_LOOP_ON_BREAK_VALUES:
            raise ConfigError(
                f"platform_ai_loop_on_break must be one of "
                f"{sorted(AI_LOOP_ON_BREAK_VALUES)}, got "
                f"{self.platform_ai_loop_on_break!r}"
            )
        # #117 N5（契约 §6.3）：保留期必须 >= max(窗口, 平台冷却)。
        # ai_loop 窗口固定 3600，而 usage_retention_seconds 最小值已是 3600，
        # 无需单独检查。平台冷却 0 表示「不设」，不参与该比较。
        min_retention = self.rate_limit_window_seconds
        if self.platform_anti_spam_cooldown_seconds > 0:
            min_retention = max(min_retention, self.platform_anti_spam_cooldown_seconds)
        if self.usage_retention_seconds < min_retention:
            raise ConfigError(
                f"usage_retention_seconds ({self.usage_retention_seconds}) must be >= "
                f"max(rate_limit_window_seconds={self.rate_limit_window_seconds}, "
                f"platform_anti_spam_cooldown_seconds={self.platform_anti_spam_cooldown_seconds}) "
                f"= {min_retention}; retention shorter than window/cooldown causes "
                "silent fail-open (events evicted before they clear the window)"
            )
        if self.max_idempotency_entries < 1:
            raise ConfigError("max_idempotency_entries must be >= 1")
        if self.idempotency_ttl_seconds < 0:
            raise ConfigError("idempotency_ttl_seconds must be >= 0")
        if self.max_queue_entries < 1:
            raise ConfigError("max_queue_entries must be >= 1")
        # #124 §1.2.3：OpenID 缓存 TTL。0 = 不缓存；负数或 >60 拒绝启动
        # （契约写死上限 60；<0 无语义）。直接构造 Config 也走同一套校验。
        if not isinstance(self.openid_cache_ttl_seconds, int) or isinstance(
            self.openid_cache_ttl_seconds, bool
        ):
            raise ConfigError(
                "openid_cache_ttl_seconds must be an integer, got "
                f"{self.openid_cache_ttl_seconds!r}"
            )
        if self.openid_cache_ttl_seconds < 0:
            raise ConfigError(
                "openid_cache_ttl_seconds must be >= 0 (0 disables the cache), got "
                f"{self.openid_cache_ttl_seconds}"
            )
        if self.openid_cache_ttl_seconds > OPENID_CACHE_TTL_MAX_SECONDS:
            raise ConfigError(
                "openid_cache_ttl_seconds must be <= "
                f"{OPENID_CACHE_TTL_MAX_SECONDS} (contract §1.2.3 hard cap), got "
                f"{self.openid_cache_ttl_seconds}"
            )
        # A2.2：动态保留天数（软删 / 审计）。直接构造 Config 也走同一校验。
        def _check_retention_days(val: int, name: str) -> None:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"{name} must be an integer, got {val!r}")
            if val < 1:
                raise ConfigError(f"{name} must be >= 1 day, got {val}")
        _check_retention_days(
            self.moments_soft_delete_retention_days,
            "moments_soft_delete_retention_days",
        )
        _check_retention_days(
            self.moments_audit_retention_days,
            "moments_audit_retention_days",
        )
        # #124 调用方身份（契约 §1.2.2）。校验放在这里（而非只在 load_config），
        # 保证直接构造 Config 的调用方（测试/嵌入）也走同一套规则。
        self._validate_caller_tokens()
        # 路径前缀：直接构造 Config 时同样归一化 + 校验（fail closed）。
        # frozen dataclass → object.__setattr__；归一是幂等的，重复构造安全。
        object.__setattr__(
            self,
            "http_path_prefix",
            normalize_http_path_prefix(self.http_path_prefix),
        )
        try:
            ZoneInfo(self.silent_hours_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"unknown timezone {self.silent_hours_timezone!r} "
                "(install tzdata on platforms without a system tz database)"
            ) from exc
        # B7/建议7: 默认/空/<change-me> token 必须拒绝启动，除非显式 dev 模式。
        # #124 §1.2.2：legacy 关闭时不要求 GATEWAY_AUTH_TOKEN（凭据只由 caller token
        # 提供）；此时「没有任何可用凭据」只是 401 + 启动 WARNING，不拒绝启动。
        if self.legacy_token_enabled:
            if self.auth_token is None or self.auth_token in _INSECURE_TOKENS:
                if not self.dev_mode:
                    raise ConfigError(
                        "GATEWAY_AUTH_TOKEN is unset or uses the default placeholder '<change-me>'. "
                        "Set a strong random token, or set GATEWAY_DEV_MODE=true to run without auth "
                        "(dev mode only; prints a WARNING on startup). Alternatively configure "
                        "per-caller credentials in GATEWAY_CALLER_TOKENS and set "
                        "GATEWAY_LEGACY_TOKEN_ENABLED=false."
                    )
        # #92: dev 模式只允许绑定 loopback；绑定非 loopback 时拒绝启动（ConfigError）。
        if self.dev_mode and self.host not in LOOPBACK_HOSTS:
            raise ConfigError(
                f"GATEWAY_DEV_MODE=true requires GATEWAY_HOST to be a loopback address "
                f"(127.0.0.1, ::1, or localhost), got {self.host!r}. "
                "Set a real GATEWAY_AUTH_TOKEN and disable dev mode for non-loopback deployments."
            )
        # #114 PR-a: GATEWAY_HMAC_SECRET 校验（fail-closed）。
        # frozen dataclass 的 __post_init__ 不能直接赋值；用 object.__setattr__。
        secret = self.hmac_secret
        if secret is None or secret in _INSECURE_SECRET_BYTES:
            if not self.dev_mode:
                raise ConfigError(
                    "GATEWAY_HMAC_SECRET is unset or uses the default placeholder. "
                    "Set a random hex secret (>= 64 hex characters, i.e. 32 bytes). "
                    f"Generate one with: {HMAC_SECRET_HINT}. "
                    "Use GATEWAY_DEV_MODE=true only for local development."
                )
            # dev 模式：自动生成每进程随机密钥（重启后 txnId/hash 不稳定）。
            generated = _dev_hmac_secret()
            object.__setattr__(self, "hmac_secret", generated)
            global _hmac_dev_warning_emitted
            if not _hmac_dev_warning_emitted:
                _hmac_dev_warning_emitted = True
                print(
                    "WARNING: GATEWAY_HMAC_SECRET is not set — using a per-process random "
                    "secret. txnId and text_sha256 will change on restart. "
                    f"Set GATEWAY_HMAC_SECRET (>= 64 hex characters; {HMAC_SECRET_HINT}) "
                    "for stable values.",
                    file=sys.stderr,
                )
        elif len(self.hmac_secret) < HMAC_SECRET_MIN_LENGTH:  # type: ignore[arg-type]
            raise ConfigError(
                f"GATEWAY_HMAC_SECRET must be at least {HMAC_SECRET_MIN_LENGTH} bytes "
                f"({HMAC_SECRET_MIN_HEX_CHARS} hex characters), got "
                f"{len(self.hmac_secret)} bytes. "  # type: ignore[arg-type]
                f"Generate with: {HMAC_SECRET_HINT}"
            )
        elif not any(self.hmac_secret):  # type: ignore[arg-type]
            # 全零密钥（如 64 个 "0"）长度达标但熵为零：等于没有密钥。
            raise ConfigError(
                "GATEWAY_HMAC_SECRET must not be all zero bytes (it provides no "
                f"secrecy). Generate with: {HMAC_SECRET_HINT}"
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
        # #124 §1.2.3：federation userinfo 直连基址（可选）。配置了就复用
        # 与 GATEWAY_HOMESERVER_URL 完全相同的明文 http 规则：transport 带的是
        # 短时 OpenID token（query），同样不能在跨主机明文上传输。
        fed = self.homeserver_federation_url
        if fed is not None:
            if not fed.startswith(("http://", "https://")):
                raise ConfigError(
                    "GATEWAY_HOMESERVER_FEDERATION_URL must be an http:// or "
                    f"https:// URL, got {fed!r}"
                )
            if not self.allow_insecure_homeserver and not fed.startswith("https://"):
                host = _plaintext_homeserver_host(fed)
                if host is None or (
                    host not in _LOCAL_HOMESERVER_HOSTS
                    and not _PLAINTEXT_HS_SINGLE_LABEL_RE.fullmatch(host)
                ):
                    raise ConfigError(
                        f"GATEWAY_HOMESERVER_FEDERATION_URL {fed!r} would send the "
                        "OpenID token over plaintext http:// to a host outside the "
                        "allowlist (loopback localhost/127.0.0.1/::1 or a dotless "
                        "single-label compose service name); use https://, point at a "
                        "loopback or compose-internal host, or set "
                        "GATEWAY_ALLOW_INSECURE_HS=true to accept the risk"
                    )

    def _validate_caller_tokens(self) -> None:
        """#124 / 契约 §1.2.2：caller token 表的结构与强度校验。

        分两类：

        - **任何模式都拒绝启动**（结构性/语义冲突，dev 也不例外）：caller token 与
          `GATEWAY_AUTH_TOKEN` 相同（两套语义不可重载同一值）、`caller_id` 重复。
        - **非 dev 拒绝启动、dev 仅 WARNING**：token 短于 32 字节、token 重复
          （强度问题，本地开发允许放宽）。

        错误信息与日志**不含 token 值**（只出现 caller_id / 位置 / 长度）。
        """
        tokens = self.caller_tokens
        seen_ids = set()
        for index, entry in enumerate(tokens):
            if entry.caller_id in seen_ids:
                raise ConfigError(
                    f"GATEWAY_CALLER_TOKENS has a duplicate caller_id "
                    f"{entry.caller_id!r} (entries must be unique per caller)"
                )
            seen_ids.add(entry.caller_id)
            if not entry.caller_id or not entry.token or not entry.owner_id:
                raise ConfigError(
                    "GATEWAY_CALLER_TOKENS entry must have non-empty "
                    "caller_id / token / owner_id"
                )
            for name, value in (
                ("caller_id", entry.caller_id),
                ("token", entry.token),
                ("owner_id", entry.owner_id),
            ):
                if _CALLER_FIELD_FORBIDDEN_RE.search(value):
                    raise ConfigError(
                        f"GATEWAY_CALLER_TOKENS field '{name}' (entry #{index + 1}) "
                        "must not contain ':', ';' or a newline"
                    )
            if self.auth_token is not None and entry.token == self.auth_token:
                # §1.2.2：同一值不能既是 caller token 又是站点级 token，
                # 否则无法判定该请求的身份边界。dev 模式也拒绝。
                raise ConfigError(
                    f"GATEWAY_CALLER_TOKENS entry #{index + 1} ({entry.caller_id!r}) "
                    "uses the same token value as GATEWAY_AUTH_TOKEN; the site-wide and "
                    "per-caller credentials must be distinct. Issue the caller its own "
                    "random token (>= 32 bytes)."
                )

        weak: list = []
        dupes: list = []
        seen_tokens = set()
        for index, entry in enumerate(tokens):
            if len(entry.token.encode("utf-8")) < CALLER_TOKEN_MIN_BYTES:
                weak.append(index)
            if entry.token in seen_tokens:
                dupes.append(index)
            seen_tokens.add(entry.token)

        for problem_kind, problem in (("weak", weak), ("dupe", dupes)):
            if not problem:
                continue
            if self.dev_mode:
                # dev：仅警告，不拒绝（本地开发允许放宽强度）。
                for index in problem:
                    detail = (
                        f"has a token shorter than {CALLER_TOKEN_MIN_BYTES} bytes"
                        if problem_kind == "weak"
                        else "reuses the token of an earlier entry"
                    )
                    logging.getLogger(__name__).warning(
                        "GATEWAY_CALLER_TOKENS entry #%d (%s) %s — accepted only because "
                        "GATEWAY_DEV_MODE=true; a production deployment would refuse to start",
                        index + 1,
                        tokens[index].caller_id,
                        detail,
                    )
                continue
            if problem_kind == "weak":
                raise ConfigError(
                    "GATEWAY_CALLER_TOKENS entry #"
                    f"{problem[0] + 1} ({tokens[problem[0]].caller_id!r}) has a token "
                    f"shorter than {CALLER_TOKEN_MIN_BYTES} bytes; use a random value of "
                    "at least 32 bytes (python3 -c 'import secrets;"
                    "print(secrets.token_urlsafe(32))'). Set GATEWAY_DEV_MODE=true only "
                    "for local development."
                )
            raise ConfigError(
                "GATEWAY_CALLER_TOKENS entry #"
                f"{problem[0] + 1} ({tokens[problem[0]].caller_id!r}) reuses the token of "
                "an earlier entry; every caller must have its own token. Set "
                "GATEWAY_DEV_MODE=true only for local development."
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
    # #114 PR-a / N10: 部署密钥（HMAC）。**只接受 hex**（#114 N10）：以前的
    # 实现把非 hex 串按 UTF-8 兜底，导致「63 个 hex 字符」被当成 63 字节密钥
    # 静默接受，与文档写的「十六进制、≥64 hex 字符」不一致。空/<change-me> →
    # None（由 Config 校验决定 dev 放行还是 fail-closed）。
    hmac_secret_raw = _env_str(source, "GATEWAY_HMAC_SECRET", "")
    if hmac_secret_raw in ("", "<change-me>"):
        hmac_secret: Optional[bytes] = None
    else:
        if not _HMAC_SECRET_HEX_RE.fullmatch(hmac_secret_raw):
            raise ConfigError(
                "GATEWAY_HMAC_SECRET must be a hexadecimal string "
                f"(0-9a-f), got {len(hmac_secret_raw)} characters containing "
                "non-hex input. "
                f"Generate one with: {HMAC_SECRET_HINT}"
            )
        if len(hmac_secret_raw) < HMAC_SECRET_MIN_HEX_CHARS:
            raise ConfigError(
                f"GATEWAY_HMAC_SECRET must be at least {HMAC_SECRET_MIN_HEX_CHARS} "
                f"hex characters (= {HMAC_SECRET_MIN_LENGTH} bytes), got "
                f"{len(hmac_secret_raw)}. Generate one with: {HMAC_SECRET_HINT}"
            )
        if len(hmac_secret_raw) % 2 == 1:
            # 奇数个 hex 字符无法解成整字节；明确报错，而不是退回 UTF-8。
            raise ConfigError(
                "GATEWAY_HMAC_SECRET must contain an even number of hex characters "
                f"(two per byte), got {len(hmac_secret_raw)}. "
                f"Generate one with: {HMAC_SECRET_HINT}"
            )
        hmac_secret = bytes.fromhex(hmac_secret_raw)
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
            source,
            "RATE_LIMIT_WINDOW_SECONDS",
            3600,
            minimum=MIN_RATE_LIMIT_WINDOW_SECONDS,
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
        hmac_secret=hmac_secret,
        room_alias_prefix=(
            _env_str(source, "GATEWAY_ROOM_ALIAS_PREFIX", DEFAULT_ROOM_ALIAS_PREFIX)
            or DEFAULT_ROOM_ALIAS_PREFIX
        ),
        room_alias_cache_ttl_seconds=_env_int(
            source, "GATEWAY_ROOM_ALIAS_CACHE_TTL", DEFAULT_ROOM_ALIAS_CACHE_TTL
        ),
        allow_insecure_homeserver=_env_bool(source, "GATEWAY_ALLOW_INSECURE_HS", False),
        platform_token_budget=_env_int(
            source, "PLATFORM_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET
        ),
        platform_proactive_per_day=_env_int(
            source, "PLATFORM_PROACTIVE_PER_DAY", DEFAULT_PROACTIVE_PER_DAY
        ),
        # N7：三个数值项 0 = 平台不设该项；非 0 的区间由 Config 校验（fail-closed）。
        platform_max_consecutive_ai_messages=_env_int(
            source,
            "PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES",
            PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES,
        ),
        platform_anti_spam_cooldown_seconds=_env_int(
            source,
            "PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS",
            PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS,
        ),
        platform_max_ai_messages_per_hour=_env_int(
            source,
            "PLATFORM_MAX_AI_MESSAGES_PER_HOUR",
            PLATFORM_MAX_AI_MESSAGES_PER_HOUR,
        ),
        # 枚举项按现有 env 惯例归一为小写；非法值由 Config 校验拒绝。
        platform_ai_loop_on_break=_env_str(
            source, "PLATFORM_AI_LOOP_ON_BREAK", DEFAULT_AI_LOOP_ON_BREAK
        ).lower()
        or DEFAULT_AI_LOOP_ON_BREAK,
        max_usage_events=_env_int(
            source, "MAX_USAGE_EVENTS", DEFAULT_MAX_USAGE_EVENTS, minimum=1
        ),
        usage_retention_seconds=_env_int(
            source, "USAGE_RETENTION_SECONDS", DEFAULT_USAGE_RETENTION_SECONDS, minimum=3600
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
        # #124 §1.2.2：结构错误（字段数/空字段/含分隔符）在解析时就拒绝启动，
        # dev 也拒绝；强度（短/重复）由 Config.__post_init__ 决定 dev 警告。
        caller_tokens=parse_caller_tokens(_env_str(source, "GATEWAY_CALLER_TOKENS", "")),
        legacy_token_enabled=_env_bool(source, "GATEWAY_LEGACY_TOKEN_ENABLED", True),
        # #124 §1.2.3：federation userinfo 直连基址（空则回落 homeserver_url）。
        homeserver_federation_url=(
            _env_str(source, "GATEWAY_HOMESERVER_FEDERATION_URL", "") or None
        ),
        # OpenID 缓存 TTL：0 = 不缓存；<0 / >60 由 Config 校验拒绝启动。
        openid_cache_ttl_seconds=_env_int(
            source,
            "GATEWAY_OPENID_CACHE_TTL_SECONDS",
            DEFAULT_OPENID_CACHE_TTL_SECONDS,
        ),
        # 默认空 = 现行行为完全不变；非法前缀在 Config 构造时抛 ConfigError。
        http_path_prefix=_env_str(source, "GATEWAY_HTTP_PATH_PREFIX", ""),
        # A2.2（issue #7）：动态模块开关（库路径空 = 关闭）与保留天数；
        # 好友图文件缺失/非法由 StaticRelationProvider.from_file 拒绝（fail-closed）。
        moments_db_path=(
            _env_str(source, "GATEWAY_MOMENTS_DB_PATH", "") or None
        ),
        moments_friends_file=(
            _env_str(source, "GATEWAY_MOMENTS_FRIENDS_FILE", "") or None
        ),
        moments_soft_delete_retention_days=_env_int(
            source,
            "GATEWAY_MOMENTS_SOFT_DELETE_RETENTION_DAYS",
            DEFAULT_MOMENTS_SOFT_DELETE_RETENTION_DAYS,
            minimum=1,
        ),
        moments_audit_retention_days=_env_int(
            source,
            "GATEWAY_MOMENTS_AUDIT_RETENTION_DAYS",
            DEFAULT_MOMENTS_AUDIT_RETENTION_DAYS,
            minimum=1,
        ),
    )

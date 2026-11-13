# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""调用方身份（caller identity，#124 / 契约 §1.2.2）。

两层 bearer 凭据：

- **caller token**（`GATEWAY_CALLER_TOKENS`，每条形如 `caller_id:token:owner_id`）：
  命中后把 `caller_id` / `owner_id` 记到请求上，`owner_id` 供
  `POST /context/assemble` 推导 persona 区（§1.2.2 / §7.2）。
- **legacy 站点级 token**（`GATEWAY_AUTH_TOKEN`，`GATEWAY_LEGACY_TOKEN_ENABLED=true`
  时生效）：命中后 owner 为 `None`（推不出 owner → persona 区 fail-closed 为空）。

比较方式（§1.2.2）：进程启动时生成一次性随机键
`secrets.token_bytes(32)`（不落盘、每次启动重新生成），存储 token 预先做
`HMAC-SHA256(进程随机键, token)` 得到 32 字节摘要；请求里的 bearer 同样 HMAC 后，
与**每一个** caller 摘要和 legacy 摘要逐一 `hmac.compare_digest` 全量比较，
**不提前返回**，最后再决定结果。这样比较的既是等长摘要（不泄漏 token 长度），
也不泄漏命中的是第几条配置。

日志（§1.2.2）：只记 `caller_id`，未知记 `<unknown>`；**绝不**记 token、
token 摘要或 owner_id 明文。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import Config

logger = logging.getLogger(__name__)

# `authenticate()` 的结果码；http_server 据此选 401 错误码或放行。
AUTH_OK = "ok"
AUTH_MISSING = "missing"
AUTH_INVALID = "invalid"
# dev 模式且无任何凭据配置：保持 B1.1 现状（免鉴权），非 §1.2.2 的封闭部署。
AUTH_PASS_THROUGH = "pass_through"

# 日志里未知调用方的占位（§1.2.2）。
UNKNOWN_CALLER = "<unknown>"

# 进程随机键长度（256 bit，与 SHA-256 输出宽度对齐）。
PROCESS_KEY_BYTES = 32


@dataclass(frozen=True)
class CallerIdentity:
    """一次请求的调用方身份。`caller_id=None` 表示没有 per-caller 身份。"""

    caller_id: Optional[str] = None
    owner_id: Optional[str] = None
    # 由站点级 GATEWAY_AUTH_TOKEN 通过：owner 不可知，persona 区只能是空。
    legacy: bool = False


class CallerAuthenticator:
    """进程内 bearer 鉴权器（每个 GatewayServer 一个实例 = 每次启动一把随机键）。"""

    def __init__(self, config: Config) -> None:
        self._key = secrets.token_bytes(PROCESS_KEY_BYTES)
        self._callers: Tuple[Tuple[str, Optional[str], bytes], ...] = tuple(
            (entry.caller_id, entry.owner_id, self._digest(entry.token))
            for entry in config.caller_tokens
        )
        self._legacy_digest: Optional[bytes] = (
            self._digest(config.auth_token)
            if config.legacy_token_enabled and config.auth_token
            else None
        )
        # 只对「dev 模式 + legacy 开启 + 没配任何 token」保留免鉴权，
        # 与 #124 之前的行为一致；legacy 关闭是显式封闭部署，绝不放过。
        self._pass_through = bool(
            config.dev_mode
            and config.legacy_token_enabled
            and config.auth_token is None
            and not config.caller_tokens
        )

    # ------------------------------------------------------------- helpers
    def _digest(self, token: str) -> bytes:
        """HMAC-SHA256(进程随机键, token) → 固定 32 字节摘要。"""
        return hmac.new(
            self._key, token.encode("utf-8"), hashlib.sha256
        ).digest()

    def _log(self, outcome: str, caller_id: Optional[str]) -> None:
        logger.info(
            "auth %s: caller_id=%s", outcome, caller_id or UNKNOWN_CALLER
        )

    # ------------------------------------------------------------- public
    def authenticate(self, header: Optional[str], *, log_failure: bool = True) -> Tuple[str, CallerIdentity]:
        """校验 `Authorization` 头，返回 (结果码, 调用方身份)。

        结果码为 `AUTH_PASS_THROUGH` / `AUTH_MISSING` / `AUTH_INVALID` / `AUTH_OK`；
        `AUTH_PASS_THROUGH` 时调用方身份为空。
        """
        if self._pass_through:
            return AUTH_PASS_THROUGH, CallerIdentity()

        if not header or not header.startswith("Bearer "):
            if log_failure:
                self._log("failed", None)
            return AUTH_MISSING, CallerIdentity()

        presented = self._digest(header[len("Bearer "):].strip())

        matched: Optional[Tuple[str, Optional[str]]] = None
        for caller_id, owner_id, stored in self._callers:
            # 每条配置都比一次（不提前返回）：命中位置不影响耗时。
            if hmac.compare_digest(presented, stored):
                matched = (caller_id, owner_id)
        legacy_matched = False
        if self._legacy_digest is not None:
            if hmac.compare_digest(presented, self._legacy_digest):
                legacy_matched = True

        if matched is not None:
            caller_id, owner_id = matched
            self._log("ok", caller_id)
            return AUTH_OK, CallerIdentity(caller_id=caller_id, owner_id=owner_id)
        if legacy_matched:
            # 站点级 token 没有 caller_id，日志按 §1.2.2 记 <unknown>。
            self._log("ok", None)
            return AUTH_OK, CallerIdentity(legacy=True)
        if log_failure:
            self._log("failed", None)
        return AUTH_INVALID, CallerIdentity()


def log_startup_warnings(config: Config) -> None:
    """启动路径上的配置 WARNING（§1.2.2；不拒绝启动）。文案不含任何 token。"""
    if config.caller_tokens and config.legacy_token_enabled and config.auth_token is not None:
        logger.warning(
            "GATEWAY_CALLER_TOKENS is configured but GATEWAY_LEGACY_TOKEN_ENABLED is still "
            "true: the site-wide GATEWAY_AUTH_TOKEN can still act for any sender_persona_id "
            "and bypass per-caller ownership. Set GATEWAY_LEGACY_TOKEN_ENABLED=false once "
            "every client uses a caller token."
        )
    if not config.legacy_token_enabled and not config.caller_tokens:
        logger.warning(
            "GATEWAY_LEGACY_TOKEN_ENABLED=false and GATEWAY_CALLER_TOKENS is empty: no "
            "credential is configured, so every request that needs authentication will be "
            "rejected with 401 auth.invalid_token. This is only intended for a deliberately "
            "closed deployment."
        )
    # AS 回调（B 线 as-transactions）：hs_token 已配但事务库未配 → HS 会收到 500
    # 并重试，接收链路实际不可用。不拒绝启动（部署可以分步接线），但必须告警。
    if config.hs_token is not None and config.as_txn_db_path is None:
        logger.warning(
            "GATEWAY_HS_TOKEN is set but GATEWAY_AS_TXN_DB_PATH is empty: /_matrix/app/v1/"
            "transactions will authenticate but answer 500 M_UNKNOWN (the homeserver will "
            "keep retrying). Set GATEWAY_AS_TXN_DB_PATH to enable transaction receipt."
        )


def caller_ids(config: Config) -> List[str]:
    """已配置的 caller_id 列表（只用于日志/诊断，不含 token 或 owner_id）。"""
    return [entry.caller_id for entry in config.caller_tokens]

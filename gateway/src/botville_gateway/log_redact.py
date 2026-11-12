# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""日志脱敏（契约 §1.7.2 / 契约决定 #15）。

**任何**写进日志的内容都不得包含原始 AS `txnId`、事件正文或 token。本模块是
脱敏逻辑的唯一定义处，供这些位置复用：

- `http_server.GatewayHandler.log_message` / `log_error` / `log_request`
  （stdlib `BaseHTTPRequestHandler` 的访问日志与 `send_error` 日志，直接写
  stderr，不经过 `logging`）；
- AS 事务持久化失败的 `log_error` 行（同样直接写 stderr），以及传输错误详情。

两档粒度：

1. **访问日志 / 自由文本**：把路径里的 txnId 段替换成 `<redacted>`，保留
   「是哪个端点」的信息。覆盖 `/_matrix/app/v1/transactions/<txnId>`（可带
   `GATEWAY_HTTP_PATH_PREFIX` 前缀、可带查询串、txnId 段可含百分号编码——一律
   匹配到 `/`、`?`、`#` 或空白为止，包含引号与尖括号）以及网关出站发送路径
   `/_matrix/client/v<N>/rooms/<room>/send/<eventType>/<txnId>`、
   `.../rooms/<room>/redact/<eventId>/<txnId>` 与 `.../sendToDevice/<type>/<txnId>`
   （会出现在 `delivery failed: ...` 的异常文本里）。
2. **需要关联同一事务的多条日志**：用 `redact_txn_id()` 给出 `txn#xxxxxxxx`，
   HMAC-SHA256 的前 8 个十六进制字符，密钥为导入时生成的随机进程盐。

`txn#xxxxxxxx` 仅在**同一次进程运行内**可关联；重启后盐改变，不支持跨进程关联。
随机盐不持久化、不写日志，避免无盐哈希对低熵 txnId 的离线枚举；32 bit 截断仍可能
碰撞，因此**不要**把它当身份/去重键使用（去重仍用数据库里的原始 `txn_id`）。

`access_token=` 查询参数的掩码（既有行为）也在这里统一实现。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import re
from typing import Any, Iterable, Tuple

__all__ = ["redact_args", "redact_text", "redact_txn_id"]

# 路径段：仅停在 `/`、`?`、`#` 或空白处；引号与尖括号也可能属于 txnId。
# 百分号编码（如 `%2F`、`%20`）不含字面 `/` 与空白，因此整段作为一个 txnId 被替换。
_SEG = r"[^/\s?#]+"

# AS 回调：/_matrix/app/v1/transactions/{txnId}
# 不在开头锚定：请求行、absolute-form 目标、`GATEWAY_HTTP_PATH_PREFIX` 前缀
# （`/_botville/gateway/_matrix/...`）以及句子中间的路径都能命中。
_AS_TXN_RE = re.compile(r"(/_matrix/app/v1/transactions/)" + _SEG)

# 客户端事务路径：room send / redact，以及 sendToDevice，覆盖所有 v<N>。
# 传输层的异常文本（`matrix.unreachable: PUT <path>: ...`）会把它带进日志。
_CS_SEND_RE = re.compile(
    r"(/_matrix/client/v\d+/(?:rooms/" + _SEG
    + r"/(?:send|redact)/" + _SEG + r"/|sendToDevice/" + _SEG + r"/))" + _SEG
)

# 既有行为：查询串里的 access_token 一律掩码。
_ACCESS_TOKEN_RE = re.compile(r"(?i)access_token=[^&#\s]*")

_REDACTED = "<redacted>"
_TXN_SALT = secrets.token_bytes(32)


def redact_text(text: str) -> str:
    """脱敏一段自由文本：txnId 路径段 → `<redacted>`，access_token → `[REDACTED]`。

    幂等：段匹配包含尖括号，因此完整的 `<redacted>` 会再次替换成同一字面量；
    不跳过以该标记开头的原始段，以免留下后缀。
    """
    text = _AS_TXN_RE.sub(r"\1" + _REDACTED, text)
    text = _CS_SEND_RE.sub(r"\1" + _REDACTED, text)
    return _ACCESS_TOKEN_RE.sub("access_token=[REDACTED]", text)


def redact_args(args: Iterable[Any]) -> Tuple[Any, ...]:
    """对日志参数逐个脱敏；非字符串原样保留以支持数字格式；调用方不得传入含秘密的对象。"""
    return tuple(redact_text(arg) if isinstance(arg, str) else arg for arg in args)


def redact_txn_id(txn_id: Any) -> str:
    """把 txnId 换成 `txn#xxxxxxxx`（进程随机盐 HMAC-SHA256 前 8 位）。

    仅在同一次进程运行内可关联，重启后改变；32 bit 截断可能碰撞，
    **不得**当身份键或去重键使用。
    `None` / 空值给出 `txn#unknown`，避免日志里出现 `txn#None` 这类歧义串。
    """
    if txn_id is None:
        return "txn#unknown"
    text = str(txn_id)
    if not text:
        return "txn#unknown"
    digest = hmac.new(_TXN_SALT, text.encode("utf-8", "backslashreplace"), hashlib.sha256).hexdigest()
    return "txn#" + digest[:8]

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""stdlib HTTP 雏形。零运行时依赖（只用 Python 标准库）。

实现的端点子集（对齐 gateway/openapi/gateway.v1.yaml）：

  GET  /health
  POST /v1/policy/evaluate
  POST /v1/conversations/{id}/messages
  GET  /v1/limits
  PUT  /v1/limits
  GET  /v1/usage
  GET  /v1/usage/events

错误体遵循 RFC 9457 problem+json（契约 §1.3）。所有响应（含 4xx/5xx）都带
`X-Request-Id`，且 `Cache-Control: no-store`。

**请求体上限 1 MiB**（契约 §1.1）：超限返回 400 `request.invalid`，
`errors[].code` 为 `too_large`；不新增 413 状态码（契约状态码集合不变）。

使用方式：
  GATEWAY_AUTH_TOKEN=<random> python3 -m botville_gateway
  GATEWAY_DEV_MODE=true python3 -m botville_gateway   # 显式开发模式，免鉴权
  GATEWAY_AUTH_TOKEN=<random> GATEWAY_PORT=9000 python3 -m botville_gateway

`build_server(config, transport=None)` 的 `transport` 可选：测试/嵌入场景可以
注入自定义 `MatrixTransport`（例如会 sleep 的假传输来验证锁外投递），
不传时经 `build_transport(config)` 按配置选择 memory / appservice。
"""

from __future__ import annotations

from datetime import datetime, timezone

import hmac
import http.server
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .as_consumer import AsTransactionConsumer
from .config import Config, load_config
from .as_txn_store import (
    AsTransactionStore,
    PersistentSpeechDedupe,
    build_as_txn_store,
)
from .memory.store import MemoryPurger, MemoryStore
from .memory.mood import MoodService
from .memory.mood_http import handle as handle_mood
from .memory.owner_http import handle as handle_memory, MemoryHTTPState, capabilities
from .engine import (
    EngineHooks,
    ModelClient,
    ModelConfig,
    load_personas,
)
from .caller_identity import (
    AUTH_INVALID,
    AUTH_MISSING,
    AUTH_OK,
    AUTH_PASS_THROUGH,
    CallerAuthenticator,
    CallerIdentity,
    log_startup_warnings,
)
from .errors import (
    GatewayDeliveryError,
    IdempotencyConflict,
    LimitError,
    PeerValidationError,
    UnprocessableError,
    ValidationError,
)
from .limits import LimitStore
from .log_redact import redact_args as _redact_args
from .log_redact import redact_txn_id
from .matrix import InMemoryMatrixTransport, MatrixTransport
from .room_members import RoomMemberSource
from .moments.http_api import handle as _handle_moments_dispatch
from .moments.service import build_moments_service
from .personas_list import PersonaListError, persona_page, persona_detail
from .service import GatewayService
from .store import UsageStore
from .timeutil import iso_utc
from .user_identity import (
    VERIFY_INVALID,
    VERIFY_MISSING,
    VERIFY_OK,
    VERIFY_SERVER_ERROR,
    UserIdentityVerifier,
)

_ROUTE_MESSAGES = re.compile(r"^/v1/conversations/([^/]+)/messages$")
# Matrix AS 回调路径（根路径，不带部署前缀；strip_path_prefix 后也能命中）。
_ROUTE_AS_TRANSACTIONS = re.compile(r"^/_matrix/app/v1/transactions/([^/]+)$")
_AS_APP_V1_PREFIX = "/_matrix/app/v1"
# Matrix AS 协议 v1 之前的 legacy 根路径（契约 §1.7）：只认路径族、一律 404。
# 这些路径与网关自身路由（`/v1/*`、`/health`）没有交集，不需要鉴权。
_AS_LEGACY_ROOTS = ("/transactions", "/users", "/rooms", "/thirdparty")
# txnId 合法字符集（URL 解码后）：长度 ≤ 255，
# 只允许印刷可见 ASCII（排除控制字符和空白），避免注入 / 路径穿越。
_TXN_ID_RE = re.compile(r"^[!-~]{1,255}$")
# 客户端回传的 X-Request-Id 必须短且安全，否则伪造/放大审计 ID。
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_JSON_MEDIA_TYPE = "application/json"

# `/moments*` 的路径族（契约 §1.2.3 / moments.md §3）：这些请求先做**终端用户**
# 身份校验（MatrixOpenID / Matrix Bearer），不走 §1.2.2 的 caller/legacy bearer。
MOMENTS_PREFIX = "/v1/moments"

# 请求体上限：远大于 max_message_chars（8000），同时防止内存拖垮。
# 契约 §1.1 明确该上限（1 MiB）；超限 → 400 problem.invalid + errors[].code=too_large。
MAX_BODY_BYTES = 1_048_576  # 1 MiB


def strip_path_prefix(path: str, prefix: str) -> str:
    """剥掉部署路径前缀（`GATEWAY_HTTP_PATH_PREFIX`），只按完整路径段匹配。

    - 空前缀 → 原样返回（默认配置下行为与以前完全一致）。
    - `path == prefix` → `"/"`；`path` 以 `prefix + "/"` 开头 → 去掉该前缀段。
    - 不带前缀的请求**原样返回**：edge / 入口反代可能已经去掉了前缀。
    - 只认整段边界：`/_botville/gatewayX/health` 不匹配 `/_botville/gateway`。
    """
    if not prefix:
        return path
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix):]
    return path


def _problem(
    status: int,
    code: str,
    title: str,
    request_id: str,
    *,
    detail: Optional[str] = None,
    errors: Optional[list] = None,
    instance: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "type": f"urn:botville:error:{code}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": request_id,
    }
    if detail:
        body["detail"] = detail
    if errors:
        body["errors"] = errors
    if instance:
        body["instance"] = instance
    return body


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 请求处理器。

    `server` 属性由 `GatewayServer` 注入，携带 `config` / `service`。
    """

    server_version = "botville-gateway"
    sys_version = ""

    def version_string(self) -> str:  # 不暴露 Python 版本
        return self.server_version

    def log_message(self, fmt: str, *args: Any) -> None:
        # Memory URLs can contain private counterpart filters. Never access-log them.
        if any(part in unquote(getattr(self, "path", ""))
               for part in ("/memories", "/personas")):
            return
        if self.server.config.access_log:  # type: ignore[attr-defined]
            super().log_message(fmt, *_redact_args(args))

    def log_error(self, fmt: str, *args: Any) -> None:
        # stdlib log_error 调用 self.log_message，仍会经过本类的脱敏。
        # 此处显式脱敏也安全：重复替换是幂等的，输出直接写 stderr。
        if self.server.config.access_log:  # type: ignore[attr-defined]
            super().log_error(fmt, *_redact_args(args))

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        # log_request 把 self.requestline 直接拼进消息；requestline 含原始 txnId，
        # 必须脱敏。走 log_message 路径即可复用 _redact_args。
        if self.server.config.access_log:  # type: ignore[attr-defined]
            super().log_request(code, size)

    # ------------------------------------------------------------- helpers
    def parse_request(self) -> bool:  # type: ignore[override]
        """在任何 do_* 之前统一校验 / 规范化请求目标（RFC 9112 §3.2）。

        stdlib 只拆请求行；目标交给 `_route_path` 的 urlparse 时，括号未闭合的
        IPv6 字面量（`http://[::1`、`//[x`）会抛 ValueError，被 do_* 兜底成 500。
        这里一次性处理，所有方法（含 HEAD / OPTIONS / TRACE 与 moments 二次取路径）
        都受益：

        - origin-form（以 `/` 开头）：原样保留，只校验可解析；
        - absolute-form（`http://` / `https://` + 非空 authority）：按 RFC 9112
          取其 path（空则 `/`）+ query 改写成 origin-form 后照常路由；
        - asterisk-form `*`：只允许 OPTIONS（随后照旧 405）；
        - 其余（authority-form、相对路径、其他 scheme、解析异常）→
          400 `request.invalid`。

        注：`//[x` 这类以 `//` 开头的目标通常走不到这里的 400——较新的 stdlib
        在 `super().parse_request()` 里会把开头的多个 `/` 折叠成单个 `/`
        （防 open redirect 的修复），于是变成普通路径 `/[x`，按路由返回 404；
        不折叠的旧版 stdlib 上则由 `_normalize_request_target` 拒为 400。
        两种结果都合法，测试对该用例同时接受 400 / 404。
        """
        # TODO(HTTP/1.1)：本 handler 目前沿用 stdlib 默认 `protocol_version =
        # "HTTP/1.0"`（每个连接只处理一个请求）。将来升级到 HTTP/1.1 keep-alive 时：
        #   1. 所有 400 响应（这里的非法目标、请求体超限 / chunked 等）除了设
        #      `close_connection = True`，还要显式带上 `Connection: close` 响应头；
        #   2. `_rid`（`_request_id` 的缓存）必须在每个请求开始时重置，否则同一
        #      连接上的后续请求会沿用上一个请求的 request_id。
        # 本条只是注释，当前行为不变。
        if not super().parse_request():
            return False
        normalized = _normalize_request_target(self.command, self.path)
        if normalized is None:
            self.close_connection = True
            self._send_problem(
                400,
                "request.invalid",
                "Bad Request",
                detail="invalid request target",
            )
            return False
        self.path = normalized
        return True

    def _route_path(self) -> Tuple[str, str, str]:
        """把一个请求解析成 (path, query, 剥掉部署前缀后的路由路径)。

        查询串不参与前缀匹配；前缀只按整段边界剥离（`strip_path_prefix`），
        不带前缀的请求也照常路由（兼容已去前缀的 edge / 入口反代）。
        请求目标已在 `parse_request` 校验 / 规范化，这里不会再遇到无法解析的目标。
        """
        parsed = urlparse(self.path)
        prefix = self.server.config.http_path_prefix  # type: ignore[attr-defined]
        return parsed.path, parsed.query, strip_path_prefix(parsed.path, prefix)

    def _request_id(self) -> str:
        """每个请求只生成/校验一次；校验失败则换成服务端新 ID（不回显可疑值）。"""
        cached = getattr(self, "_rid", None)
        if cached:
            return cached
        raw = self.headers.get("X-Request-Id") if self.headers else None
        if raw and _REQUEST_ID_RE.fullmatch(raw):
            self._rid = raw
        else:
            self._rid = f"req_{uuid.uuid4().hex}"
        return self._rid

    def _idempotency_key(self) -> Optional[str]:
        """读取 Idempotency-Key，按 OpenAPI 声明的 8–128 长度校验。

        头部缺失 → 幂等关闭；头部存在但不在 8–128 字符内 → 400 problem+json
        （契约 `IdempotencyKey` 参数是 minLength 8 / maxLength 128，不能静默忽略）。
        """
        raw = self.headers.get("Idempotency-Key")
        if raw is None:
            return None
        raw = raw.strip()
        if not 8 <= len(raw) <= 128:
            raise ValidationError(
                "request.invalid", "Idempotency-Key must be 8–128 characters"
            )
        return raw

    def _problem_body(
        self,
        status: int,
        code: str,
        title: str,
        *,
        detail: Optional[str] = None,
        errors: Optional[list] = None,
    ) -> Dict[str, Any]:
        return _problem(
            status, code, title, self._request_id(), detail=detail, errors=errors
        )

    def _send_problem(
        self,
        status: int,
        code: str,
        title: str,
        *,
        detail: Optional[str] = None,
        errors: Optional[list] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._send_json(
            status,
            self._problem_body(status, code, title, detail=detail, errors=errors),
            problem=True,
            extra_headers=extra_headers,
        )
    def _send_json(
        self,
        status: int,
        body: Any,
        *,
        extra_headers: Optional[Dict[str, str]] = None,
        problem: bool = False,
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        content_type = (
            "application/problem+json; charset=utf-8"
            if problem
            else "application/json; charset=utf-8"
        )
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            # §1.3：每个响应都带 X-Request-Id，不只是 2xx。
            self.send_header("X-Request-Id", self._request_id())
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开：不是服务端错误，别让 handler 线程抛栈。
            self.close_connection = True

    def _read_json(self) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """读取请求体并反序列化。返回 (body, problem_body)；其中一个为 None。

        校验：Content-Length 可解析且非负、不超过 MAX_BODY_BYTES、
        Content-Type 为 application/json、JSON 顶层必须是对象。
        """
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if transfer_encoding and transfer_encoding != "identity":
            return None, self._problem_body(
                400,
                "request.invalid",
                "Unsupported Transfer-Encoding",
                detail="chunked request bodies are not supported; send Content-Length",
            )

        raw_length = (self.headers.get("Content-Length") or "0").strip() or "0"
        try:
            length = int(raw_length)
        except ValueError:
            return None, self._problem_body(
                400,
                "request.invalid",
                "Invalid Content-Length",
                detail=f"Content-Length must be an integer, got {raw_length!r}",
            )
        if length < 0:
            return None, self._problem_body(
                400,
                "request.invalid",
                "Invalid Content-Length",
                detail="Content-Length must not be negative",
            )
        if length > MAX_BODY_BYTES:
            # 契约 §1.1：不新增 413；用 400 request.invalid，errors[].code = too_large。
            return None, self._problem_body(
                400,
                "request.invalid",
                "Request body too large",
                detail=f"body exceeds {MAX_BODY_BYTES} bytes (1 MiB)",
                errors=[
                    {
                        "field": "body",
                        "code": "too_large",
                        "message": f"request body must not exceed {MAX_BODY_BYTES} bytes",
                    }
                ],
            )

        if length == 0:
            return {}, None

        media_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if media_type != _JSON_MEDIA_TYPE:
            return None, self._problem_body(
                400,
                "request.invalid",
                "Unsupported Media Type",
                detail=f"Content-Type must be {_JSON_MEDIA_TYPE}, got {media_type or '(none)'}",
            )

        raw = self.rfile.read(length)
        if len(raw) != length:
            # 客户端中途断开；不要尝试读更多（会阻塞）。
            return None, self._problem_body(
                400, "request.invalid", "Incomplete request body"
            )

        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return None, self._problem_body(
                400, "request.invalid", "Invalid JSON", detail=str(exc)
            )
        if not isinstance(body, dict):
            return None, self._problem_body(
                400,
                "request.invalid",
                "Invalid JSON",
                detail="request body must be a JSON object",
            )
        return body, None

    def _auth_ok(self) -> bool:
        """鉴权（契约 §1.2 / §1.2.2）。

        由一个进程内的 `CallerAuthenticator` 判定 caller token / legacy 站点级
        token；命中 caller token 时把 `caller_id` / `owner_id` 记到 `self._caller`，
        供 `/context/assemble` 推导 persona 区（legacy 与 dev 放行时 owner 为 None）。
        401 的错误码保持不变：缺头 `auth.missing`，其余 `auth.invalid_token`。
        """
        authenticator: CallerAuthenticator = self.server.authenticator  # type: ignore[attr-defined]
        outcome, identity = authenticator.authenticate(self.headers.get("Authorization"))
        if outcome == AUTH_PASS_THROUGH:
            return True
        self._caller = identity
        if outcome == AUTH_OK:
            return True
        if outcome == AUTH_MISSING:
            self._send_problem(401, "auth.missing", "Authorization header required")
            return False
        # AUTH_INVALID：错 token，或 legacy 关闭且无 caller token 命中。
        self._send_problem(401, "auth.invalid_token", "Invalid bearer token")
        return False

    def _auth_capabilities_ok(self, *, persona_detail: bool = False) -> bool:
        """Accept site/caller or end-user identity without sending an early 401."""
        header = self.headers.get("Authorization")
        outcome, identity = self.server.authenticator.authenticate(header, log_failure=False)
        if outcome in (AUTH_OK, AUTH_PASS_THROUGH):
            self._caller = identity
            return True
        verifier = (self.server.persona_detail_verifier if persona_detail
                    else self.server.user_verifier)
        user_outcome, user_id = verifier.verify(header)
        if user_outcome == VERIFY_OK:
            self._user_id = user_id
            return True
        # Only a definitive rejection is an authentication failure; an
        # unavailable homeserver has not determined whether the token is valid.
        if not header or user_outcome in (VERIFY_MISSING, VERIFY_INVALID):
            self.server.authenticator._log("failed", None)
        if not header:
            self._send_problem(401, "auth.missing", "Authorization header required")
        elif user_outcome in (VERIFY_MISSING, VERIFY_INVALID):
            self._send_problem(401, "auth.invalid_token", "Invalid bearer token")
        else:
            # Same as /moments: homeserver unavailable is retryable, not an invalid token.
            self._send_problem(
                500,
                "internal.error",
                "Homeserver unavailable",
                detail="user identity could not be verified; retry later",
            )
        return False

    def _persona_owner_identity(self) -> Optional[str]:
        """Resolve an owner only after shared authentication succeeds."""
        user_id = getattr(self, "_user_id", None)
        if user_id:
            return user_id
        identity = self._caller_identity()
        if not identity.legacy and identity.owner_id:
            return identity.owner_id
        self._send_problem(403, "persona.not_owned", "Persona access denied")
        return None

    def _handle_personas_list(self, query: str) -> None:
        if not self._auth_capabilities_ok():
            return
        owner = self._persona_owner_identity()
        if owner is None:
            return
        try:
            page = persona_page(self.server, owner, query)
        except PersonaListError as exc:
            self._send_problem(exc.status, exc.code, "Persona request rejected")
            return
        self._send_json(200, page)

    def _route_persona(self, path: str) -> None:
        rest = path[len("/v1/personas/"):]
        parts = rest.split("/")
        if len(parts) == 2 and parts[1] == "state" and self.command in ("GET", "HEAD", "PATCH"):
            handle_mood(self, parts[0])
        elif len(parts) == 1 and self.command in ("GET", "HEAD"):
            self._handle_persona_detail(rest)
        else:
            self._send_problem(404, "not_found", "Not Found")

    def _handle_persona_detail(self, encoded_id: str) -> None:
        if not self._auth_capabilities_ok(persona_detail=True):
            return
        owner = self._persona_owner_identity()
        if owner is None:
            return
        try:
            result = persona_detail(self.server, owner, unquote(encoded_id, errors="strict"))
        except (PersonaListError, UnicodeError):
            self._send_problem(404, "persona.not_found", "Persona not found")
            return
        self._send_json(200, result)

    def _caller_identity(self) -> CallerIdentity:
        """当前请求的调用方身份；未经鉴权（如 /health）时为空身份。"""
        identity = getattr(self, "_caller", None)
        return identity if identity is not None else CallerIdentity()

    def _auth_user_ok(self) -> bool:
        """终端用户身份校验（`/moments*`，#124 §1.2.3）。

        按 Authorization scheme 分流（`MatrixOpenID` → federation userinfo，
        `Bearer` → whoami），**不先试 OpenID 再试 whoami**。站点/调用方 bearer
        （`GATEWAY_AUTH_TOKEN` / caller token）到这里会被 homeserver whoami 拒绝，
        因此**不会**被当成用户身份放行（`await` 见 `caller_identity` 的语义：
        `/moments*` 不调用 `CallerAuthenticator`）。

        错误映射（不新增状态码）：
          - 缺头 → 401 `auth.missing`
          - 无效 / 过期 / 域不匹配 → 401 `auth.invalid_token`
          - homeserver 5xx / 超时 / 不可达 → 500 `internal.error`（可重试，
            **不**当成无效 token 缓存）
        """
        verifier: UserIdentityVerifier = self.server.user_verifier  # type: ignore[attr-defined]
        outcome, user_id = verifier.verify(self.headers.get("Authorization"))
        if outcome == VERIFY_OK:
            self._user_id = user_id
            return True
        if outcome == VERIFY_MISSING:
            self._send_problem(401, "auth.missing", "Authorization header required")
            return False
        if outcome == VERIFY_INVALID:
            self._send_problem(401, "auth.invalid_token", "Invalid user token")
            return False
        # VERIFY_SERVER_ERROR：homeserver 5xx / 超时 / 连接失败。
        # 不缓存、不误判为无效 token；返回已有的 500 internal.error（可重试）。
        self._send_problem(
            500,
            "internal.error",
            "Homeserver unavailable",
            detail="user identity could not be verified; retry later",
        )
        return False

    def _user_identity(self) -> Optional[str]:
        """当前请求通过校验后的 Matrix user_id（未校验时 None）。"""
        return getattr(self, "_user_id", None)

    # ------------------------------------------------ Matrix AS callbacks
    # 只处理 /_matrix/app/v1/*（协议层）；事件消费留给后续 PR。
    def _send_matrix_error(self, status: int, errcode: str, error: str) -> None:
        """Matrix 标准错误体（`{"errcode","error"}`，application/json）。

        不用 problem+json：AS 回调的调用方是 homeserver，按 Matrix 规范解析。
        """
        self._send_json(status, {"errcode": errcode, "error": error})

    def _drain_as_body(self) -> None:
        """丢弃（或读完）请求体，避免连接错乱。超过上限时直接关闭连接。

        认证失败也要调用：先完成认证再解析/消费 body，但 body 仍须被读出，
        否则同一连接上的后续字节会被当成新请求行。
        """
        length, framing_error = self._as_content_length()
        if framing_error:
            self.close_connection = True
            return
        if length is None or length <= 0:
            # 头部缺失：没有可读的正文；0 同理。
            return
        if length > self.server.config.as_max_body_bytes:  # type: ignore[attr-defined]
            # 不读超大 body；直接关闭连接（HTTP/1.0 下本就逐请求关闭）。
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _as_content_length(self) -> Tuple[Optional[int], bool]:
        """解析 Content-Length。返回 `(length, framing_error)`。

        - 头部**缺失** → `(None, False)`：这里不判定对错，由各端点决定
          （ping 的空体合法；transactions 的空体不是可解析 JSON → 调用方报
          `M_NOT_JSON`）。
        - 非法 framing（chunked 等非 identity 的 Transfer-Encoding、非整数、
          负数）→ `(None, True)`，调用方统一按 `400 M_NOT_JSON` 处理。
        - 合法 → `(length, False)`，`length` 可能是 0（空体）。
        """
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if transfer_encoding and transfer_encoding != "identity":
            return None, True
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None, False
        raw = raw.strip()
        if not raw:
            # 头部存在但为空：不是合法的 Content-Length。
            return None, True
        try:
            length = int(raw)
        except ValueError:
            return None, True
        if length < 0:
            return None, True
        return length, False

    def _as_auth_ok(self) -> bool:
        """校验 `Authorization: Bearer <hs_token>`（只认表头，不认查询参数）。

        - 网关未配置 `GATEWAY_HS_TOKEN` → 403 M_FORBIDDEN。
        - 缺 Authorization 头 / 非 Bearer scheme → 401 M_MISSING_TOKEN。
        - token 不匹配 → 403 M_FORBIDDEN（AS 规范 v1.4+）。
        恒定时间比较；token 值绝不写入日志或响应体。
        """
        expected = self.server.config.hs_token  # type: ignore[attr-defined]
        if expected is None:
            self._drain_as_body()
            self._send_matrix_error(
                403, "M_FORBIDDEN", "homeserver token is not configured on this gateway"
            )
            return False
        header = self.headers.get("Authorization") if self.headers else None
        scheme, _, token = (header or "").partition(" ")
        token = token.strip()
        if not header or scheme.lower() != "bearer" or not token:
            self._drain_as_body()
            self._send_matrix_error(401, "M_MISSING_TOKEN", "missing bearer token")
            return False
        if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            self._drain_as_body()
            self._send_matrix_error(403, "M_FORBIDDEN", "invalid homeserver token")
            return False
        return True

    def _read_as_body(
        self, *, allow_empty: bool = False
    ) -> Tuple[Optional[bytes], Optional[Tuple[int, str, str]]]:
        """读取 AS 回调请求体。返回 (raw, error)；error 为 (status, errcode, error)。

        Body 上限用 `GATEWAY_AS_MAX_BODY_BYTES`（与 /v1 的 1 MiB 独立）。

        framing 错误（chunked 传输、坏的/缺失的 Content-Length、读写不完整）
        返回 400 `M_NOT_JSON`：按 Matrix 规范，`M_NOT_JSON` 表示「请求体不是
        合法 JSON / 无法解析」，而 `M_BAD_JSON` 只用于「JSON 合法但结构不对」。
        读不出来的体自然属于前者。

        `allow_empty=False`（transactions）：**缺失 `Content-Length` 或空体**
        （`Content-Length: 0` / 纯空白）都不是可解析的 JSON → 400 `M_NOT_JSON`。
        `allow_empty=True`（ping）：空体合法，返回 `b""` 交给调用方按可选体处理。
        """
        length, framing_error = self._as_content_length()
        if framing_error:
            return None, (400, "M_NOT_JSON", "unreadable request body framing")
        if length is None:
            # 头部缺失：没有声明正文。transactions 必须带 JSON 体 → framing 错误；
            # ping 的正文可选 → 视为空体。
            if allow_empty:
                return b"", None
            return None, (
                400,
                "M_NOT_JSON",
                "missing Content-Length: transaction body must be JSON",
            )
        limit = self.server.config.as_max_body_bytes  # type: ignore[attr-defined]
        if length > limit:
            self.close_connection = True
            return None, (
                413,
                "M_TOO_LARGE",
                f"request body exceeds {limit} bytes",
            )
        if length == 0:
            # transactions：空体不是可解析 JSON（不是「结构不对」）→ M_NOT_JSON。
            if allow_empty:
                return b"", None
            return None, (400, "M_NOT_JSON", "request body is empty")
        raw = self.rfile.read(length)
        if len(raw) != length:
            return None, (400, "M_NOT_JSON", "incomplete request body")
        if not allow_empty and not raw.strip():
            # 纯空白体同样不是可解析 JSON（与 Content-Length: 0 同码）。
            return None, (400, "M_NOT_JSON", "request body is empty")
        return raw, None

    @staticmethod
    def _decode_json_body(raw: bytes) -> Tuple[Any, Optional[Tuple[int, str, str]]]:
        """把请求体解码为 JSON：严格 UTF-8 → `json.loads`。

        返回 (payload, error)。**先按 UTF-8 严格解码**再看 JSON：`json.loads`
        本身也接受 UTF-16/32（BOM 嗅探），但事务要按文本落库、日志/比对都假定
        UTF-8，放行 UTF-16 会在后面 decode 时炸成 500（HS 会无限重试）。因此
        非 UTF-8 体一律 400 `M_NOT_JSON`，与「不是合法 JSON」同码。
        空体按调用方语义处理（返回 None，不报错）。
        """
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, (400, "M_NOT_JSON", "request body is not valid UTF-8 JSON")
        try:
            return (json.loads(text) if text.strip() else None), None
        except (json.JSONDecodeError, ValueError):
            return None, (400, "M_NOT_JSON", "request body is not valid JSON")

    def _handle_as_request(self, path: str) -> None:
        """`/_matrix/app/v1/*` 的方法分发：未知路径 404，已知路径错误方法 405。"""
        if path == "/_matrix/app/v1/ping":
            if self.command == "POST":
                self._handle_as_ping()
            else:
                self._send_matrix_error(
                    405, "M_UNRECOGNIZED", f"{self.command} is not supported on {path}"
                )
            return
        match = _ROUTE_AS_TRANSACTIONS.match(path)
        if match:
            if self.command == "PUT":
                self._handle_as_transactions(unquote(match.group(1)))
            else:
                self._send_matrix_error(
                    405, "M_UNRECOGNIZED", f"{self.command} is not supported on {path}"
                )
            return
        # 未知路径：未知的 `/_matrix/app/v1/*` 与 legacy 根路径（transactions/users/
        # rooms/thirdparty）都按 Matrix 格式 404，不泄露路由细节、不做鉴权。
        self._drain_as_body()
        self._send_matrix_error(404, "M_UNRECOGNIZED", "unrecognized request")

    def _handle_as_matrix_path(self, path: str) -> None:
        """统一入口：legacy 根路径直接 404，`/_matrix/app/v1/*` 走方法分发。"""
        if _is_legacy_as_path(path):
            self._drain_as_body()
            self._send_matrix_error(404, "M_UNRECOGNIZED", "unrecognized request")
            return
        self._handle_as_request(path)

    def _handle_as_transactions(self, txn_id: str) -> None:
        """`PUT /_matrix/app/v1/transactions/{txnId}`：认证 → 校验 → 幂等持久化。"""
        if not self._as_auth_ok():
            return
        raw, err = self._read_as_body()
        if err is not None:
            self._send_matrix_error(*err)
            return
        # txnId 已 URL 解码；长度 ≤255、只允许印刷可见 ASCII。
        if not _TXN_ID_RE.fullmatch(txn_id):
            self._send_matrix_error(
                400, "M_INVALID_PARAM", "transaction id must be 1-255 printable ASCII characters"
            )
            return

        assert raw is not None
        payload, decode_err = self._decode_json_body(raw)
        if decode_err is not None:
            self._send_matrix_error(*decode_err)
            return
        if not isinstance(payload, dict):
            self._send_matrix_error(400, "M_BAD_JSON", "request body must be a JSON object")
            return
        events = payload.get("events")
        if not isinstance(events, list):
            self._send_matrix_error(400, "M_BAD_JSON", "'events' must be an array")
            return
        ephemeral = payload.get("ephemeral")
        if ephemeral is not None and not isinstance(ephemeral, list):
            self._send_matrix_error(400, "M_BAD_JSON", "'ephemeral' must be an array")
            return

        store: Optional[AsTransactionStore] = self.server.as_txn_store  # type: ignore[attr-defined]
        if store is None:
            # 未配置持久化路径：不能保证「200 前落库」，返回 500 让 HS 重试。
            self._send_matrix_error(
                500, "M_UNKNOWN", "transaction storage is not configured"
            )
            return
        try:
            # 幂等：store_transaction 在锁内判重 + 插入（返回 False = 已存在，
            # 含已清理的墓碑），重放直接 200 且不重复落库。
            # 体已按严格 UTF-8 解码成功，这里再编码回文本按原样落库。
            store.store_transaction(txn_id, raw.decode("utf-8"))
        except Exception as exc:  # sqlite3.Error / IO
            if self.server.config.access_log:  # type: ignore[attr-defined]
                # 契约决定 #15：日志不落原始 txnId，只记可关联的短哈希标记
                # （`txn#xxxxxxxx`，见 log_redact.redact_txn_id）。
                self.log_error(
                    "AS transaction persist failed for txn %s: %s",
                    redact_txn_id(txn_id),
                    type(exc).__name__,
                )
            self._send_matrix_error(
                500, "M_UNKNOWN", "failed to persist transaction; retry later"
            )
            return
        # 事件消费（B 线 PR1，contract §1.7.8）：进程内消费者已由
        # `GatewayServer` 在开启 GATEWAY_CONSUMER_ENABLED 时启动，事务落库后经
        # `store.set_on_new_transaction()` 立即唤醒（另有 5 秒兜底轮询）。
        # 本端点只负责协议层接收与持久化，**不**在当前请求里处理事件。
        self._send_json(200, {})

    def _handle_as_ping(self) -> None:
        """`POST /_matrix/app/v1/ping`：认证 → 可选 transaction_id → 200 {}。"""
        if not self._as_auth_ok():
            return
        # ping 的正文可选：缺失 Content-Length / 空体都按 200 处理（见 §1.7.5）。
        raw, err = self._read_as_body(allow_empty=True)
        if err is not None:
            self._send_matrix_error(*err)
            return
        assert raw is not None
        payload: Any = {}
        if raw.strip():
            payload, decode_err = self._decode_json_body(raw)
            if decode_err is not None:
                # framing ok 但非 UTF-8 / 非 JSON：ping 也按 M_NOT_JSON 报
                self._send_matrix_error(*decode_err)
                return
            if not isinstance(payload, dict):
                self._send_matrix_error(400, "M_BAD_JSON", "request body must be a JSON object")
                return
        transaction_id = (payload or {}).get("transaction_id")
        if transaction_id is not None and not isinstance(transaction_id, str):
            self._send_matrix_error(400, "M_BAD_JSON", "'transaction_id' must be a string")
            return
        self._send_json(200, {})

    def _handle_moments(self) -> None:
        """`/moments*` 的请求入口（#124）。

        先做**用户身份校验**（401 先于业务）；通过后：moments 已启用则交给
        新模块分发（A2.2 PR2），未配置 `GATEWAY_MOMENTS_DB_PATH` 时保持
        原占位行为（404）。
        """
        if not self._auth_user_ok():
            return
        if self.server.moments_service is None:
            self._send_problem(404, "not_found", "Not Found")
            return
        _handle_moments_dispatch(self)

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:
        self._request_id()
        try:
            _raw_path, query, path = self._route_path()
            if _is_matrix_app_path(path):
                self._handle_as_matrix_path(path)
                return
            if path in ("/health", "/v1/health"):
                self._handle_health()
                return
            if path == "/v1/memories" or path.startswith("/v1/memories/"):
                handle_memory(self)
                return
            if _is_moments_path(path):
                self._handle_moments()
                return
            if path == "/v1/capabilities":
                if self._auth_capabilities_ok():
                    self._send_json(200, capabilities(self.server, query))
                return
            if path == "/v1/personas":
                self._handle_personas_list(query)
                return
            if path.startswith("/v1/personas/"):
                self._route_persona(path)
                return
            if not self._auth_ok():
                return
            if path == "/v1/limits":
                self._handle_get_limits(query)
            elif path == "/v1/usage":
                self._handle_get_usage(query)
            elif path == "/v1/usage/events":
                self._handle_list_usage_events(query)
            else:
                self._send_problem(404, "not_found", "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - 兜底，避免线程裸抛
            self._internal_error(exc)

    def do_HEAD(self) -> None:
        self.do_GET()

    # --------------------------------------------------------------- POST
    def do_POST(self) -> None:
        self._request_id()
        try:
            _raw_path, _query, path = self._route_path()
            if _is_matrix_app_path(path):
                self._handle_as_matrix_path(path)
                return
            if path == "/v1/memories" or path.startswith("/v1/memories/"):
                handle_memory(self)
                return
            if _is_moments_path(path):
                self._handle_moments()
                return
            if path.startswith("/v1/personas/"):
                self._route_persona(path)
                return
            if not self._auth_ok():
                return
            if path == "/v1/policy/evaluate":
                self._handle_policy_evaluate()
            elif path == "/v1/context/assemble":
                self._handle_context_assemble()
            elif m := _ROUTE_MESSAGES.match(path):
                self._handle_send_message(m.group(1))
            else:
                self._send_problem(404, "not_found", "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - 兜底
            self._internal_error(exc)

    # 不支持的方法：仍返回 problem+json，而不是 stdlib 的 501 HTML。
    def _method_not_allowed(self) -> None:
        self._request_id()
        self._send_problem(
            405,
            "request.invalid",
            "Method Not Allowed",
            detail=f"{self.command} is not supported on this endpoint",
        )

    def do_PUT(self) -> None:
        """只有 `PUT /v1/limits` 支持 PUT；AS 回调走 `_handle_as_matrix_path`。"""
        self._request_id()
        try:
            _raw_path, _query, path = self._route_path()
            if _is_matrix_app_path(path):
                self._handle_as_matrix_path(path)
                return
            if path == "/v1/memories" or path.startswith("/v1/memories/"):
                handle_memory(self)
                return
            if _is_moments_path(path):
                self._handle_moments()
                return
            if path.startswith("/v1/personas/"):
                self._route_persona(path)
                return
            if not self._auth_ok():
                return
            if path != "/v1/limits":
                self._send_problem(
                    405,
                    "request.invalid",
                    "Method Not Allowed",
                    detail=f"PUT is not supported on {path}",
                )
                return
            self._handle_put_limits()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - 兜底
            self._internal_error(exc)

    def _moments_method_not_allowed(self) -> None:
        """PATCH / DELETE 在 `/moments*` 上：先鉴权，再按 moments 开关分发。

        moments 已启用 → 交新模块（PATCH/DELETE 是合法操作）；未启用 →
        保持原占位 405。
        """
        self._request_id()
        try:
            _raw, _query, path = self._route_path()
            if _is_matrix_app_path(path):
                # AS 路径上的 PATCH/DELETE：legacy / 未知路径 404，
                # 已知 v1 路径错误方法 405（都是 Matrix JSON、不做鉴权）。
                self._handle_as_matrix_path(path)
                return
            if path == "/v1/memories" or path.startswith("/v1/memories/"):
                handle_memory(self)
                return
            if path.startswith("/v1/personas/"):
                self._route_persona(path)
                return
            if path == "/v1/personas":
                self._send_problem(404, "not_found", "Not Found")
                return
            if _is_moments_path(path):
                # 先校验用户身份；鉴权失败已在 _auth_user_ok 里发响应。
                if not self._auth_user_ok():
                    return
                if self.server.moments_service is None:
                    self._send_problem(
                        405,
                        "request.invalid",
                        "Method Not Allowed",
                        detail=f"{self.command} is not yet supported on {path}",
                    )
                    return
                _handle_moments_dispatch(self)
                return
            self._method_not_allowed()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # pragma: no cover
            self._internal_error(exc)

    def do_PATCH(self) -> None:  # type: ignore[override]
        self._moments_method_not_allowed()

    def do_DELETE(self) -> None:  # type: ignore[override]
        self._moments_method_not_allowed()

    def _method_not_allowed_or_as(self) -> None:
        """OPTIONS / TRACE：AS 路径走 Matrix 错误体，其余保持原 problem+json。"""
        try:
            _raw, _query, path = self._route_path()
        except Exception:  # pragma: no cover - 目标已在 parse_request 校验
            path = ""
        if _is_matrix_app_path(path):
            self._request_id()
            self._handle_as_matrix_path(path)
            return
        if path.startswith("/v1/personas/"):
            self._route_persona(path)
            return
        self._method_not_allowed()

    do_OPTIONS = _method_not_allowed_or_as
    do_TRACE = _method_not_allowed_or_as

    def _internal_error(self, exc: BaseException) -> None:
        if self.server.config.access_log:  # type: ignore[attr-defined]
            self.log_error("unhandled error: %s", type(exc).__name__)
        try:
            self._send_problem(
                500, "internal.error", "Internal Server Error"
            )
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ----------------------------------------------------------- handlers
    def _handle_health(self) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        cfg: Config = self.server.config            # type: ignore[attr-defined]
        transport = self.server.transport           # type: ignore[attr-defined]
        now = svc.now
        matrix_ok = transport.healthy()
        # DB / 模型提供方尚未接入：不报假 ok，直接从 checks 里省略。
        checks: List[Dict[str, Any]] = [
            {"name": "matrix", "status": "ok" if matrix_ok else "degraded"},
        ]
        status = "ok" if matrix_ok else "degraded"
        body = {
            "status": status,
            "version": cfg.version,
            "contract_version": cfg.contract_version,
            "started_at": iso_utc(svc.started_at),
            "uptime_seconds": max(0, int((now - svc.started_at).total_seconds())),
            "checks": checks,
            "modules": [
                {"name": "core", "enabled": True, "status": "ok"},
            ],
        }
        # OpenAPI /health 描述：`status` 非 `ok` 时返回 503（结构相同）。
        self._send_json(200 if status == "ok" else 503, body)

    def _handle_context_assemble(self) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        body, err = self._read_json()
        if err:
            self._send_json(400, err, problem=True)
            return
        assert body is not None
        try:
            idempotency_key = self._idempotency_key()
            # #124 §1.2.2：owner 由鉴权后的调用方身份推出（legacy / dev 放行时为 None
            # → persona 区 fail-closed 为空，状态码仍 200）。
            result = svc.assemble_context(
                body, owner_id=self._caller_identity().owner_id
            )
        except ValidationError as exc:
            errors = None
            if exc.field:
                errors = [{"field": exc.field, "code": exc.code, "message": exc.detail}]
            status = getattr(exc, "http_status", 400)
            title = "Request validation failed" if status == 400 else "Request invalid"
            self._send_problem(
                status, exc.code, title,
                detail=exc.detail, errors=errors,
            )
            return
        self._send_json(200, result)

    def _handle_policy_evaluate(self) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        body, err = self._read_json()
        if err:
            self._send_json(400, err, problem=True)
            return
        assert body is not None
        try:
            result = svc.evaluate_policy(body)
        except ValidationError as exc:
            errors = None
            if exc.field:
                errors = [{"field": exc.field, "code": exc.code, "message": exc.detail}]
            # PeerValidationError 携带 http_status=422（§6.4 peer_id 校验）；
            # 普通 ValidationError 仍 400。
            status = getattr(exc, "http_status", 400)
            title = "Request validation failed" if status == 400 else "Request invalid"
            self._send_problem(
                status, exc.code, title,
                detail=exc.detail, errors=errors,
            )
            return
        self._send_json(200, result)

    def _handle_send_message(self, conversation_id: str) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        body, err = self._read_json()
        if err:
            self._send_json(400, err, problem=True)
            return
        assert body is not None
        try:
            idempotency_key = self._idempotency_key()
            receipt = svc.send_ai_message(
                conversation_id, body, idempotency_key=idempotency_key
            )
        except ValidationError as exc:
            errors = None
            if exc.field:
                errors = [{"field": exc.field, "code": exc.code, "message": exc.detail}]
            # PeerValidationError 携带 http_status=422（§6.4 peer_id 校验）；
            # 普通 ValidationError 仍 400。
            status = getattr(exc, "http_status", 400)
            title = "Request validation failed" if status == 400 else "Request invalid"
            self._send_problem(
                status, exc.code, title,
                detail=exc.detail, errors=errors,
            )
            return
        except IdempotencyConflict:
            self._send_problem(
                409,
                "idempotency.conflict",
                "Idempotency-Key reused with different request body",
            )
            return
        except GatewayDeliveryError as exc:
            if self.server.config.access_log:  # type: ignore[attr-defined]
                self.log_error("delivery failed: %s: %s", exc.code, exc.detail)
            # appservice 传输在客户端侧就能判定的请求错误映射为 4xx（契约
            # sendMessage 已声明 400/404；其余传输失败仍是网关内部错误 → 500，
            # 502/`matrix.unavailable` 未入契约）。
            if exc.code == "appservice.namespace":
                self._send_problem(
                    400, "request.invalid", "Request validation failed",
                    detail=exc.detail,
                    errors=[{
                        "field": "sender_persona_id",
                        "code": exc.code,
                        "message": exc.detail,
                    }],
                )
                return
            if exc.code == "conversation.invalid":
                self._send_problem(
                    400, "request.invalid", "Request validation failed",
                    detail=exc.detail,
                    errors=[{
                        "field": "conversation_id",
                        "code": exc.code,
                        "message": exc.detail,
                    }],
                )
                return
            if exc.code == "conversation.not_found":
                self._send_problem(
                    404, "conversation.not_found", "Conversation not found",
                    detail=exc.detail,
                )
                return
            self._send_problem(
                500, "internal.error", "Internal Server Error",
                detail="message delivery failed; the message was not recorded",
            )
            return
        self._send_json(200, receipt)

    # ------------------------------------------------------- limits / usage
    def _handle_get_limits(self, query: str) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        params = parse_qs(query, keep_blank_values=True)
        try:
            result = svc.get_limits(
                persona_id=_single(params, "persona_id"),
                conversation_id=_single(params, "conversation_id"),
                peer_id=_single(params, "peer_id"),
                include_platform=_bool_param(params, "include_platform", True),
            )
        except ValidationError as exc:
            self._send_validation_error(exc)
            return
        self._send_json(200, result)

    def _handle_put_limits(self) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        body, err = self._read_json()
        if err:
            self._send_json(400, err, problem=True)
            return
        assert body is not None
        try:
            result = svc.put_limits(body)
        except ValidationError as exc:
            self._send_validation_error(exc)
            return
        except LimitError as exc:
            self._send_problem(
                422,
                exc.code,
                "Limit out of range",
                detail=exc.detail,
                errors=exc.errors or None,
            )
            return
        self._send_json(200, result)

    def _handle_get_usage(self, query: str) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        params = parse_qs(query, keep_blank_values=True)
        try:
            result = svc.get_usage(
                persona_id=_single(params, "persona_id"),
                conversation_id=_single(params, "conversation_id"),
                from_=_single(params, "from"),
                to=_single(params, "to"),
                timezone_name=_single(params, "timezone"),
                group_by=_single(params, "group_by") or "conversation",
            )
        except ValidationError as exc:
            self._send_validation_error(exc)
            return
        self._send_json(200, result)

    def _handle_list_usage_events(self, query: str) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        params = parse_qs(query, keep_blank_values=True)
        try:
            limit = _int_param(params, "limit", 20)
            result = svc.list_usage_events(
                persona_id=_single(params, "persona_id"),
                kind=_single(params, "kind"),
                from_=_single(params, "from"),
                to=_single(params, "to"),
                cursor=_single(params, "cursor"),
                limit=limit,
            )
        except ValidationError as exc:
            self._send_validation_error(exc)
            return
        self._send_json(200, result)

    def _send_validation_error(self, exc: ValidationError) -> None:
        errors = None
        if exc.field:
            errors = [{"field": exc.field, "code": exc.code, "message": exc.detail}]
        # PeerValidationError 携带 http_status=422（§6.4 peer_id 校验）；
        # 普通 ValidationError 仍 400。
        status = getattr(exc, "http_status", 400)
        title = "Request validation failed" if status == 400 else "Request invalid"
        self._send_problem(
            status, exc.code, title,
            detail=exc.detail, errors=errors,
        )


def _normalize_request_target(method: str, target: str) -> Optional[str]:
    """请求目标 → 可安全交给 `_route_path` 的 origin-form；非法返回 None（→ 400）。"""
    if target == "*":
        # asterisk-form 只用于服务器级 OPTIONS（RFC 9112 §3.2.4）。
        return target if method == "OPTIONS" else None
    if target.startswith("/"):
        candidate = target
    else:
        try:
            parsed = urlparse(target)
        except ValueError:
            return None
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return None  # authority-form / 相对路径 / 其他 scheme
        candidate = parsed.path or "/"
        if not candidate.startswith("/"):
            return None
        if parsed.params:
            candidate += ";" + parsed.params
        if parsed.query:
            candidate += "?" + parsed.query
    try:
        # 与 `_route_path` 同一解析器再走一遍：`//[x` 这类 origin-form 也会抛。
        urlparse(candidate)
    except ValueError:
        return None
    return candidate


def _is_moments_path(path: str) -> bool:
    """`/moments*` 路径族（部署前缀已剥离）：等于前缀或以 `前缀 + "/"` 开头。

    契约 §1.2.3：这些路径用**终端用户**身份（MatrixOpenID / Matrix Bearer），
    不走 §1.2.2 的 caller/legacy bearer。只匹配整段边界，
    `/v1/momentsX` 不算。
    """
    return path == MOMENTS_PREFIX or path.startswith(MOMENTS_PREFIX + "/")


def _is_as_path(path: str) -> bool:
    """`/_matrix/app/v1/*` 路径族（部署前缀剥离后命中根路径）。"""
    return path.startswith(_AS_APP_V1_PREFIX + "/") or path == _AS_APP_V1_PREFIX


def _is_legacy_as_path(path: str) -> bool:
    """legacy AS 根路径族（`/transactions`、`/users`、`/rooms`、`/thirdparty`）。

    契约 §1.7：只实现 `/_matrix/app/v1/*`，这些 Matrix AS v1 之前的老路径
    （带或不带子路径）按未知路径返回 `404 M_UNRECOGNIZED`（Matrix 错误体、
    任何方法、不做鉴权）。只按整段边界匹配，且与网关既有路由无交集。
    """
    for root in _AS_LEGACY_ROOTS:
        if path == root or path.startswith(root + "/"):
            return True
    return False


def _is_matrix_app_path(path: str) -> bool:
    """AS 回调路径（新 v1 路径 + legacy 根路径），统一走 Matrix 错误体。"""
    return _is_as_path(path) or _is_legacy_as_path(path)


def _single(params: Dict[str, List[str]], name: str) -> Optional[str]:
    values = params.get(name)
    if not values:
        return None
    return values[-1]


def _bool_param(params: Dict[str, List[str]], name: str, default: bool) -> bool:
    raw = _single(params, name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValidationError(
        "request.invalid", f"'{name}' must be a boolean", name
    )


def _int_param(params: Dict[str, List[str]], name: str, default: int) -> int:
    raw = _single(params, name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError(
            "request.invalid", f"'{name}' must be an integer", name
        ) from exc
    return value


class GatewayServer(http.server.ThreadingHTTPServer):
    """注入 config / service / transport，供 handler 访问。"""

    daemon_threads = True

    def __init__(self, config: Config, service: GatewayService, transport: MatrixTransport) -> None:
        self.config = config
        self.service = service
        self.transport = transport
        self.authenticator = CallerAuthenticator(config)
        # #124 §1.2.3：`/moments*` 的终端用户身份校验（MatrixOpenID / Matrix Bearer）。
        # 与上面的 caller/legacy authenticator 并存但互不替代：`/moments*` 只走这里。
        self.user_verifier = UserIdentityVerifier(config)
        self.persona_detail_verifier = UserIdentityVerifier(config, allow_foreign_identity=True)
        # A2.2（issue #7）动态服务：未配置 GATEWAY_MOMENTS_DB_PATH → None
        # （模块关闭，`/moments*` 鉴权后行为与合入前一致）。
        self.moments_service = build_moments_service(config)
        # AS 事务存储：未配置 GATEWAY_AS_TXN_DB_PATH → None
        # （transactions 端点返回 500，让 HS 重试）。配置了库与保留期时启动
        # 保留期清理（启动跑一次 + 后台守护线程，见 as_txn_store.py）。
        self.as_txn_store: Optional[AsTransactionStore] = build_as_txn_store(
            config.as_txn_db_path,
            # 间隔 0 = 关闭：启动时那一次也不跑（与 moments 清理一致）。
            purge_retention_days=(
                config.as_txn_retention_days
                if config.as_txn_db_path and config.as_txn_purge_interval_seconds > 0
                else None
            ),
            purge_interval_seconds=config.as_txn_purge_interval_seconds,
        )
        # B2.6 PR3a: local persistent memory is opt-in.  This is deliberately
        # independent of the AS transaction database and never affects health.
        self.memory_store: Optional[MemoryStore] = None
        self.memory_purger: Optional[MemoryPurger] = None
        if config.memory_db_path:
            self.memory_store = MemoryStore(
                config.memory_db_path, retention_days=config.memory_retention_days,
                hmac_secret=config.hmac_secret,
            )
            self.memory_purger = MemoryPurger(
                self.memory_store, config.memory_purge_interval_seconds
            )
        self.mood_service = None
        if config.mood_enabled:
            if self.memory_store is None:
                logging.getLogger(__name__).warning("Mood disabled: memory database is not configured")
            else:
                self.mood_service = MoodService(self.memory_store, config.mood_half_life_minutes)
        self.memory_worker = None
        self.memory_idle = None
        self.memory_embedding = None
        self.memory_vectors = None
        self.memory_vector_stats = {}
        self._build_memory_worker(config, service)
        # B 线 PR1（contract §1.7.8）：进程内 AS 事务消费者。
        # GATEWAY_CONSUMER_ENABLED=false（默认）时不起线程、不唤醒，行为与现在完全一致。
        # 开启但 as_txn_db_path 未配置时打 warning 且不起线程（不拒绝启动）。
        self.as_consumer: Optional[AsTransactionConsumer] = None
        self.room_member_source: Optional[RoomMemberSource] = None
        # B 线 PR2：内置角色引擎（contract §0 / §1.7.8 ②）。只在三个条件**同时**
        # 成立时装配：消费开启 + 人设有内容 + 模型已配置。否则保持「只记录/计数、
        # 不发言」，/health 不 degraded。
        self.engine_hooks: Optional[EngineHooks] = None
        # Persona ownership is a startup-time security boundary, independent
        # of whether this process currently starts the consumer/model engine.
        self.memory_personas = load_personas(
            config.personas_file, server_name=config.matrix_server_name,
            max_message_chars=config.max_message_chars)
        from .groups import GroupInvites
        self.group_invites = GroupInvites(
            self.memory_store, transport, self.memory_personas,
            enabled=config.groups_enabled, server_name=config.matrix_server_name,
            max_rooms=config.group_max_rooms_per_persona)
        if hasattr(transport, "accepted_group"):
            transport.accepted_group = self.group_invites.is_accepted
            transport.disabled_group = self.group_invites.is_disabled
            transport.left_group = self.group_invites.is_left
        self.personas_timestamp = iso_utc(service.started_at)
        if config.personas_file:
            try:
                self.personas_timestamp = iso_utc(datetime.fromtimestamp(
                    os.stat(config.personas_file).st_mtime, timezone.utc))
            except (OSError, ValueError, OverflowError):
                pass
        if config.consumer_enabled:
            if self.as_txn_store is None:
                logging.getLogger(__name__).warning(
                    "GATEWAY_CONSUMER_ENABLED=true but GATEWAY_AS_TXN_DB_PATH is not "
                    "set — consumer thread will NOT start"
                )
            else:
                hooks = self._build_engine_hooks(config, service)
                self.engine_hooks = hooks
                # Production membership source for DM routing.  State events
                # keep a bounded local cache warm; AppService transport fills a
                # cold cache through joined_members while masquerading as one
                # configured persona.  Other transports remain mention-only.
                personas = hooks.persona_ids if hooks is not None else ()
                server_name = (config.matrix_server_name or "").lower()

                def is_local_ai(user_id: str) -> bool:
                    if not isinstance(user_id, str) or not user_id.startswith("@ai_"):
                        return False
                    _localpart, separator, domain = user_id[1:].partition(":")
                    return bool(separator and domain.lower() == server_name)

                joined_members = getattr(transport, "joined_members", None)
                fetcher = joined_members if callable(joined_members) else None
                self.room_member_source = RoomMemberSource(
                    fetcher=fetcher,
                    personas=personas,
                    is_local_ai=is_local_ai,
                )
                consumer = AsTransactionConsumer(
                    self.as_txn_store,
                    hooks=hooks,
                    service=service,
                    server_name=config.matrix_server_name,
                    sender_fetcher=getattr(transport, "event_sender", None),
                    room_members=self.room_member_source.members,
                    room_member_observer=self.room_member_source.observe_member_event,
                    group_max_responders=config.group_max_responders,
                    persona_user_ids=personas,
                    # Engine persists each persona separately; consumer completion
                    # must not share the first persona key on partial failures.
                    dedupe=None if hooks else PersistentSpeechDedupe(self.as_txn_store),
                )
                # 注册唤醒钩子：事务落库成功后立即唤醒消费线程。
                def wake_consumers():
                    if self.memory_idle is not None:
                        self.memory_idle.notify()
                    consumer.wake()
                self.as_txn_store.set_on_new_transaction(wake_consumers)
                self.as_consumer = consumer
        self.memory_http = MemoryHTTPState(self.memory_store, config.hmac_secret)
        if self.room_member_source is None and self.memory_store is not None:
            joined_members = getattr(transport, "joined_members", None)
            self.room_member_source = RoomMemberSource(
                fetcher=joined_members if callable(joined_members) else None,
                personas=tuple(self.memory_personas),
                is_local_ai=lambda uid: uid in self.memory_personas)
        self.proactive_worker = None
        if config.proactive_enabled:
            from .proactive import ProactiveWorker
            model_config = ModelConfig(
                base_url=(config.model_base_url or '').rstrip('/'),
                model=config.model_name or '', _api_key=config.model_api_key,
                timeout_seconds=config.model_timeout_seconds)
            members = getattr(transport, 'joined_members', None)
            if config.memory_db_path and model_config.configured and callable(members):
                # Own connection: a timed-out proactive thread must not lose its
                # DB when the independent memory worker finishes shutdown.
                proactive_store = MemoryStore(
                    config.memory_db_path, retention_days=config.memory_retention_days,
                    hmac_secret=config.hmac_secret)
                self.proactive_worker = ProactiveWorker(
                    proactive_store, service, self.memory_personas,
                    ModelClient(model_config), config, members=members, close_store=True,
                    mood_label_provider=(self._proactive_mood_label
                                         if self.mood_service is not None else None))
            else:
                logging.getLogger(__name__).warning(
                    'Proactive inactive: requires memory DB, model and membership transport')
        log_startup_warnings(config)
        super().__init__((config.host, config.port), GatewayHandler)
        # 监听端口绑定成功后才起消费线程：绑定失败（OSError）时不留下孤儿线程。
        if self.as_consumer is not None:
            self.as_consumer.start()
        if self.memory_purger is not None:
            self.memory_purger.start()
        if self.memory_worker is not None:
            self.memory_worker.start()
        if self.proactive_worker is not None:
            self.proactive_worker.start()

    def _proactive_mood_label(self, persona_id: str) -> Optional[str]:
        """Read the decayed label without making mood availability a send gate."""
        try:
            return self.mood_service.get(persona_id)["label"]
        except Exception:
            # Exception text may contain private data; log only a fixed message.
            logging.getLogger(__name__).warning("Proactive mood unavailable; omitting label")
            return None

    def _build_memory_worker(self, config, service):
        if self.memory_store is None:
            return
        from .memory.worker import IdleSignal, MemoryIdleWorker
        from .memory.embedding import EmbeddingClient, EmbeddingConfig
        from .memory.vector import build_vector_store
        model_config = ModelConfig(
            base_url=(config.model_base_url or '').rstrip('/'),
            model=config.model_name or '', _api_key=config.model_api_key,
            timeout_seconds=config.model_timeout_seconds)
        embedding_config = EmbeddingConfig(
            base_url=(config.embedding_base_url or config.model_base_url or '').rstrip('/'),
            model=config.embedding_model or '', dim=config.embedding_dim,
            _api_key=config.embedding_api_key or config.model_api_key)
        if not model_config.configured and not embedding_config.configured:
            return
        personas = load_personas(config.personas_file, server_name=config.matrix_server_name,
                                 max_message_chars=config.max_message_chars)
        self.memory_idle = IdleSignal(pending=lambda: bool(
            self.as_txn_store is not None and
            self.as_txn_store.has_processable_transactions(datetime.now(timezone.utc))))
        if self.as_txn_store is not None:
            self.as_txn_store.set_on_new_transaction(self.memory_idle.notify)
        if embedding_config.configured and config.memory_vector_backend != 'none':
            self.memory_embedding = EmbeddingClient(
                embedding_config, timeout_seconds=config.model_timeout_seconds)
            self.memory_vectors = build_vector_store(self.memory_store, config.memory_vector_backend,
                                                          stats=self.memory_vector_stats)
        self.memory_worker = MemoryIdleWorker(
            self.memory_store, service, personas, idle_signal=self.memory_idle,
            model_client=ModelClient(model_config) if model_config.configured else None,
            summary_model=config.memory_summary_model or config.model_name or '',
            embedding=self.memory_embedding, vectors=self.memory_vectors,
            server_name=config.matrix_server_name or '')

    def _build_engine_hooks(
        self, config: Config, service: GatewayService
    ) -> Optional[EngineHooks]:
        """按配置装配内置角色引擎；不满足条件时返回 None（消费者保持 no-op 钩子）。

        激活条件（三者同时成立）：
          1. `GATEWAY_CONSUMER_ENABLED=true`（调用方已保证）；
          2. `GATEWAY_PERSONAS_FILE` 读出的 `personas` **非空**；
          3. `GATEWAY_MODEL_BASE_URL` + `GATEWAY_MODEL_NAME` 均已配置。

        人设文件非法 → `PersonaConfigError` 向上抛（**拒绝启动**，不回显文件内容）。
        未激活时打一条 info（只说明缺哪一项，不含任何配置值）。
        """
        personas = load_personas(
            config.personas_file,
            server_name=config.matrix_server_name,
            max_message_chars=config.max_message_chars,
        )
        model_config = ModelConfig(
            base_url=(config.model_base_url or "").rstrip("/"),
            model=(config.model_name or ""),
            timeout_seconds=config.model_timeout_seconds,
            _api_key=config.model_api_key,
        )
        missing = []
        if not personas:
            missing.append("GATEWAY_PERSONAS_FILE has no personas")
        if not model_config.configured:
            missing.append("GATEWAY_MODEL_BASE_URL / GATEWAY_MODEL_NAME not configured")
        if not personas:
            logging.getLogger(__name__).info(
                "AS consumer engine is inactive (record/count only): %s",
                "; ".join(missing),
            )
            return None
        if not model_config.configured and self.memory_store is None:
            logging.getLogger(__name__).info(
                "AS consumer engine is inactive (record/count only): %s",
                "; ".join(missing),
            )
            return None
        logging.getLogger(__name__).info(
            "AS consumer engine is active for %d persona(s)", len(personas)
        )
        return EngineHooks(
            personas=personas,
            client=ModelClient(model_config),
            service=service,
            dedupe=PersistentSpeechDedupe(self.as_txn_store),
            mood_service=self.mood_service,
            group_invites=self.group_invites,
            memory_store=self.memory_store,
            memory_embedding=self.memory_embedding,
            memory_vectors=self.memory_vectors,
        )

    def server_close(self) -> None:
        """关闭监听前先停消费者，再停清理线程（未启用时为 no-op）。"""
        # 消费者先于清理线程停（避免清理线程 purge 后消费者还在处理已清理行）。
        consumer = getattr(self, "as_consumer", None)
        if consumer is not None:
            consumer.stop(timeout=5.0)
        moments = getattr(self, "moments_service", None)
        if moments is not None:
            moments.stop_purge_scheduler()
        store = getattr(self, "as_txn_store", None)
        if store is not None:
            store.stop_purge_scheduler()
        proactive_worker = getattr(self, "proactive_worker", None)
        if proactive_worker is not None:
            proactive_worker.stop()
        memory_worker = getattr(self, "memory_worker", None)
        memory_worker_stopped = memory_worker is None or memory_worker.stop()
        memory_purger = getattr(self, "memory_purger", None)
        if memory_purger is not None:
            memory_purger.stop()
        memory_store = getattr(self, "memory_store", None)
        if memory_store is not None and memory_worker_stopped:
            memory_store.close()
        super().server_close()


def build_transport(config: Config) -> MatrixTransport:
    """按 `GATEWAY_MATRIX_TRANSPORT` 选择传输的唯一工厂（PR #99 审查阻塞项 3）。

    `__main__` 与 `build_server` 共用本函数：任何入口在 appservice 配置下
    都拿到真实传输，而不是静默回落内存假后端。appservice 所需的 URL /
    server_name / as_token 已在 `Config` 构造时校验，这里只负责装配；
    token 不打印。
    """
    if config.matrix_transport == "appservice":
        from .matrix_appservice import AppServiceMatrixTransport

        return AppServiceMatrixTransport(
            homeserver_url=config.homeserver_url,
            as_token=config.as_token,
            server_name=config.matrix_server_name,
            room_alias_prefix=config.room_alias_prefix,
            room_cache_ttl_seconds=config.room_alias_cache_ttl_seconds,
        )
    return InMemoryMatrixTransport()


def build_server(
    config: Optional[Config] = None,
    transport: Optional[MatrixTransport] = None,
) -> GatewayServer:
    """transport 缺省时按 config 构建（memory→内存假后端；
    appservice→AppServiceMatrixTransport，不静默降级）；显式传入的
    transport 优先（测试注入用，例如会 sleep 的假传输验证锁外投递）。
    同时装配 UsageStore / LimitStore（B2.1）。"""
    cfg = config or load_config()
    if transport is None:
        transport = build_transport(cfg)
    store = UsageStore(
        cfg.max_usage_events, cfg.usage_retention_seconds, secret=cfg.hmac_secret
    )
    limit_store = LimitStore(cfg)
    svc = GatewayService(cfg, transport, store, limit_store)
    return GatewayServer(cfg, svc, transport)

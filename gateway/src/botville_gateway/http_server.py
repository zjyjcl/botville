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

import hmac
import http.server
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .config import Config, load_config
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
from .matrix import InMemoryMatrixTransport, MatrixTransport
from .moments.http_api import handle as _handle_moments_dispatch
from .moments.service import build_moments_service
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
        if self.server.config.access_log:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

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
            if path in ("/health", "/v1/health"):
                self._handle_health()
                return
            if _is_moments_path(path):
                self._handle_moments()
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
            if _is_moments_path(path):
                self._handle_moments()
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
        """只有 `PUT /v1/limits` 支持 PUT；`/moments*` 先做用户鉴权再 404。"""
        self._request_id()
        try:
            _raw_path, _query, path = self._route_path()
            if _is_moments_path(path):
                self._handle_moments()
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

    do_OPTIONS = _method_not_allowed
    do_TRACE = _method_not_allowed

    def _internal_error(self, exc: BaseException) -> None:
        if self.server.config.access_log:  # type: ignore[attr-defined]
            self.log_error("unhandled error: %r", exc)
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
        # A2.2（issue #7）动态服务：未配置 GATEWAY_MOMENTS_DB_PATH → None
        # （模块关闭，`/moments*` 鉴权后行为与合入前一致）。
        self.moments_service = build_moments_service(config)
        log_startup_warnings(config)
        super().__init__((config.host, config.port), GatewayHandler)

    def server_close(self) -> None:
        """关闭监听前先停 moments 清理线程（未启用时为 no-op）。"""
        moments = getattr(self, "moments_service", None)
        if moments is not None:
            moments.stop_purge_scheduler()
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

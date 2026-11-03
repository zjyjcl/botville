# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""stdlib HTTP 雏形。零运行时依赖（只用 Python 标准库）。

实现的端点子集（对齐 gateway/openapi/gateway.v1.yaml）：

  GET  /health
  POST /v1/policy/evaluate
  POST /v1/conversations/{id}/messages

错误体遵循 RFC 9457 problem+json（契约 §1.3）。

使用方式：
  python3 -m botville_gateway            # 开发模式，端口 8080
  GATEWAY_PORT=9000 python3 -m botville_gateway
  GATEWAY_AUTH_TOKEN=secret python3 -m botville_gateway  # 校验 Bearer token
"""

from __future__ import annotations

import http.server
import json
import re
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .config import Config, load_config
from .matrix import InMemoryMatrixTransport
from .service import (
    GatewayDeliveryError,
    GatewayService,
    IdempotencyConflict,
    ValidationError,
)
from .store import UsageStore
from .timeutil import iso_utc, utcnow

_ROUTE_MESSAGES = re.compile(r"^/v1/conversations/([^/]+)/messages$")


def _problem(
    status: int,
    code: str,
    title: str,
    request_id: str,
    *,
    detail: Optional[str] = None,
    errors: Optional[list] = None,
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
    return body


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 请求处理器。

    `server` 属性由 `GatewayServer` 注入，携带 `config` / `service`。
    """

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.config.access_log:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _request_id(self) -> str:
        raw = self.headers.get("X-Request-Id")
        return raw if raw else f"req_{uuid.uuid4().hex}"

    def _idempotency_key(self) -> Optional[str]:
        return self.headers.get("Idempotency-Key") or None

    def _read_json(self) -> Tuple[Optional[Dict], Optional[Dict]]:
        """读取请求体并反序列化。返回 (body, error_body)；其中一个为 None。"""
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw), None
        except (json.JSONDecodeError, ValueError) as exc:
            return None, _problem(
                400, "request.invalid", "Invalid JSON", self._request_id(),
                detail=str(exc)
            )

    def _auth_ok(self, request_id: str) -> bool:
        cfg: Config = self.server.config  # type: ignore[attr-defined]
        if cfg.auth_token is None:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._send_json(
                401,
                _problem(401, "auth.missing", "Authorization header required", request_id),
                problem=True,
            )
            return False
        token = header.removeprefix("Bearer ").strip()
        if token != cfg.auth_token:
            self._send_json(
                403,
                _problem(403, "auth.forbidden", "Invalid bearer token", request_id),
                problem=True,
            )
            return False
        return True

    def _send_json(self, status: int, body: Any, *, extra_headers: Optional[Dict[str, str]] = None, problem: bool = False) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        content_type = "application/problem+json; charset=utf-8" if problem else "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:
        request_id = self._request_id()
        path = urlparse(self.path).path

        if path in ("/health", "/v1/health"):
            self._handle_health(request_id)
        else:
            self._send_json(
                404,
                _problem(404, "not_found", "Not Found", request_id),
                extra_headers={"X-Request-Id": request_id},
                problem=True,
            )

    # --------------------------------------------------------------- POST
    def do_POST(self) -> None:
        request_id = self._request_id()
        path = urlparse(self.path).path

        if not self._auth_ok(request_id):
            return

        if path == "/v1/policy/evaluate":
            self._handle_policy_evaluate(request_id)
        elif m := _ROUTE_MESSAGES.match(path):
            self._handle_send_message(request_id, m.group(1))
        else:
            self._send_json(
                404,
                _problem(404, "not_found", "Not Found", request_id),
                extra_headers={"X-Request-Id": request_id},
                problem=True,
            )

    # ----------------------------------------------------------- handlers
    def _handle_health(self, request_id: str) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        cfg: Config = self.server.config            # type: ignore[attr-defined]
        transport = self.server.transport           # type: ignore[attr-defined]
        now = utcnow()
        matrix_ok = transport.healthy()
        body = {
            "status": "ok" if matrix_ok else "degraded",
            "version": cfg.version,
            "contract_version": cfg.contract_version,
            "started_at": iso_utc(svc.started_at),
            "uptime_seconds": max(0, int((now - svc.started_at).total_seconds())),
            "checks": [
                {"name": "matrix", "status": "ok" if matrix_ok else "degraded"},
                {"name": "database", "status": "ok"},
            ],
            "modules": [
                {"name": "core", "enabled": True, "status": "ok"},
            ],
        }
        self._send_json(200, body, extra_headers={"X-Request-Id": request_id})

    def _handle_policy_evaluate(self, request_id: str) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        body, err = self._read_json()
        if err:
            self._send_json(400, err, extra_headers={"X-Request-Id": request_id}, problem=True)
            return
        try:
            result = svc.evaluate_policy(body)
        except ValidationError as exc:
            self._send_json(
                400,
                _problem(400, exc.code, "Request validation failed", request_id, detail=exc.detail),
                extra_headers={"X-Request-Id": request_id},
                problem=True,
            )
            return
        self._send_json(200, result, extra_headers={"X-Request-Id": request_id})

    def _handle_send_message(self, request_id: str, conversation_id: str) -> None:
        svc: GatewayService = self.server.service  # type: ignore[attr-defined]
        body, err = self._read_json()
        if err:
            self._send_json(400, err, extra_headers={"X-Request-Id": request_id}, problem=True)
            return
        idempotency_key = self._idempotency_key()
        try:
            receipt = svc.send_ai_message(
                conversation_id, body, idempotency_key=idempotency_key
            )
        except ValidationError as exc:
            errors = None
            if exc.field:
                errors = [{"field": exc.field, "code": exc.code, "message": exc.detail}]
            self._send_json(
                400,
                _problem(
                    400, exc.code, "Request validation failed", request_id,
                    detail=exc.detail, errors=errors
                ),
                extra_headers={"X-Request-Id": request_id},
                problem=True,
            )
            return
        except IdempotencyConflict:
            self._send_json(
                409,
                _problem(409, "idempotency.conflict",
                         "Idempotency-Key reused with different request body", request_id),
                extra_headers={"X-Request-Id": request_id},
                problem=True,
            )
            return
        except GatewayDeliveryError as exc:
            # 传输层失败是 5xx，不是策略结论；不附 Retry-After 提示策略重试。
            self._send_json(
                502,
                _problem(502, exc.code, "Matrix delivery failed", request_id, detail=exc.detail),
                extra_headers={"X-Request-Id": request_id},
                problem=True,
            )
            return
        self._send_json(200, receipt, extra_headers={"X-Request-Id": request_id})


class GatewayServer(http.server.ThreadingHTTPServer):
    """注入 config / service / transport，供 handler 访问。"""

    def __init__(self, config: Config, service: GatewayService, transport: InMemoryMatrixTransport) -> None:
        self.config = config
        self.service = service
        self.transport = transport
        super().__init__((config.host, config.port), GatewayHandler)


def build_server(config: Optional[Config] = None) -> GatewayServer:
    cfg = config or load_config()
    transport = InMemoryMatrixTransport()
    store = UsageStore()
    svc = GatewayService(cfg, transport, store)
    return GatewayServer(cfg, svc, transport)

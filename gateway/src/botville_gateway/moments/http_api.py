# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""`/v1/moments*` 的 HTTP 分发（挂在 http_server 鉴权之后）。

http_server 在 `_auth_user_ok()` 通过后调用本模块的 `handle(handler)`；
路由只做**方法分发与请求翻译**，业务与可见性全在 `service`。本模块不复制
JSON / problem 辅助，直接复用 handler 的 `_send_json` / `_send_problem` /
`_read_json`（1 MiB 上限、Content-Type 检查等语义与网关其他端点一致）。

所有「越权 / 不存在 / 非 ULID id / 软删」统一 404 `moment.not_found`，
响应体由 `moment_not_found_problem` 单一构造函数生成——同 path 同
X-Request-Id 下逐字节相同，不泄露存在性（moments.md §6）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import parse_qs

from ..errors import IdempotencyConflict, ValidationError
from .service import (
    MomentNotFound,
    MomentsService,
    PersonaNotOwned,
    RevisionConflict,
)

# 与 examples/problem-moment-not-found.json 逐字段一致（detail / title 原文）。
MOMENT_NOT_FOUND_TITLE = "Moment not found"
MOMENT_NOT_FOUND_DETAIL = (
    "Moment does not exist or is not visible to the caller; the response is"
    " byte-identical in both cases."
)

# 各端点的 limit 上限（契约：moments / comments / likes 均为 1..50，默认 20）。
MAX_PAGE_LIMIT = 50
DEFAULT_PAGE_LIMIT = 20


def moment_not_found_problem(
    request_id: str, instance: str
) -> Dict[str, Any]:
    """404 moment.not_found 的唯一构造函数（字段顺序固定，保证逐字节相同）。"""
    return {
        "type": "urn:botville:error:moment.not_found",
        "title": MOMENT_NOT_FOUND_TITLE,
        "status": 404,
        "detail": MOMENT_NOT_FOUND_DETAIL,
        "instance": instance,
        "code": "moment.not_found",
        "request_id": request_id,
    }


def _send_no_content(handler) -> None:
    """204 响应（无响应体）；头部语义与 handler._send_json 保持一致。"""
    handler.send_response(204)
    handler.send_header("Content-Length", "0")
    handler.send_header("X-Request-Id", handler._request_id())
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def _parse_limit(params: Dict[str, list]) -> int:
    raw = params.get("limit")
    if not raw or not raw[-1]:
        return DEFAULT_PAGE_LIMIT
    text = raw[-1]
    try:
        limit = int(text)
    except ValueError as exc:
        raise ValidationError(
            "request.invalid", "'limit' must be an integer", "limit"
        ) from exc
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValidationError(
            "request.invalid",
            f"'limit' must be between 1 and {MAX_PAGE_LIMIT}", "limit",
        )
    return limit


def _query_value(params: Dict[str, list], name: str) -> Optional[str]:
    values = params.get(name)
    if not values or not values[-1]:
        return None
    return values[-1]


def _method_not_allowed(handler, path: str) -> None:
    handler._send_problem(
        405,
        "request.invalid",
        "Method Not Allowed",
        detail=f"{handler.command} is not supported on {path}",
    )


def handle(handler) -> None:
    """moments 分发入口。前置条件：`_auth_user_ok()` 已通过（401 先于 404）。"""
    service: Optional[MomentsService] = handler.server.moments_service
    if service is None:  # pragma: no cover - http_server 已在调用前判 None
        handler._send_problem(404, "not_found", "Not Found")
        return
    _raw, query, path = handler._route_path()
    instance = path
    params = parse_qs(query, keep_blank_values=True)
    viewer = handler._user_identity()
    rest = path[len("/v1/moments"):]
    segments = [seg for seg in rest.split("/") if seg]
    method = handler.command

    try:
        _route(handler, service, method, path, instance, params, segments, viewer)
    except MomentNotFound:
        handler._send_json(
            404,
            moment_not_found_problem(handler._request_id(), instance),
            problem=True,
        )
    except PersonaNotOwned:
        handler._send_problem(
            403,
            "persona.not_owned",
            "Persona not owned",
            detail="author_persona_id does not belong to the caller",
        )
    except RevisionConflict:
        handler._send_problem(
            409,
            "moment.revision_conflict",
            "Revision conflict",
            detail="the 'revision' does not match the current moment revision",
        )
    except IdempotencyConflict:
        handler._send_problem(
            409,
            "idempotency.conflict",
            "Idempotency-Key reused with different request body",
        )
    except ValidationError as exc:
        # 400 / 422 按 http_status 属性路由（与网关其他端点同一映射）。
        handler._send_validation_error(exc)
    except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
        handler.close_connection = True


def _route(
    handler, service, method, path, instance, params, segments, viewer
) -> None:
    # 路由树（路径已剥离部署前缀）：
    #   /v1/moments
    #   /v1/moments/{mid}
    #   /v1/moments/{mid}/comments            /v1/moments/{mid}/comments/{cid}
    #   /v1/moments/{mid}/likes               /v1/moments/{mid}/likes/me
    if not segments:
        if method == "GET":
            page = service.list_moments(
                viewer,
                author=_query_value(params, "author"),
                cursor=_query_value(params, "cursor"),
                limit=_parse_limit(params),
            )
            handler._send_json(200, page)
            return
        if method == "POST":
            _create_moment(handler, service, viewer)
            return
        _method_not_allowed(handler, path)
        return

    moment_id = segments[0]
    if len(segments) == 1:
        if method == "GET":
            handler._send_json(200, service.get_moment(viewer, moment_id))
            return
        if method == "PATCH":
            _update_moment(handler, service, viewer, moment_id)
            return
        if method == "DELETE":
            service.delete_moment(viewer, moment_id)
            _send_no_content(handler)
            return
        _method_not_allowed(handler, path)
        return

    sub = segments[1]
    if sub == "comments" and len(segments) == 2:
        if method == "GET":
            page = service.list_comments(
                viewer, moment_id,
                cursor=_query_value(params, "cursor"),
                limit=_parse_limit(params),
            )
            handler._send_json(200, page)
            return
        if method == "POST":
            _create_comment(handler, service, viewer, moment_id)
            return
        _method_not_allowed(handler, path)
        return

    if sub == "comments" and len(segments) == 3:
        if method == "DELETE":
            service.delete_comment(viewer, moment_id, segments[2])
            _send_no_content(handler)
            return
        _method_not_allowed(handler, path)
        return

    if sub == "likes" and len(segments) == 2:
        if method == "GET":
            page = service.list_likes(
                viewer, moment_id,
                cursor=_query_value(params, "cursor"),
                limit=_parse_limit(params),
            )
            handler._send_json(200, page)
            return
        _method_not_allowed(handler, path)
        return

    if sub == "likes" and len(segments) == 3 and segments[2] == "me":
        if method == "PUT":
            handler._send_json(200, service.like_moment(viewer, moment_id))
            return
        if method == "DELETE":
            handler._send_json(200, service.unlike_moment(viewer, moment_id))
            return
        _method_not_allowed(handler, path)
        return

    # 未知子路径 → 现有惯例的通用 404（不进入 moments 404 构造函数）。
    handler._send_problem(404, "not_found", "Not Found")


def _read_body(handler) -> Optional[Dict[str, Any]]:
    """读请求体；已发送 400 problem 时返回 None（调用方直接 return）。"""
    body, problem = handler._read_json()
    if problem is not None:
        handler._send_json(400, problem, problem=True)
        return None
    return body


def _create_moment(handler, service, viewer) -> None:
    payload = _read_body(handler)
    if payload is None:
        return
    idempotency_key = handler._idempotency_key()
    moment = service.create_moment(viewer, payload, idempotency_key)
    handler._send_json(201, moment)


def _update_moment(handler, service, viewer, moment_id) -> None:
    payload = _read_body(handler)
    if payload is None:
        return
    moment = service.update_moment(viewer, moment_id, payload)
    handler._send_json(200, moment)


def _create_comment(handler, service, viewer, moment_id) -> None:
    payload = _read_body(handler)
    if payload is None:
        return
    idempotency_key = handler._idempotency_key()
    comment = service.create_comment(viewer, moment_id, payload, idempotency_key)
    handler._send_json(201, comment)

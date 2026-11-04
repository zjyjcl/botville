# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""网关内部异常 → HTTP problem+json 的映射载体。

单独成模块是为了避免 `service` ⇄ `limits` 的循环导入：两边都需要
`ValidationError` / `LimitError`。`service` 仍以原名字导出这些异常，
既有 `from botville_gateway.service import ValidationError` 不受影响。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class ValidationError(Exception):
    """请求体不符合契约（映射为 400 problem+json）。"""

    def __init__(self, code: str, detail: str, field: Optional[str] = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.field = field
        self.http_status: int = 400


class PeerValidationError(ValidationError):
    """peer_id 校验失败（422 request.invalid）。

    结构上是请求校验错误（code=request.invalid），但语义上属于会话成员约束，
    映射为 422 而非 400（§6.4、B2.2）。HTTP 层按 `http_status` 属性路由。
    """

    def __init__(self, detail: str) -> None:
        super().__init__("request.invalid", detail, field="peer_id")
        self.http_status = 422


class LimitError(Exception):
    """限额语义不合法（映射为 422 `limit.out_of_range`）。

    与 `ValidationError`（400，语法/字段名错误）严格区分：值超出契约范围、
    放宽超过平台底线、平台层只读等都属于「语法正确但语义不合法」。
    """

    def __init__(self, code: str, detail: str, errors: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.errors = errors or []


class IdempotencyConflict(Exception):
    """同一 Idempotency-Key 配了不同请求体。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"idempotency key {key!r} was reused with a different request")
        self.key = key


class GatewayDeliveryError(Exception):
    """transport 投递失败（映射为 500 internal.error，不是策略结论）。

    注意：契约状态码集合不含 502/`matrix.unavailable`；B6 选方案(a)，
    统一映射为 500 internal.error，不改契约状态码集合。
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

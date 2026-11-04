# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""botville AI Gateway — B2.1 三层限额与 token 预算 + A1.3 appservice transport.

Package layout
  config            env-based configuration
  matrix            MatrixTransport Protocol + InMemoryMatrixTransport stub
  matrix_appservice appservice transport (AS token masquerade, A1.3)
  policy            三层限速（消息/token/间隔）+ 深夜静默 → PolicyDecision
  limits            三层限额存储、校验、生效值计算
  store             内存 UsageEvent 日志（append-only、容量有界）
  service           send_ai_message() 编排、幂等、用量账单查询
  http_server       stdlib HTTP 服务器（/health、/policy/evaluate、/messages、/limits、/usage）
  errors            异常类（ValidationError、LimitError、…）
  timeutil          UTC `Z` 结尾的 RFC 3339 格式化
"""

from .config import Config, load_config
from .errors import GatewayDeliveryError, IdempotencyConflict, LimitError, ValidationError
from .limits import LimitStore, EffectiveLimits, LimitLayer
from .matrix import InMemoryMatrixTransport, MatrixTransport
from .matrix_appservice import AppServiceMatrixTransport
from .policy import PolicyDecision, PolicyEngine, estimate_tokens
from .service import GatewayService
from .store import UsageEvent, UsageStore

__all__ = [
    "Config",
    "load_config",
    "AppServiceMatrixTransport",
    "GatewayDeliveryError",
    "IdempotencyConflict",
    "LimitError",
    "ValidationError",
    "LimitStore",
    "EffectiveLimits",
    "LimitLayer",
    "InMemoryMatrixTransport",
    "MatrixTransport",
    "PolicyDecision",
    "PolicyEngine",
    "estimate_tokens",
    "GatewayService",
    "UsageEvent",
    "UsageStore",
]

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""botville AI Gateway — B1.1 skeleton + A1.3 appservice transport.

Package layout
  config            env-based configuration
  matrix            MatrixTransport Protocol + InMemoryMatrixTransport stub
  matrix_appservice appservice transport (AS token masquerade, A1.3)
  policy            rate-limit (sliding window) + silent-hours engine
  store             in-memory UsageEvent log
  service           send_ai_message() orchestrator
  http_server       stdlib HTTP server (zero deps)
"""

from .config import Config, load_config
from .matrix import InMemoryMatrixTransport, MatrixTransport
from .matrix_appservice import AppServiceMatrixTransport
from .policy import PolicyDecision, PolicyEngine
from .service import GatewayService
from .store import UsageEvent, UsageStore

__all__ = [
    "Config",
    "load_config",
    "AppServiceMatrixTransport",
    "InMemoryMatrixTransport",
    "MatrixTransport",
    "PolicyDecision",
    "PolicyEngine",
    "GatewayService",
    "UsageEvent",
    "UsageStore",
]

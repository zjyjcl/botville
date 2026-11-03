# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""botville AI Gateway — B1.1 skeleton.

Package layout
  config      env-based configuration
  matrix      MatrixTransport Protocol + InMemoryMatrixTransport stub
  policy      rate-limit (sliding window) + silent-hours engine
  store       in-memory UsageEvent log
  service     send_ai_message() orchestrator
  http_server stdlib HTTP server (zero deps)
"""

from .config import Config, load_config
from .matrix import InMemoryMatrixTransport, MatrixTransport
from .policy import PolicyDecision, PolicyEngine
from .service import GatewayService
from .store import UsageEvent, UsageStore

__all__ = [
    "Config",
    "load_config",
    "InMemoryMatrixTransport",
    "MatrixTransport",
    "PolicyDecision",
    "PolicyEngine",
    "GatewayService",
    "UsageEvent",
    "UsageStore",
]

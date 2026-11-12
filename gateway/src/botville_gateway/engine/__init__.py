# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.4 内置角色引擎（简单接入，contract §0 / §1.7.8 ②①）。

「填 API + 人设，平台套角色引擎」的运行时：网关进程内把消费骨架的
`on_human_text` 钩子接到模型（限额 → 模型 → 发言），人设来自只读 JSON 文件
（`GATEWAY_PERSONAS_FILE`），模型配置来自 `GATEWAY_MODEL_*` 环境变量。

三个子模块：

- `personas.py`：人设文件读取与**启动期**校验（只认本服 `@ai_*`）；
- `model_client.py`：OpenAI 兼容 `POST /chat/completions`（urllib，零依赖），
  错误映射到 `TransientUnavailable` / `ModelError`；
- `engine.py`：`EngineHooks`（`as_consumer.ConsumerHooks` 实现）与 per-room
  最近消息环形缓冲；上下文按 §7.2 四区隔离拼装。

未配置模型 / 人设为空 / 消费开关关闭 → 引擎不发言，只记录与计数，
`/health` 不 degraded（contract §1.7.8 ⑫）。
"""

from __future__ import annotations

from .engine import MAX_CONTEXT_MESSAGES, EngineHooks
from .model_client import (
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    ModelClient,
    ModelConfig,
    ModelConfigError,
    ModelError,
)
from .personas import Persona, PersonaConfigError, load_personas

__all__ = [
    "DEFAULT_MODEL_TIMEOUT_SECONDS",
    "EngineHooks",
    "MAX_CONTEXT_MESSAGES",
    "ModelClient",
    "ModelConfig",
    "ModelConfigError",
    "ModelError",
    "Persona",
    "PersonaConfigError",
    "load_personas",
]

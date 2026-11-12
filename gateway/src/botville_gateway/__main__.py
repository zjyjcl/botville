# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""`python -m botville_gateway` 入口：读环境变量并启动 HTTP 雏形。"""

from __future__ import annotations

import sys
import sqlite3

from .config import ConfigError, load_config
from .http_server import build_server, build_transport
from .engine.personas import PersonaConfigError


def main(argv=None) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # GATEWAY_DEV_MODE=true 是唯一允许无鉴权启动的方式；设置 token 永远优先。
    if config.auth_token is None:
        print(
            "WARNING: GATEWAY_AUTH_TOKEN is not set and GATEWAY_DEV_MODE=true — "
            "Authorization is NOT checked. Never run this way outside local development.",
            file=sys.stderr,
        )

    # 传输选择收敛在 http_server.build_transport（与 build_server 同一逻辑）。
    transport = build_transport(config)

    # appservice 模式启动即校验可达性与 AS token（whoami）：配错就 fail fast，
    # 不带病运行到第一条消息才失败，也不让 /health 一开始就误报 degraded。
    if config.matrix_transport == "appservice" and not transport.healthy():
        print(
            "config error: homeserver health check failed (whoami with the appservice "
            "token); check GATEWAY_HOMESERVER_URL / GATEWAY_AS_TOKEN and that the "
            "homeserver loaded the appservice registration",
            file=sys.stderr,
        )
        return 2

    try:
        server = build_server(config, transport)
    except PersonaConfigError:
        print("config error: persona configuration is invalid", file=sys.stderr)
        return 2
    except (sqlite3.Error, RuntimeError):
        print("config error: memory storage initialization failed", file=sys.stderr)
        return 2
    except OSError:
        print("config error: gateway startup failed", file=sys.stderr)
        return 2

    host, port = config.host, config.port
    print(f"botville gateway {config.version} listening on http://{host}:{port}")
    print("  GET  /health")
    print("  POST /v1/policy/evaluate")
    print("  POST /v1/conversations/{conversation_id}/messages")
    print("  PUT  /_matrix/app/v1/transactions/{txnId}")
    print("  POST /_matrix/app/v1/ping")
    if config.auth_token is None:
        print("  (dev mode: Authorization is not checked)")
    # AS 回调：未配置 hs_token 时网关照常启动，但两条回调一律 403。
    # compose 在 `make up GATEWAY=1` 且 HS 为 synapse/tuwunel 时注入（#193）。
    if config.hs_token is None:
        print(
            "  (AS callbacks: GATEWAY_HS_TOKEN is not set — /_matrix/app/v1/* returns "
            "403 M_FORBIDDEN until the deployment injects the registration's hs_token)"
        )
    if config.as_txn_db_path is None:
        print(
            "  (AS transactions: GATEWAY_AS_TXN_DB_PATH is not set — transactions "
            "return 500 M_UNKNOWN instead of acknowledging)"
        )
    elif config.as_txn_purge_interval_seconds:
        print(
            f"  (AS transactions: retention {config.as_txn_retention_days}d, purge every "
            f"{config.as_txn_purge_interval_seconds}s — bodies are cleared, tombstones kept)"
        )
    else:
        print("  (AS transactions: retention purge disabled — the transaction DB keeps growing)")
    # 消费骨架 + 内置引擎（contract §1.7.8）：只打印状态，不打印任何敏感值。
    if not config.consumer_enabled:
        print("  (AS consumer: disabled — GATEWAY_CONSUMER_ENABLED=false)")
    elif server.as_consumer is None:
        print(
            "  (AS consumer: enabled but NOT running — GATEWAY_AS_TXN_DB_PATH is not set)"
        )
    elif server.engine_hooks is None:
        print(
            "  (AS consumer: enabled — record/count only; the built-in engine is "
            "inactive because GATEWAY_PERSONAS_FILE has no personas and/or "
            "GATEWAY_MODEL_BASE_URL / GATEWAY_MODEL_NAME are not configured)"
        )
    elif not (config.model_base_url and config.model_name):
        print(
            "  (AS consumer: enabled — persistent memory records layer-1 events; "
            "the built-in engine will not speak until a model is configured)"
        )
    else:
        print(
            "  (AS consumer: enabled — the built-in engine speaks via "
            "policy evaluate -> model -> send for the configured personas)"
        )
    print(f"transport: {type(transport).__name__} (GATEWAY_MATRIX_TRANSPORT={config.matrix_transport})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

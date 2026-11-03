# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""`python -m botville_gateway` 入口：读环境变量并启动 HTTP 雏形。"""

from __future__ import annotations

import sys

from .config import ConfigError, load_config
from .http_server import build_server, build_transport


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
    except OSError as exc:
        print(f"config error: cannot bind {config.host}:{config.port}: {exc}", file=sys.stderr)
        return 2

    host, port = config.host, config.port
    print(f"botville gateway {config.version} listening on http://{host}:{port}")
    print("  GET  /health")
    print("  POST /v1/policy/evaluate")
    print("  POST /v1/conversations/{conversation_id}/messages")
    if config.auth_token is None:
        print("  (dev mode: Authorization is not checked)")
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

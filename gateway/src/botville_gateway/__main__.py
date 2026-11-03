# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""`python -m botville_gateway` 入口：读环境变量并启动 HTTP 雏形。"""

from __future__ import annotations

import sys

from .config import Config, ConfigError, load_config
from .http_server import build_server
from .matrix import InMemoryMatrixTransport, MatrixTransport


def _build_transport(config: Config) -> MatrixTransport:
    """按 `GATEWAY_MATRIX_TRANSPORT` 选择传输（A1.3）。

    appservice 所需的 URL / server_name / as_token 已在 `Config` 构造时校验，
    这里只负责装配；token 不打印。
    """
    if config.matrix_transport == "appservice":
        from .matrix_appservice import AppServiceMatrixTransport

        return AppServiceMatrixTransport(
            homeserver_url=config.homeserver_url,
            as_token=config.as_token,
            server_name=config.matrix_server_name,
        )
    return InMemoryMatrixTransport()


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

    transport = _build_transport(config)
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

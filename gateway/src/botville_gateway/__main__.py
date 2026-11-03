# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""`python -m botville_gateway` 入口：读环境变量并启动 HTTP 雏形。"""

from __future__ import annotations

import sys

from .config import ConfigError, load_config
from .http_server import build_server


def main(argv=None) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    server = build_server(config)
    host, port = config.host, config.port
    print(f"botville gateway {config.version} listening on http://{host}:{port}")
    print("  GET  /health")
    print("  POST /v1/policy/evaluate")
    print("  POST /v1/conversations/{conversation_id}/messages")
    if config.auth_token is None:
        print("  (dev mode: no GATEWAY_AUTH_TOKEN set, Authorization is not checked)")
    print("transport: InMemoryMatrixTransport (A1.3 appservice not wired yet)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

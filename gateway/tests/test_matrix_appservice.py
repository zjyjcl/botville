# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""AppServiceMatrixTransport 单测（A1.3）：http.server 假 HS，仅标准库。

运行：python3 -m unittest discover -s gateway/tests -v

覆盖：
- m.login.application_service 注册（Bearer as_token + inhibit_login）与进程内缓存；
- M_USER_IN_USE 视为已存在（幂等）；
- M_FORBIDDEN（未加入房间）→ 自动 join 后重试一次；
- 命名空间外 sender 客户端侧拒绝（零 HTTP 请求）；
- HTTP 错误映射 DeliveryError(code=errcode / http.<status>)；
- masquerade 参数（?user_id=）与 markdown / reply 内容字段；
- healthy()；
- config：GATEWAY_MATRIX_TRANSPORT 的默认/校验/fail-closed 与 token 不进 repr。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Tuple

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 让 `python3 -m unittest discover -s gateway/tests` 无需安装即可导入包。
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import ConfigError, load_config  # noqa: E402
from botville_gateway.matrix import DeliveryError, MatrixTransport  # noqa: E402
from botville_gateway.matrix_appservice import (  # noqa: E402
    AppServiceMatrixTransport,
)

AS_TOKEN = "as-unit-test-token"
SERVER_NAME = "localhost"
ROOM = "!room:localhost"
AI = "@ai_demo:localhost"


class FakeHS:
    """可编程假 homeserver：按 (method, path 前缀) 队列依次回放响应。

    每条路由的响应队列按请求次序弹出；队列空了以后统一回 200 {}。
    所有收到的请求（method/path/query/Authorization/body）记录在 requests。
    """

    def __init__(self):
        outer = self
        self.requests: List[dict] = []
        self.routes: Dict[Tuple[str, str], List[Tuple[int, dict]]] = {}

        class Handler(BaseHTTPRequestHandler):
            def _respond(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parsed_path, _, query = self.path.partition("?")
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = None
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": parsed_path,
                        "query": urllib.parse.parse_qs(query or ""),
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                status, payload = 200, {}
                for (method, prefix), queue in outer.routes.items():
                    if self.command == method and parsed_path.startswith(prefix):
                        if queue:
                            status, payload = queue.pop(0)
                        break
                payload_bytes = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload_bytes)))
                self.end_headers()
                self.wfile.write(payload_bytes)

            do_GET = do_POST = do_PUT = _respond

            def log_message(self, *args):  # 保持测试输出安静
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address
        return "http://{}:{}".format(host, port)

    def route(self, method: str, prefix: str, responses: List[Tuple[int, dict]]) -> None:
        self.routes[(method, prefix)] = list(responses)

    def matching(self, method: str, prefix: str) -> List[dict]:
        return [
            r for r in self.requests if r["method"] == method and r["path"].startswith(prefix)
        ]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class AppServiceTransportTest(unittest.TestCase):
    def setUp(self):
        self.hs = FakeHS()
        self.addCleanup(self.hs.close)
        self.hs.route("GET", "/_matrix/client/versions", [(200, {"versions": ["v1.11"]})])
        self.hs.route("POST", "/_matrix/client/v3/register", [(200, {"user_id": AI})])
        self.hs.route("POST", "/_matrix/client/v3/join/", [(200, {"room_id": ROOM})])
        self.hs.route(
            "PUT", "/_matrix/client/v3/rooms/", [(200, {"event_id": "$e1"}), (200, {"event_id": "$e2"})]
        )

    def make_transport(self, **overrides) -> AppServiceMatrixTransport:
        kwargs = dict(
            homeserver_url=self.hs.base_url, as_token=AS_TOKEN, server_name=SERVER_NAME
        )
        kwargs.update(overrides)
        return AppServiceMatrixTransport(**kwargs)

    def test_satisfies_matrix_transport_protocol(self):
        self.assertIsInstance(self.make_transport(), MatrixTransport)

    def test_register_then_send_with_masquerade(self):
        transport = self.make_transport()
        event = transport.send_text(room_id=ROOM, sender_id=AI, text="你好 botville")

        self.assertEqual(event.event_id, "$e1")
        self.assertEqual(event.room_id, ROOM)
        self.assertEqual(event.sender_id, AI)
        self.assertEqual(event.text, "你好 botville")
        self.assertEqual(event.content_type, "text")
        self.assertIsNone(event.reply_to)
        # service 层会把它交给 iso_utc()：必须带时区。
        self.assertIsNotNone(event.sent_at.tzinfo)

        registers = self.hs.matching("POST", "/_matrix/client/v3/register")
        self.assertEqual(len(registers), 1)
        self.assertEqual(registers[0]["authorization"], "Bearer " + AS_TOKEN)
        self.assertEqual(
            registers[0]["body"],
            {
                "type": "m.login.application_service",
                "username": "ai_demo",
                "inhibit_login": True,
            },
        )

        sends = self.hs.matching("PUT", "/_matrix/client/v3/rooms/")
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["query"].get("user_id"), [AI])
        self.assertEqual(sends[0]["authorization"], "Bearer " + AS_TOKEN)
        self.assertEqual(sends[0]["body"], {"msgtype": "m.text", "body": "你好 botville"})

        # 第二条消息：ensure_user 命中缓存，不再注册。
        transport.send_text(room_id=ROOM, sender_id=AI, text="second")
        self.assertEqual(len(self.hs.matching("POST", "/_matrix/client/v3/register")), 1)
        self.assertEqual(len(self.hs.matching("PUT", "/_matrix/client/v3/rooms/")), 2)

    def test_user_in_use_is_treated_as_existing(self):
        self.hs.route(
            "POST",
            "/_matrix/client/v3/register",
            [(400, {"errcode": "M_USER_IN_USE", "error": "User ID already taken"})],
        )
        transport = self.make_transport()
        event = transport.send_text(room_id=ROOM, sender_id=AI, text="hello")
        self.assertEqual(event.event_id, "$e1")
        # 已存在同样进缓存：第二次不再尝试注册。
        transport.send_text(room_id=ROOM, sender_id=AI, text="again")
        self.assertEqual(len(self.hs.matching("POST", "/_matrix/client/v3/register")), 1)

    def test_forbidden_triggers_join_and_single_retry(self):
        self.hs.route(
            "PUT",
            "/_matrix/client/v3/rooms/",
            [
                (403, {"errcode": "M_FORBIDDEN", "error": "not in room"}),
                (200, {"event_id": "$after-join"}),
            ],
        )
        transport = self.make_transport()
        event = transport.send_text(room_id=ROOM, sender_id=AI, text="hello")

        self.assertEqual(event.event_id, "$after-join")
        joins = self.hs.matching("POST", "/_matrix/client/v3/join/")
        self.assertEqual(len(joins), 1)
        self.assertEqual(joins[0]["query"].get("user_id"), [AI])
        # join 的路径是 URL 编码后的 room id。
        self.assertIn(
            urllib.parse.quote(ROOM, safe=""), joins[0]["path"]
        )
        sends = self.hs.matching("PUT", "/_matrix/client/v3/rooms/")
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0]["body"], sends[1]["body"])

    def test_other_errors_are_not_retried(self):
        self.hs.route(
            "PUT",
            "/_matrix/client/v3/rooms/",
            [(429, {"errcode": "M_LIMIT_EXCEEDED", "error": "slow down"})],
        )
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.send_text(room_id=ROOM, sender_id=AI, text="hello")
        self.assertEqual(ctx.exception.code, "M_LIMIT_EXCEEDED")
        # 非 M_FORBIDDEN 不触发 join 重试。
        self.assertEqual(len(self.hs.matching("POST", "/_matrix/client/v3/join/")), 0)
        self.assertEqual(len(self.hs.matching("PUT", "/_matrix/client/v3/rooms/")), 1)

    def test_error_without_errcode_maps_to_http_status(self):
        self.hs.route("PUT", "/_matrix/client/v3/rooms/", [(502, {})])
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.send_text(room_id=ROOM, sender_id=AI, text="hello")
        self.assertEqual(ctx.exception.code, "http.502")

    def test_sender_outside_namespace_rejected_without_requests(self):
        transport = self.make_transport()
        for bad in ("@bob:localhost", "@ai_x:elsewhere.example", "@ai_:localhost"):
            with self.assertRaises(DeliveryError) as ctx:
                transport.send_text(room_id=ROOM, sender_id=bad, text="hello")
            self.assertEqual(ctx.exception.code, "appservice.namespace", bad)
        # 客户端侧拒绝：一次 HTTP 都没发（也没有把 as_token 发出去）。
        self.assertEqual(self.hs.requests, [])

    def test_markdown_and_reply_content(self):
        transport = self.make_transport()
        event = transport.send_text(
            room_id=ROOM,
            sender_id=AI,
            text="**hi**",
            reply_to="$orig",
            content_type="markdown",
        )
        self.assertEqual(event.content_type, "markdown")
        self.assertEqual(event.reply_to, "$orig")
        send = self.hs.matching("PUT", "/_matrix/client/v3/rooms/")[0]
        self.assertEqual(
            send["body"],
            {
                "msgtype": "m.text",
                "body": "**hi**",
                # markdown 不降级：body 原文 + 自定义类型标注（B 线不做 HTML 转换）。
                "org.botville.content_type": "markdown",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$orig"}},
            },
        )

    def test_healthy(self):
        self.assertTrue(self.make_transport().healthy())
        # 不可达的 homeserver → False（不抛异常；连接失败/超时都算不健康）。
        dead = AppServiceMatrixTransport(
            homeserver_url="http://127.0.0.1:1",
            as_token=AS_TOKEN,
            server_name=SERVER_NAME,
            timeout=0.5,
        )
        self.assertFalse(dead.healthy())


class AppServiceTransportConfigTest(unittest.TestCase):
    """GATEWAY_MATRIX_TRANSPORT 相关配置：默认保持现状、appservice fail closed。"""

    BASE_ENV = {
        "GATEWAY_AUTH_TOKEN": "gateway-token",
        "GATEWAY_HOMESERVER_URL": "http://127.0.0.1:8008",
        "GATEWAY_MATRIX_SERVER_NAME": "localhost",
        "GATEWAY_AS_TOKEN": "secret-as-token",
    }

    def test_default_transport_is_memory(self):
        cfg = load_config({"GATEWAY_AUTH_TOKEN": "gateway-token"})
        self.assertEqual(cfg.matrix_transport, "memory")
        self.assertIsNone(cfg.homeserver_url)
        self.assertIsNone(cfg.as_token)

    def test_appservice_requires_full_config(self):
        # 缺 URL / token / server_name 都拒绝启动。
        for missing in (
            "GATEWAY_HOMESERVER_URL",
            "GATEWAY_AS_TOKEN",
            "GATEWAY_MATRIX_SERVER_NAME",
        ):
            env = {k: v for k, v in self.BASE_ENV.items() if k != missing}
            env["GATEWAY_MATRIX_TRANSPORT"] = "appservice"
            with self.assertRaises(ConfigError, msg=missing):
                load_config(env)
        # URL 必须是 http(s)。
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
        env["GATEWAY_HOMESERVER_URL"] = "127.0.0.1:8008"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_placeholder_as_token_rejected(self):
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
        env["GATEWAY_AS_TOKEN"] = "<change-me>"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_unknown_transport_value_rejected(self):
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="grpc")
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_valid_appservice_config_and_token_stays_out_of_repr(self):
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
        cfg = load_config(env)
        self.assertEqual(cfg.matrix_transport, "appservice")
        self.assertEqual(cfg.matrix_server_name, "localhost")
        # as_token 是命名空间管理员凭据：不得出现在 repr（日志/断言消息）里。
        self.assertNotIn("secret-as-token", repr(cfg))


if __name__ == "__main__":
    unittest.main()

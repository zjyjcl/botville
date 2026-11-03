# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""AppServiceMatrixTransport 单测（A1.3）：http.server 假 HS，仅标准库。

运行：python3 -m unittest discover -s gateway/tests -v

覆盖：
- m.login.application_service 注册（Bearer as_token + inhibit_login）与进程内缓存；
  200 响应的 user_id 必须与请求一致（缺失/不符 → appservice.namespace）；
- M_USER_IN_USE 视为已存在（幂等）；
- M_FORBIDDEN（未加入房间）→ 自动 join 后重试一次；
- 身份/房间映射（PR #99 阻塞项 1）：persona_id → @ai_<persona>:<server>；
  conversation_id → 房间别名 directory/room 解析 + 缓存；别名不存在 →
  conversation.not_found；非法字符集 → 客户端侧拒绝（零请求）；
- 命名空间外 sender 客户端侧拒绝（send_text/ensure_user/join 全部校验）；
- HTTP 错误映射 DeliveryError(code=errcode / http.<status>)；非 JSON 2xx →
  appservice.response；重定向不跟随；
- masquerade 参数（?user_id=）与 markdown / reply 内容字段；
- healthy()（带 AS token 的 whoami）；
- config：GATEWAY_MATRIX_TRANSPORT 的默认/校验/fail-closed、as_token 强度、
  server_name 归一化、明文 http 主机白名单、别名前缀校验、token 不进 repr。
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
    响应体是 dict → JSON；bytes → 原样返回（非 JSON 2xx 用）；dict 带
    "__redirect__" → 返回 3xx + Location（重定向用）。
    所有收到的请求（method/path/query/Authorization/body）记录在 requests。
    """

    def __init__(self):
        outer = self
        self.requests: List[dict] = []
        self.routes: Dict[Tuple[str, str], List[Tuple[int, object]]] = {}

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
                if isinstance(payload, dict) and "__redirect__" in payload:
                    self.send_response(payload.get("__status__", 302))
                    self.send_header("Location", payload["__redirect__"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload_bytes = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload).encode("utf-8")
                )
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

    def route(self, method: str, prefix: str, responses: List[Tuple[int, object]]) -> None:
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
        # healthy()：带 AS token 的 whoami（versions 校验不了 token）。
        self.hs.route(
            "GET",
            "/_matrix/client/v3/account/whoami",
            [(200, {"user_id": "@botville-gateway:" + SERVER_NAME})],
        )
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

    # --------------------------------------------- id mapping (PR #99 #1)
    def test_persona_id_mapped_into_ai_namespace(self):
        self.hs.route(
            "POST",
            "/_matrix/client/v3/register",
            [(200, {"user_id": "@ai_persona_demo:" + SERVER_NAME})],
        )
        transport = self.make_transport()
        event = transport.send_text(room_id=ROOM, sender_id="persona_demo", text="hi")

        self.assertEqual(event.sender_id, "@ai_persona_demo:" + SERVER_NAME)
        register = self.hs.matching("POST", "/_matrix/client/v3/register")[0]
        self.assertEqual(register["body"]["username"], "ai_persona_demo")
        send = self.hs.matching("PUT", "/_matrix/client/v3/rooms/")[0]
        self.assertEqual(send["query"].get("user_id"), ["@ai_persona_demo:" + SERVER_NAME])
        self.assertEqual(send["authorization"], "Bearer " + AS_TOKEN)

    def test_persona_charset_rejected_client_side(self):
        """persona id 字符集 ^[a-z0-9._=-]{1,64}$：大写即非法（不做静默小写化），
        空白/特殊字符/超长同理；客户端侧拒绝，零 HTTP 请求。"""
        transport = self.make_transport()
        for bad in ("Persona_Demo", "per sona", "per$ona", "per/sona", "x" * 65):
            with self.assertRaises(DeliveryError) as ctx:
                transport.send_text(room_id=ROOM, sender_id=bad, text="hi")
            self.assertEqual(ctx.exception.code, "appservice.namespace", bad)
        self.assertEqual(self.hs.requests, [])

    def test_conversation_id_resolved_via_room_alias_and_cached(self):
        self.hs.route(
            "GET",
            "/_matrix/client/v3/directory/room/",
            [(200, {"room_id": "!conv_room:" + SERVER_NAME, "servers": [SERVER_NAME]})],
        )
        transport = self.make_transport()
        event = transport.send_text(room_id="conv_demo", sender_id=AI, text="hi")

        self.assertEqual(event.room_id, "!conv_room:" + SERVER_NAME)
        lookups = self.hs.matching("GET", "/_matrix/client/v3/directory/room/")
        self.assertEqual(len(lookups), 1)
        expected_alias = urllib.parse.quote(
            "#botville_conv_conv_demo:" + SERVER_NAME, safe=""
        )
        self.assertTrue(lookups[0]["path"].endswith(expected_alias))
        self.assertEqual(lookups[0]["authorization"], "Bearer " + AS_TOKEN)

        # 命中进程内缓存：第二条消息不再查目录。
        transport.send_text(room_id="conv_demo", sender_id=AI, text="again")
        self.assertEqual(len(self.hs.matching("GET", "/_matrix/client/v3/directory/room/")), 1)

    def test_unknown_room_alias_is_conversation_not_found(self):
        self.hs.route(
            "GET",
            "/_matrix/client/v3/directory/room/",
            [(404, {"errcode": "M_NOT_FOUND", "error": "Room alias not found"})],
        )
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.send_text(room_id="conv_missing", sender_id=AI, text="hi")
        self.assertEqual(ctx.exception.code, "conversation.not_found")

    def test_invalid_conversation_id_rejected_client_side(self):
        transport = self.make_transport()
        for bad in ("Conv Demo", "conv/id", "conv#id", "x" * 65):
            with self.assertRaises(DeliveryError) as ctx:
                transport.send_text(room_id=bad, sender_id=AI, text="hi")
            self.assertEqual(ctx.exception.code, "conversation.invalid", bad)
        self.assertEqual(self.hs.requests, [])

    def test_room_id_passthrough_skips_directory(self):
        transport = self.make_transport()
        transport.send_text(room_id=ROOM, sender_id=AI, text="hi")
        self.assertEqual(self.hs.matching("GET", "/_matrix/client/v3/directory/room/"), [])

    def test_custom_room_alias_prefix(self):
        self.hs.route(
            "GET", "/_matrix/client/v3/directory/room/",
            [(200, {"room_id": ROOM, "servers": [SERVER_NAME]})],
        )
        transport = self.make_transport(room_alias_prefix="conv_")
        transport.send_text(room_id="demo", sender_id=AI, text="hi")
        lookup = self.hs.matching("GET", "/_matrix/client/v3/directory/room/")[0]
        self.assertIn(urllib.parse.quote("#conv_demo:" + SERVER_NAME, safe=""), lookup["path"])

    # --------------------------------------- defense in depth (PR #99 建议)
    def test_ensure_user_validates_namespace(self):
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.ensure_user("@bob:localhost")
        self.assertEqual(ctx.exception.code, "appservice.namespace")
        self.assertEqual(self.hs.requests, [])

    def test_join_validates_namespace(self):
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.join(ROOM, "@bob:localhost")
        self.assertEqual(ctx.exception.code, "appservice.namespace")
        self.assertEqual(self.hs.requests, [])

    def test_ensure_user_requires_matching_user_id_in_response(self):
        self.hs.route(
            "POST", "/_matrix/client/v3/register",
            [(200, {"user_id": "@ai_other:" + SERVER_NAME})],
        )
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.ensure_user(AI)
        self.assertEqual(ctx.exception.code, "appservice.namespace")
        # 失败不进缓存：第二次仍会重试（并再次被拒）。
        with self.assertRaises(DeliveryError):
            transport.ensure_user(AI)
        self.assertEqual(len(self.hs.matching("POST", "/_matrix/client/v3/register")), 2)

    def test_ensure_user_rejects_200_without_user_id(self):
        self.hs.route("POST", "/_matrix/client/v3/register", [(200, {})])
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.ensure_user(AI)
        self.assertEqual(ctx.exception.code, "appservice.namespace")

    def test_non_json_2xx_is_delivery_error(self):
        self.hs.route("PUT", "/_matrix/client/v3/rooms/", [(200, b"<html>not json</html>")])
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.send_text(room_id=ROOM, sender_id=AI, text="hi")
        self.assertEqual(ctx.exception.code, "appservice.response")

    def test_redirects_are_not_followed(self):
        """AS token 在 Authorization 头里：3xx 一律不跟随，按错误上抛。"""
        self.hs.route(
            "PUT",
            "/_matrix/client/v3/rooms/",
            [(302, {"__redirect__": "http://127.0.0.1:9/evil", "__status__": 302})],
        )
        transport = self.make_transport()
        with self.assertRaises(DeliveryError) as ctx:
            transport.send_text(room_id=ROOM, sender_id=AI, text="hi")
        self.assertTrue(ctx.exception.code.startswith("http.3"), ctx.exception.code)
        # 只有最初那次请求：没有向 Location 发起第二次请求。
        self.assertEqual(len(self.hs.matching("PUT", "/_matrix/client/v3/rooms/")), 1)

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
        # setUp 路由了带 AS token 的 whoami → 200：可达且 token 有效。
        self.assertTrue(self.make_transport().healthy())
        # 不可达的 homeserver → False（不抛异常；连接失败/超时都算不健康）。
        dead = AppServiceMatrixTransport(
            homeserver_url="http://127.0.0.1:1",
            as_token=AS_TOKEN,
            server_name=SERVER_NAME,
            timeout=0.5,
        )
        self.assertFalse(dead.healthy())

    def test_unhealthy_when_as_token_rejected(self):
        """whoami 校验 token：/versions 校验不了的东西（token 错也不能报 ok）。"""
        self.hs.route(
            "GET",
            "/_matrix/client/v3/account/whoami",
            [(401, {"errcode": "M_UNKNOWN_TOKEN", "error": "Unrecognised access token"})],
        )
        self.assertFalse(self.make_transport().healthy())


class AppServiceTransportConfigTest(unittest.TestCase):
    """GATEWAY_MATRIX_TRANSPORT 相关配置：默认保持现状、appservice fail closed。"""

    BASE_ENV = {
        "GATEWAY_AUTH_TOKEN": "gateway-token",
        "GATEWAY_HOMESERVER_URL": "http://127.0.0.1:8008",
        "GATEWAY_MATRIX_SERVER_NAME": "localhost",
        # 64-hex 长度（init.sh 生成的形态）；也满足 >=32 的强度下限。
        "GATEWAY_AS_TOKEN": "unit-test-as-token-0123456789abcdef",
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

    # ---------------------------------- as_token strength (PR #99 阻塞项 2)
    def test_empty_as_token_rejected(self):
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
        env["GATEWAY_AS_TOKEN"] = ""
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_short_as_token_rejected(self):
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
        env["GATEWAY_AS_TOKEN"] = "short-token"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_whitespace_as_token_rejected(self):
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
        env["GATEWAY_AS_TOKEN"] = "a" * 40 + " b"
        with self.assertRaises(ConfigError):
            load_config(env)

    # ------------------------------------- server_name (PR #99 建议)
    def test_server_name_normalized_to_lowercase(self):
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
        env["GATEWAY_MATRIX_SERVER_NAME"] = "LocalHost"
        cfg = load_config(env)
        self.assertEqual(cfg.matrix_server_name, "localhost")

    def test_server_name_with_port_accepted(self):
        env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
        env["GATEWAY_MATRIX_SERVER_NAME"] = "Example.Invalid:8448"
        cfg = load_config(env)
        self.assertEqual(cfg.matrix_server_name, "example.invalid:8448")

    def test_invalid_server_name_rejected(self):
        for bad in (
            "not a name", "ex ample", "example..com", "-bad.example",
            "example.com:99999", "example.com:port", "::1",
        ):
            env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
            env["GATEWAY_MATRIX_SERVER_NAME"] = bad
            with self.assertRaises(ConfigError, msg=bad):
                load_config(env)

    # --------------------------- plaintext homeserver URL (PR #99 建议)
    def test_plain_http_homeserver_restricted_to_local_hosts(self):
        def load_with(url, **extra):
            env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice", **extra)
            env["GATEWAY_HOMESERVER_URL"] = url
            return load_config(env)

        for ok in (
            "http://127.0.0.1:8008",
            "http://localhost:8008",
            "http://[::1]:8008",
            "http://synapse:8008",  # 无点单标签 = compose 网络容器名
            "https://hs.example.invalid",  # https 不受限
        ):
            cfg = load_with(ok)
            self.assertEqual(cfg.homeserver_url, ok)
        for bad in ("http://192.168.1.5:8008", "http://hs.example.invalid:8008"):
            with self.assertRaises(ConfigError, msg=bad):
                load_with(bad)
        # 显式放行明文跨主机。
        cfg = load_with(
            "http://hs.example.invalid:8008", GATEWAY_ALLOW_INSECURE_HS="true"
        )
        self.assertTrue(cfg.allow_insecure_homeserver)

    # ------------------------------------- room alias prefix (PR #99 阻塞项 1)
    def test_room_alias_prefix_validated(self):
        def load_with(prefix):
            env = dict(self.BASE_ENV, GATEWAY_MATRIX_TRANSPORT="appservice")
            env["GATEWAY_ROOM_ALIAS_PREFIX"] = prefix
            return load_config(env)

        self.assertEqual(load_with("conv_").room_alias_prefix, "conv_")
        for bad in ("has space", "#x", "x:y", "UPPER"):
            with self.assertRaises(ConfigError, msg=bad):
                load_with(bad)

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
        self.assertNotIn(self.BASE_ENV["GATEWAY_AS_TOKEN"], repr(cfg))


if __name__ == "__main__":
    unittest.main()

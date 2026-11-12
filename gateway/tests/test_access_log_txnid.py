# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""契约决定 #15：日志绝不出现原始 AS txnId / 事件正文 / token。

覆盖两档脱敏：

1. **访问日志**（stdlib `BaseHTTPRequestHandler.log_message` / `log_error` /
   `log_request`，直接写 stderr）把路径里的 txnId 段替换成 `<redacted>`；
   `access_token=` 查询参数保持既有 `[REDACTED]` 掩码。
2. **带 txnId 的错误日志**只用短标记 `txn#xxxxxxxx`（加盐 HMAC-SHA256 前 8 位，
   仅在同一次进程运行内可关联）。

用例形态：
- 纯 txnId、百分号编码 txnId、带 `?access_token=`、带 `GATEWAY_HTTP_PATH_PREFIX`；
- 非法 txnId 触发 400（仍要脱敏）；
- 持久化失败（500）路径把 txnId 写进 `log_error`，必须只剩 `txn#xxxxxxxx`；
- 坏请求行（stdlib `send_error` → `log_error`）也经过脱敏；
- 辅助函数的单元测试（幂等、出站发送路径、长短哈希）。

运行：python3 -m unittest gateway.tests.test_access_log_txnid -v
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.as_txn_store import AsTransactionStore, build_as_txn_store  # noqa: E402
from botville_gateway.matrix import DeliveryError, InMemoryMatrixTransport, resolve_txn_id  # noqa: E402
from botville_gateway.config import Config  # noqa: E402
from botville_gateway.http_server import build_server  # noqa: E402
from botville_gateway.log_redact import (  # noqa: E402
    redact_args,
    redact_text,
    redact_txn_id,
)

HS_TOKEN = "test-hs-token-0123456789abcdefghijklmnop"   # 明显假值，≥32
PREFIX = "/_botville/gateway"
# 足够醒目：一旦原样出现在日志里，子串断言必失败。
TXN = "TXID-LEAK-0123456789abcdef"
# 百分号编码形态：`%2E` = `.`、`%5F` = `_`，解码后仍是合法 txnId 字符集
# （`[!-~]` 且 ≤255）；日志里出现的是**编码后**的原始路径段。
TXN_PCT = "TXID-PCT-abc%2Edef%5Fghi"


def _cfg(**overrides):
    base = dict(
        host="127.0.0.1",
        port=0,
        dev_mode=True,
        auth_token=None,
        rate_limit_messages=60,
        silent_hours_enabled=False,
        hmac_secret=b"test-hmac-secret" * 2,
        access_log=True,
    )
    base.update(overrides)
    return Config(**base)


class _ServerFixture:
    def __init__(self, config: Config, transport=None):
        self.server = build_server(config, transport=transport)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"


def _http(method, url, body=None, headers=None, timeout=5, raw_body=None):
    if raw_body is not None:
        data = raw_body
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None), dict(exc.headers)


def _auth(token=HS_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _raw_request(server, raw: bytes) -> bytes:
    """发一段手拼的原始 HTTP 请求（坏请求行等），返回完整响应字节。"""
    host, port = server.server_address[:2]
    sock = socket.create_connection((host, port), timeout=5)
    try:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        pass
    finally:
        sock.close()
    return b"".join(chunks)


_TXN_BODY = {"events": [{"type": "m.room.message", "room_id": "!r:hs", "content": {"body": "hi"}}]}
_MARKER_RE = re.compile(r"txn#[0-9a-f]{8}")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "as_transactions.db")

    def tearDown(self):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 1. 辅助函数单元测试
# ---------------------------------------------------------------------------
class TestRedactHelper(unittest.TestCase):
    def test_plain_txn_segment(self):
        line = 'PUT /_matrix/app/v1/transactions/abc123 HTTP/1.0'
        self.assertEqual(
            redact_text(line), 'PUT /_matrix/app/v1/transactions/<redacted> HTTP/1.0'
        )

    def test_quoted_requestline_as_logged_by_stdlib(self):
        # stdlib 的访问日志格式："%s" %s %s → requestline 在引号里。
        line = '"PUT /_matrix/app/v1/transactions/abc123 HTTP/1.0" 200 -'
        out = redact_text(line)
        self.assertNotIn("abc123", out)
        self.assertIn('''"PUT /_matrix/app/v1/transactions/<redacted> HTTP/1.0" 200 -''', out)

    def test_query_string_kept_and_token_masked(self):
        line = 'PUT /_matrix/app/v1/transactions/abc123?access_token=sekrit HTTP/1.0'
        out = redact_text(line)
        self.assertNotIn("abc123", out)
        self.assertNotIn("sekrit", out)
        self.assertIn("/_matrix/app/v1/transactions/<redacted>?access_token=[REDACTED]", out)

    def test_path_prefix_in_front(self):
        line = f'PUT {PREFIX}/_matrix/app/v1/transactions/abc123 HTTP/1.0'
        out = redact_text(line)
        self.assertNotIn("abc123", out)
        self.assertIn(f"{PREFIX}/_matrix/app/v1/transactions/<redacted>", out)

    def test_absolute_form_target(self):
        line = "PUT http://127.0.0.1:8080/_matrix/app/v1/transactions/abc123 HTTP/1.0"
        out = redact_text(line)
        self.assertNotIn("abc123", out)
        self.assertIn("/_matrix/app/v1/transactions/<redacted>", out)

    def test_percent_encoded_segment_matched_whole(self):
        line = "PUT /_matrix/app/v1/transactions/abc%2Fdef%20ghi HTTP/1.0"
        out = redact_text(line)
        self.assertNotIn("abc%2Fdef", out)
        self.assertNotIn("%20ghi", out)
        self.assertIn("/_matrix/app/v1/transactions/<redacted>", out)

    def test_client_send_path_redacted(self):
        # 传输层异常文本会带出站发送路径，txnId 同样不得进日志。
        line = (
            "delivery failed: matrix.unreachable: PUT "
            "/_matrix/client/v3/rooms/!r:hs/send/m.room.message/abc123?user_id=@ai_x:hs: timed out"
        )
        out = redact_text(line)
        self.assertNotIn("abc123", out)
        self.assertIn("/send/m.room.message/<redacted>", out)

    def test_all_client_transaction_paths(self):
        for version in ("v1", "v3", "v12"):
            for route in ("rooms/!r:hs/send/m.reaction", "rooms/!r:hs/send/custom.event",
                          "sendToDevice/m.room.encrypted", "rooms/!r:hs/redact/$event:hs"):
                for txn in ("abc123", "HEAD\"'<>TAIL", "<redacted>TAIL"):
                    with self.subTest(version=version, route=route, txn=txn):
                        base = f"/_matrix/client/{version}/{route}/"
                        out = redact_text(base + txn + "?access_token=fake-token")
                        self.assertEqual(out, base + "<redacted>?access_token=[REDACTED]")
                        self.assertEqual(redact_text(out), out)

    def test_idempotent(self):
        once = redact_text("PUT /_matrix/app/v1/transactions/abc123 HTTP/1.0")
        self.assertEqual(redact_text(once), once)

    def test_quotes_and_angles_redact_entire_segment(self):
        for chars in ('"', "'", "<", ">", "\"'<>", '<redacted>'):
            for base in ("/_matrix/app/v1/transactions/",
                         "/_matrix/client/v3/rooms/!r:hs/send/m.room.message/"):
                for boundary in ("/next", "?query=1", "#fragment", " HTTP/1.1"):
                    with self.subTest(chars=chars, base=base, boundary=boundary):
                        raw = base + "HEAD" + chars + "TAIL" + boundary
                        out = redact_text(raw)
                        self.assertEqual(out, base + "<redacted>" + boundary)
                        self.assertEqual(redact_text(out), out)

    def test_other_paths_untouched(self):
        line = "GET /_matrix/app/v1/ping HTTP/1.0"
        self.assertEqual(redact_text(line), line)

    def test_redact_args_handles_non_strings(self):
        self.assertEqual(
            redact_args(("x %s", 200, None, "/_matrix/app/v1/transactions/abc123")),
            ("x %s", 200, None, "/_matrix/app/v1/transactions/<redacted>"),
        )

    def test_redact_txn_id_is_salted_short_marker(self):
        raw = "some-txn-id"
        marker = redact_txn_id(raw)
        self.assertNotEqual(marker, "txn#" + hashlib.sha256(raw.encode()).hexdigest()[:8])
        self.assertRegex(marker, r"^txn#[0-9a-f]{8}$")
        self.assertNotIn(raw, marker)

    def test_redact_txn_id_deterministic_and_distinct(self):
        self.assertEqual(redact_txn_id("a"), redact_txn_id("a"))
        self.assertNotEqual(redact_txn_id("a"), redact_txn_id("b"))

    def test_markers_change_in_fresh_process(self):
        code = (
            "from botville_gateway.log_redact import redact_txn_id; "
            "print(redact_txn_id('a'), redact_txn_id('b'))"
        )
        env = dict(os.environ, PYTHONPATH=_SRC)
        other = subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip()
        self.assertNotEqual(other, redact_txn_id("a") + " " + redact_txn_id("b"))

    def test_redact_txn_id_missing_values(self):
        self.assertEqual(redact_txn_id(None), "txn#unknown")
        self.assertEqual(redact_txn_id(""), "txn#unknown")


# ---------------------------------------------------------------------------
# 2. 真实网关：访问日志 + log_error
# ---------------------------------------------------------------------------
class TestAccessLogTxnId(_Base):
    def _capture(self, cfg, send):
        """起真实服务器，把 stderr（stdlib 访问日志的落点）收集进 buffer。"""
        buf = io.StringIO()
        with _ServerFixture(cfg) as s, contextlib.redirect_stderr(buf):
            result = send(s)
        return result, buf.getvalue()

    def _assert_clean(self, joined: str, raw_txn: str):
        self.assertNotIn(raw_txn, joined)
        self.assertIn("/_matrix/app/v1/transactions/<redacted>", joined)

    def test_plain_txn_id_never_logged(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        status, joined = self._capture(
            cfg,
            lambda s: _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/{TXN}", _TXN_BODY, headers=_auth()
            )[0],
        )
        self.assertEqual(status, 200)
        self._assert_clean(joined, TXN)
        self.assertIn("PUT /_matrix/app/v1/transactions/<redacted>", joined)  # 确有日志

    def test_quotes_and_angles_access_log_has_no_tail(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)

        def send(s):
            statuses = []
            for chars in ('"', "'", "<", ">", "\"'<>", '<redacted>'):
                raw_txn = "HEAD" + chars + "TAIL"
                status = _http(
                    "PUT", f"{s.base}/_matrix/app/v1/transactions/{raw_txn}",
                    _TXN_BODY, headers=_auth(),
                )[0]
                statuses.append(status)
            return statuses

        statuses, joined = self._capture(cfg, send)
        self.assertEqual(statuses, [200] * 6)
        self.assertEqual(joined.count("/transactions/<redacted>"), 6)
        self.assertNotIn("HEAD", joined)
        self.assertNotIn("TAIL", joined)

    def test_percent_encoded_txn_id_never_logged(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        status, joined = self._capture(
            cfg,
            lambda s: _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/{TXN_PCT}", _TXN_BODY, headers=_auth()
            )[0],
        )
        self.assertEqual(status, 200)  # 解码后仍是合法 txnId
        self.assertNotIn("TXID-PCT", joined)
        self.assertNotIn("abc%2Edef", joined)
        self.assertIn("/_matrix/app/v1/transactions/<redacted>", joined)

    def test_query_string_token_masked_and_txn_redacted(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        status, joined = self._capture(
            cfg,
            lambda s: _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/{TXN}?access_token={HS_TOKEN}",
                _TXN_BODY,
                headers=_auth(),
            )[0],
        )
        self.assertEqual(status, 200)
        self._assert_clean(joined, TXN)
        self.assertNotIn(HS_TOKEN, joined)
        self.assertIn("access_token=[REDACTED]", joined)

    def test_path_prefix_redacted(self):
        cfg = _cfg(
            hs_token=HS_TOKEN, as_txn_db_path=self.db_path, http_path_prefix=PREFIX
        )
        status, joined = self._capture(
            cfg,
            lambda s: _http(
                "PUT",
                f"{s.base}{PREFIX}/_matrix/app/v1/transactions/{TXN}",
                _TXN_BODY,
                headers=_auth(),
            )[0],
        )
        self.assertEqual(status, 200)
        self._assert_clean(joined, TXN)
        self.assertIn(f"{PREFIX}/_matrix/app/v1/transactions/<redacted>", joined)

    def test_invalid_txn_id_400_still_redacted(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        bad = "TXID-BAD-" + "a" * 256  # > 255 → 400 M_INVALID_PARAM
        status, joined = self._capture(
            cfg,
            lambda s: _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/{bad}", _TXN_BODY, headers=_auth()
            )[0],
        )
        self.assertEqual(status, 400)
        self.assertNotIn("TXID-BAD", joined)
        self.assertIn("/_matrix/app/v1/transactions/<redacted>", joined)

    def test_persist_failure_uses_short_marker(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)

        def send(s):
            def _boom(txn_id, body_text):
                raise RuntimeError("disk on fire")

            # 只替换实例上的方法：server_close 仍要用真实 store 停清理线程。
            s.server.as_txn_store.store_transaction = _boom
            return _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/{TXN}", _TXN_BODY, headers=_auth()
            )[0]

        status, joined = self._capture(cfg, send)
        self.assertEqual(status, 500)
        self._assert_clean(joined, TXN)
        self.assertIn("AS transaction persist failed for txn " + redact_txn_id(TXN), joined)
        self.assertRegex(joined, _MARKER_RE)

    def test_bad_request_line_log_error_redacted(self):
        # 单 token 请求行 → stdlib send_error(Bad request syntax (%r)，含 requestline)
        # → log_error。requestline 里的 txnId 也必须被替换。
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        buf = io.StringIO()
        with _ServerFixture(cfg) as s, contextlib.redirect_stderr(buf):
            resp = _raw_request(
                s.server, f"/_matrix/app/v1/transactions/{TXN}\r\n\r\n".encode("ascii")
            )
        joined = buf.getvalue()
        self.assertIn(b"400", resp.split(b"\r\n", 1)[0])
        self.assertNotIn(TXN, joined)
        self.assertIn("/_matrix/app/v1/transactions/<redacted>", joined)

    def test_internal_error_logs_only_exception_type(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)

        def send(s):
            def boom():
                raise RuntimeError(
                    f"private body /_matrix/app/v1/transactions/{TXN} "
                    f"access_token={HS_TOKEN}"
                )
            s.server.transport.healthy = boom
            return _http("GET", f"{s.base}/health")[0]

        status, joined = self._capture(cfg, send)
        self.assertEqual(status, 500)
        self.assertIn("unhandled error: RuntimeError", joined)
        for secret in (TXN, HS_TOKEN, "private body"):
            self.assertNotIn(secret, joined)

    def test_non_txn_paths_still_logged_verbatim(self):
        # 脱敏不得误伤：ping / health 路径原样出现在日志里。
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        statuses, joined = self._capture(
            cfg,
            lambda s: (
                _http("POST", f"{s.base}/_matrix/app/v1/ping", {}, headers=_auth())[0],
                _http("GET", f"{s.base}/health")[0],
            ),
        )
        self.assertEqual(statuses, (200, 200))
        self.assertIn("POST /_matrix/app/v1/ping HTTP/1.1", joined)
        self.assertIn("GET /health HTTP/1.1", joined)

    def test_delivery_failure_log_redacts_txn_and_token(self):
        class FailingTransport(InMemoryMatrixTransport):
            attempted_txn = None

            def send_text(self, **kwargs):
                self.attempted_txn = resolve_txn_id(kwargs["txn_id"])
                raise DeliveryError(
                    "matrix.unreachable",
                    "PUT /_matrix/client/v3/rooms/!r:hs/send/m.room.message/"
                    f"{self.attempted_txn}?access_token={HS_TOKEN}: timed out",
                )

        transport = FailingTransport()
        buf = io.StringIO()
        cfg = _cfg(as_txn_db_path=self.db_path)
        with contextlib.redirect_stderr(buf), _ServerFixture(cfg, transport) as s:
            status, body, _ = _http(
                "POST", f"{s.base}/v1/conversations/c/messages",
                {"sender_persona_id": "p", "content": {"type": "text", "text": "hi"}},
                headers={"Idempotency-Key": "delivery-log-test"},
            )
        self.assertEqual(status, 500)
        self.assertEqual(body["code"], "internal.error")
        self.assertTrue(transport.attempted_txn)
        joined = buf.getvalue()
        lines = [line for line in joined.splitlines() if "delivery failed:" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("/send/m.room.message/<redacted>", lines[0])
        self.assertIn("access_token=[REDACTED]", lines[0])
        self.assertNotIn(transport.attempted_txn, joined)
        self.assertNotIn(HS_TOKEN, joined)

    def test_access_log_disabled_writes_nothing(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path, access_log=False)
        status, joined = self._capture(
            cfg,
            lambda s: _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/{TXN}", _TXN_BODY, headers=_auth()
            )[0],
        )
        self.assertEqual(status, 200)
        self.assertEqual(joined, "")


# ---------------------------------------------------------------------------
# 3. logger 行：不写 txnId
# ---------------------------------------------------------------------------
class TestLoggerLinesHaveNoTxnId(_Base):
    def test_as_store_purge_logger_has_no_txn_id(self):
        """AS 事务库的清理日志只记条数，不写 txn_id / 事件体。"""
        from botville_gateway.as_txn_store import connect

        conn = connect(self.db_path)
        conn.execute(
            "INSERT INTO as_transactions(txn_id, body, received_at)"
            " VALUES (?, ?, '2000-01-01T00:00:00Z')",
            (TXN, '{"events":[]}'),
        )
        conn.commit()
        store = AsTransactionStore(conn, purge_retention_days=1)
        with self.assertLogs("botville_gateway.as_txn_store", level="INFO") as logs:
            result = store.run_purge_safely()
        conn.close()
        self.assertEqual(result["bodies_purged"], 1)  # 确有清理、确有日志
        joined = "\n".join(logs.output)
        self.assertNotIn(TXN, joined)
        self.assertNotIn("events", joined)  # 事件正文也不进日志

    def test_redact_txn_id_marker_roundtrip_in_logger(self):
        """logger 行使用短标记后：原始值不出现、标记可关联。"""
        import logging

        logger = logging.getLogger("botville_gateway.test_access_log_txnid")
        with self.assertLogs(logger, level="INFO") as logs:
            logger.info("persist failed for txn %s", redact_txn_id(TXN))
        joined = "\n".join(logs.output)
        self.assertNotIn(TXN, joined)
        self.assertIn(redact_txn_id(TXN), joined)


if __name__ == "__main__":
    unittest.main()

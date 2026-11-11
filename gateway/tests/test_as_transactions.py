# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Matrix AS 回调（B 线 as-transactions）协议层测试。

覆盖：
- 认证：缺 token 401 M_MISSING_TOKEN、非 Bearer 401、错 token 403、正确 200；
  未配置 GATEWAY_HS_TOKEN 时网关仍能启动且回调一律 403；只带 access_token
  查询参数不被接受。
- transactions：幂等重放只存一次；持久化失败 → 500 M_UNKNOWN 且未记为已存；
  DB 重开后仍能读到；体积超限 413 M_TOO_LARGE；非 JSON 400 M_NOT_JSON；
  缺 events / 结构不对 400 M_BAD_JSON；txnId 非法 400 M_INVALID_PARAM。
- ping：带/不带 transaction_id、类型错误。
- 错误体是 Matrix 格式（errcode/error）且 Content-Type: application/json。
- 未知路径 404 M_UNRECOGNIZED、已知路径错误方法 405 M_UNRECOGNIZED。
- 根路径可用；设置 GATEWAY_HTTP_PATH_PREFIX 后根路径回调仍可用。
- /v1 与 /health 行为不变（抽查）。
- GATEWAY_HS_TOKEN 配置校验（占位符/过短/含空白/等于 AS token 均拒；未设置可启动）。
- 日志里不出现 token 值（含带 access_token 查询参数的请求）。

运行：python3 -m unittest gateway.tests.test_as_transactions -v
"""

from __future__ import annotations

import json
import os
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

from botville_gateway.as_txn_store import (  # noqa: E402
    AsTransactionStore,
    build_as_txn_store,
    connect,
)
from botville_gateway.config import Config, ConfigError, load_config  # noqa: E402
from botville_gateway.http_server import build_server  # noqa: E402

HS_TOKEN = "test-hs-token-0123456789abcdefghijklmnop"   # 明显假值，≥32
AS_TOKEN = "test-as-token-0123456789abcdefghijklmnop"
PREFIX = "/_botville/gateway"


def _cfg(**overrides):
    base = dict(
        host="127.0.0.1",
        port=0,
        dev_mode=True,
        auth_token=None,
        rate_limit_messages=60,
        silent_hours_enabled=False,
        hmac_secret=b"test-hmac-secret" * 2,
    )
    base.update(overrides)
    return Config(**base)


class _ServerFixture:
    def __init__(self, config: Config):
        self.server = build_server(config)
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
    """发一个 HTTP 请求；返回 (status, parsed_json_or_None, headers)。

    `raw_body` 原样发送（用于非 JSON / 超大测试），否则 json.dumps(body)。
    """
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


_TXN_BODY = {"events": [{"type": "m.room.message", "room_id": "!r:hs", "content": {"body": "hi"}}]}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "as_transactions.db")

    def tearDown(self):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 1. 认证
# ---------------------------------------------------------------------------
class TestAsAuth(_Base):
    def test_missing_token_401_missing_token(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, headers = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/t1", _TXN_BODY
            )
        self.assertEqual(status, 401)
        self.assertEqual(body["errcode"], "M_MISSING_TOKEN")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_non_bearer_scheme_401(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/t1",
                _TXN_BODY,
                headers={"Authorization": f"Basic {HS_TOKEN}"},
            )
        self.assertEqual(status, 401)
        self.assertEqual(body["errcode"], "M_MISSING_TOKEN")

    def test_wrong_token_403_forbidden(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/t1",
                _TXN_BODY,
                headers=_auth("wrong-token-0123456789abcdefghijklmn"),
            )
        self.assertEqual(status, 403)
        self.assertEqual(body["errcode"], "M_FORBIDDEN")

    def test_correct_token_200_and_stores(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/t1", _TXN_BODY,
                headers=_auth(),
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {})
        store = build_as_txn_store(self.db_path)
        unprocessed = store.get_unprocessed_transactions()
        self.assertEqual(len(unprocessed), 1)
        self.assertEqual(unprocessed[0][0], "t1")

    def test_no_hs_token_configured_still_boots_and_forbids(self):
        # 未配置 GATEWAY_HS_TOKEN：网关能启动，transactions/ping 一律 403。
        with _ServerFixture(_cfg(hs_token=None, as_txn_db_path=self.db_path)) as s:
            tx, tx_body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/t1", _TXN_BODY, headers=_auth()
            )
            ping, ping_body, _ = _http("POST", f"{s.base}/_matrix/app/v1/ping", {}, headers=_auth())
        self.assertEqual(tx, 403)
        self.assertEqual(tx_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(ping, 403)
        self.assertEqual(ping_body["errcode"], "M_FORBIDDEN")

    def test_query_string_access_token_not_accepted(self):
        # 只带 ?access_token= 不算凭证（必须走 Authorization 头）。
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/t1?access_token={HS_TOKEN}",
                _TXN_BODY,
            )
        self.assertEqual(status, 401)
        self.assertEqual(body["errcode"], "M_MISSING_TOKEN")


# ---------------------------------------------------------------------------
# 2. transactions：幂等 / 持久化 / 校验 / 体积
# ---------------------------------------------------------------------------
class TestAsTransactions(_Base):
    def test_idempotent_replay_stores_once(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            a_status, a_body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/dup1", _TXN_BODY, headers=_auth()
            )
            b_status, b_body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/dup1", _TXN_BODY, headers=_auth()
            )
        self.assertEqual((a_status, a_body), (200, {}))
        self.assertEqual((b_status, b_body), (200, {}))
        store = build_as_txn_store(self.db_path)
        self.assertEqual(len(store.get_unprocessed_transactions()), 1)

    def test_persistence_failure_500_and_not_stored(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        with _ServerFixture(cfg) as s:
            original = s.server.as_txn_store

            class Boom:
                def store_transaction(self, txn_id, body_text):
                    raise RuntimeError("disk on fire")

                def get_unprocessed_transactions(self):
                    return []

            s.server.as_txn_store = Boom()
            status, body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/boom1", _TXN_BODY, headers=_auth()
            )
            s.server.as_txn_store = original
        self.assertEqual(status, 500)
        self.assertEqual(body["errcode"], "M_UNKNOWN")
        # 未记为已存：同 txnId 再发应重新落库。
        store = build_as_txn_store(self.db_path)
        self.assertEqual(store.get_unprocessed_transactions(), [])

    def test_reopen_db_after_restart_sees_transaction(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, _, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/persist1", _TXN_BODY, headers=_auth()
            )
        self.assertEqual(status, 200)
        # 重新打开同一路径（模拟重启）。
        reopened = build_as_txn_store(self.db_path)
        unprocessed = reopened.get_unprocessed_transactions()
        self.assertEqual([row[0] for row in unprocessed], ["persist1"])
        self.assertIn("m.room.message", unprocessed[0][1])

    def test_oversize_body_413_too_large(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path, as_max_body_bytes=64)
        with _ServerFixture(cfg) as s:
            big = json.dumps({"events": [], "pad": "x" * 200}).encode("utf-8")
            status, body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/big1", raw_body=big, headers=_auth()
            )
        self.assertEqual(status, 413)
        self.assertEqual(body["errcode"], "M_TOO_LARGE")

    def test_non_json_body_400_m_not_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, headers = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/nj1",
                raw_body=b"{not json",
                headers=_auth(),
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["errcode"], "M_NOT_JSON")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_missing_events_400_m_bad_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/ne1", {"foo": "bar"}, headers=_auth()
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["errcode"], "M_BAD_JSON")

    def test_events_not_array_400_m_bad_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/ne2",
                {"events": "nope"},
                headers=_auth(),
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["errcode"], "M_BAD_JSON")

    def test_ephemeral_must_be_array_if_present(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            ok, _, _ = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/ep_ok",
                {"events": [], "ephemeral": []},
                headers=_auth(),
            )
            bad, body, _ = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/ep_bad",
                {"events": [], "ephemeral": {}},
                headers=_auth(),
            )
        self.assertEqual(ok, 200)
        self.assertEqual(bad, 400)
        self.assertEqual(body["errcode"], "M_BAD_JSON")

    def test_invalid_txn_id_400_invalid_param(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            too_long, body, _ = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/{'a' * 256}",
                _TXN_BODY,
                headers=_auth(),
            )
        self.assertEqual(too_long, 400)
        self.assertEqual(body["errcode"], "M_INVALID_PARAM")

    def test_no_db_path_returns_500_unknown(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=None)) as s:
            status, body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/nodb", _TXN_BODY, headers=_auth()
            )
        self.assertEqual(status, 500)
        self.assertEqual(body["errcode"], "M_UNKNOWN")


# ---------------------------------------------------------------------------
# 3. ping
# ---------------------------------------------------------------------------
class TestAsPing(_Base):
    def test_ping_without_body(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, headers = _http(
                "POST", f"{s.base}/_matrix/app/v1/ping", headers=_auth()
            )
        self.assertEqual((status, body), (200, {}))
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_ping_empty_object(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http("POST", f"{s.base}/_matrix/app/v1/ping", {}, headers=_auth())
        self.assertEqual((status, body), (200, {}))

    def test_ping_with_transaction_id(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "POST",
                f"{s.base}/_matrix/app/v1/ping",
                {"transaction_id": "abc"},
                headers=_auth(),
            )
        self.assertEqual((status, body), (200, {}))

    def test_ping_transaction_id_wrong_type_400(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "POST",
                f"{s.base}/_matrix/app/v1/ping",
                {"transaction_id": 123},
                headers=_auth(),
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["errcode"], "M_BAD_JSON")

    def test_ping_requires_auth(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http("POST", f"{s.base}/_matrix/app/v1/ping", {})
        self.assertEqual(status, 401)
        self.assertEqual(body["errcode"], "M_MISSING_TOKEN")


# ---------------------------------------------------------------------------
# 4. 路由：未知路径 / 错误方法 / 根路径 / 前缀
# ---------------------------------------------------------------------------
class TestAsRouting(_Base):
    def test_unknown_as_path_404_unrecognized(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, headers = _http(
                "PUT", f"{s.base}/_matrix/app/v1/users/x", {}, headers=_auth()
            )
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "M_UNRECOGNIZED")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_legacy_transactions_path_not_implemented(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "PUT", f"{s.base}/transactions/t1", _TXN_BODY, headers=_auth()
            )
        self.assertNotEqual(status, 200)
        self.assertNotEqual(body, {})

    def test_wrong_method_on_known_path_405_unrecognized(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            tx, tx_body, _ = _http(
                "POST", f"{s.base}/_matrix/app/v1/transactions/t1", _TXN_BODY, headers=_auth()
            )
            ping, ping_body, _ = _http("GET", f"{s.base}/_matrix/app/v1/ping", headers=_auth())
        self.assertEqual(tx, 405)
        self.assertEqual(tx_body["errcode"], "M_UNRECOGNIZED")
        self.assertEqual(ping, 405)
        self.assertEqual(ping_body["errcode"], "M_UNRECOGNIZED")

    def test_root_path_works(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/root1", _TXN_BODY, headers=_auth()
            )
        self.assertEqual((status, body), (200, {}))

    def test_root_path_works_with_http_path_prefix(self):
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path, http_path_prefix=PREFIX)
        with _ServerFixture(cfg) as s:
            root, rb, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/root2", _TXN_BODY, headers=_auth()
            )
            ping, pb, _ = _http("POST", f"{s.base}/_matrix/app/v1/ping", {}, headers=_auth())
        self.assertEqual((root, rb), (200, {}))
        self.assertEqual((ping, pb), (200, {}))

    def test_v1_and_health_unchanged(self):
        with _ServerFixture(_cfg(auth_token="site-token-0123456789")) as s:
            health, health_body, _ = _http("GET", f"{s.base}/health")
            no_auth, problem, hdrs = _http("GET", f"{s.base}/v1/limits")
            with_auth, limits, _ = _http(
                "GET", f"{s.base}/v1/limits",
                headers={"Authorization": "Bearer site-token-0123456789"},
            )
        self.assertEqual(health, 200)
        self.assertEqual(health_body["status"], "ok")
        self.assertEqual(no_auth, 401)
        # /v1 仍是 problem+json（不是 Matrix 格式）
        self.assertEqual(problem["code"], "auth.missing")
        self.assertIn("application/problem+json", hdrs["Content-Type"])
        self.assertEqual(with_auth, 200)
        self.assertIn("effective", limits)


# ---------------------------------------------------------------------------
# 5. 配置校验
# ---------------------------------------------------------------------------
class TestAsConfig(unittest.TestCase):
    def test_unset_hs_token_boots(self):
        cfg = _cfg(hs_token=None)
        self.assertIsNone(cfg.hs_token)

    def test_placeholder_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_HS_TOKEN": "<change-me>"})

    def test_too_short_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_HS_TOKEN": "short"})

    def test_whitespace_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_HS_TOKEN": "a" * 20 + " " + "b" * 20})

    def test_equal_to_as_token_rejected(self):
        tok = "same-token-0123456789abcdefghijklmnop"
        with self.assertRaises(ConfigError):
            load_config({
                "GATEWAY_DEV_MODE": "true",
                "GATEWAY_HS_TOKEN": tok,
                "GATEWAY_AS_TOKEN": tok,
            })

    def test_valid_hs_token_accepted(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_HS_TOKEN": "a" * 40})
        self.assertEqual(cfg.hs_token, "a" * 40)

    def test_as_max_body_bytes_bounds(self):
        with self.assertRaises(ConfigError):
            _cfg(as_max_body_bytes=0)
        with self.assertRaises(ConfigError):
            _cfg(as_max_body_bytes=268_435_457)

    def test_as_max_body_bytes_default(self):
        self.assertEqual(_cfg().as_max_body_bytes, 16_777_216)
        self.assertEqual(
            load_config({"GATEWAY_DEV_MODE": "true"}).as_max_body_bytes, 16_777_216
        )


# ---------------------------------------------------------------------------
# 6. 日志脱敏
# ---------------------------------------------------------------------------
class TestAsLogRedaction(_Base):
    def test_token_never_logged(self):
        # stdlib BaseHTTPRequestHandler 直接写 sys.stderr（不走 logging），
        # 因此用 redirect_stderr 捕获访问日志。
        import contextlib
        import io

        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path, access_log=True)
        buf = io.StringIO()
        with _ServerFixture(cfg) as s, contextlib.redirect_stderr(buf):
            _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/log1"
                f"?access_token={HS_TOKEN}",
                _TXN_BODY,
                headers=_auth(),
            )
            _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/log2",
                _TXN_BODY,
                headers={"Authorization": f"Bearer {HS_TOKEN}"},
            )
            _http(
                "GET",
                f"{s.base}/health?access_token={HS_TOKEN}-evil",
            )
        joined = buf.getvalue()
        self.assertIn("PUT /_matrix/app/v1/transactions/log1", joined)  # 确有日志
        self.assertNotIn(HS_TOKEN, joined)
        self.assertNotIn(f"Bearer {HS_TOKEN}", joined)
        self.assertIn("access_token=[REDACTED]", joined)


if __name__ == "__main__":
    unittest.main()

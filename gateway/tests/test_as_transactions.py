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
        # 契约 §1.7：legacy /transactions/{txnId} 返回 404 M_UNRECOGNIZED。
        # 任何方法、任何鉴权状态均如此；不走 /v1 鉴权流程（无 problem+json）。
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, hdrs = _http(
                "PUT", f"{s.base}/transactions/t1", _TXN_BODY, headers=_auth()
            )
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "M_UNRECOGNIZED")
        self.assertEqual(hdrs["Content-Type"], "application/json; charset=utf-8")

    def test_legacy_transactions_path_with_site_auth_configured(self):
        # 有站点 auth_token 时，legacy 路径也不走 /v1 鉴权：直接 404 M_UNRECOGNIZED。
        with _ServerFixture(
            _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path,
                 auth_token="site-token-0123456789012345678901")
        ) as s:
            status, body, hdrs = _http(
                "PUT", f"{s.base}/transactions/t1", _TXN_BODY
            )
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "M_UNRECOGNIZED")
        self.assertEqual(hdrs["Content-Type"], "application/json; charset=utf-8")

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
        # 契约决定 #15：路径里的 txnId 段被替换为 `<redacted>`（端点仍在日志里）。
        self.assertIn(
            'PUT /_matrix/app/v1/transactions/<redacted>', joined
        )  # 确有日志
        self.assertNotIn("log1", joined)
        self.assertNotIn("log2", joined)
        self.assertNotIn(HS_TOKEN, joined)
        self.assertNotIn(f"Bearer {HS_TOKEN}", joined)
        self.assertIn("access_token=[REDACTED]", joined)




# ---------------------------------------------------------------------------
# 7. 配置：保留期与清理
# ---------------------------------------------------------------------------
class TestAsTxnPurgeConfig(unittest.TestCase):
    def test_retention_days_default(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true"})
        self.assertEqual(cfg.as_txn_retention_days, 90)

    def test_retention_days_valid(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_AS_TXN_RETENTION_DAYS": "30"})
        self.assertEqual(cfg.as_txn_retention_days, 30)

    def test_retention_days_invalid_zero(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_AS_TXN_RETENTION_DAYS": "0"})

    def test_retention_days_invalid_negative(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_AS_TXN_RETENTION_DAYS": "-1"})

    def test_purge_interval_default(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true"})
        self.assertEqual(cfg.as_txn_purge_interval_seconds, 86_400)

    def test_purge_interval_zero_ok(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS": "0"})
        self.assertEqual(cfg.as_txn_purge_interval_seconds, 0)

    def test_purge_interval_60_ok(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS": "60"})
        self.assertEqual(cfg.as_txn_purge_interval_seconds, 60)

    def test_purge_interval_59_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS": "59"})

    def test_hs_token_empty_string_treated_as_none(self):
        cfg = _cfg(hs_token="")
        self.assertIsNone(cfg.hs_token)

    def test_hs_token_empty_env_treated_as_none(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_HS_TOKEN": ""})
        self.assertIsNone(cfg.hs_token)


# ---------------------------------------------------------------------------
# 8. 保留期清理（store 层）
# ---------------------------------------------------------------------------
class TestAsTxnPurgeStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "as_transactions.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _fresh_store(self, retention_days=None):
        from botville_gateway.as_txn_store import connect, AsTransactionStore
        conn = connect(self.db_path)
        return AsTransactionStore(conn, purge_retention_days=retention_days)

    def test_purge_clears_old_body_keeps_new(self):
        from datetime import datetime, timezone, timedelta
        store = self._fresh_store()
        store.store_transaction("old1", '{"events":[]}')
        store.store_transaction("new1", '{"events":[]}')
        # Purge with 10-day retention, but fake "now" as 15 days after old1 was stored.
        # Easier: manually manipulate received_at for old1 to be 20 days ago.
        from botville_gateway.as_txn_store import connect as _connect
        conn = _connect(self.db_path)
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "UPDATE as_transactions SET received_at = ? WHERE txn_id = 'old1'",
            (cutoff_ts,),
        )
        conn.commit()
        # Purge with 10-day retention: old1 is 20 days old, new1 is fresh.
        result = store.purge(10)
        self.assertEqual(result["bodies_purged"], 1)
        # old1 body is empty; row still exists
        row = conn.execute(
            "SELECT body, purged_at FROM as_transactions WHERE txn_id='old1'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "")
        self.assertIsNotNone(row[1])
        # new1 body is intact
        row2 = conn.execute(
            "SELECT body, purged_at FROM as_transactions WHERE txn_id='new1'"
        ).fetchone()
        self.assertIsNotNone(row2[0])
        self.assertGreater(len(row2[0]), 0)
        self.assertIsNone(row2[1])

    def test_purge_idempotent(self):
        from datetime import datetime, timezone, timedelta
        store = self._fresh_store()
        store.store_transaction("idem1", '{"events":[]}')
        from botville_gateway.as_txn_store import connect as _connect
        conn = _connect(self.db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "UPDATE as_transactions SET received_at = ? WHERE txn_id = 'idem1'",
            (old_ts,),
        )
        conn.commit()
        r1 = store.purge(10)
        r2 = store.purge(10)
        self.assertEqual(r1["bodies_purged"], 1)
        self.assertEqual(r2["bodies_purged"], 0)

    def test_tombstone_replay_returns_200_not_stored_twice(self):
        """清理后重放同一 txnId → 200 {}，且不重新落库。"""
        from datetime import datetime, timezone, timedelta
        store = self._fresh_store()
        store.store_transaction("tomb1", '{"events":[]}')
        from botville_gateway.as_txn_store import connect as _connect
        conn = _connect(self.db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "UPDATE as_transactions SET received_at = ? WHERE txn_id = 'tomb1'",
            (old_ts,),
        )
        conn.commit()
        store.purge(10)
        # store_transaction on a tombstone returns False (already exists)
        result = store.store_transaction("tomb1", '{"events":[]}')
        self.assertFalse(result)
        # Only one row
        count = conn.execute(
            "SELECT COUNT(*) FROM as_transactions WHERE txn_id='tomb1'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_tombstone_not_in_unprocessed(self):
        from datetime import datetime, timezone, timedelta
        store = self._fresh_store()
        store.store_transaction("unproc1", '{"events":[]}')
        from botville_gateway.as_txn_store import connect as _connect
        conn = _connect(self.db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "UPDATE as_transactions SET received_at = ? WHERE txn_id = 'unproc1'",
            (old_ts,),
        )
        conn.commit()
        store.purge(10)
        unprocessed = store.get_unprocessed_transactions()
        txn_ids = [r[0] for r in unprocessed]
        self.assertNotIn("unproc1", txn_ids)

    def test_migration_adds_purged_at_to_legacy_db(self):
        """已有 #192 结构的库（无 purged_at 列）能被 connect() 就地迁移。"""
        import sqlite3 as _sqlite3
        # Build a #192-era DB without purged_at.
        conn = _sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE as_transactions ("
            "txn_id TEXT PRIMARY KEY, body TEXT NOT NULL, "
            "received_at TEXT NOT NULL, processed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO as_transactions(txn_id,body,received_at) VALUES (?,?,?)",
            ("legacy1", '{"events":[]}', "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()
        # connect() should migrate it.
        from botville_gateway.as_txn_store import connect as _connect
        conn2 = _connect(self.db_path)
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(as_transactions)")}
        self.assertIn("purged_at", cols)
        # Existing row still there
        row = conn2.execute(
            "SELECT txn_id FROM as_transactions WHERE txn_id='legacy1'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_purge_scheduler_runs_and_stops(self):
        """后台调度线程启动、跑一次、停止。"""
        store = self._fresh_store(retention_days=1)
        # Insert a row old enough to be purged.
        store.store_transaction("sched1", '{"events":[]}')
        from botville_gateway.as_txn_store import connect as _connect
        conn = _connect(self.db_path)
        conn.execute(
            "UPDATE as_transactions SET received_at='2020-01-01T00:00:00Z' WHERE txn_id='sched1'"
        )
        conn.commit()
        # Use a small interval so the first scheduled tick fires quickly.
        store.start_purge_scheduler(60)
        # Manually trigger once instead of sleeping.
        store.run_purge_safely()
        store.stop_purge_scheduler(timeout=1.0)
        row = conn.execute(
            "SELECT purged_at FROM as_transactions WHERE txn_id='sched1'"
        ).fetchone()
        self.assertIsNotNone(row[0])


    def _seed_old_row(self, txn_id):
        store = self._fresh_store()
        store.store_transaction(txn_id, '{"events":[]}')
        conn = connect(self.db_path)
        conn.execute(
            "UPDATE as_transactions SET received_at='2020-01-01T00:00:00Z' WHERE txn_id=?",
            (txn_id,),
        )
        conn.commit()
        return conn

    def test_server_startup_purges_when_interval_enabled(self):
        """配置了库且间隔非 0：网关启动时同步清理一次，并起后台线程。"""
        conn = self._seed_old_row("boot1")
        server = build_server(
            _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path, as_txn_purge_interval_seconds=60)
        )
        try:
            row = conn.execute(
                "SELECT body, purged_at FROM as_transactions WHERE txn_id='boot1'"
            ).fetchone()
            self.assertEqual(row[0], "")
            self.assertIsNotNone(row[1])
            self.assertIsNotNone(server.as_txn_store._purge_thread)
        finally:
            server.server_close()
        self.assertIsNone(server.as_txn_store._purge_thread)

    def test_server_interval_zero_disables_purge(self):
        """间隔 0 = 关闭：启动时也不清理，不起线程（与 moments 一致）。"""
        conn = self._seed_old_row("boot0")
        server = build_server(
            _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path, as_txn_purge_interval_seconds=0)
        )
        try:
            row = conn.execute(
                "SELECT body, purged_at FROM as_transactions WHERE txn_id='boot0'"
            ).fetchone()
            self.assertNotEqual(row[0], "")
            self.assertIsNone(row[1])
            self.assertIsNone(server.as_txn_store._purge_thread)
        finally:
            server.server_close()


# ---------------------------------------------------------------------------
# 9. HTTP 层：body 编码 / framing
# ---------------------------------------------------------------------------
class TestAsBodyEncoding(_Base):
    def test_non_utf8_body_400_m_not_json(self):
        """UTF-16 编码的 JSON 体（非 UTF-8）→ 400 M_NOT_JSON，不是 500。"""
        utf16_body = '{"events":[]}'.encode("utf-16")
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, hdrs = _http(
                "PUT",
                f"{s.base}/_matrix/app/v1/transactions/utf16",
                raw_body=utf16_body,
                headers=_auth(),
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["errcode"], "M_NOT_JSON")
        self.assertEqual(hdrs["Content-Type"], "application/json; charset=utf-8")

    def test_non_utf8_ping_body_400_m_not_json(self):
        """ping 收到 UTF-16 体 → 400 M_NOT_JSON。"""
        utf16_body = '{"transaction_id":"abc"}'.encode("utf-16")
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            status, body, _ = _http(
                "POST",
                f"{s.base}/_matrix/app/v1/ping",
                raw_body=utf16_body,
                headers=_auth(),
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["errcode"], "M_NOT_JSON")

    def test_framing_error_400_m_not_json(self):
        """chunked / 坏 Content-Length 返回 400 M_NOT_JSON（不是 M_BAD_JSON）。

        Content-Length 声明 999 但只发 10 字节 → 读不完整 → M_NOT_JSON。
        """
        import socket
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            host, port = s.server.server_address[:2]
            # 手拼一个 Content-Length: 999 但只发 10 字节正文的请求。
            raw_req = (
                f"PUT /_matrix/app/v1/transactions/frag1 HTTP/1.0\r\n"
                f"Authorization: Bearer {HS_TOKEN}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: 999\r\n"
                f"\r\n"
                f"{{'events':"
            )
            sock = socket.create_connection((host, port), timeout=5)
            sock.sendall(raw_req.encode("ascii"))
            sock.shutdown(socket.SHUT_WR)
            resp_bytes = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    resp_bytes += chunk
            except Exception:
                pass
            sock.close()
        status_line = resp_bytes.split(b"\r\n")[0].decode()
        self.assertIn("400", status_line)
        body_text = resp_bytes.split(b"\r\n\r\n", 1)[-1].decode("utf-8", errors="replace")
        self.assertIn("M_NOT_JSON", body_text)


def _raw_request(server, raw: bytes) -> bytes:
    """发一段手拼的原始 HTTP 请求，返回完整响应字节（直到对端关闭）。"""
    import socket

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


def _status_and_body(resp_bytes: bytes):
    status_line = resp_bytes.split(b"\r\n")[0].decode()
    body = resp_bytes.split(b"\r\n\r\n", 1)[-1].decode("utf-8", errors="replace")
    return status_line, body


class TestAsFramingConsistency(_Base):
    """§1.7.3 / §1.7.5：framing 与空体都归 `M_NOT_JSON`（不是 `M_BAD_JSON`）。

    `M_BAD_JSON` 只用于「JSON 合法但结构不对」。transactions 的缺失
    `Content-Length` 与空体都读不出 JSON，因此一律 `400 M_NOT_JSON`；
    ping 的正文可选，缺失/空体仍是 `200 {}`。
    """

    def test_transactions_missing_content_length_400_m_not_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "PUT /_matrix/app/v1/transactions/nocl HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Content-Type: application/json\r\n"
                    "\r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("400", status_line)
        self.assertIn("M_NOT_JSON", body)
        self.assertNotIn("M_BAD_JSON", body)

    def test_transactions_content_length_zero_400_m_not_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "PUT /_matrix/app/v1/transactions/empty0 HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: 0\r\n"
                    "\r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("400", status_line)
        self.assertIn("M_NOT_JSON", body)
        self.assertNotIn("M_BAD_JSON", body)
        # 空体不得落库。
        store = build_as_txn_store(self.db_path)
        self.assertEqual(store.get_unprocessed_transactions(), [])

    def test_transactions_whitespace_body_400_m_not_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "PUT /_matrix/app/v1/transactions/ws1 HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: 3\r\n"
                    "\r\n"
                    " \r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("400", status_line)
        self.assertIn("M_NOT_JSON", body)

    def test_transactions_invalid_content_length_400_m_not_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "PUT /_matrix/app/v1/transactions/badcl HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: not-a-number\r\n"
                    "\r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("400", status_line)
        self.assertIn("M_NOT_JSON", body)

    def test_transactions_chunked_400_m_not_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "PUT /_matrix/app/v1/transactions/chunk1 HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Content-Type: application/json\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "\r\n"
                    "2\r\n{}\r\n0\r\n\r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("400", status_line)
        self.assertIn("M_NOT_JSON", body)

    def test_ping_without_body_or_content_length_200(self):
        # ping 的正文可选：缺失 Content-Length 也是 200 {}（不是 M_NOT_JSON）。
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "POST /_matrix/app/v1/ping HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "\r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("200", status_line)
        self.assertIn("{}", body)
        self.assertNotIn("M_NOT_JSON", body)

    def test_ping_content_length_zero_200(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "POST /_matrix/app/v1/ping HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Content-Length: 0\r\n"
                    "\r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("200", status_line)
        self.assertIn("{}", body)

    def test_ping_whitespace_only_body_200(self):
        """ping 的纯空白体等价空体 → 200 {}（contract §1.7.5）。

        回归：`_read_as_body(allow_empty=True)` 只在 `allow_empty=False` 时把纯
        空白体判为 `M_NOT_JSON`；ping 走 allow_empty，纯空白应视作空体，由
        `_handle_as_ping` 的 `raw.strip()` 分支直接返回 `{}`。
        """
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "POST /_matrix/app/v1/ping HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Content-Length: 3\r\n"
                    "\r\n"
                    "  \n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("200", status_line)
        self.assertIn("{}", body)
        self.assertNotIn("M_NOT_JSON", body)
        self.assertNotIn("M_BAD_JSON", body)

    def test_ping_invalid_content_length_still_m_not_json(self):
        # 坏 Content-Length 是 framing 错误：ping 也不放行。
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "POST /_matrix/app/v1/ping HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Content-Length: abc\r\n"
                    "\r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("400", status_line)
        self.assertIn("M_NOT_JSON", body)

    def test_ping_chunked_still_m_not_json(self):
        with _ServerFixture(_cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)) as s:
            resp = _raw_request(
                s.server,
                (
                    "POST /_matrix/app/v1/ping HTTP/1.0\r\n"
                    f"Authorization: Bearer {HS_TOKEN}\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "\r\n"
                    "0\r\n\r\n"
                ).encode("ascii"),
            )
        status_line, body = _status_and_body(resp)
        self.assertIn("400", status_line)
        self.assertIn("M_NOT_JSON", body)


# ---------------------------------------------------------------------------
# 10. HTTP 层：真实 sqlite 失败 → 500；并发幂等
# ---------------------------------------------------------------------------
class TestAsTxnSQLiteFailure(_Base):
    def test_real_sqlite_failure_500_then_replay_ok(self):
        """真实 SQLite 错误（表被删）→ 500 M_UNKNOWN；修复后重放 → 200 且只存一次。"""
        import sqlite3
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        with _ServerFixture(cfg) as s:
            # Drop the table to trigger a real sqlite3.OperationalError.
            conn = sqlite3.connect(self.db_path)
            conn.execute("DROP TABLE as_transactions")
            conn.commit()
            conn.close()
            status500, body500, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/real_fail",
                _TXN_BODY, headers=_auth()
            )
            self.assertEqual(status500, 500)
            self.assertEqual(body500["errcode"], "M_UNKNOWN")
            # Recreate the table and retry.
            from botville_gateway.as_txn_store import connect as _connect
            _connect(self.db_path)  # will run DDL + migration
            status200, body200, _ = _http(
                "PUT", f"{s.base}/_matrix/app/v1/transactions/real_fail",
                _TXN_BODY, headers=_auth()
            )
        self.assertEqual(status200, 200)
        self.assertEqual(body200, {})
        store = build_as_txn_store(self.db_path)
        self.assertEqual(len(store.get_unprocessed_transactions()), 1)

    def test_concurrent_idempotency(self):
        """多线程同时 PUT 同一 txnId → 全部 200，库里恰好一行。"""
        cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=self.db_path)
        with _ServerFixture(cfg) as s:
            results = []
            lock = threading.Lock()

            def _send():
                st, bd, _ = _http(
                    "PUT", f"{s.base}/_matrix/app/v1/transactions/concurrent1",
                    _TXN_BODY, headers=_auth()
                )
                with lock:
                    results.append((st, bd))

            threads = [threading.Thread(target=_send) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        for st, bd in results:
            self.assertEqual(st, 200)
            self.assertEqual(bd, {})
        store = build_as_txn_store(self.db_path)
        self.assertEqual(len(store.get_unprocessed_transactions()), 1)


if __name__ == "__main__":
    unittest.main()


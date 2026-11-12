# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""AS 事务消费骨架测试（B 线 PR1，contract §1.7.8）。

覆盖：
- enabled/disabled 开关（关闭时不起线程、store 行为不变）；
- rowid 顺序处理；
- at-least-once（钩子执行后 mark 前模拟崩溃 → 新消费者重新处理）；
- 幂等键稳定且区分 persona/event；
- 失败退避序列与封顶 1h、连续 5 次后 failed 不再取出；
- transient 不计 attempts；
- @ai_* 与 AS sender 不触发 human 钩子、计为 AI 事件；
- 人类消息调用 record_human_message（重置计数）；
- 外服 sender → untrusted；
- 8000 截断；
- malformed body / 事件安全跳过；
- 日志不含正文/txn_id/token；
- #192 旧表（无额外列）和 #194 表（+purged_at）迁移成功且幂等；
- purge 后不再处理；
- should_reply 决策表；
- wake 能让线程在轮询间隔之前处理；
- stop 优雅退出；
- GatewayServer 集成：启用时 PUT 事务后被消费（processed_at 非空）。

运行：python3 -m unittest gateway.tests.test_as_consumer -v
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.as_consumer import (  # noqa: E402
    AsTransactionConsumer,
    ConsumerContext,
    ConsumerHooks,
    InMemorySpeechDedupe,
    TransientUnavailable,
    backoff_seconds,
    should_reply,
    speech_idempotency_key,
    truncate_text,
)
from botville_gateway.as_txn_store import AsTransactionStore, build_as_txn_store, connect  # noqa: E402
from botville_gateway.config import Config  # noqa: E402
from botville_gateway.http_server import build_server  # noqa: E402

# 明显假值；绝不是真实凭据
HS_TOKEN = "test-hs-token-0123456789abcdefghijklmnop"
SERVER_NAME = "example.test"


def _cfg(**overrides):
    base = dict(
        host="127.0.0.1",
        port=0,
        dev_mode=True,
        auth_token=None,
        silent_hours_enabled=False,
        hmac_secret=b"test-hmac-secret" * 2,
        matrix_server_name=SERVER_NAME,
    )
    base.update(overrides)
    return Config(**base)


class _TmpDb(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "as_transactions.db")
        self.conn = connect(self.db_path)
        self.store = AsTransactionStore(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _make_consumer(self, hooks=None, service=None, poll_interval=0.05, **kwargs):
        kwargs.setdefault("server_name", SERVER_NAME)
        return AsTransactionConsumer(
            self.store,
            hooks=hooks,
            service=service,
            poll_interval=poll_interval,
            **kwargs,
        )

    def _human_event(self, room_id="!r:example.test", sender="@alice:example.test", text="hi"):
        return {
            "type": "m.room.message",
            "event_id": f"$evt-{text[:8].replace(' ','_')}",
            "room_id": room_id,
            "sender": sender,
            "content": {"msgtype": "m.text", "body": text},
        }

    def _put_txn(self, txn_id="t1", events=None, **extra):
        payload = {"events": events or [self._human_event()]}
        payload.update(extra)
        return self.store.store_transaction(txn_id, json.dumps(payload))


# ---------------------------------------------------------------------------
# 1. 基础工具函数
# ---------------------------------------------------------------------------
class TestSpeechIdempotencyKey(unittest.TestCase):
    def test_stable_same_inputs(self):
        k1 = speech_idempotency_key("@ai_a:hs", "$ev1")
        k2 = speech_idempotency_key("@ai_a:hs", "$ev1")
        self.assertEqual(k1, k2)

    def test_different_persona(self):
        k1 = speech_idempotency_key("@ai_a:hs", "$ev1")
        k2 = speech_idempotency_key("@ai_b:hs", "$ev1")
        self.assertNotEqual(k1, k2)

    def test_different_event(self):
        k1 = speech_idempotency_key("@ai_a:hs", "$ev1")
        k2 = speech_idempotency_key("@ai_a:hs", "$ev2")
        self.assertNotEqual(k1, k2)

    def test_prefix_spk(self):
        k = speech_idempotency_key("@ai_x:hs", "$e")
        self.assertTrue(k.startswith("spk_"))


class TestTruncateText(unittest.TestCase):
    def test_short_unchanged(self):
        self.assertEqual(truncate_text("hello"), "hello")

    def test_truncates_at_limit(self):
        text = "a" * 9000
        self.assertEqual(len(truncate_text(text, 8000)), 8000)

    def test_zero_limit(self):
        self.assertEqual(truncate_text("hello", 0), "")

    def test_unicode_codepoints_not_bytes(self):
        # 表情符 1 codepoint
        emoji = "😀" * 10
        truncated = truncate_text(emoji, 5)
        self.assertEqual(len(truncated), 5)


class TestBackoffSeconds(unittest.TestCase):
    def test_first_attempt_base(self):
        self.assertAlmostEqual(backoff_seconds(1), 5.0)

    def test_doubles(self):
        self.assertAlmostEqual(backoff_seconds(2), 10.0)
        self.assertAlmostEqual(backoff_seconds(3), 20.0)

    def test_caps_at_1_hour(self):
        self.assertAlmostEqual(backoff_seconds(100), 3600.0)


# ---------------------------------------------------------------------------
# 2. should_reply 决策表
# ---------------------------------------------------------------------------
class TestShouldReply(unittest.TestCase):
    AI_A = "@ai_a:hs"
    AI_B = "@ai_b:hs"

    def test_ai_sender_no_reply(self):
        self.assertFalse(should_reply(
            sender=self.AI_A,
            room_ai_user_ids=[self.AI_A, self.AI_B],
            is_ai_sender=True,
        ))

    def test_single_ai_room_always_reply(self):
        self.assertTrue(should_reply(
            sender="@human:hs",
            room_ai_user_ids=[self.AI_A],
        ))

    def test_group_no_mention_no_reply(self):
        self.assertFalse(should_reply(
            sender="@human:hs",
            room_ai_user_ids=[self.AI_A, self.AI_B],
        ))

    def test_group_mention_in_mentions_field(self):
        self.assertTrue(should_reply(
            sender="@human:hs",
            room_ai_user_ids=[self.AI_A, self.AI_B],
            mentions=[self.AI_A],
        ))

    def test_group_mention_in_text(self):
        self.assertTrue(should_reply(
            sender="@human:hs",
            room_ai_user_ids=[self.AI_A, self.AI_B],
            text=f"hey {self.AI_A} what do you think?",
        ))

    def test_no_ai_in_room_no_reply(self):
        self.assertFalse(should_reply(
            sender="@human:hs",
            room_ai_user_ids=[],
        ))

    def test_sender_in_ai_list_no_reply(self):
        self.assertFalse(should_reply(
            sender=self.AI_A,
            room_ai_user_ids=[self.AI_A],
        ))


# ---------------------------------------------------------------------------
# 3. 迁移幂等（#192 旧表 / #194 结构）
# ---------------------------------------------------------------------------
class TestMigrationCompat(unittest.TestCase):
    # #192 时期的原始 DDL（无任何额外列）
    _LEGACY_192 = (
        "CREATE TABLE as_transactions ("
        "txn_id TEXT PRIMARY KEY, body TEXT NOT NULL, "
        "received_at TEXT NOT NULL, processed_at TEXT)"
    )
    # #194 时期（+purged_at）
    _LEGACY_194 = (
        "CREATE TABLE as_transactions ("
        "txn_id TEXT PRIMARY KEY, body TEXT NOT NULL, "
        "received_at TEXT NOT NULL, processed_at TEXT, purged_at TEXT)"
    )

    def _test_migration(self, legacy_ddl: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "old.db")
            # 先建旧结构
            conn = sqlite3.connect(db_path)
            conn.execute(legacy_ddl)
            conn.execute(
                "INSERT INTO as_transactions(txn_id, body, received_at)"
                " VALUES ('old1', '{\"events\":[]}', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
            conn.close()
            # 通过 connect() 触发迁移
            conn2 = connect(db_path)
            cols = {row[1] for row in conn2.execute("PRAGMA table_info(as_transactions)")}
            # 迁移后必须有所有新列
            for col in ("purged_at", "attempts", "last_error_type", "failed_at", "next_attempt_at"):
                self.assertIn(col, cols, f"missing column {col}")
            # 旧行仍然存在，attempts = 0
            row = conn2.execute(
                "SELECT attempts FROM as_transactions WHERE txn_id='old1'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 0)
            conn2.close()
            # 再次 connect() 验证幂等（不应报错）
            conn3 = connect(db_path)
            conn3.close()

    def test_migration_from_192(self):
        self._test_migration(self._LEGACY_192)

    def test_migration_from_194(self):
        self._test_migration(self._LEGACY_194)


# ---------------------------------------------------------------------------
# 4. get_consumable_batch 顺序与过滤
# ---------------------------------------------------------------------------
class TestGetConsumableBatch(_TmpDb):
    def _now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    def test_rowid_order(self):
        for i in range(3):
            self.store.store_transaction(f"t{i}", json.dumps({"events": []}))
        batch = self.store.get_consumable_batch(self._now())
        self.assertEqual([r[0] for r in batch], ["t0", "t1", "t2"])

    def test_excludes_processed(self):
        self.store.store_transaction("t1", json.dumps({"events": []}))
        self.store.mark_processed("t1")
        batch = self.store.get_consumable_batch(self._now())
        self.assertEqual(batch, [])

    def test_excludes_purged(self):
        from datetime import datetime, timezone
        self.store.store_transaction("t1", json.dumps({"events": []}))
        self.store.purge(1, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        batch = self.store.get_consumable_batch(self._now())
        self.assertEqual(batch, [])

    def test_excludes_failed(self):
        self.store.store_transaction("t1", json.dumps({"events": []}))
        now = self._now()
        self.store.record_failure("t1", "SomeError", now, mark_failed=True)
        batch = self.store.get_consumable_batch(self._now())
        self.assertEqual(batch, [])

    def test_respects_next_attempt_at(self):
        from datetime import datetime, timedelta, timezone
        self.store.store_transaction("t1", json.dumps({"events": []}))
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        self.store.record_failure("t1", "E", datetime.now(timezone.utc), next_attempt_at=future)
        batch = self.store.get_consumable_batch(self._now())
        self.assertEqual(batch, [])

    def test_limit_honoured(self):
        for i in range(5):
            self.store.store_transaction(f"t{i}", json.dumps({"events": []}))
        batch = self.store.get_consumable_batch(self._now(), limit=2)
        self.assertEqual(len(batch), 2)


# ---------------------------------------------------------------------------
# 5. record_failure / record_transient / oldest_unprocessed
# ---------------------------------------------------------------------------
class TestStoreFailureMethods(_TmpDb):
    def _now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    def test_record_failure_increments_attempts(self):
        self.store.store_transaction("t1", json.dumps({"events": []}))
        now = self._now()
        self.store.record_failure("t1", "ValueError", now)
        row = self.conn.execute(
            "SELECT attempts, last_error_type FROM as_transactions WHERE txn_id='t1'"
        ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "ValueError")

    def test_record_failure_mark_failed(self):
        self.store.store_transaction("t1", json.dumps({"events": []}))
        now = self._now()
        self.store.record_failure("t1", "FatalError", now, mark_failed=True)
        row = self.conn.execute(
            "SELECT failed_at FROM as_transactions WHERE txn_id='t1'"
        ).fetchone()
        self.assertIsNotNone(row[0])

    def test_record_transient_no_attempts(self):
        self.store.store_transaction("t1", json.dumps({"events": []}))
        from datetime import timedelta
        now = self._now()
        self.store.record_transient("t1", now + timedelta(seconds=30))
        row = self.conn.execute(
            "SELECT attempts FROM as_transactions WHERE txn_id='t1'"
        ).fetchone()
        self.assertEqual(row[0], 0)

    def test_oldest_unprocessed(self):
        self.store.store_transaction("t1", json.dumps({"events": []}))
        result = self.store.oldest_unprocessed_received_at()
        self.assertIsNotNone(result)

    def test_oldest_unprocessed_none_when_empty(self):
        result = self.store.oldest_unprocessed_received_at()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 6. 消费逻辑：at-least-once
# ---------------------------------------------------------------------------
class TestAtLeastOnce(_TmpDb):
    def test_replay_on_crash_before_mark(self):
        """钩子执行后、mark_processed 之前进程崩溃 → 重新处理。"""
        calls = []

        class _Hooks(ConsumerHooks):
            def on_human_text(self, ctx):
                calls.append(ctx.event_id)

        # 第一轮：正常处理
        self._put_txn("t1")
        c1 = self._make_consumer(hooks=_Hooks(), server_name=SERVER_NAME)
        c1.run_once()
        self.assertEqual(len(calls), 1)

        # 模拟「已处理但未 mark」：手工清掉 processed_at
        self.conn.execute("UPDATE as_transactions SET processed_at=NULL WHERE txn_id='t1'")
        self.conn.commit()

        # 第二轮：换一个新消费者（无内存去重状态）→ 应重新执行钩子
        c2 = self._make_consumer(hooks=_Hooks(), server_name=SERVER_NAME)
        c2.run_once()
        self.assertEqual(len(calls), 2)


# ---------------------------------------------------------------------------
# 7. 失败退避序列 + 5 次后 failed
# ---------------------------------------------------------------------------
class TestFailureBackoff(_TmpDb):
    def _now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    def test_five_failures_then_failed(self):
        class _FailHooks(ConsumerHooks):
            def on_human_text(self, ctx):
                raise RuntimeError("hook failure")

        self._put_txn("t1")
        c = self._make_consumer(hooks=_FailHooks())

        for _ in range(5):
            c.run_once(now=self._now())
            # 退避到期：把 next_attempt_at 清掉，模拟时间已推进到重试点。
            self.conn.execute(
                "UPDATE as_transactions SET next_attempt_at=NULL WHERE txn_id='t1'"
            )
            self.conn.commit()

        # 5 次后标 failed，不再取出
        batch = self.store.get_consumable_batch(self._now())
        self.assertEqual(batch, [])
        row = self.conn.execute(
            "SELECT attempts, failed_at FROM as_transactions WHERE txn_id='t1'"
        ).fetchone()
        self.assertEqual(row[0], 5)
        self.assertIsNotNone(row[1])

    def test_transient_does_not_increment_attempts(self):
        class _TransientHooks(ConsumerHooks):
            def on_human_text(self, ctx):
                raise TransientUnavailable("model down")

        self._put_txn("t1")
        c = self._make_consumer(hooks=_TransientHooks())

        # 模拟 10 次 transient（不设 next_attempt_at 偏移，保持立即可取）
        for _ in range(10):
            # 手工清掉 next_attempt_at，模拟已到退避时间
            self.conn.execute("UPDATE as_transactions SET next_attempt_at=NULL WHERE txn_id='t1'")
            self.conn.commit()
            c.run_once(now=self._now())

        row = self.conn.execute(
            "SELECT attempts, failed_at FROM as_transactions WHERE txn_id='t1'"
        ).fetchone()
        self.assertEqual(row[0], 0)   # attempts 仍然为 0
        self.assertIsNone(row[1])     # 未 failed


# ---------------------------------------------------------------------------
# 8. 防循环：AI sender / AS sender
# ---------------------------------------------------------------------------
class TestAntiLoop(_TmpDb):
    def _make_ai_event(self, sender, text="hello"):
        return {
            "type": "m.room.message",
            "event_id": "$ai-ev",
            "room_id": "!r:example.test",
            "sender": sender,
            "content": {"msgtype": "m.text", "body": text},
        }

    def test_ai_sender_triggers_on_ai_text_not_human(self):
        human_calls = []
        ai_calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                human_calls.append(ctx.sender)
            def on_ai_text(self, ctx):
                ai_calls.append(ctx.sender)

        ai_sender = f"@ai_bot:{SERVER_NAME}"
        self.store.store_transaction(
            "t1", json.dumps({"events": [self._make_ai_event(ai_sender)]})
        )
        c = self._make_consumer(hooks=_H())
        c.run_once()
        self.assertEqual(human_calls, [])
        self.assertIn(ai_sender, ai_calls)

    def test_as_sender_localpart_not_human(self):
        human_calls = []
        ai_calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                human_calls.append(ctx.sender)
            def on_ai_text(self, ctx):
                ai_calls.append(ctx.sender)

        as_sender = f"@botville-gateway:{SERVER_NAME}"
        self.store.store_transaction(
            "t1", json.dumps({"events": [self._make_ai_event(as_sender)]})
        )
        c = self._make_consumer(hooks=_H())
        c.run_once()
        self.assertEqual(human_calls, [])
        self.assertIn(as_sender, ai_calls)


# ---------------------------------------------------------------------------
# 9. trust 分类：外服 sender → untrusted
# ---------------------------------------------------------------------------
class TestTrustClassification(_TmpDb):
    def test_external_sender_untrusted(self):
        trust_seen = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                trust_seen.append(ctx.trust)

        event = {
            "type": "m.room.message",
            "event_id": "$ext",
            "room_id": "!r:example.test",
            "sender": "@bob:other.server",
            "content": {"msgtype": "m.text", "body": "hi"},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(hooks=_H())
        c.run_once()
        self.assertEqual(trust_seen, ["untrusted"])

    def test_local_sender_trusted(self):
        trust_seen = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                trust_seen.append(ctx.trust)

        event = {
            "type": "m.room.message",
            "event_id": "$loc",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {"msgtype": "m.text", "body": "hi"},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(hooks=_H())
        c.run_once()
        self.assertEqual(trust_seen, ["trusted"])


# ---------------------------------------------------------------------------
# 10. 8000 字符截断
# ---------------------------------------------------------------------------
class TestTextTruncation(_TmpDb):
    def test_long_text_truncated(self):
        texts_seen = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                texts_seen.append(ctx.text)

        long_text = "x" * 10_000
        event = {
            "type": "m.room.message",
            "event_id": "$long",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {"msgtype": "m.text", "body": long_text},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(hooks=_H())
        c.run_once()
        self.assertEqual(len(texts_seen), 1)
        self.assertLessEqual(len(texts_seen[0]), 8000)


# ---------------------------------------------------------------------------
# 11. malformed body / 单事件跳过
# ---------------------------------------------------------------------------
class TestMalformedHandling(_TmpDb):
    def _now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    def test_malformed_body_marked_failed(self):
        self.store.store_transaction("bad", "NOT JSON{{{")
        c = self._make_consumer()
        c.run_once()
        row = self.conn.execute(
            "SELECT failed_at FROM as_transactions WHERE txn_id='bad'"
        ).fetchone()
        self.assertIsNotNone(row[0])

    def test_malformed_event_skipped_rest_processed(self):
        good_calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                good_calls.append(ctx.event_id)

        events = [
            "not_a_dict",
            {
                "type": "m.room.message",
                "event_id": "$good",
                "room_id": "!r:example.test",
                "sender": f"@alice:{SERVER_NAME}",
                "content": {"msgtype": "m.text", "body": "hi"},
            },
        ]
        self.store.store_transaction("t1", json.dumps({"events": events}))
        c = self._make_consumer(hooks=_H())
        c.run_once()
        self.assertIn("$good", good_calls)
        # 整条事务已处理（不因单个坏事件而失败）
        row = self.conn.execute(
            "SELECT processed_at FROM as_transactions WHERE txn_id='t1'"
        ).fetchone()
        self.assertIsNotNone(row[0])

    def test_missing_events_key_marked_failed(self):
        self.store.store_transaction("bad2", json.dumps({"noevents": True}))
        c = self._make_consumer()
        c.run_once()
        row = self.conn.execute(
            "SELECT failed_at FROM as_transactions WHERE txn_id='bad2'"
        ).fetchone()
        self.assertIsNotNone(row[0])


# ---------------------------------------------------------------------------
# 12. 日志不含正文 / txn_id / token
# ---------------------------------------------------------------------------
class TestLogSafety(_TmpDb):
    def test_no_body_in_logs(self):
        secret_text = "super_secret_message_body_xyz"
        txn_id = "txn-secret-id-123"
        event = {
            "type": "m.room.message",
            "event_id": "$safe",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {"msgtype": "m.text", "body": secret_text},
        }
        self.store.store_transaction(txn_id, json.dumps({"events": [event]}))
        c = self._make_consumer()
        with self.assertLogs("botville_gateway.as_consumer", level="DEBUG") as cm:
            # 注入一条以确保 assertLogs 不因无日志而失败
            logging.getLogger("botville_gateway.as_consumer").debug("test-probe")
            c.run_once()
        combined = " ".join(cm.output)
        self.assertNotIn(secret_text, combined)
        self.assertNotIn(txn_id, combined)


# ---------------------------------------------------------------------------
# 13. purge 后不再处理
# ---------------------------------------------------------------------------
class TestPurgedNotProcessed(_TmpDb):
    def test_purged_transaction_skipped(self):
        from datetime import datetime, timezone
        human_calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                human_calls.append(1)

        self.store.store_transaction("t1", json.dumps({"events": [
            {
                "type": "m.room.message",
                "event_id": "$e",
                "room_id": "!r:example.test",
                "sender": f"@alice:{SERVER_NAME}",
                "content": {"msgtype": "m.text", "body": "msg"},
            }
        ]}))
        # 清理（强制过期）
        self.store.purge(1, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        c = self._make_consumer(hooks=_H())
        c.run_once()
        self.assertEqual(human_calls, [])


# ---------------------------------------------------------------------------
# 14. wake 在 poll_interval 之前触发
# ---------------------------------------------------------------------------
class TestWakeMechanism(_TmpDb):
    def test_wake_processes_before_poll(self):
        processed = threading.Event()

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                processed.set()

        c = self._make_consumer(hooks=_H(), poll_interval=60.0)  # 60 秒兜底
        c.start()
        # 先放事务再唤醒
        self._put_txn("t1")
        c.wake()
        ok = processed.wait(timeout=5.0)
        c.stop()
        self.assertTrue(ok, "consumer did not wake up within 5s after wake()")


# ---------------------------------------------------------------------------
# 15. stop 优雅退出
# ---------------------------------------------------------------------------
class TestGracefulStop(_TmpDb):
    def test_stop_joins_thread(self):
        c = self._make_consumer(poll_interval=5.0)
        c.start()
        self.assertTrue(c.running)
        c.stop(timeout=3.0)
        self.assertFalse(c.running)


# ---------------------------------------------------------------------------
# 16. 人类消息调用 record_human_message
# ---------------------------------------------------------------------------
class TestRecordHumanMessage(_TmpDb):
    def test_local_human_calls_service(self):
        calls = []

        class _FakeSvc:
            def record_human_message(self, conversation_id):
                calls.append(conversation_id)

        event = {
            "type": "m.room.message",
            "event_id": "$hm",
            "room_id": "!room1:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {"msgtype": "m.text", "body": "hello"},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(service=_FakeSvc())
        c.run_once()
        self.assertEqual(calls, ["!room1:example.test"])

    def test_external_human_does_not_call_service(self):
        """外服 sender → untrusted → 不重置熔断计数。"""
        calls = []

        class _FakeSvc:
            def record_human_message(self, conversation_id):
                calls.append(conversation_id)

        event = {
            "type": "m.room.message",
            "event_id": "$ext",
            "room_id": "!room2:example.test",
            "sender": "@eve:other.server",
            "content": {"msgtype": "m.text", "body": "hi"},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(service=_FakeSvc())
        c.run_once()
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# 17. 消费者禁用时 store 行为不变（GatewayServer 集成）
# ---------------------------------------------------------------------------
class _ServerFixture:
    def __init__(self, config):
        from botville_gateway.http_server import build_server
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
    def base(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"


def _http_put_txn(base, txn_id, body=None):
    if body is None:
        body = {"events": []}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}/_matrix/app/v1/transactions/{txn_id}",
        data=data,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {HS_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class TestConsumerDisabled(unittest.TestCase):
    def test_disabled_no_consumer_thread(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = os.path.join(tmpdir, "as.db")
            cfg = _cfg(hs_token=HS_TOKEN, as_txn_db_path=db, consumer_enabled=False)
            with _ServerFixture(cfg) as s:
                self.assertIsNone(s.server.as_consumer)
                status = _http_put_txn(s.base, "t1")
                self.assertEqual(status, 200)


class TestConsumerEnabled(unittest.TestCase):
    def test_put_txn_gets_processed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = os.path.join(tmpdir, "as.db")
            cfg = _cfg(
                hs_token=HS_TOKEN,
                as_txn_db_path=db,
                consumer_enabled=True,
                matrix_server_name=SERVER_NAME,
            )
            with _ServerFixture(cfg) as s:
                self.assertIsNotNone(s.server.as_consumer)
                body = {"events": [{
                    "type": "m.room.message",
                    "event_id": "$e1",
                    "room_id": "!r:example.test",
                    "sender": f"@alice:{SERVER_NAME}",
                    "content": {"msgtype": "m.text", "body": "hello"},
                }]}
                status = _http_put_txn(s.base, "tx1", body)
                self.assertEqual(status, 200)
                # 等消费者处理（唤醒机制，最多等 3 秒）
                store = build_as_txn_store(db)
                deadline = time.time() + 3.0
                processed = False
                while time.time() < deadline:
                    rows = store.get_unprocessed_transactions()
                    if not rows:
                        processed = True
                        break
                    time.sleep(0.05)
                self.assertTrue(processed, "transaction was not processed within 3s")


# ---------------------------------------------------------------------------
# 18. on_new_transaction 回调
# ---------------------------------------------------------------------------
class TestOnNewTransaction(_TmpDb):
    def test_callback_called_on_new(self):
        fired = threading.Event()
        self.store.set_on_new_transaction(lambda: fired.set())
        self.store.store_transaction("t1", "{}")
        self.assertTrue(fired.is_set())

    def test_callback_not_called_on_replay(self):
        count = [0]
        self.store.set_on_new_transaction(lambda: count.__setitem__(0, count[0] + 1))
        self.store.store_transaction("t1", "{}")
        self.store.store_transaction("t1", "{}")  # replay
        self.assertEqual(count[0], 1)

    def test_callback_exception_swallowed(self):
        def _bad():
            raise RuntimeError("wake error")
        self.store.set_on_new_transaction(_bad)
        # should not raise
        result = self.store.store_transaction("t1", "{}")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 本服判定：外服 @ai_* 与「目标 AI 只认本服」（contract §1.7.8 ⑦）
# ---------------------------------------------------------------------------
class TestLocalAiTargeting(_TmpDb):
    def _collect(self):
        calls = {"human": [], "ai": [], "invite": [], "other": []}

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                calls["human"].append(ctx)

            def on_ai_text(self, ctx):
                calls["ai"].append(ctx)

            def on_invite_to_ai(self, ctx):
                calls["invite"].append(ctx)

            def on_other(self, ctx):
                calls["other"].append(ctx)

        return calls, _H()

    def test_foreign_ai_sender_treated_as_ai_untrusted_no_record(self):
        calls, hooks = self._collect()
        service = mock.Mock()
        event = {
            "type": "m.room.message",
            "event_id": "$f1",
            "room_id": "!r:example.test",
            "sender": "@ai_x:other.server",
            "content": {"msgtype": "m.text", "body": "hello"},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        self._make_consumer(hooks=hooks, service=service).run_once()
        self.assertEqual(calls["human"], [])
        self.assertEqual(len(calls["ai"]), 1)
        self.assertEqual(calls["ai"][0].trust, "untrusted")
        self.assertFalse(calls["ai"][0].should_reply)
        service.record_human_message.assert_not_called()

    def test_foreign_ai_invite_is_not_invite_to_ai(self):
        calls, hooks = self._collect()
        event = {
            "type": "m.room.member",
            "event_id": "$inv1",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "state_key": "@ai_x:other.server",
            "content": {"membership": "invite"},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        self._make_consumer(hooks=hooks).run_once()
        self.assertEqual(calls["invite"], [])
        self.assertEqual([c.event_type for c in calls["other"]], ["other_state"])

    def test_local_ai_invite_routed(self):
        calls, hooks = self._collect()
        target = f"@ai_a:{SERVER_NAME.upper()}"
        event = {
            "type": "m.room.member",
            "event_id": "$inv2",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "state_key": target,
            "content": {"membership": "invite"},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        self._make_consumer(hooks=hooks).run_once()
        self.assertEqual(len(calls["invite"]), 1)
        self.assertEqual(calls["invite"][0].persona_user_id, target)

    def test_invite_without_server_name_fail_closed(self):
        calls, hooks = self._collect()
        event = {
            "type": "m.room.member",
            "event_id": "$inv3",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "state_key": f"@ai_a:{SERVER_NAME}",
            "content": {"membership": "invite"},
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        self._make_consumer(hooks=hooks, server_name=None).run_once()
        self.assertEqual(calls["invite"], [])
        self.assertEqual(len(calls["other"]), 1)

    def test_foreign_ai_mention_is_not_target(self):
        calls, hooks = self._collect()
        event = {
            "type": "m.room.message",
            "event_id": "$m1",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {
                "msgtype": "m.text",
                "body": "hey @ai_x:other.server",
                "m.mentions": {"user_ids": ["@ai_x:other.server"]},
            },
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        self._make_consumer(hooks=hooks).run_once()
        self.assertEqual(len(calls["human"]), 1)
        ctx = calls["human"][0]
        self.assertIsNone(ctx.persona_user_id)
        self.assertIsNone(ctx.idempotency_key)
        self.assertFalse(ctx.should_reply)

    def test_local_ai_mention_is_target(self):
        calls, hooks = self._collect()
        target = f"@ai_a:{SERVER_NAME}"
        event = {
            "type": "m.room.message",
            "event_id": "$m2",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {
                "msgtype": "m.text",
                "body": "hey",
                "m.mentions": {"user_ids": [target]},
            },
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        self._make_consumer(hooks=hooks).run_once()
        ctx = calls["human"][0]
        self.assertEqual(ctx.persona_user_id, target)
        self.assertEqual(ctx.idempotency_key, speech_idempotency_key(target, "$m2"))
        self.assertTrue(ctx.should_reply)

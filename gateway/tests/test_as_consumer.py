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
from botville_gateway.config import Config, ConfigError, load_config  # noqa: E402
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
            room_ai_user_ids=[self.AI_A], room_is_dm=True,
        ))

    def test_single_ai_is_not_automatically_a_dm(self):
        self.assertFalse(should_reply(
            sender="@human:hs", room_ai_user_ids=[self.AI_A],
        ))

    def test_full_id_is_not_a_longer_domain_prefix(self):
        self.assertFalse(should_reply(
            sender="@human:hs", room_ai_user_ids=[self.AI_A],
            text=self.AI_A + ".foreign.test",
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


class TestQ5Routing(_TmpDb):
    AI_A = "@ai_a:" + SERVER_NAME
    AI_B = "@ai_b:" + SERVER_NAME

    def test_dm_requires_exact_local_pair(self):
        cases = (
            ([self.AI_A, "@human:" + SERVER_NAME], True),
            ([self.AI_A, "@one:" + SERVER_NAME, "@two:" + SERVER_NAME], False),
            ([self.AI_A, "@human:other.test"], False),
        )
        for members, expected in cases:
            consumer = self._make_consumer(room_members=lambda _room, m=members: m)
            _ids, confirmed, is_dm, _human = consumer._confirmed_room_ai_ids("!r:" + SERVER_NAME)
            self.assertTrue(confirmed)
            self.assertEqual(is_dm, expected)

    def test_failed_lookup_allows_explicit_target_but_not_dm(self):
        seen = []
        class Hooks(ConsumerHooks):
            def on_human_text(self, ctx):
                seen.append(ctx)
        event = {"type": "m.room.message", "event_id": "$q5", "room_id": "!r:" + SERVER_NAME,
                 "sender": "@human:" + SERVER_NAME,
                 "content": {"msgtype": "m.text", "body": "ask " + self.AI_A}}
        self.store.store_transaction("q5", json.dumps({"events": [event]}))
        consumer = self._make_consumer(hooks=Hooks(), room_members=lambda _room: (_ for _ in ()).throw(RuntimeError()), persona_user_ids=(self.AI_A,))
        consumer.run_once()
        self.assertFalse(seen[0].room_is_dm)
        self.assertEqual(seen[0].target_user_ids, (self.AI_A,))

    def test_group_cap_preserves_mention_order(self):
        ids = tuple("@ai_%d:" % i + SERVER_NAME for i in range(4))
        seen = []
        class Hooks(ConsumerHooks):
            def on_human_text(self, ctx): seen.append(ctx)
        event = {"type": "m.room.message", "event_id": "$cap", "room_id": "!r:" + SERVER_NAME,
                 "sender": "@human:" + SERVER_NAME,
                 "content": {"msgtype": "m.text", "body": "hi", "m.mentions": {"user_ids": list(reversed(ids))}}}
        self.store.store_transaction("cap", json.dumps({"events": [event]}))
        consumer = self._make_consumer(hooks=Hooks(), room_members=lambda _room: list(ids) + ["@human:" + SERVER_NAME], persona_user_ids=ids, group_max_responders=2)
        consumer.run_once()
        self.assertEqual(seen[0].target_user_ids, tuple(reversed(ids))[:2])
        self.assertEqual(consumer.stats["group_responder_skips"], 2)

    def test_group_responder_config_hides_bad_value(self):
        for raw in ("0", "21", "bad"):
            with self.assertRaises(ConfigError) as caught:
                load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_GROUP_MAX_RESPONDERS": raw})
            self.assertNotIn(raw, str(caught.exception))

    def test_room_broadcast_has_no_persona_target(self):
        seen = []
        class Hooks(ConsumerHooks):
            def on_human_text(self, ctx): seen.append(ctx)
        event = {"type": "m.room.message", "event_id": "$room", "room_id": "!r:" + SERVER_NAME,
                 "sender": "@human:" + SERVER_NAME,
                 "content": {"msgtype": "m.text", "body": "all", "m.mentions": {"room": True}}}
        self.store.store_transaction("room", json.dumps({"events": [event]}))
        self._make_consumer(hooks=Hooks(), room_members=lambda _room: [self.AI_A, "@human:" + SERVER_NAME, "@other:" + SERVER_NAME], persona_user_ids=(self.AI_A,)).run_once()
        self.assertEqual(seen[0].target_user_ids, ())

    def test_dm_uses_confirmed_persona_with_multiple_configured_personas(self):
        seen = []
        class Hooks(ConsumerHooks):
            def on_human_text(self, ctx): seen.append(ctx)
        event = {"type": "m.room.message", "event_id": "$dm", "room_id": "!r:" + SERVER_NAME,
                 "sender": "@human:" + SERVER_NAME,
                 "content": {"msgtype": "m.text", "body": "ordinary text"}}
        self.store.store_transaction("dm", json.dumps({"events": [event]}))
        self._make_consumer(hooks=Hooks(), persona_user_ids=(self.AI_A, self.AI_B),
            room_members=lambda _room: [self.AI_A, "@human:" + SERVER_NAME]).run_once()
        self.assertTrue(seen[0].room_is_dm)
        self.assertEqual(seen[0].target_user_ids, (self.AI_A,))

    def test_nonmember_sender_never_inherits_dm_scope(self):
        seen = []
        class Hooks(ConsumerHooks):
            def on_human_text(self, ctx): seen.append(ctx)
        event = {"type": "m.room.message", "event_id": "$stale", "room_id": "!r:" + SERVER_NAME,
                 "sender": "@former:" + SERVER_NAME,
                 "content": {"msgtype": "m.text", "body": "ordinary text"}}
        self.store.store_transaction("stale", json.dumps({"events": [event]}))
        self._make_consumer(hooks=Hooks(), persona_user_ids=(self.AI_A,),
            room_members=lambda _room: [self.AI_A, "@human:" + SERVER_NAME]).run_once()
        self.assertFalse(seen[0].room_is_dm)
        self.assertEqual(seen[0].target_user_ids, ())

    def test_two_ai_one_human_and_lone_ai_are_group_memory(self):
        from botville_gateway.engine.engine import EngineHooks
        from botville_gateway.engine.personas import Persona
        for index, (members, mentioned, targets) in enumerate((
            ([self.AI_A, self.AI_B, "@human:" + SERVER_NAME], False, ()),
            ([self.AI_A, self.AI_B, "@human:" + SERVER_NAME], True, (self.AI_A,)),
            ([self.AI_A], False, ()),
        )):
            with self.subTest(index=index):
                seen = []
                class Hooks(ConsumerHooks):
                    def on_human_text(self, ctx): seen.append(ctx)
                event = {"type": "m.room.message", "event_id": "$group-shape" + str(index),
                         "room_id": "!r:" + SERVER_NAME, "sender": "@human:" + SERVER_NAME,
                         "content": {"msgtype": "m.text", "body": "hi",
                                     "m.mentions": {"user_ids": [self.AI_A] if mentioned else []}}}
                self.store.store_transaction("shape" + str(index), json.dumps({"events": [event]}))
                self._make_consumer(hooks=Hooks(), persona_user_ids=(self.AI_A, self.AI_B),
                                    room_members=lambda _: members).run_once()
                self.assertFalse(seen[0].room_is_dm)
                self.assertEqual(seen[0].target_user_ids, targets)
                self.assertEqual(EngineHooks._memory_scope(Persona(self.AI_A, "test"), seen[0])[0], "group")

    def test_invitation_observation_encryption_membership_and_replay(self):
        room = "!invitation:" + SERVER_NAME
        consumer = self._make_consumer()
        def event(membership, persona=self.AI_A, encrypted=False):
            return {"type": "m.room.member", "event_id": "$" + membership + persona,
                    "room_id": room, "sender": "@human:" + SERVER_NAME,
                    "state_key": persona, "content": {"membership": membership},
                    "unsigned": {"invite_room_state": [{"type": "m.room.encryption"}] if encrypted else []}}
        invite = event("invite", encrypted=True)
        consumer._dispatch_event(invite)
        self.assertIs(self.store.invitation(self.AI_A, room), True)
        for membership in ("join", "leave", "ban"):
            self.store.invitation_update(self.AI_A, room, "invite")
            consumer._dispatch_event(event(membership))
            self.assertIsNone(self.store.invitation(self.AI_A, room))
        consumer._dispatch_event(invite)
        self.assertIsNone(self.store.invitation(self.AI_A, room))
        consumer._dispatch_event(event("invite", persona="@ai_foreign:elsewhere"))
        self.assertIsNone(self.store.invitation("@ai_foreign:elsewhere", room))
        columns = self.store._conn.execute("SELECT * FROM as_invitations").description
        self.assertEqual([column[0] for column in columns], ["key", "encrypted"])

    def test_multi_human_room_ordinary_text_does_not_reply(self):
        seen = []
        class Hooks(ConsumerHooks):
            def on_human_text(self, ctx): seen.append(ctx)
        event = {"type": "m.room.message", "event_id": "$group", "room_id": "!r:" + SERVER_NAME,
                 "sender": "@human:" + SERVER_NAME,
                 "content": {"msgtype": "m.text", "body": "ordinary text"}}
        self.store.store_transaction("group", json.dumps({"events": [event]}))
        self._make_consumer(hooks=Hooks(), persona_user_ids=(self.AI_A,),
            room_members=lambda _room: [self.AI_A, "@human:" + SERVER_NAME, "@other:" + SERVER_NAME]).run_once()
        self.assertFalse(seen[0].room_is_dm)
        self.assertEqual(seen[0].target_user_ids, ())

    def test_failed_lookup_cached_ai_reply_remains_explicit_group_target(self):
        seen = []
        class Hooks(ConsumerHooks):
            def on_human_text(self, ctx): seen.append(ctx)
        events = [
            {"type": "m.room.message", "event_id": "$ai", "room_id": "!r:" + SERVER_NAME,
             "sender": self.AI_A, "content": {"msgtype": "m.text", "body": "prior"}},
            {"type": "m.room.message", "event_id": "$reply", "room_id": "!r:" + SERVER_NAME,
             "sender": "@human:" + SERVER_NAME,
             "content": {"msgtype": "m.text", "body": "answer", "m.relates_to": {"m.in_reply_to": {"event_id": "$ai"}}}},
        ]
        self.store.store_transaction("reply", json.dumps({"events": events}))
        self._make_consumer(hooks=Hooks(), persona_user_ids=(self.AI_A,),
            room_members=lambda _room: (_ for _ in ()).throw(RuntimeError())).run_once()
        self.assertFalse(seen[0].room_is_dm)
        self.assertEqual(seen[0].target_user_ids, (self.AI_A,))


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




# ---------------------------------------------------------------------------
# 19. #200 审查项 1：房间 AI 成员只保留本服 @ai_*（fail-closed）
# ---------------------------------------------------------------------------
class TestRoomAiIdsFiltering(_TmpDb):
    def _ids_for(self, members, mentions=(), server_name=SERVER_NAME):
        c = self._make_consumer(room_members=lambda room: members, server_name=server_name)
        return c._room_ai_ids("!r:example.test", mentions)

    def test_filters_non_ai_and_foreign(self):
        ids = self._ids_for([
            f"@ai_a:{SERVER_NAME}",
            f"@alice:{SERVER_NAME}",          # 本服人类
            "@ai_x:other.server",             # 外服 @ai_*
            f"@botville-gateway:{SERVER_NAME}",  # AS sender 不是 @ai_*
            "not-a-user-id",
            f"@ai_b:{SERVER_NAME.upper()}",   # 大小写不敏感
        ])
        self.assertEqual(ids, [f"@ai_a:{SERVER_NAME}", f"@ai_b:{SERVER_NAME.upper()}"])

    def test_no_server_name_fail_closed(self):
        self.assertEqual(self._ids_for([f"@ai_a:{SERVER_NAME}"], server_name=None), [])

    def test_callback_error_fail_closed_keeps_mentions_only(self):
        def _boom(_room):
            raise RuntimeError("member lookup failed")

        target = f"@ai_a:{SERVER_NAME}"
        c = self._make_consumer(room_members=_boom)
        ids = c._room_ai_ids("!r:example.test", [target])
        self.assertEqual(ids, [target])
        self.assertEqual(c.stats.get("room_member_errors"), 1)

    def test_human_member_does_not_make_room_a_dm(self):
        """回调里混入人类时，房间不能被当成「只有一个 AI」的单聊。"""
        target = f"@ai_a:{SERVER_NAME}"
        members = [target, f"@alice:{SERVER_NAME}"]
        self.assertEqual(self._ids_for(members), [target])
        call = self._make_consumer(room_members=lambda room: members)


# ---------------------------------------------------------------------------
# 20. #200 审查项 3：should_reply 尊重 in_reply_to
# ---------------------------------------------------------------------------
class TestShouldReplyInReplyTo(_TmpDb):
    AI_A = f"@ai_a:{SERVER_NAME}"
    AI_B = f"@ai_b:{SERVER_NAME}"

    def test_pure_function_reply_target_ai(self):
        self.assertTrue(should_reply(
            sender=f"@alice:{SERVER_NAME}",
            room_ai_user_ids=[self.AI_A, self.AI_B],
            reply_to_sender=self.AI_B,
        ))

    def test_pure_function_reply_target_human_no_reply(self):
        self.assertFalse(should_reply(
            sender=f"@alice:{SERVER_NAME}",
            room_ai_user_ids=[self.AI_A, self.AI_B],
            reply_to_sender=f"@bob:{SERVER_NAME}",
        ))

    def test_pure_function_reply_target_foreign_ai_no_reply(self):
        self.assertFalse(should_reply(
            sender=f"@alice:{SERVER_NAME}",
            room_ai_user_ids=[self.AI_A, self.AI_B],
            reply_to_sender="@ai_x:other.server",
        ))

    # ---- 消费者级：缓存命中 ----
    def test_cache_hit_from_previous_event_in_same_batch(self):
        calls = []

        class _H(ConsumerHooks):
            def on_ai_text(self, ctx):
                pass
            def on_human_text(self, ctx):
                calls.append(ctx)

        # 事件 1：本服 AI 发言（写入 sender 缓存）
        ai_event = {
            "type": "m.room.message",
            "event_id": "$ai-speaking",
            "room_id": "!r:example.test",
            "sender": self.AI_B,
            "content": {"msgtype": "m.text", "body": "我先说一句"},
        }
        # 事件 2：人类群聊里回复那条 AI 消息（没 @ 任何人）
        reply_event = {
            "type": "m.room.message",
            "event_id": "$human-reply",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {
                "msgtype": "m.text",
                "body": "说得好",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$ai-speaking"}},
            },
        }
        ai_ids = [self.AI_A, self.AI_B]
        self.store.store_transaction(
            "t1", json.dumps({"events": [ai_event, reply_event]})
        )
        c = self._make_consumer(hooks=_H(), room_members=lambda room: ai_ids)
        c.run_once()
        self.assertEqual(len(calls), 1)
        ctx = calls[0]
        self.assertTrue(ctx.should_reply)
        self.assertEqual(ctx.reply_to_event_id, "$ai-speaking")
        self.assertEqual(ctx.target_user_ids, (self.AI_B,))
        self.assertEqual(ctx.persona_user_id, self.AI_B)
        self.assertEqual(c.stats.get("reply_target_cached"), 1)

    def test_reply_to_human_in_group_no_reply(self):
        calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                calls.append(ctx)

        human_event = {
            "type": "m.room.message",
            "event_id": "$bob-msg",
            "room_id": "!r:example.test",
            "sender": f"@bob:{SERVER_NAME}",
            "content": {"msgtype": "m.text", "body": "大家好"},
        }
        reply_event = {
            "type": "m.room.message",
            "event_id": "$alice-reply",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {
                "msgtype": "m.text",
                "body": "嗯",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$bob-msg"}},
            },
        }
        ai_ids = [self.AI_A, self.AI_B]
        self.store.store_transaction(
            "t1", json.dumps({"events": [human_event, reply_event]})
        )
        c = self._make_consumer(hooks=_H(), room_members=lambda room: ai_ids)
        c.run_once()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(not ctx.should_reply for ctx in calls))
        self.assertEqual(calls[-1].target_user_ids, ())

    # ---- 消费者级：fetcher 回填 ----
    def test_fetch_fallback_used_when_cache_misses(self):
        calls = []
        fetched = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                calls.append(ctx)

        def _fetcher(room_id, event_id):
            fetched.append((room_id, event_id))
            return self.AI_A

        event = {
            "type": "m.room.message",
            "event_id": "$human-reply",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {
                "msgtype": "m.text",
                "body": "继续",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$older-ai-event"}},
            },
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(
            hooks=_H(),
            room_members=lambda room: [self.AI_A, self.AI_B],
            sender_fetcher=_fetcher,
        )
        c.run_once()
        self.assertEqual(fetched, [("!r:example.test", "$older-ai-event")])
        self.assertTrue(calls[0].should_reply)
        self.assertEqual(calls[0].target_user_ids, (self.AI_A,))
        self.assertEqual(c.stats.get("reply_target_fetched"), 1)
        # fetcher 结果写回缓存：再消费同一事件不再回填。
        c2 = self._make_consumer(
            hooks=_H(),
            room_members=lambda room: [self.AI_A, self.AI_B],
            sender_fetcher=_fetcher,
        )
        c2._sender_cache.remember("$older-ai-event", self.AI_A)
        c2.run_once()
        self.assertEqual(len(fetched), 1)

    def test_fetch_error_is_fail_closed_not_a_reply(self):
        calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                calls.append(ctx)

        def _boom(room_id, event_id):
            raise OSError("connection refused")

        event = {
            "type": "m.room.message",
            "event_id": "$human-reply",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {
                "msgtype": "m.text",
                "body": "继续",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$missing"}},
            },
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(
            hooks=_H(),
            room_members=lambda room: [self.AI_A, self.AI_B],
            sender_fetcher=_boom,
        )
        c.run_once()
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].should_reply)
        self.assertEqual(calls[0].target_user_ids, ())
        self.assertEqual(c.stats.get("reply_lookup_errors"), 1)
        # 事务仍然被标记 processed（回填失败不能把整条事务标 failed）。
        row = self.conn.execute(
            "SELECT processed_at, failed_at FROM as_transactions WHERE txn_id='t1'"
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNone(row[1])

    def test_fetch_returns_none_treated_as_no_reply(self):
        calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                calls.append(ctx)

        event = {
            "type": "m.room.message",
            "event_id": "$human-reply",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {
                "msgtype": "m.text",
                "body": "继续",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$gone"}},
            },
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(
            hooks=_H(),
            room_members=lambda room: [self.AI_A, self.AI_B],
            sender_fetcher=lambda room, ev: None,
        )
        c.run_once()
        self.assertFalse(calls[0].should_reply)
        self.assertEqual(calls[0].target_user_ids, ())

    def test_non_ai_reply_target_in_group(self):
        """被回复的是本服人类 → 不是回复，不发言。"""
        calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                calls.append(ctx)

        event = {
            "type": "m.room.message",
            "event_id": "$human-reply",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {
                "msgtype": "m.text",
                "body": "继续",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$older"}},
            },
        }
        self.store.store_transaction("t1", json.dumps({"events": [event]}))
        c = self._make_consumer(
            hooks=_H(),
            room_members=lambda room: [self.AI_A, self.AI_B],
            sender_fetcher=lambda room, ev: f"@bob:{SERVER_NAME}",
        )
        c.run_once()
        self.assertFalse(calls[0].should_reply)
        self.assertEqual(calls[0].target_user_ids, ())

    def test_reply_event_id_only_accepts_strings(self):
        from botville_gateway.as_consumer import reply_to_event_id

        self.assertEqual(
            reply_to_event_id({"m.relates_to": {"m.in_reply_to": {"event_id": "$e"}}}),
            "$e",
        )
        self.assertIsNone(reply_to_event_id({}))
        self.assertIsNone(reply_to_event_id(None))
        self.assertIsNone(
            reply_to_event_id({"m.relates_to": {"m.in_reply_to": {"event_id": 42}}})
        )
        self.assertIsNone(
            reply_to_event_id({"m.relates_to": {"m.in_reply_to": {"event_id": ""}}})
        )
        self.assertIsNone(
            reply_to_event_id({"m.relates_to": {"m.in_reply_to": "not-a-dict"}})
        )


# ---------------------------------------------------------------------------
# 21. #200 审查项 4：非字符串 event_id 只跳过该事件
# ---------------------------------------------------------------------------
class TestInvalidEventIdSkipped(_TmpDb):
    def test_non_string_event_id_skips_only_that_event(self):
        calls = []

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                calls.append(ctx.event_id)

        base = {
            "type": "m.room.message",
            "room_id": "!r:example.test",
            "sender": f"@alice:{SERVER_NAME}",
            "content": {"msgtype": "m.text", "body": "hi"},
        }
        events = [
            dict(base, event_id=12345),          # 数字
            dict(base, event_id=None),           # null
            {k: v for k, v in base.items() if k != "event_id"},  # 缺失
            dict(base, event_id="$good"),        # 正常
        ]
        self.store.store_transaction("t1", json.dumps({"events": events}))
        c = self._make_consumer(hooks=_H())
        c.run_once()
        self.assertEqual(calls, ["$good"])
        self.assertEqual(c.stats.get("invalid_event_ids"), 3)
        row = self.conn.execute(
            "SELECT processed_at, failed_at FROM as_transactions WHERE txn_id='t1'"
        ).fetchone()
        self.assertIsNotNone(row[0], "transaction should be marked processed")
        self.assertIsNone(row[1], "transaction must not be marked failed")


# ---------------------------------------------------------------------------
# 22. #200 审查项 5：处理期间到达的唤醒不能被吞掉（无 ~5s 延迟）
# ---------------------------------------------------------------------------
class TestWakeDuringProcessing(_TmpDb):
    def test_second_transaction_consumed_well_before_poll_interval(self):
        seen = []
        first_started = threading.Event()
        release_first = threading.Event()
        second_seen = threading.Event()

        class _H(ConsumerHooks):
            def on_human_text(self, ctx):
                if ctx.event_id == "$first":
                    first_started.set()
                    # 卡住第一轮处理，模拟「慢钩子 / 慢模型」。
                    release_first.wait(timeout=5.0)
                elif ctx.event_id == "$second":
                    second_seen.set()
                seen.append(ctx.event_id)

        # 轮询间隔 30 秒：只有唤醒机制生效才能远早于它看到第二条事务。
        c = self._make_consumer(hooks=_H(), poll_interval=30.0)
        c.start()
        try:
            self.store.store_transaction("t1", json.dumps({"events": [{
                "type": "m.room.message",
                "event_id": "$first",
                "room_id": "!r:example.test",
                "sender": f"@alice:{SERVER_NAME}",
                "content": {"msgtype": "m.text", "body": "one"},
            }]}))
            c.wake()
            self.assertTrue(first_started.wait(timeout=5.0), "first event never processed")

            # 在第一轮仍在处理时落库第二条并唤醒。
            self.store.store_transaction("t2", json.dumps({"events": [{
                "type": "m.room.message",
                "event_id": "$second",
                "room_id": "!r:example.test",
                "sender": f"@alice:{SERVER_NAME}",
                "content": {"msgtype": "m.text", "body": "two"},
            }]}))
            c.wake()
            release_first.set()

            # 若「处理期间的唤醒被吞掉」，这里要等满 30s 轮询；断言远小于它。
            self.assertTrue(
                second_seen.wait(timeout=3.0),
                "wake that arrived during processing was lost",
            )
        finally:
            release_first.set()
            c.stop(timeout=5.0)
        self.assertIn("$second", seen)



# ---------------------------------------------------------------------------
# 23. #200 审查项 6：启动顺序回归（无库路径 / 端口绑定失败）
# ---------------------------------------------------------------------------
class TestConsumerStartupOrdering(unittest.TestCase):
    def test_enabled_without_db_path_warns_and_serves_health(self):
        cfg = _cfg(consumer_enabled=True, as_txn_db_path=None)
        with self.assertLogs("botville_gateway.http_server", level="WARNING") as cm:
            with _ServerFixture(cfg) as s:
                self.assertIsNone(s.server.as_consumer)
                self.assertFalse(
                    any(t.name == "as-txn-consumer" for t in threading.enumerate()),
                    "no consumer thread may be started without a transaction DB",
                )
                # 网关照常可用：/health 仍然 200（消费开关不影响 HTTP 端口）。
                with urllib.request.urlopen(f"{s.base}/health", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
        combined = " ".join(cm.output)
        self.assertIn("GATEWAY_CONSUMER_ENABLED", combined)
        self.assertIn("GATEWAY_AS_TXN_DB_PATH", combined)

    def test_bind_failure_does_not_start_consumer(self):
        """端口已被占用（bind 失败）时不能起消费线程，也不能启动清理线程。"""
        import socket as _socket

        with tempfile.TemporaryDirectory() as tmpdir:
            db = os.path.join(tmpdir, "as.db")
            # 用裸 socket 占住一个端口：比再起一个网关更干净，不会留下消费线程。
            holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            holder.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            started = []
            real_start = AsTransactionConsumer.start

            def _record_start(consumer):  # noqa: ANN001
                started.append(consumer)
                return real_start(consumer)

            try:
                with mock.patch.object(AsTransactionConsumer, "start", _record_start):
                    with self.assertRaises(OSError):
                        build_server(
                            _cfg(consumer_enabled=True, as_txn_db_path=db, port=port)
                        )
                self.assertEqual(
                    started, [], "consumer must not start when the bind failed"
                )
                self.assertFalse(
                    any(t.name == "as-txn-consumer" for t in threading.enumerate())
                )
            finally:
                holder.close()

    def test_consumer_starts_only_after_bind_succeeds(self):
        import http.server as _http_server

        order = []
        real_init = _http_server.ThreadingHTTPServer.__init__
        real_start = AsTransactionConsumer.start

        def _init(srv, *args, **kwargs):  # noqa: ANN001
            real_init(srv, *args, **kwargs)
            order.append("bound")

        def _start(consumer):  # noqa: ANN001
            order.append("consumer-start")
            return real_start(consumer)

        with tempfile.TemporaryDirectory() as tmpdir:
            db = os.path.join(tmpdir, "as.db")
            with mock.patch.object(_http_server.ThreadingHTTPServer, "__init__", _init):
                with mock.patch.object(AsTransactionConsumer, "start", _start):
                    server = build_server(
                        _cfg(consumer_enabled=True, as_txn_db_path=db, port=0)
                    )
            try:
                self.assertEqual(order, ["bound", "consumer-start"])
                self.assertTrue(server.as_consumer.running)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()

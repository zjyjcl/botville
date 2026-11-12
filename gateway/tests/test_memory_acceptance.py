# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Black-box acceptance checks for the PR3a memory boundary."""

from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest

_GATEWAY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)

from botville_gateway.memory.profile import apply_extractions
from botville_gateway.memory.recall import assemble_recall
from botville_gateway.memory.store import MemoryStore
from botville_gateway.engine.engine import estimate_tokens
from botville_gateway.memory.api import MemoryPermissionError, OwnerMemoryAPI


class MemoryAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def _item(
        self, *, scope_kind: str, scope_key: str, text: str, event_id: str,
        pinned: bool = False,
    ) -> None:
        self.assertTrue(self.store.add_item(
            persona_id="@ai:example.org", scope_kind=scope_kind,
            scope_key=scope_key, room_id=scope_key,
            counterpart="@human:example.org", event_id=event_id,
            sender="@human:example.org", role="user", trust="untrusted",
            text=text, pinned=pinned,
        ))

    def test_group_never_recalls_dm_or_dm_only_fact(self) -> None:
        self._item(scope_kind="dm", scope_key="@human:example.org",
                   text="private direct message", event_id="$dm")
        self._item(scope_kind="group", scope_key="!one:example.org",
                   text="group one", event_id="$group-one")
        self._item(scope_kind="group", scope_key="!two:example.org",
                   text="group two", event_id="$group-two")
        self.store.upsert_fact(
            "@ai:example.org", "@human:example.org", "name", "Ada",
            visibility="dm_only", source="owner", force=True,
        )
        self.store.upsert_fact(
            "@ai:example.org", "@human:example.org", "language", "中文",
            visibility="shared", source="owner", force=True,
        )

        group = assemble_recall(
            self.store, persona_id="@ai:example.org", scope_kind="group",
            scope_key="!one:example.org", counterpart="@human:example.org",
        )
        text = "\n".join(entry["content"] for entry in group)
        self.assertIn("group one", text)
        self.assertIn("language: 中文", text)
        self.assertNotIn("private direct message", text)
        self.assertNotIn("group two", text)
        self.assertNotIn("name: Ada", text)

    def test_priority_and_untrusted_wrapper_survive_budgeting(self) -> None:
        self._item(scope_kind="dm", scope_key="@human:example.org",
                   text="recent ordinary content", event_id="$recent")
        self._item(scope_kind="dm", scope_key="@human:example.org",
                   text="pinned content " * 700, event_id="$pinned", pinned=True)
        self.store.upsert_fact(
            "@ai:example.org", "@human:example.org", "name", "Ada",
            source="owner", force=True,
        )
        recalled = assemble_recall(
            self.store, persona_id="@ai:example.org", scope_kind="dm",
            scope_key="@human:example.org", counterpart="@human:example.org",
            budget=100,
        )
        rendered = "\n".join(entry["content"] for entry in recalled)
        # A limited budget must preserve the highest-priority pinned data before
        # lower-priority profile/recent data, and never makes it instruction text.
        self.assertIn("pinned content", rendered)
        self.assertNotIn("recent ordinary content", rendered)
        self.assertNotIn("system", rendered.lower().replace("untrusted", ""))
        for entry in recalled:
            self.assertEqual(entry["role"], "user")
            self.assertTrue(entry["content"].startswith("<untrusted id="))
            self.assertIn("</untrusted id=", entry["content"])
        self.assertLessEqual(
            sum(estimate_tokens(entry["content"]) for entry in recalled),
            100,
        )

    def test_redaction_removes_only_sole_source_fact_and_blocks_replay(self) -> None:
        self.assertEqual(apply_extractions(
            self.store, persona_id="@ai:example.org",
            counterpart="@human:example.org", trusted_local=True, role="user",
            facts=[{"key": "likes", "value": "tea"}], event_id="$one",
            sender="@human:example.org", server_name="example.org",
        ), 1)
        self.store.redact_event("$one")
        self.assertEqual(self.store.facts("@ai:example.org"), [])
        self.assertEqual(apply_extractions(
            self.store, persona_id="@ai:example.org",
            counterpart="@human:example.org", trusted_local=True, role="user",
            facts=[{"key": "likes", "value": "tea"}], event_id="$one",
            sender="@human:example.org", server_name="example.org",
        ), 0)

    def test_new_db_is_wal_and_has_required_private_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "memory.db")
            store = MemoryStore(path)
            try:
                tables = {
                    row[0] for row in store._db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue({
                    "memory_schema", "memory_items", "memory_facts",
                    "memory_tombstones",
                }.issubset(tables))
                self.assertEqual(
                    store._db.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
            finally:
                store.close()

    def test_cli_uses_persona_owner_and_returns_json(self) -> None:
        """The administrative entry point is owner-gated and emits no prose."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "memory.db")
            personas_path = os.path.join(directory, "personas.json")
            with open(personas_path, "w", encoding="utf-8") as handle:
                json.dump({"personas": [{
                    "user_id": "@ai_example:example.org",
                    "system_prompt": "test persona",
                    "owner_user_id": "@owner:example.org",
                }]}, handle)
            env = dict(os.environ)
            env["PYTHONPATH"] = os.path.abspath("gateway/src")
            env["GATEWAY_MATRIX_SERVER_NAME"] = "example.org"
            command = [
                sys.executable, "-m", "botville_gateway.memory", "set-fact",
                "--db", db_path, "--persona", "@ai_example:example.org",
                "--as-owner", "@owner:example.org", "--personas-file", personas_path,
                "--key", "name", "--value", "Ada", "--counterpart", "@owner:example.org",
            ]
            result = subprocess.run(
                command, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["changed"])

    def test_owner_api_denies_non_owner_and_ownerless_persona(self) -> None:
        api = OwnerMemoryAPI(self.store, {
            "@ai:example.org": "@owner:example.org",
            "@ai_no_owner:example.org": None,
        })
        with self.assertRaises(MemoryPermissionError):
            api.list_items("@ai:example.org", "@other:example.org")
        with self.assertRaises(MemoryPermissionError):
            api.list_items("@ai_no_owner:example.org", "@owner:example.org")

    def test_cli_item_and_fact_operations_and_denial_are_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "memory.db")
            personas_path = os.path.join(directory, "personas.json")
            with open(personas_path, "w", encoding="utf-8") as handle:
                json.dump({"personas": [{
                    "user_id": "@ai_example:example.org",
                    "system_prompt": "test persona",
                    "owner_user_id": "@owner:example.org",
                }, {
                    "user_id": "@ai_no_owner:example.org",
                    "system_prompt": "test persona",
                }]}, handle)
            seeded = MemoryStore(db_path)
            try:
                seeded.add_item(
                    persona_id="@ai_example:example.org", scope_kind="dm",
                    scope_key="@owner:example.org", room_id="!room:example.org",
                    counterpart="@owner:example.org", event_id="$event",
                    sender="@owner:example.org", role="user", trust="trusted",
                    text="CLI fixture body",
                )
                item_id = seeded.items("@ai_example:example.org")[0]["id"]
            finally:
                seeded.close()
            env = dict(os.environ)
            env["PYTHONPATH"] = _GATEWAY_SRC
            env["GATEWAY_MATRIX_SERVER_NAME"] = "example.org"

            def invoke(command: str, *extra: str, persona: str = "@ai_example:example.org",
                       owner: str = "@owner:example.org") -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, "-m", "botville_gateway.memory", command,
                     "--db", db_path, "--persona", persona, "--as-owner", owner,
                     "--personas-file", personas_path, *extra],
                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False,
                )

            for command, extra in (
                ("show", ("--kind", "item", "--id", str(item_id))),
                ("pin", ("--kind", "item", "--id", str(item_id))),
                ("unpin", ("--kind", "item", "--id", str(item_id))),
                ("set-fact", ("--key", "name", "--value", "Ada", "--counterpart", "@owner:example.org")),
                ("show", ("--kind", "fact", "--key", "name", "--counterpart", "@owner:example.org")),
                ("pin", ("--kind", "fact", "--key", "name", "--counterpart", "@owner:example.org")),
                ("unpin", ("--kind", "fact", "--key", "name", "--counterpart", "@owner:example.org")),
                ("delete", ("--kind", "fact", "--key", "name", "--counterpart", "@owner:example.org")),
                ("delete", ("--kind", "item", "--id", str(item_id))),
            ):
                result = invoke(command, *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsInstance(json.loads(result.stdout), dict)

            denied = invoke("list", owner="@other:example.org")
            self.assertEqual(denied.returncode, 3)
            self.assertEqual(
                json.loads(denied.stderr)["error"]["code"],
                "memory.owner_permission_denied",
            )
            no_owner = invoke("list", persona="@ai_no_owner:example.org")
            self.assertEqual(no_owner.returncode, 3)
            self.assertEqual(
                json.loads(no_owner.stderr)["error"]["code"],
                "memory.owner_permission_denied",
            )

            unopened_db = os.path.join(directory, "ownerless-memory.db")
            original_db = db_path
            db_path = unopened_db
            no_owner = invoke("list", persona="@ai_no_owner:example.org")
            self.assertEqual(no_owner.returncode, 3)
            self.assertFalse(os.path.exists(unopened_db))
            db_path = original_db


if __name__ == "__main__":
    unittest.main()

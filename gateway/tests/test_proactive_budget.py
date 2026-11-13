# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""PR4c accounting regressions for successful proactive delivery."""

from __future__ import annotations

import datetime
import os
import sys
import unittest
from datetime import timedelta, timezone

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config
from botville_gateway.limits import LimitStore
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.service import GatewayService
from botville_gateway.store import EventKind, UsageStore


class Clock:
    def __init__(self):
        self.now = datetime.datetime(2026, 11, 3, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def make_service(**overrides):
    values = dict(dev_mode=True, silent_hours_enabled=False,
                  rate_limit_messages=10, rate_limit_window_seconds=3600,
                  platform_token_budget=100, platform_proactive_per_day=3)
    values.update(overrides)
    config = Config(**values)
    clock = Clock()
    service = GatewayService(config, InMemoryMatrixTransport(), UsageStore(max_events=0),
                             LimitStore(config), now_fn=clock)
    return service, clock


def send_proactive(service, *, tokens=50):
    reservation = service.reserve_proactive(
        persona_id="p", conversation_id="!dm", peer_id="@human:local", token_estimate=tokens,
    )
    assert reservation is not None
    return service.send_ai_message(
        "!dm", {"sender_persona_id": "p", "peer_id": "@human:local",
                  "content": {"type": "text", "text": "hello"},
                  "token_estimate": tokens},
        idempotency_key="proactive-test-" + str(tokens), proactive_reservation=reservation,
    )


class ProactiveBudgetTests(unittest.TestCase):
    def test_successful_proactive_consumes_token_budget_before_next_model(self):
        service, clock = make_service(platform_token_budget=90)
        self.assertEqual(send_proactive(service, tokens=60)["outcome"], "delivered")
        self.assertEqual(service.get_usage(persona_id="p")["budget"]["used"], 60)
        # A distinct later tick must still see the successful proactive event.
        clock.advance(seconds=1)
        # A worker consults this pre-model reservation gate.  It must fail
        # before the second model invocation rather than after generating text.
        self.assertIsNone(service.reserve_proactive(
            persona_id="p", conversation_id="!dm", peer_id="@human:local", token_estimate=40,
        ))

    def test_successful_proactive_counts_for_message_and_min_interval(self):
        service, _ = make_service()
        service.put_limits({"layer": "owner", "scope": "persona", "scope_id": "p",
                            "rules": {"messages_per_window": 1,
                                      "min_seconds_between_messages": 120}})
        self.assertEqual(send_proactive(service)["outcome"], "delivered")
        decision = service.evaluate_policy({"persona_id": "p", "conversation_id": "!dm",
                                            "peer_id": "@human:local", "content": "x"})
        checks = {item["key"]: item for item in decision["checks"]}
        self.assertFalse(checks["messages.window"]["allowed"])
        self.assertFalse(checks["min_interval"]["allowed"])

    def test_concurrent_proactive_daily_reservations_fill_single_slot(self):
        service, _ = make_service(platform_proactive_per_day=1)
        first = service.reserve_proactive(
            persona_id="p", conversation_id="!dm", peer_id="@human:local", token_estimate=1,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(service.reserve_proactive(
            persona_id="p", conversation_id="!dm", peer_id="@human:local", token_estimate=1,
        ))
        service.release_proactive(first)

    def test_proactive_at_local_midnight_counts_in_daily_budget(self):
        service, clock = make_service(platform_proactive_per_day=1)
        # Default platform timezone is Asia/Shanghai: 16:00 UTC is midnight.
        clock.now = clock.now.replace(hour=16, minute=0, second=0)
        send_proactive(service)
        clock.advance(seconds=1)
        self.assertIsNone(service.reserve_proactive(
            persona_id="p", conversation_id="!dm", peer_id="@human:local", token_estimate=1))

    def test_proactive_success_is_a_proactive_usage_event(self):
        service, _ = make_service()
        send_proactive(service)
        self.assertEqual([event.kind for event in service._store.all_events()
                          if event.kind == EventKind.PROACTIVE], [EventKind.PROACTIVE])


if __name__ == "__main__":
    unittest.main()

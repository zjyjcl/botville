"""Botville mock Sygnal pushkin (A1.2).

A stand-in for the APNs pushkin that records every dispatched notification as
one JSONL line instead of talking to Apple. It exists so the homeserver ->
Sygnal half of the push path can be latency-tested end to end in CI and on
machines with no Apple developer account / no real .p8 key.

Sygnal loads this class because the app config in sygnal.yaml.tmpl declares

    "org.botville.mock":
      type: botville_mock_pushkin.MockPushkin

and docker-compose.yml bind-mounts this directory at /custom with
PYTHONPATH=/custom (custom pushkins must be importable; see sygnal's
docs/applications.md). The interface implemented here matches sygnal 0.17's
sygnal/notifications.py Pushkin: subclasses provide

    async def dispatch_notification(self, n, device, context) -> List[str]

returning the list of rejected pushkeys (devices the homeserver should drop).

Each JSONL record (one line per notification *device*):

    {"app_id": ..., "event_id": ..., "prio": ..., "pushkey": ...,
     "received_at_ms": ..., "room_id": ..., "type": ...}

server/tests/push_e2e.py polls this file (docker compose exec ... cat, or a
host path via --pushes-file) and computes send -> receive latency from
received_at_ms. Standard library only; the unit test in
server/tests/test_push_sygnal.py stubs the sygnal import so it runs without
the sygnal package installed.
"""

import json
import logging
import time

log = logging.getLogger("botville.mock_pushkin")

# Default inside the container; overridable per-app via `pushes_path` in
# sygnal.yaml (the compose stack bind-mounts a writable scratch dir at /data).
DEFAULT_PUSHES_PATH = "/data/mock-pushes.jsonl"

try:
    # Pushkin only provides name/sygnal/cfg attributes and the dispatch
    # interface; importing it is the whole sygnal dependency of this file.
    from sygnal.notifications import Pushkin
except ImportError as exc:  # pragma: no cover - only outside the sygnal image
    raise ImportError(
        "botville_mock_pushkin must be imported by sygnal (inside the "
        "matrixdotorg/sygnal image, with this directory on PYTHONPATH); "
        "the unit test stubs this import instead of installing sygnal"
    ) from exc


class MockPushkin(Pushkin):
    """Records notifications to a JSONL file; never rejects a pushkey."""

    def __init__(self, name, sygnal, config):
        super().__init__(name, sygnal, config)
        self.pushes_path = (self.cfg or {}).get("pushes_path", DEFAULT_PUSHES_PATH)

    async def dispatch_notification(self, n, device, context):
        # getattr everywhere: with format=event_id_only the homeserver omits
        # most fields; sygnal leaves the attributes None rather than raising.
        record = {
            "received_at_ms": int(time.time() * 1000),
            "app_id": getattr(device, "app_id", None),
            "pushkey": getattr(device, "pushkey", None),
            "event_id": getattr(n, "event_id", None),
            "room_id": getattr(n, "room_id", None),
            "type": getattr(n, "type", None),
            "prio": getattr(n, "prio", None),
        }
        # One short append per dispatch; a blocking write is fine at push
        # volumes and keeps the file readable by `cat` from outside.
        with open(self.pushes_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
        log.info(
            "mock push recorded: app_id=%s event_id=%s pushkey=%s -> %s",
            record["app_id"],
            record["event_id"],
            record["pushkey"],
            self.pushes_path,
        )
        # Nothing is ever invalid for the mock — no rejected pushkeys.
        return []

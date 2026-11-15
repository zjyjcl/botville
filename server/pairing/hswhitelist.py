"""Homeserver federation whitelist rendering (A-R2, issue #118 item 8).

fedgate is the real-time gate: it refuses unpaired traffic in both
directions within ~1s of a pairing change. What it cannot stop is the
local homeserver *trying* to send to a severed peer (the attempt dies at
the peer's gate, but the local HS still opens the connection and queues
retries). This module makes the homeserver itself refuse: on every pairing
change the pairing service renders Synapse's `federation_domain_whitelist`
into a small include file -- active peers plus our own server name, never
severed ones -- and drops a "restart required" marker next to it.

The pairing service never restarts anything (it holds no docker socket).
Synapse reads the include only at startup, so an operator or an external
supervisor (e.g. the optional systemd path unit in
docs/deploy/paired-federation.md) restarts Synapse when the marker shows
up and removes the marker afterwards. Until then fedgate keeps enforcing
the new state on its own.

Guarantees (tests: server/pairing/tests/test_hswhitelist.py):
- the list is sorted and de-duplicated, so the same pairing state always
  renders byte-identical text;
- writes are atomic (temp file in the same directory, fsync, os.replace),
  so Synapse never reads a half-written include;
- when the rendered text equals what is already on disk nothing is
  written and NO marker is created (no pointless restarts);
- the marker is written (atomically) only after the include changed;
- exception: when this process's FIRST sync finds no include at all, the
  file is created without a marker. The homeserver loads the include via
  --config-path and cannot have started without it, so there is nothing
  running to restart (first boot: pairing renders before the homeserver
  starts). A file that disappears later is recreated WITH a marker.

Only Synapse is supported. Conduit-family homeservers (tuwunel,
continuwuity) use a different config surface and are left for a later
change after A-R3; see docs/deploy/paired-federation.md.
"""

import json
import logging
import os
import tempfile
import threading
import time

log = logging.getLogger("pairing.hswhitelist")

WHITELIST_KEY = "federation_domain_whitelist"
MARKER_SUFFIX = ".restart-required"
FILE_MODE = 0o644  # not a secret; the homeserver (other uid) must read it


def render(own_server_name, active_peers):
    """Synapse YAML include text for own server + active peers.

    Names are emitted as JSON strings (valid YAML scalars) so no name can
    ever change the YAML structure; pairing only admits bare DNS names
    anyway (validate.normalize_server_name).
    """
    names = sorted({own_server_name.lower()} | {p.lower() for p in active_peers})
    lines = [
        "# Rendered by the botville pairing service (A-R2). Do not edit:",
        "# overwritten on every pairing change. Active peers + this server;",
        "# Synapse reads it at startup only (restart when the marker appears).",
        "{}:".format(WHITELIST_KEY),
    ]
    lines.extend("  - {}".format(json.dumps(name)) for name in names)
    return "\n".join(lines) + "\n"


def _atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


class HsWhitelistWriter:
    """Renders the include file and the restart marker; thread-safe."""

    def __init__(self, path, server_name, marker_path=None):
        self.path = path
        self.server_name = server_name
        self.marker_path = marker_path or path + MARKER_SUFFIX
        self._lock = threading.Lock()
        # Set when the include changed but the marker could not be written;
        # the next sync retries the marker even though the content is
        # already current, so a restart is never silently lost.
        self._marker_pending = False
        self._first_sync = True

    def sync(self, active_peers, now=None):
        """Render for `active_peers`; True when the file and the restart
        marker were written, False when nothing needs a restart (content
        already current, or the first-boot creation above). Raises
        OSError on write failure (callers log and retry later)."""
        text = render(self.server_name, active_peers)
        with self._lock:
            current = _read(self.path)
            first, self._first_sync = self._first_sync, False
            if current == text and not self._marker_pending:
                return False
            _atomic_write(self.path, text)
            if current is None and first and not self._marker_pending:
                return False  # first boot: the homeserver is not running yet
            self._marker_pending = True
            stamp = time.time() if now is None else now
            _atomic_write(self.marker_path, json.dumps({
                "reason": "federation whitelist changed",
                "whitelist": os.path.basename(self.path),
                "at": int(stamp),
            }) + "\n")
            self._marker_pending = False
            return True

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
- the marker is written (atomically) only when the include changes, and
  it is written TWICE around the include write (#247 review S2): once
  BEFORE the include is replaced, so a crash between the two writes can
  never lose the restart (the next process would find the include already
  current and write nothing), and once AFTER, so a supervisor that
  consumed the first marker before the include landed restarts again with
  the new content (on the normal path the worst case is one extra
  restart). A narrow crash window remains (#253 review nit 4): if a
  supervisor claims the pre-marker before the include is replaced and
  this process then dies after the include lands but before the
  post-marker is written (or dies after a persistent post-marker
  failure), a newly started process may see current content and no
  in-memory pending flag, and skip a rewrite. The missed-restart
  chance is small; it is not "never";
- reading the peers table, rendering and writing all happen under one
  lock (#247 review S1), so a slower concurrent sync can never write an
  older snapshot over a newer one;
- a marker that cannot be written stays pending in this process: every
  later sync (the service's 60s loop included) rewrites the include and
  retries the marker, raising MarkerWriteError each time so the caller
  logs an ERROR (#247 review N3). The pending flag is process-local
  (#253 review nit 3): if this process exits while the marker is still
  unwritable, the next process sees an already-current include and will
  not retry or log ERROR until the next pairing change. An operator who
  saw those errors and then restarted pairing should restart the
  homeserver once by hand;
- if the include write fails after this sync created a pre-marker and
  no marker was already on disk, the pre-marker is removed so a
  supervisor is not restarted every 60s for a write that never landed
  (#253 review 2). A crash during the include write still leaves the
  pre-marker (S2). A pre-existing marker is left in place;
- exception: when this process's FIRST sync finds no include at all, the
  file is created without a marker. The homeserver loads the include via
  --config-path and cannot have started without it, so there is nothing
  running to restart (first boot: pairing renders before the homeserver
  starts). A file that disappears later is recreated WITH a marker -- but
  only within the same process (#247 review S4): "first sync" is
  per-process state, so a file deleted while the service is down is
  recreated at the next start WITHOUT a marker.

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


class MarkerWriteError(OSError):
    """The include was written but the restart marker could not be; the
    restart stays pending and is retried on every later sync."""


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

    def _write_marker(self, stamp):
        _atomic_write(self.marker_path, json.dumps({
            "reason": "federation whitelist changed",
            "whitelist": os.path.basename(self.path),
            "at": int(stamp),
        }) + "\n")

    def sync(self, active_peers, now=None):
        """Render for `active_peers` -- an iterable of names, or a callable
        returning one (called under the writer lock, so read -> render ->
        write is one critical section). If the callable is
        store.active_server_names, that takes the store lock next: the
        lock order is always writer -> store (#253 review nit 10). True
        when the file and the restart marker were written, False when
        nothing needs a restart (content already current, or the
        first-boot creation above). Raises OSError when the include
        cannot be written and MarkerWriteError when only the marker
        failed (callers log and retry later)."""
        with self._lock:
            peers = active_peers() if callable(active_peers) else active_peers
            text = render(self.server_name, peers)
            current = _read(self.path)
            first, self._first_sync = self._first_sync, False
            if current == text and not self._marker_pending:
                return False
            # first + no include => first boot; _marker_pending cannot be
            # set yet (#253 review nit 9).
            needs_restart = not (current is None and first)
            stamp = time.time() if now is None else now
            pre_marker_written = False
            had_marker = os.path.exists(self.marker_path)
            if needs_restart:
                self._marker_pending = True
                try:
                    self._write_marker(stamp)  # before: crash-safe
                    pre_marker_written = True
                except OSError:
                    pass  # retried right after the include write below
            try:
                _atomic_write(self.path, text)
            except Exception:
                # Include is still the old content (replace is the last
                # step that can raise). Drop a pre-marker this sync just
                # created, so a 60s retry does not bounce Synapse
                # (#253 review 2). Leave a marker that was already there.
                if pre_marker_written and not had_marker:
                    try:
                        os.unlink(self.marker_path)
                    except OSError:
                        pass
                raise
            if not needs_restart:
                return False  # first boot: the homeserver is not running yet
            try:
                self._write_marker(stamp)  # after: covers an early consumer
            except OSError as exc:
                raise MarkerWriteError(
                    "restart marker {} not written: {}".format(
                        self.marker_path, exc)) from exc
            self._marker_pending = False
            return True

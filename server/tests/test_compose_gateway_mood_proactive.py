#!/usr/bin/env python3
"""Gateway mood (B line PR4b) / proactive speech (B line PR4c) / group reply
cap (B line B2.8a, GATEWAY_GROUP_MAX_RESPONDERS) / group invites (B line
B2.8b, GATEWAY_GROUPS_ENABLED, GATEWAY_GROUP_MAX_ROOMS_PER_PERSONA,
GATEWAY_INVITE_LEDGER_TTL_SECONDS) / group throttling (B line B2.8c,
GATEWAY_GROUP_FORGET_STALE_KEYS, GATEWAY_GROUP_COOLDOWN_SECONDS,
GATEWAY_GROUP_MAX_REPLIES_PER_HOUR, GATEWAY_GROUP_AI_LOOP_MAX_PER_HOUR,
GATEWAY_GROUP_MAX_CONSECUTIVE_AI_MESSAGES) pass-through.

The names and defaults must agree across the compose file, all three
.env.example files (repo root, server/compose, gateway), the CI allowlist,
the docs and what gateway/src config.py actually does (load_config is run in a subprocess with
a throwaway env; nothing secret is involved). Render checks need docker
compose and are skipped without it. Run with:
  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "server" / "compose" / "docker-compose.yml"
GATEWAY = REPO_ROOT / "gateway"
CONFIG_PY = GATEWAY / "src" / "botville_gateway" / "config.py"
ENV_EXAMPLES = (
    REPO_ROOT / ".env.example",
    REPO_ROOT / "server" / "compose" / ".env.example",
    GATEWAY / ".env.example",
)
REQUIRED_ENV = "POSTGRES_PASSWORD=test-only\nBOTVILLE_REGISTRATION_TOKEN=test-only\n"

# name -> (compose/.env.example default, Config field, parsed default)
EXPECTED = {
    "GATEWAY_MOOD_ENABLED": ("false", "mood_enabled", False),
    "GATEWAY_MOOD_HALF_LIFE_MINUTES": ("240", "mood_half_life_minutes", 240),
    "GATEWAY_PROACTIVE_ENABLED": ("false", "proactive_enabled", False),
    "GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED": (
        "false", "proactive_recent_contacts_enabled", False),
    "GATEWAY_PROACTIVE_TICK_SECONDS": ("300", "proactive_tick_seconds", 300),
    "GATEWAY_PROACTIVE_MIN_IDLE_MINUTES": ("180", "proactive_min_idle_minutes", 180),
    "GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES": (
        "240", "proactive_min_interval_minutes", 240),
    "GATEWAY_PROACTIVE_PER_TARGET_PER_DAY": ("1", "proactive_per_target_per_day", 1),
    # B line B2.8a: max AIs answering one human group message, 1-20.
    "GATEWAY_GROUP_MAX_RESPONDERS": ("3", "group_max_responders", 3),
    # B line B2.8b group invites.
    "GATEWAY_GROUPS_ENABLED": ("false", "groups_enabled", False),
    "GATEWAY_GROUP_MAX_ROOMS_PER_PERSONA": ("20", "group_max_rooms_per_persona", 20),
    "GATEWAY_INVITE_LEDGER_TTL_SECONDS": ("86400", "invite_ledger_ttl_seconds", 86400),
    # B line B2.8c group throttling.
    "GATEWAY_GROUP_FORGET_STALE_KEYS": ("false", "group_forget_stale_keys", False),
    "GATEWAY_GROUP_COOLDOWN_SECONDS": ("30", "group_cooldown_seconds", 30),
    "GATEWAY_GROUP_MAX_REPLIES_PER_HOUR": ("20", "group_max_replies_per_hour", 20),
    "GATEWAY_GROUP_AI_LOOP_MAX_PER_HOUR": ("30", "group_ai_loop_max_per_hour", 30),
    "GATEWAY_GROUP_MAX_CONSECUTIVE_AI_MESSAGES": (
        "3", "group_max_consecutive_ai_messages", 3),
}
# Supported ranges: name -> (minimum, maximum).
# KEEP IN SYNC with gateway/src (change both together -- the edge tests below
# start the gateway at both ends and just outside, so drift fails here):
#   MOOD_HALF_LIFE_MINUTES          config.py MOOD_HALF_LIFE_MINUTES_MIN / _MAX
#   PROACTIVE_* (4 ranges)          config.py Config.__post_init__ proactive_ranges
#   GROUP_MAX_RESPONDERS            config.py _env_group_max_responders + __post_init__
#   GROUP_MAX_ROOMS_PER_PERSONA     config.py Config.__post_init__ (1-200)
#   INVITE_LEDGER_TTL_SECONDS       as_txn_store.py INVITE_LEDGER_TTL_MIN/MAX_SECONDS
#   GROUP_COOLDOWN_SECONDS,         config.py Config.__post_init__ B2.8c loop
#   GROUP_MAX_REPLIES_PER_HOUR,       ("group_cooldown_seconds", 0, 3600), ...
#   GROUP_AI_LOOP_MAX_PER_HOUR,
#   GROUP_MAX_CONSECUTIVE_AI_MESSAGES
# BOOLS below pairs with the _env_bool(...) calls in config.py load_config
# (GATEWAY_GROUP_FORGET_STALE_KEYS: __post_init__ boolean check).
# The docs tables (appservice.md), the .env.example comments and the
# compose comments quote the same ranges.
RANGES = {
    "GATEWAY_MOOD_HALF_LIFE_MINUTES": (1, 10_080),
    "GATEWAY_PROACTIVE_TICK_SECONDS": (30, 86_400),
    "GATEWAY_PROACTIVE_MIN_IDLE_MINUTES": (0, 43_200),
    "GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES": (0, 43_200),
    "GATEWAY_PROACTIVE_PER_TARGET_PER_DAY": (0, 24),
    "GATEWAY_GROUP_MAX_RESPONDERS": (1, 20),
    "GATEWAY_GROUP_MAX_ROOMS_PER_PERSONA": (1, 200),
    "GATEWAY_INVITE_LEDGER_TTL_SECONDS": (60, 2_592_000),
    "GATEWAY_GROUP_COOLDOWN_SECONDS": (0, 3600),
    "GATEWAY_GROUP_MAX_REPLIES_PER_HOUR": (1, 200),
    "GATEWAY_GROUP_AI_LOOP_MAX_PER_HOUR": (1, 1000),
    "GATEWAY_GROUP_MAX_CONSECUTIVE_AI_MESSAGES": (1, 50),
}
# Distinctive invalid values: if one shows up in an error message, the
# gateway echoed deployment input. None of them can occur in a fixed message.
SENTINEL = "873461"
BOOLS = ("GATEWAY_MOOD_ENABLED", "GATEWAY_PROACTIVE_ENABLED",
         "GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED", "GATEWAY_GROUPS_ENABLED",
         "GATEWAY_GROUP_FORGET_STALE_KEYS")

# Runs gateway/src load_config on a JSON env from argv; prints the parsed
# fields, or the ConfigError message. Dev mode, no secrets.
PROBE = r"""
import json, sys
from botville_gateway.config import ConfigError, load_config
env = json.loads(sys.argv[1])
fields = json.loads(sys.argv[2])
try:
    cfg = load_config(env)
except ConfigError as exc:
    print(json.dumps({"error": str(exc)}))
else:
    print(json.dumps({f: getattr(cfg, f) for f in fields}))
"""


def env_example_values(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values.setdefault(key, []).append(value)
    return values


def run_gateway_config(env):
    fields = [field for _, field, _ in EXPECTED.values()]
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(env), json.dumps(fields)],
        cwd=str(GATEWAY), env={"PYTHONPATH": str(GATEWAY / "src"), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError("gateway config probe failed (exit {}): {}".format(
            proc.returncode, proc.stderr[-2000:]))
    return json.loads(proc.stdout.strip().splitlines()[-1])


class MoodProactiveEnvStaticTest(unittest.TestCase):
    def test_compose_passes_through_with_defaults(self):
        text = COMPOSE.read_text(encoding="utf-8")
        for key, (default, _, _) in EXPECTED.items():
            line = "      {0}: ${{{0}:-{1}}}\n".format(key, default)
            self.assertEqual(text.count(line), 1, key)
            # Nowhere else (e.g. a hard-coded value in another service).
            self.assertEqual(len(re.findall(r"(?m)^\s+{}:".format(key), text)), 1, key)

    def test_env_examples_register_the_same_defaults(self):
        for path in ENV_EXAMPLES:
            values = env_example_values(path)
            for key, (default, _, _) in EXPECTED.items():
                with self.subTest(path=str(path.relative_to(REPO_ROOT)), key=key):
                    self.assertEqual(values.get(key), [default])

    def test_names_are_read_by_gateway_src(self):
        src = CONFIG_PY.read_text(encoding="utf-8")
        for key, (_, field, _) in EXPECTED.items():
            self.assertIn('"{}"'.format(key), src, key + " is not read by config.py")
            self.assertRegex(src, r"(?m)^\s+{}\s*[:=]".format(field), field)
        for key in BOOLS:
            self.assertRegex(src, r'_env_bool\(\s*source,\s*"{}",\s*False\s*\)'.format(key), key)

    def test_gateway_src_defaults_match(self):
        """Run load_config with nothing set: the parsed defaults are the ones
        compose / .env.example ship."""
        got = run_gateway_config({"GATEWAY_DEV_MODE": "true"})
        for key, (default, field, parsed) in EXPECTED.items():
            self.assertEqual(got.get(field), parsed, key)
            self.assertEqual(str(parsed).lower(), default, key)
        # Explicitly setting the shipped defaults parses to the same thing.
        env = {"GATEWAY_DEV_MODE": "true"}
        env.update({k: v[0] for k, v in EXPECTED.items()
                    if k != "GATEWAY_PROACTIVE_ENABLED"})
        got = run_gateway_config(env)
        for key, (_, field, parsed) in EXPECTED.items():
            self.assertEqual(got.get(field), parsed, key)

    def assert_rejected_without_echo(self, key, value):
        got = run_gateway_config({"GATEWAY_DEV_MODE": "true", key: value})
        self.assertIn("error", got, "{}={} accepted".format(key, value))
        message = got["error"]
        if SENTINEL in value:
            self.assertNotIn(SENTINEL, message)
        digits = value.strip().lstrip("+-")
        if digits.isdigit():
            # A plain number must not appear as a standalone number. (Fixed
            # messages may quote the range, e.g. "within 60-2592000", so a
            # substring check would flag "59" / "0" without any echo.)
            # LIMITATION: this regex cannot see the value embedded in a
            # longer digit run (an echo such as "got 590" for value 59 is
            # missed; see test_standalone_number_check_limitation). The gap
            # is closed by test_error_messages_do_not_depend_on_the_value,
            # which requires the SAME message for different invalid values.
            self.assertNotRegex(message, r"(?<![0-9]){}(?![0-9])".format(re.escape(digits)))
        else:
            self.assertNotIn(value.strip(), message)
        return message

    def test_standalone_number_check_limitation(self):
        """Documents why the value-independence test exists: the standalone
        regex used above misses a value glued into a longer number."""
        pattern = r"(?<![0-9]){}(?![0-9])".format(re.escape("59"))
        self.assertIsNone(re.search(pattern, "must be within 60-2592000"))  # no false hit
        self.assertIsNone(re.search(pattern, "got 590"))  # an echo it cannot see
        self.assertIsNotNone(re.search(pattern, "got 59"))

    def test_error_messages_do_not_depend_on_the_value(self):
        """#229 nit: invalid values of the same kind must produce the SAME
        error text. Any echo -- even one embedded in a longer number -- makes
        two such messages differ."""
        def message(key, value):
            got = run_gateway_config({"GATEWAY_DEV_MODE": "true", key: value})
            self.assertIn("error", got, "{}={} accepted".format(key, value))
            return got["error"]
        for key, (minimum, maximum) in RANGES.items():
            above = {message(key, str(maximum + 1)), message(key, SENTINEL + "0"),
                     message(key, str(maximum + 7)), message(key, " " + SENTINEL + "0 ")}
            if minimum > 0:
                above |= {message(key, str(minimum - 1))}
            with self.subTest(key=key, kind="out of range"):
                self.assertEqual(len(above), 1, above)
            negative = {message(key, "-1"), message(key, "-" + SENTINEL),
                        message(key, "-" + SENTINEL + "9")}
            with self.subTest(key=key, kind="negative"):
                self.assertEqual(len(negative), 1, negative)
            garbage = {message(key, "x" + SENTINEL), message(key, "y" + SENTINEL + "1"),
                       message(key, SENTINEL + ".5")}
            with self.subTest(key=key, kind="not an integer"):
                self.assertEqual(len(garbage), 1, garbage)
        for key in BOOLS:
            texts = {message(key, v) for v in ("maybe-" + SENTINEL, SENTINEL, "-" + SENTINEL, "2")}
            with self.subTest(key=key, kind="boolean"):
                self.assertEqual(len(texts), 1, texts)

    def test_gateway_src_rejects_invalid_values_without_echoing_them(self):
        """Every invalid value is refused at startup and never reflected in
        the error: booleans, non-integers and both sides of every range, with
        distinctive sentinel numbers wherever the range allows one."""
        bad = []
        for key in BOOLS:
            bad += [(key, "maybe-" + SENTINEL), (key, SENTINEL), (key, "-" + SENTINEL)]
        for key, (minimum, maximum) in RANGES.items():
            bad += [
                (key, str(minimum - 1)),            # just below
                (key, str(maximum + 1)),            # just above
                (key, SENTINEL + "0"),              # far above (sentinel)
                (key, "-" + SENTINEL),              # negative (sentinel)
                (key, "x" + SENTINEL),              # not a number (sentinel)
                (key, SENTINEL + ".5"),             # not an integer (sentinel)
                (key, "1e" + SENTINEL),             # float syntax (sentinel)
                (key, " " + SENTINEL + "0 "),       # padded, still out of range
            ]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                self.assert_rejected_without_echo(key, value)

    def test_gateway_src_accepts_both_range_edges(self):
        for side in (0, 1):
            env = {key: str(edges[side]) for key, edges in RANGES.items()}
            with self.subTest(side="min" if side == 0 else "max", env=env):
                got = run_gateway_config(dict(env, GATEWAY_DEV_MODE="true"))
                self.assertNotIn("error", got)
                for key, value in env.items():
                    self.assertEqual(got[EXPECTED[key][1]], int(value), key)

    def test_group_max_responders_bounds(self):
        """GATEWAY_GROUP_MAX_RESPONDERS: 1 and 20 start, 0 and 21 refuse to
        start, and the error does not echo the value."""
        key = "GATEWAY_GROUP_MAX_RESPONDERS"
        for value in ("1", "20"):
            got = run_gateway_config({"GATEWAY_DEV_MODE": "true", key: value})
            self.assertEqual(got.get("group_max_responders"), int(value), value)
        for value in ("0", "21", "-" + SENTINEL, SENTINEL, "x" + SENTINEL):
            with self.subTest(value=value):
                self.assert_rejected_without_echo(key, value)

    def test_proactive_requires_a_persistent_hmac_secret(self):
        """Even in dev mode the gateway refuses GATEWAY_PROACTIVE_ENABLED=true
        without an explicit GATEWAY_HMAC_SECRET (documented prerequisite)."""
        got = run_gateway_config({"GATEWAY_DEV_MODE": "true",
                                  "GATEWAY_PROACTIVE_ENABLED": "true"})
        self.assertIn("error", got)
        self.assertIn("GATEWAY_HMAC_SECRET", got["error"])

    def test_groups_require_a_persistent_hmac_secret(self):
        """config.py: GATEWAY_GROUPS_ENABLED=true with a memory DB and no
        explicit GATEWAY_HMAC_SECRET refuses to start, even in dev mode (the
        accepted-group state depends on that secret). Without a memory DB
        groups stay off (WARNING) and the gateway starts. With an explicit
        throwaway test secret it starts."""
        base = {"GATEWAY_DEV_MODE": "true", "GATEWAY_GROUPS_ENABLED": "true"}
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "memory.db")
            got = run_gateway_config(dict(base, GATEWAY_MEMORY_DB_PATH=db))
            self.assertIn("error", got)
            self.assertIn("GATEWAY_GROUPS_ENABLED requires an explicit persistent "
                          "GATEWAY_HMAC_SECRET", got["error"])
            for placeholder in ("", "<change-me>"):
                with self.subTest(placeholder=placeholder):
                    got = run_gateway_config(dict(base, GATEWAY_MEMORY_DB_PATH=db,
                                                  GATEWAY_HMAC_SECRET=placeholder))
                    self.assertIn("error", got)
            # Test-only value generated here, never a deployment secret.
            got = run_gateway_config(dict(base, GATEWAY_MEMORY_DB_PATH=db,
                                          GATEWAY_HMAC_SECRET="ab" * 32))
            self.assertNotIn("error", got)
            self.assertIs(got["groups_enabled"], True)
        got = run_gateway_config(base)
        self.assertNotIn("error", got)

    def test_production_missing_hmac_is_stopped_by_the_global_rule(self):
        """#229 nit (docs): the "no memory DB -> WARNING, groups off" case only
        applies with a real HMAC secret or in dev mode. In production (no dev
        mode) a missing secret is refused by the global rule first; with a
        memory DB the groups-specific check runs before it (also refused)."""
        prod = {"GATEWAY_AUTH_TOKEN": "test-only-auth-" + "t" * 40,
                "GATEWAY_GROUPS_ENABLED": "true"}
        got = run_gateway_config(prod)
        self.assertIn("error", got)
        self.assertIn("GATEWAY_HMAC_SECRET is unset", got["error"])
        with tempfile.TemporaryDirectory() as tmp:
            got = run_gateway_config(dict(prod, GATEWAY_MEMORY_DB_PATH=str(Path(tmp) / "m.db")))
            self.assertIn("error", got)
            self.assertIn("GATEWAY_GROUPS_ENABLED requires an explicit persistent", got["error"])
        # Test-only value generated here, never a deployment secret.
        got = run_gateway_config(dict(prod, GATEWAY_HMAC_SECRET="cd" * 32))
        self.assertNotIn("error", got)

    def test_forget_stale_keys_is_a_plain_startup_flag(self):
        """GATEWAY_GROUP_FORGET_STALE_KEYS parses as a strict boolean on its
        own (no other switch needed to start) -- the cleanup it triggers runs
        at startup only when a memory DB is configured (documented)."""
        for value, expected in (("true", True), ("false", False), ("", False)):
            got = run_gateway_config({"GATEWAY_DEV_MODE": "true",
                                      "GATEWAY_GROUP_FORGET_STALE_KEYS": value})
            self.assertIs(got.get("group_forget_stale_keys"), expected, value)

    def test_init_sh_generates_none_of_them(self):
        init = (REPO_ROOT / "server" / "scripts" / "init.sh").read_text(encoding="utf-8")
        for key in EXPECTED:
            self.assertNotIn(key, init, key)
        self.assertNotIn("GATEWAY_MOOD_", init)
        self.assertNotIn("GATEWAY_PROACTIVE_", init)
        self.assertNotIn("GATEWAY_GROUPS_", init)
        self.assertNotIn("GATEWAY_INVITE_", init)

    def test_ci_allowlist(self):
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for key, (default, _, _) in EXPECTED.items():
            self.assertEqual(ci.count('"{}": "{}",'.format(key, default)), 1, key)

    def test_docs_cover_names_defaults_and_prerequisites(self):
        doc = (REPO_ROOT / "docs" / "deploy" / "appservice.md").read_text(encoding="utf-8")
        readme = (REPO_ROOT / "server" / "README.md").read_text(encoding="utf-8")
        for key, (default, _, _) in EXPECTED.items():
            self.assertIn("| `{0}` | `${{{0}:-{1}}}` | `{1}` |".format(key, default), doc, key)
            self.assertIn("`{}`".format(key), readme, key)
        self.assertIn("**开启 `GATEWAY_PROACTIVE_ENABLED` 的前提**", doc)
        self.assertIn("**先启用记忆库**：`GATEWAY_MEMORY_DB_PATH=", doc)
        self.assertIn("**持久的 `GATEWAY_HMAC_SECRET`**", doc)
        self.assertIn("开启前必须先启用记忆库（`GATEWAY_MEMORY_DB_PATH`），并有持久的 `GATEWAY_HMAC_SECRET`", doc)
        self.assertIn("前必须先启用记忆库（`GATEWAY_MEMORY_DB_PATH`），并且要有持久的\n`GATEWAY_HMAC_SECRET`", readme)
        for path in ENV_EXAMPLES[:2]:
            text = path.read_text(encoding="utf-8")
            self.assertIn("GATEWAY_MEMORY_DB_PATH", text.split("GATEWAY_PROACTIVE_ENABLED", 1)[1][:400])
        # Group invites: persistent HMAC secret, never emptied / regenerated,
        # rotation invalidates accepted groups -- prominent in every doc.
        z423 = (REPO_ROOT / "docs" / "deploy" / "z423-synapse.md").read_text(encoding="utf-8")
        self.assertIn("> ⚠️ **开启 `GATEWAY_GROUPS_ENABLED` 必须配置持久的 HMAC 密钥。**", doc)
        self.assertIn("**轮换这个密钥会让已接受的群失效**", doc)
        self.assertIn("**轮换会让已接受的群失效**", doc)  # table row
        self.assertIn("⚠️ **开启群功能必须有持久的 HMAC 密钥**", readme)
        self.assertIn("轮换它会让已接受的群失效", readme)
        self.assertIn("⚠️ **开启群功能必须有持久的 HMAC 密钥**", z423)
        self.assertIn("**轮换会让已接受的群失效**", z423)
        for text in (doc, readme, z423):
            self.assertIn("BOTVILLE_GATEWAY_HMAC_SECRET", text)
            self.assertIn("不能清空", text)
            self.assertIn("重新生成", text)
        for path in ENV_EXAMPLES[:2]:
            text = path.read_text(encoding="utf-8")
            block = text.split("GATEWAY_GROUPS_ENABLED", 1)[1][:600]
            self.assertRegex(block, r"invalidates every accepted group|会让已接受的群全部失效")
            self.assertRegex(text, r"three \.env\.example|三份 \.env\.example")
        self.assertNotIn("两份 .env.example", doc + readme + z423)
        # B2.8c: one-off cleanup switch (what it deletes, set back to false)
        # and counters reset on restart -- in every doc.
        for text in (doc, readme, z423):
            self.assertIn("GATEWAY_GROUP_FORGET_STALE_KEYS", text)
            self.assertIn("重启后清零", text)
            self.assertRegex(text, r"一次性")
            self.assertRegex(text, r"永久删除旧 key_id 的群台账")
            self.assertRegex(text, r"改回 `?false`?")
        self.assertIn("⚠️ **`GATEWAY_GROUP_FORGET_STALE_KEYS` 是轮换密钥后的一次性清理开关**", doc)
        self.assertIn("⚠️ **节流计数只在网关进程内存里，重启后清零**", doc)
        self.assertIn("**生产环境（非 dev）全局缺 HMAC 时，会先被\n  全局规则拦下**", doc)
        for path in ENV_EXAMPLES[:2]:
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r"reset on gateway restart|网关重启后清零")
            self.assertRegex(text, r"set it back to false|改回 false")

    def test_no_hmac_secret_value_written_anywhere(self):
        """The HMAC secret is only ever a placeholder / empty in tracked text."""
        hexval = re.compile(r"HMAC_SECRET\s*[=:]\s*['\"]?[0-9a-fA-F]{16,}")
        paths = [REPO_ROOT / "docs" / "deploy" / "appservice.md",
                 REPO_ROOT / "docs" / "deploy" / "z423-synapse.md",
                 REPO_ROOT / "server" / "README.md", COMPOSE] + list(ENV_EXAMPLES[:2])
        for path in paths:
            self.assertIsNone(hexval.search(path.read_text(encoding="utf-8")), str(path))


def compose_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"], stdin=subprocess.DEVNULL,
                          capture_output=True).returncode == 0


@unittest.skipUnless(compose_available(), "docker compose v2 not available")
class MoodProactiveEnvRenderTest(unittest.TestCase):
    def gateway_env(self, extra_env=""):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            env_file.write_text(REQUIRED_ENV + extra_env, encoding="utf-8")
            proc = subprocess.run(
                ["docker", "compose", "-p", "botville-test", "-f", str(COMPOSE),
                 "--env-file", str(env_file), "--profile", "gateway",
                 "config", "--format", "json"],
                cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                # Never proc.stdout: rendered config holds interpolated values.
                self.fail(proc.stderr or "compose failed (exit {})".format(proc.returncode))
            return json.loads(proc.stdout)["services"]["gateway"]["environment"]

    def test_defaults(self):
        env = self.gateway_env()
        for key, (default, _, _) in EXPECTED.items():
            self.assertEqual(env.get(key), default, key)

    def test_values_pass_through(self):
        values = {
            "GATEWAY_MOOD_ENABLED": "true",
            "GATEWAY_MOOD_HALF_LIFE_MINUTES": "60",
            "GATEWAY_PROACTIVE_ENABLED": "true",
            "GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED": "true",
            "GATEWAY_PROACTIVE_TICK_SECONDS": "45",
            "GATEWAY_PROACTIVE_MIN_IDLE_MINUTES": "0",
            "GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES": "30",
            "GATEWAY_PROACTIVE_PER_TARGET_PER_DAY": "3",
            "GATEWAY_GROUP_MAX_RESPONDERS": "7",
            "GATEWAY_GROUPS_ENABLED": "true",
            "GATEWAY_GROUP_MAX_ROOMS_PER_PERSONA": "150",
            "GATEWAY_INVITE_LEDGER_TTL_SECONDS": "3600",
            "GATEWAY_GROUP_FORGET_STALE_KEYS": "true",
            "GATEWAY_GROUP_COOLDOWN_SECONDS": "0",
            "GATEWAY_GROUP_MAX_REPLIES_PER_HOUR": "150",
            "GATEWAY_GROUP_AI_LOOP_MAX_PER_HOUR": "999",
            "GATEWAY_GROUP_MAX_CONSECUTIVE_AI_MESSAGES": "50",
        }
        self.assertEqual(set(values), set(EXPECTED))
        env = self.gateway_env("".join("{}={}\n".format(k, v) for k, v in values.items()))
        for key, value in values.items():
            self.assertEqual(env.get(key), value, key)


if __name__ == "__main__":
    unittest.main()

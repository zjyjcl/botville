#!/usr/bin/env python3
"""Unit tests for server/scripts/render_push_denylist.py (A1.2, review B1).

TUWUNEL_/CONTINUWUITY_IP_RANGE_DENYLIST replace the homeserver default
denylist entirely (upstream does not append to an explicit list), so the
rendered list must keep every tuwunel v1.9.4 upstream default entry —
including 0.0.0.0/8 and ::/128, which an earlier revision of the generator
dropped and thereby re-allowed — except the sygnal container's allowed /32.
These tests pin the list down, assert every other private address stays
denied, cover the .env validation errors, and run the real init.sh once
end to end. No Docker; standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import importlib.util
import ipaddress
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
RENDERER = SERVER_DIR / "scripts" / "render_push_denylist.py"
INIT = SERVER_DIR / "scripts" / "init.sh"
COMPOSE_DIR = SERVER_DIR / "compose"

SUBNET = "172.31.250.0/24"
POOL = "172.31.250.64/26"
SYGNAL_IP = "172.31.250.2"


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_push_denylist", RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_denylist(text, key="TUWUNEL_IP_RANGE_DENYLIST"):
    """The rendered KEY=[...] value back into a list of CIDR strings."""
    for line in text.splitlines():
        if line.startswith(key + "="):
            value = line.partition("=")[2]
            assert value.startswith("[") and value.endswith("]"), value
            inner = value[1:-1].strip()
            return [part.strip().strip('"') for part in inner.split(",")] if inner else []
    raise AssertionError("{}= not found in rendered output:\n{}".format(key, text))


def denied(cidrs, address):
    """True when `address` falls inside at least one of `cidrs`."""
    ip = ipaddress.ip_address(address)
    return any(ip in ipaddress.ip_network(c) for c in cidrs)


class DenylistRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_renderer()

    def test_no_allow_keeps_exact_upstream_default(self):
        # Empty BOTVILLE_SYGNAL_IP: nothing carved out, set-equal to the
        # tuwunel v1.9.4 default — in particular 0.0.0.0/8 and ::/128 (B1).
        rendered = self.module.render_denylist(None)
        self.assertEqual(set(rendered), set(self.module.UPSTREAM_DEFAULT_DENYLIST))
        self.assertEqual(len(rendered), len(self.module.UPSTREAM_DEFAULT_DENYLIST))
        for cidr in ("0.0.0.0/8", "::/128"):
            self.assertIn(cidr, rendered)

    def test_carve_out_keeps_all_other_defaults(self):
        # With the sygnal /32 allowed, every upstream default that does not
        # contain it must survive verbatim; only 172.16.0.0/12 is replaced
        # by its address_exclude() pieces.
        rendered = self.module.render_denylist(ipaddress.ip_address(SYGNAL_IP))
        for cidr in self.module.UPSTREAM_DEFAULT_DENYLIST:
            if cidr == "172.16.0.0/12":
                self.assertNotIn(cidr, rendered)
            else:
                self.assertIn(cidr, rendered)
        self.assertIn("0.0.0.0/8", rendered)
        self.assertIn("::/128", rendered)

    def test_allowed_ip_not_in_any_denied_range(self):
        rendered = self.module.render_denylist(ipaddress.ip_address(SYGNAL_IP))
        self.assertFalse(denied(rendered, SYGNAL_IP))

    def test_other_private_addresses_stay_denied(self):
        # The gateway (.1), neighbours in the same /24, other RFC1918 space,
        # loopback, "this network" and the IPv6 unspecified address must all
        # remain denied — only the sygnal /32 is poked out.
        rendered = self.module.render_denylist(ipaddress.ip_address(SYGNAL_IP))
        for address in (
            "10.1.2.3",
            "172.31.250.1",
            "172.31.250.3",
            "172.31.251.4",
            "192.168.1.1",
            "127.0.0.1",
            "0.0.0.0",
            "169.254.1.1",
            "::",
            "::1",
            "fe80::1",
        ):
            self.assertTrue(denied(rendered, address), address)

    def test_file_text_renders_both_homeserver_keys_identically(self):
        text = self.module.render_file_text(
            ipaddress.ip_network(SUBNET),
            ipaddress.ip_network(POOL),
            ipaddress.ip_address(SYGNAL_IP),
        )
        self.assertEqual(
            parse_denylist(text, "TUWUNEL_IP_RANGE_DENYLIST"),
            parse_denylist(text, "CONTINUWUITY_IP_RANGE_DENYLIST"),
        )
        self.assertIn(SYGNAL_IP, text)

    def test_file_text_without_allow_marks_it(self):
        text = self.module.render_file_text(
            ipaddress.ip_network(SUBNET), ipaddress.ip_network(POOL), None
        )
        self.assertIn("no private range is allow-listed", text)
        self.assertTrue(denied(parse_denylist(text), SYGNAL_IP))


class ValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_renderer()

    def test_valid_subnet_accepted(self):
        for value in ("172.31.250.0/24", "10.9.0.0/24", "192.168.77.0/24", "172.31.250.0/25"):
            self.assertEqual(str(self.module.parse_subnet(value)), value)

    def test_subnet_rejects_host_bits(self):
        with self.assertRaises(self.module.ConfigError) as ctx:
            self.module.parse_subnet("172.31.250.1/24")
        self.assertIn("172.31.250.0/24", str(ctx.exception))

    def test_subnet_rejects_non_rfc1918(self):
        for value in ("8.8.8.0/24", "100.64.0.0/10", "203.0.113.0/24"):
            with self.assertRaises(self.module.ConfigError):
                self.module.parse_subnet(value)

    def test_subnet_rejects_prefix_shorter_than_24(self):
        for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
            with self.assertRaises(self.module.ConfigError) as ctx:
                self.module.parse_subnet(value)
            self.assertIn("/24", str(ctx.exception))

    def test_subnet_rejects_ipv6_and_garbage(self):
        for value in ("fd00::/8", "not-a-subnet", "172.31.250.0"):
            with self.assertRaises(self.module.ConfigError):
                self.module.parse_subnet(value)

    def test_ip_range_defaults_for_default_subnet(self):
        subnet = ipaddress.ip_network(SUBNET)
        self.assertEqual(
            str(self.module.parse_ip_range("", subnet)), self.module.DEFAULT_IP_RANGE
        )
        self.assertEqual(
            str(self.module.parse_ip_range(POOL, subnet)), POOL
        )

    def test_ip_range_unset_for_custom_subnet_is_an_error(self):
        subnet = ipaddress.ip_network("172.31.251.0/24")
        with self.assertRaises(self.module.ConfigError) as ctx:
            self.module.parse_ip_range("", subnet)
        self.assertIn("BOTVILLE_DOCKER_IP_RANGE", str(ctx.exception))
        self.assertIn("172.31.251.64/26", str(ctx.exception))  # suggested pool

    def test_ip_range_rejects_outside_subnet_and_host_bits(self):
        subnet = ipaddress.ip_network(SUBNET)
        for value in ("172.31.251.64/26", "10.9.0.64/26", "172.31.250.64/25"):
            with self.assertRaises(self.module.ConfigError):
                self.module.parse_ip_range(value, subnet)

    def test_sygnal_ip_valid_hosts_accepted(self):
        subnet = ipaddress.ip_network(SUBNET)
        pool = ipaddress.ip_network(POOL)
        # Hosts outside the dynamic pool and clear of .0/.1/.255.
        for value in ("172.31.250.2", "172.31.250.200"):
            self.assertEqual(
                str(self.module.parse_sygnal_ip(value, subnet, pool)), value
            )

    def test_sygnal_ip_empty_means_no_allow(self):
        subnet = ipaddress.ip_network(SUBNET)
        self.assertIsNone(
            self.module.parse_sygnal_ip("", subnet, ipaddress.ip_network(POOL))
        )

    def test_sygnal_ip_rejects_outside_subnet(self):
        subnet = ipaddress.ip_network(SUBNET)
        with self.assertRaises(self.module.ConfigError):
            self.module.parse_sygnal_ip(
                "172.31.251.2", subnet, ipaddress.ip_network(POOL)
            )

    def test_sygnal_ip_rejects_network_gateway_broadcast(self):
        subnet = ipaddress.ip_network(SUBNET)
        for value, fragment in (
            ("172.31.250.0", "network"),
            ("172.31.250.1", "gateway"),
            ("172.31.250.255", "broadcast"),
        ):
            with self.assertRaises(self.module.ConfigError) as ctx:
                self.module.parse_sygnal_ip(value, subnet, ipaddress.ip_network(POOL))
            self.assertIn(fragment, str(ctx.exception))

    def test_sygnal_ip_rejects_dynamic_pool_addresses(self):
        # .64-.127 is the dynamic pool; a fixed sygnal address in there
        # would race docker's sequential allocation.
        subnet = ipaddress.ip_network(SUBNET)
        for value in ("172.31.250.64", "172.31.250.100", "172.31.250.127"):
            with self.assertRaises(self.module.ConfigError) as ctx:
                self.module.parse_sygnal_ip(value, subnet, ipaddress.ip_network(POOL))
            self.assertIn("dynamic address pool", str(ctx.exception))

    def test_sygnal_ip_rejects_ipv6_and_garbage(self):
        subnet = ipaddress.ip_network(SUBNET)
        for value in ("::1", "not-an-ip"):
            with self.assertRaises(self.module.ConfigError):
                self.module.parse_sygnal_ip(value, subnet, ipaddress.ip_network(POOL))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botville-denylist-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_renderer(self, env_text):
        env = self.tmp / ".env"
        env.write_text(env_text, encoding="utf-8")
        out = self.tmp / "push-denylist.env"
        proc = subprocess.run(
            [sys.executable, str(RENDERER), str(env), str(out)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc, out

    def test_cli_renders_carve_out(self):
        proc, out = self.run_renderer(
            "BOTVILLE_DOCKER_SUBNET={}\nBOTVILLE_SYGNAL_IP={}\n".format(SUBNET, SYGNAL_IP)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = out.read_text(encoding="utf-8")
        cidrs = parse_denylist(text)
        self.assertIn("0.0.0.0/8", cidrs)
        self.assertIn("::/128", cidrs)
        self.assertFalse(denied(cidrs, SYGNAL_IP))

    def test_cli_invalid_value_fails_friendly_without_traceback(self):
        proc, out = self.run_renderer(
            "BOTVILLE_DOCKER_SUBNET=10.0.0.0/8\nBOTVILLE_SYGNAL_IP=10.0.0.2\n"
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("BOTVILLE_DOCKER_SUBNET=10.0.0.0/8", proc.stderr)
        self.assertIn("/24", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(out.exists())


# --- init.sh end to end ------------------------------------------------------

BASE_ENV = (
    # Every key the real synapse/sygnal templates substitute, so init.sh can
    # run against the shipped templates (all values are fakes).
    "BOTVILLE_HOMESERVER=synapse\n"
    "BOTVILLE_SERVER_NAME=localhost\n"
    "BOTVILLE_HTTP_PORT=8008\n"
    "BOTVILLE_OPEN_REGISTRATION=false\n"
    "BOTVILLE_REGISTRATION_TOKEN=fixture-token\n"
    "BOTVILLE_PUSH_PORT=8019\n"
    "POSTGRES_USER=botville\n"
    "POSTGRES_PASSWORD=fixture\n"
    "POSTGRES_DB=synapse\n"
    "SYNAPSE_MACAROON_SECRET=fixture\n"
    "SYNAPSE_FORM_SECRET=fixture\n"
    "SYNAPSE_REGISTRATION_SHARED_SECRET=fixture\n"
)


class InitScriptDenylistTest(unittest.TestCase):
    """The real init.sh rendering denylist + whitelist part + sygnal apps."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botville-denylist-init-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "scripts").mkdir()
        shutil.copy(INIT, self.tmp / "scripts" / "init.sh")
        shutil.copy(RENDERER, self.tmp / "scripts" / "render_push_denylist.py")
        compose = self.tmp / "compose"
        (compose / "synapse").mkdir(parents=True)
        (compose / "sygnal").mkdir(parents=True)
        # The real templates: these tests double as a guard that the shipped
        # homeserver.yaml.tmpl / ip-range-whitelist.part keep rendering cleanly.
        shutil.copy(
            COMPOSE_DIR / "synapse" / "homeserver.yaml.tmpl",
            compose / "synapse" / "homeserver.yaml.tmpl",
        )
        shutil.copy(
            COMPOSE_DIR / "synapse" / "ip-range-whitelist.part",
            compose / "synapse" / "ip-range-whitelist.part",
        )
        shutil.copytree(COMPOSE_DIR / "sygnal", compose / "sygnal", dirs_exist_ok=True)
        self.env_file = compose / ".env"

    def write_env(self, extra):
        text = BASE_ENV + extra
        self.env_file.write_text(text, encoding="utf-8")
        (self.tmp / "compose" / ".env.example").write_text(text, encoding="utf-8")

    def run_init(self):
        return subprocess.run(
            ["bash", str(self.tmp / "scripts" / "init.sh")],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def generated(self, name):
        return (self.tmp / "compose" / "generated" / name).read_text(encoding="utf-8")

    def test_denylist_and_whitelist_rendered_with_sygnal_ip(self):
        self.write_env(
            "BOTVILLE_DOCKER_SUBNET={}\nBOTVILLE_SYGNAL_IP={}\n".format(SUBNET, SYGNAL_IP)
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        cidrs = parse_denylist(self.generated("push-denylist.env"))
        self.assertIn("0.0.0.0/8", cidrs)
        self.assertIn("::/128", cidrs)
        self.assertFalse(denied(cidrs, SYGNAL_IP))
        self.assertTrue(denied(cidrs, "172.31.250.1"))
        homeserver = self.generated("synapse/homeserver.yaml")
        self.assertIn("ip_range_whitelist:", homeserver)
        self.assertIn('"{}/32"'.format(SYGNAL_IP), homeserver)
        # mock pushkin stays configured by default (BOTVILLE_PUSH_MOCK unset)
        self.assertIn('"org.botville.mock":', self.generated("sygnal/sygnal.yaml"))

    def test_empty_sygnal_ip_disables_allow_entirely(self):
        self.write_env("BOTVILLE_DOCKER_SUBNET={}\nBOTVILLE_SYGNAL_IP=\n".format(SUBNET))
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = self.generated("push-denylist.env")
        self.assertEqual(
            set(parse_denylist(text)),
            set(load_renderer().UPSTREAM_DEFAULT_DENYLIST),
        )
        # The template's explanatory comment mentions the key without a
        # colon; the appended block is the only thing that renders one.
        self.assertNotIn("ip_range_whitelist:", self.generated("synapse/homeserver.yaml"))

    def test_invalid_subnet_aborts_init_friendly(self):
        self.write_env(
            "BOTVILLE_DOCKER_SUBNET=10.0.0.0/8\nBOTVILLE_SYGNAL_IP=10.0.0.2\n"
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("BOTVILLE_DOCKER_SUBNET=10.0.0.0/8", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_custom_subnet_without_ip_range_aborts_init_friendly(self):
        # e.g. the botville-zcode multi-instance layout: the appended
        # default pool (172.31.250.64/26) does not fit the custom subnet and
        # the error names the value to set.
        self.write_env(
            "BOTVILLE_DOCKER_SUBNET=172.31.251.0/24\n"
            "BOTVILLE_DOCKER_IP_RANGE=172.31.250.64/26\n"
            "BOTVILLE_SYGNAL_IP=172.31.251.2\n"
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("BOTVILLE_DOCKER_IP_RANGE=172.31.250.64/26", proc.stderr)
        self.assertIn("172.31.251.64/26", proc.stderr)  # suggested fix
        self.assertNotIn("Traceback", proc.stderr)

    def test_gateway_sygnal_ip_aborts_init_friendly(self):
        self.write_env(
            "BOTVILLE_DOCKER_SUBNET={}\nBOTVILLE_SYGNAL_IP=172.31.250.1\n".format(SUBNET)
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("gateway", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Live access-log regression for the optional HTTPS edge (A-R8).

The official Caddyfile must not write URI query strings, Authorization
headers, or Cookie headers to stdout. This is a behavior test against
the pinned Caddy image and the in-repo Caddyfile; asserting Caddyfile
literals alone would not have caught Caddy 2.11.9 treating
`request>uri query delete` as a no-op.

Local or mock TLS here is not public HTTPS and not a phone-scan test.
"""

import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = ROOT / "server" / "compose" / "pair-redeem-edge" / "Caddyfile"
ENV_EXAMPLE = ROOT / "server" / "compose" / ".env.example"

QUERY_SENTINEL = "UNITLOGQUERYSECRET"
AUTH_SENTINEL = "UNITLOGAUTHSECRET"
COOKIE_SENTINEL = "UNITLOGCOOKIESECRET"
PROJECT = "botville-edge-logtest"


def _image_value(key):
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("missing %s in server/compose/.env.example" % key)


def docker_ok():
    if not shutil.which("docker"):
        return "docker not available"
    probe = subprocess.run(
        ["docker", "compose", "version"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    if probe.returncode:
        return "docker compose v2 not available"
    return None


def image_present(ref):
    probe = subprocess.run(
        ["docker", "image", "inspect", ref],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    return probe.returncode == 0


def free_loopback_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run(cmd, **kwargs):
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **kwargs)


class PairRedeemEdgeAccessLogTests(unittest.TestCase):
    def test_access_log_omits_query_and_secrets(self):
        reason = docker_ok()
        if reason:
            self.skipTest(reason)
        caddy_image = _image_value("CADDY_IMAGE")
        python_image = _image_value("PAIRING_IMAGE")
        if not image_present(caddy_image):
            self.skipTest("pinned Caddy image is not present locally")
        if not image_present(python_image):
            self.skipTest("pinned python image is not present locally")

        # Temp files must live under /Users so Colima virtiofs can bind-mount
        # them. Host /tmp and mkdtemp() are not visible inside the VM.
        tmp = ROOT / "server" / "compose" / "generated" / "edge-logtest"
        if tmp.exists():
            shutil.rmtree(tmp)
        cert_dir = tmp / "certs"
        cert_dir.mkdir(parents=True, mode=0o700)
        cert = cert_dir / "cert.pem"
        key = cert_dir / "key.pem"
        openssl = run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
            "-days", "1", "-nodes", "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=localhost",
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if openssl.returncode:
            shutil.rmtree(tmp, ignore_errors=True)
            self.skipTest("openssl could not mint a throwaway mock cert")
        os.chmod(key, 0o600)

        port = free_loopback_port()
        backend = PROJECT + "-backend"
        edge = PROJECT + "-edge"
        network = PROJECT + "-net"
        started = []

        def cleanup():
            for name in (edge, backend):
                run(["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run(["docker", "network", "rm", network],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.rmtree(tmp, ignore_errors=True)

        try:
            net = run(["docker", "network", "create", network],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(net.returncode, 0, "could not create test network")
            started.append("net")

            backend_cmd = (
                "import http.server\n"
                "class H(http.server.BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        self.end_headers()\n"
                "        self.wfile.write(b'ok')\n"
                "    def do_POST(self):\n"
                "        self.send_response(200)\n"
                "        self.end_headers()\n"
                "        self.wfile.write(b'ok')\n"
                "    def log_message(self, *args):\n"
                "        pass\n"
                "http.server.HTTPServer(('0.0.0.0', 8092), H).serve_forever()\n"
            )
            back = run([
                "docker", "run", "-d", "--name", backend,
                "--network", network, "--network-alias", "pair-redeem",
                "--pull", "never", python_image,
                "python", "-c", backend_cmd,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(
                back.returncode, 0,
                "backend failed to start (details suppressed)")
            started.append("backend")

            edge_run = run([
                "docker", "run", "-d", "--name", edge,
                "--network", network,
                "--pull", "never",
                "-p", "127.0.0.1:%d:8443" % port,
                "-v", "%s:/etc/caddy/Caddyfile:ro" % CADDYFILE,
                "-v", "%s:/certs:ro" % cert_dir,
                caddy_image,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(
                edge_run.returncode, 0,
                "edge failed to start (details suppressed)")
            started.append("edge")

            ctx = ssl._create_unverified_context()
            url = "https://127.0.0.1:%d/health?code=%s" % (port, QUERY_SENTINEL)
            deadline = time.time() + 20
            last = None
            while time.time() < deadline:
                try:
                    req = urllib.request.Request(url, method="GET", headers={
                        "Authorization": "Bearer %s" % AUTH_SENTINEL,
                        "Cookie": "session=%s" % COOKIE_SENTINEL,
                    })
                    with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
                        if resp.status == 200:
                            break
                        last = "status %s" % resp.status
                except Exception as exc:
                    last = str(exc)
                time.sleep(0.3)
            else:
                logs = run(["docker", "logs", edge],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self.fail("timeout waiting for HTTPS /health (%s)" % last)

            # One more request after the server is up, so the sentinel is
            # definitely in a post-startup access line.
            req = urllib.request.Request(url, method="GET", headers={
                "Authorization": "Bearer %s" % AUTH_SENTINEL,
                "Cookie": "session=%s" % COOKIE_SENTINEL,
            })
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                self.assertEqual(resp.status, 200)

            logs = run(["docker", "logs", edge],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            blob = logs.stdout.decode("utf-8", "replace")
            self.assertNotIn(QUERY_SENTINEL, blob)
            self.assertNotIn(AUTH_SENTINEL, blob)
            self.assertNotIn(COOKIE_SENTINEL, blob)
            self.assertIn("/health", blob)
            self.assertNotIn("?" + "code=", blob)
        finally:
            cleanup()


if __name__ == "__main__":
    unittest.main()

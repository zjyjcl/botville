#!/usr/bin/env python3
"""Live access-log and error-log regression for the optional HTTPS edge (A-R8).

The official Caddyfile must not write URI query strings, Authorization
headers, or Cookie headers to stdout or to Caddy's default error
logger. This is a behavior test against the pinned Caddy image and
the in-repo Caddyfile; asserting Caddyfile literals alone would not
have caught Caddy 2.11.9 treating `request>uri query delete` as a
no-op, or error logs (`http.log.error`) keeping the raw query when
only the site access `log` was filtered.

The lint CI job does not pull the pinned Caddy or python images, so
`image_present()` skips this test there. Behavioral coverage in CI
is the isolated `pair-redeem-edge-e2e` job. Do not pull those images
in lint just to run this test, and do not weaken the .env.example
allowlist.

Local or mock TLS here is not public HTTPS and not a phone-scan test.
"""

import os
import shutil
import socket
import ssl
import subprocess
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = ROOT / "server" / "compose" / "pair-redeem-edge" / "Caddyfile"
ENV_EXAMPLE = ROOT / "server" / "compose" / ".env.example"

QUERY_SENTINEL = "UNITLOGQUERYSECRET"
AUTH_SENTINEL = "UNITLOGAUTHSECRET"
COOKIE_SENTINEL = "UNITLOGCOOKIESECRET"
# Unique per run so parallel worktrees and repeated rmtree/rebuild
# under Colima virtiofs do not share a container name or cert dir.
_RUN = uuid.uuid4().hex[:12]
PROJECT = "botville-edge-logtest-" + _RUN


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


def fsync_path(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PairRedeemEdgeAccessLogTests(unittest.TestCase):
    def test_access_and_error_logs_omit_query_and_secrets(self):
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
        # A unique directory avoids virtiofs stale views after rmtree of a
        # reused path, and avoids collisions across parallel worktrees.
        tmp = ROOT / "server" / "compose" / "generated" / (
            "edge-logtest-" + _RUN)
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
        for path in (cert, key, cert_dir):
            fsync_path(path)

        port = free_loopback_port()
        backend = PROJECT + "-backend"
        edge = PROJECT + "-edge"
        network = PROJECT + "-net"
        ssl_ctx = ssl._create_unverified_context()
        query_url = "https://127.0.0.1:%d/health?code=%s" % (
            port, QUERY_SENTINEL)
        health_url = "https://127.0.0.1:%d/health" % port
        secret_headers = {
            "Authorization": "Bearer %s" % AUTH_SENTINEL,
            "Cookie": "session=%s" % COOKIE_SENTINEL,
        }

        def cleanup():
            for name in (edge, backend):
                run(["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run(["docker", "network", "rm", network],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.rmtree(tmp, ignore_errors=True)

        def container_line(name):
            insp = run([
                "docker", "inspect", "-f",
                "{{.Name}} status={{.State.Status}} "
                "exit={{.State.ExitCode}} oom={{.State.OOMKilled}}",
                name,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if insp.returncode:
                err = insp.stderr.decode("utf-8", "replace").strip()
                return "%s: inspect failed (%s)" % (name, err or "missing")
            return insp.stdout.decode("utf-8", "replace").strip()

        def container_logs(name, tail=80):
            logs = run(
                ["docker", "logs", "--tail", str(tail), name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            blob = logs.stdout.decode("utf-8", "replace")
            if len(blob) > 8000:
                blob = blob[-8000:]
            return blob

        def diagnose():
            parts = [container_line(edge), container_line(backend)]
            for name in (edge, backend):
                parts.append("----- docker logs %s -----" % name)
                parts.append(container_logs(name))
            return "\n".join(parts)

        def wait_certs_visible(timeout=15):
            deadline = time.time() + timeout
            last = "not probed"
            while time.time() < deadline:
                probe = run([
                    "docker", "run", "--rm", "--pull", "never",
                    "-v", "%s:/certs:ro" % cert_dir,
                    caddy_image,
                    "sh", "-c", "test -s /certs/cert.pem && test -s /certs/key.pem",
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if probe.returncode == 0:
                    return
                err = probe.stderr.decode("utf-8", "replace").strip()
                out = probe.stdout.decode("utf-8", "replace").strip()
                last = err or out or ("exit %s" % probe.returncode)
                time.sleep(0.2)
            self.fail(
                "certs not visible in a bind-mount after %ss (%s)\n%s" % (
                    timeout, last, diagnose()))

        def wait_tls_listener(timeout=20):
            deadline = time.time() + timeout
            last = "not started"
            while time.time() < deadline:
                line = container_line(edge)
                if "status=exited" in line or "status=dead" in line:
                    self.fail(
                        "edge container exited before HTTPS was up (%s)\n%s" % (
                            line, diagnose()))
                if "status=running" in line:
                    sock = None
                    try:
                        sock = socket.create_connection(("127.0.0.1", port), 1)
                        ssock = ssl_ctx.wrap_socket(
                            sock, server_hostname="localhost")
                        ssock.close()
                        sock = None
                        return
                    except Exception as exc:
                        last = str(exc)
                    finally:
                        if sock is not None:
                            try:
                                sock.close()
                            except Exception:
                                pass
                else:
                    last = line
                time.sleep(0.2)
            self.fail(
                "timeout waiting for HTTPS listener (%s)\n%s" % (
                    last, diagnose()))

        def wait_backend_ready(timeout=15):
            deadline = time.time() + timeout
            last = "not probed"
            while time.time() < deadline:
                line = container_line(backend)
                if "status=exited" in line or "status=dead" in line:
                    self.fail(
                        "backend exited before it listened (%s)\n%s" % (
                            line, diagnose()))
                probe = run([
                    "docker", "exec", backend, "python", "-c",
                    "import socket; "
                    "socket.create_connection(('127.0.0.1', 8092), 2).close()",
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if probe.returncode == 0:
                    return
                err = probe.stderr.decode("utf-8", "replace").strip()
                last = err or ("exit %s" % probe.returncode)
                time.sleep(0.2)
            self.fail(
                "timeout waiting for backend :8092 (%s)\n%s" % (
                    last, diagnose()))

        def http_request(url, headers=None, timeout=5):
            req = urllib.request.Request(
                url, method="GET", headers=headers or {})
            try:
                with urllib.request.urlopen(
                        req, timeout=timeout, context=ssl_ctx) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        def wait_health_ok(timeout=15):
            deadline = time.time() + timeout
            last = "not requested"
            while time.time() < deadline:
                try:
                    status, _body = http_request(health_url, timeout=3)
                    if status == 200:
                        return
                    last = "status %s" % status
                except Exception as exc:
                    last = str(exc)
                time.sleep(0.2)
            self.fail(
                "timeout waiting for HTTPS /health 200 (%s)\n%s" % (
                    last, diagnose()))

        def assert_logs_omit_secrets(blob, context):
            self.assertNotIn(QUERY_SENTINEL, blob, context + "\n" + blob)
            self.assertNotIn(AUTH_SENTINEL, blob, context + "\n" + blob)
            self.assertNotIn(COOKIE_SENTINEL, blob, context + "\n" + blob)
            self.assertIn("/health", blob, context + "\n" + blob)
            self.assertNotIn("?" + "code=", blob, context + "\n" + blob)

        try:
            wait_certs_visible()

            net = run(["docker", "network", "create", network],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(net.returncode, 0, "could not create test network")

            # Start the edge first, with no backend, so we can assert the
            # 502 error-log path (global default logger) also strips query.
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
            wait_tls_listener()

            try:
                status, _body = http_request(
                    query_url, headers=secret_headers, timeout=5)
            except Exception as exc:
                self.fail(
                    "502 probe request failed (%s)\n%s" % (exc, diagnose()))
            self.assertEqual(
                status, 502,
                "expected 502 before backend is up, got %s\n%s" % (
                    status, diagnose()))
            logs_502 = run(
                ["docker", "logs", edge],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            blob_502 = logs_502.stdout.decode("utf-8", "replace")
            assert_logs_omit_secrets(
                blob_502,
                "edge docker logs after intentional 502 (error log path)")

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
            wait_backend_ready()
            wait_health_ok()

            try:
                status, _body = http_request(
                    query_url, headers=secret_headers, timeout=5)
            except Exception as exc:
                self.fail(
                    "200 probe request failed (%s)\n%s" % (exc, diagnose()))
            self.assertEqual(
                status, 200,
                "expected 200 after backend is up, got %s\n%s" % (
                    status, diagnose()))
            logs_200 = run(
                ["docker", "logs", edge],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            blob_200 = logs_200.stdout.decode("utf-8", "replace")
            assert_logs_omit_secrets(
                blob_200,
                "edge docker logs after 200 (access log path)")
        finally:
            cleanup()


if __name__ == "__main__":
    unittest.main()

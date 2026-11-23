#!/usr/bin/env python3
"""Isolated optional HTTPS edge acceptance test (A-R8 / #128).

This is an isolated Docker Compose test: Synapse + pair_redeem + a Caddy
HTTPS edge with a throwaway self-signed certificate. It is NOT public
HTTPS, NOT a substitute for a public CA, and NOT a phone camera scan.

Project name: botville-pair-redeem-edge-e2e
Host ports bind 127.0.0.1 only (Synapse :18408, redeem :18492, edge :18443).
Does not read or write server/compose/.env, and does not touch the
default `botville` compose project. Always `down -v` this project and
delete generated certs and the secret env file.

Coverage:
  1. Official compose: default off => pair-redeem-edge absent; enabling
     the profile publishes only 127.0.0.1:8443.
  2. Isolated stack with the edge profile off leaves :18443 closed.
  3. Enabled: GET /health and POST /_botville/pair/redeem over HTTPS
     reach pair_redeem (redeem issues a real Synapse registration_token).
  4. Denied paths return the edge 404 and do not reach Synapse admin,
     federation redeem, gateway, or pair_redeem subpaths.
  5. Edge logs contain no redeem code, registration_token, or admin token.
  6. A request with `?code=` (and Authorization/Cookie) does not put
     those values in edge access / compose logs, including the 502
     error-log path when pair-redeem is stopped.

Run: PYTHONDONTWRITEBYTECODE=1 python3 server/tests/pair_redeem_edge_e2e.py
  or: make pair-redeem-edge-e2e
"""

from __future__ import print_function

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pairing_qr_contract as contract  # noqa: E402
from smoke import Failure, expect  # noqa: E402

PROJECT = "botville-pair-redeem-edge-e2e"
COMPOSE_FILE = os.path.join(ROOT, "server", "compose", "pair-redeem-e2e.yml")
OFFICIAL_COMPOSE = os.path.join(ROOT, "server", "compose", "docker-compose.yml")
CADDYFILE = os.path.join(
    ROOT, "server", "compose", "pair-redeem-edge", "Caddyfile")
GENERATED = os.path.join(
    ROOT, "server", "compose", "generated", "pair-redeem-edge-e2e")
ENV_FILE = os.path.join(
    ROOT, "server", "compose", "generated", "pair-redeem-edge-e2e.env")
HOMESERVER_FILE = os.path.join(GENERATED, "synapse", "homeserver.yaml")
CERT_DIR = os.path.join(GENERATED, "certs")
CERT_FILE = os.path.join(CERT_DIR, "cert.pem")
KEY_FILE = os.path.join(CERT_DIR, "key.pem")
LOG_DUMP = os.path.join(GENERATED, "compose-logs.txt")
TEMPLATE = os.path.join(
    ROOT, "server", "compose", "synapse", "homeserver.yaml.tmpl")
HS_PLACEHOLDER = "https://hs.example.org"
GW_PLACEHOLDER = "https://gw.example.org"
DEVICE = "e2e-edge-device"
SYNAPSE_PORT = 18408
REDEEM_PORT = 18492
EDGE_PORT = 18443
REDEEM_PATH = "/_botville/pair/redeem"
QUERY_SENTINEL = "EDGELOGQUERYSECRET"
AUTH_SENTINEL = "EDGELOGAUTHSECRET"
COOKIE_SENTINEL = "EDGELOGCOOKIESECRET"

# Paths that must not be reachable through the public edge.
DENIED_PATHS = (
    "/",
    "/_botville/pair/redeem/",
    "/_botville/pair/redeem/admin",
    "/_botville/pair/issue",
    "/health/extra",
    "/_synapse/admin/v1/register",
    "/_synapse/admin/v1/registration_tokens/new",
    "/v1/pairing/redeem",
    "/_botville/gateway/v1/health",
    "/_matrix/client/v3/login",
    "/.well-known/matrix/client",
)


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def run(cmd, **kwargs):
    kwargs.setdefault("cwd", ROOT)
    return subprocess.run(cmd, **kwargs)


def compose_cmd(*profiles):
    cmd = [
        "docker", "compose", "-p", PROJECT, "-f", COMPOSE_FILE,
        "--env-file", ENV_FILE,
    ]
    for profile in profiles:
        cmd.extend(["--profile", profile])
    return cmd


def make_code(seed):
    if isinstance(seed, str):
        seed = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:4], "big")
    raw = bytes((seed + i) % 256 for i in range(32))
    return __import__("base64").urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def render_homeserver(env_map, dest):
    text = open(TEMPLATE, encoding="utf-8").read()

    def repl(match):
        key = match.group(1)
        if key not in env_map:
            raise SystemExit("homeserver template missing env key %s" % key)
        return env_map[key]

    text = re.sub(r"\$\{([A-Z0-9_]+)\}", repl, text)
    text = text.replace(
        'app_service_config_files: ["/appservice/botville-gateway.yaml"]',
        "app_service_config_files: []",
    )
    if "registration_requires_token:" not in text:
        text += "\nregistration_requires_token: true\n"
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(dest, 0o600)


def write_env(values):
    os.makedirs(os.path.dirname(ENV_FILE), mode=0o700, exist_ok=True)
    lines = []
    for key, value in values.items():
        lines.append("%s=%s\n" % (key, value))
    with open(ENV_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.chmod(ENV_FILE, 0o600)


def generate_mock_tls():
    """Throwaway self-signed cert for isolated tests only."""
    os.makedirs(CERT_DIR, mode=0o700, exist_ok=True)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
        "-days", "1", "-nodes",
        "-keyout", KEY_FILE, "-out", CERT_FILE,
        "-subj", "/CN=localhost",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise SystemExit(
            "openssl failed to mint mock cert: %s" %
            proc.stderr.decode("utf-8", "replace"))
    os.chmod(KEY_FILE, 0o600)
    os.chmod(CERT_FILE, 0o644)


def wait_http(url, timeout=90, expect_status=None, ssl_context=None):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(
                    req, timeout=5, context=ssl_context) as resp:
                if expect_status is None or resp.status == expect_status:
                    return True
                last = "status %s" % resp.status
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    raise Failure("timeout waiting for %s (%s)" % (url, last))


def port_open(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def mock_ssl_context():
    # Isolated mock TLS only. This context is not a public-CA trust store.
    return ssl._create_unverified_context()


def http_json(method, url, body=None, token=None, timeout=10, ssl_context=None,
              extra_headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {}
    if extra_headers:
        headers.update(extra_headers)
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(
                req, timeout=timeout, context=ssl_context) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeError):
                parsed = {"_raw": raw.decode("utf-8", "replace")}
            return resp.status, parsed, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeError):
            parsed = {"_raw": raw.decode("utf-8", "replace")}
        return exc.code, parsed, dict(exc.headers), raw


def https_edge(method, path, body=None, timeout=10, extra_headers=None):
    url = "https://127.0.0.1:%d%s" % (EDGE_PORT, path)
    return http_json(
        method, url, body=body, timeout=timeout,
        ssl_context=mock_ssl_context(), extra_headers=extra_headers)


def register_admin(synapse, shared_secret, username, password):
    status, body, _, _ = http_json("GET", synapse + "/_synapse/admin/v1/register")
    expect(status == 200 and body.get("nonce"), "admin nonce: %s %r" % (status, body))
    nonce = body["nonce"]
    mac = hmac.new(
        shared_secret.encode("utf-8"),
        ("%s\0%s\0%s\0admin" % (nonce, username, password)).encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    status, body, _, _ = http_json("POST", synapse + "/_synapse/admin/v1/register", {
        "nonce": nonce,
        "username": username,
        "password": password,
        "admin": True,
        "mac": mac,
    })
    expect(status == 200 and body.get("access_token"),
           "admin register failed: %s %r" % (status, body))
    token = body["access_token"]
    expect("\n" not in token and " " not in token, "admin token looks unusable")
    return token


def exec_store(py, stdin_obj=None):
    cmd = compose_cmd("pair-redeem", "pair-redeem-edge") + [
        "exec", "-T", "pair-redeem", "python", "-c", py]
    data = None if stdin_obj is None else json.dumps(stdin_obj).encode("utf-8")
    proc = subprocess.run(
        cmd, cwd=ROOT, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise Failure("pair-redeem exec failed: %s" % proc.stderr.decode("utf-8", "replace"))
    out = proc.stdout.decode("utf-8").strip()
    return json.loads(out) if out else None


def issue_in_store(code, hs, gw=None, ttl=300):
    script = (
        "import json,sys\n"
        "from pair_redeem.store import PairRedeemStore\n"
        "spec=json.load(sys.stdin)\n"
        "store=PairRedeemStore('/data/pair_redeem.db')\n"
        "store.issue(spec['hs'], spec['code'], gw=spec.get('gw'), "
        "ttl_seconds=spec.get('ttl',300))\n"
        "store.close()\n"
        "print('{}')\n"
    )
    exec_store(script, {"hs": hs, "code": code, "gw": gw, "ttl": ttl})


def compose_logs(service=None):
    cmd = compose_cmd("pair-redeem", "pair-redeem-edge") + ["logs", "--no-color"]
    if service:
        cmd.append(service)
    proc = run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return proc.stdout.decode("utf-8", "replace")


_captured_logs = []


def snapshot_logs(label):
    blob = compose_logs()
    _captured_logs.append("===== %s =====\n%s\n" % (label, blob))
    os.makedirs(GENERATED, mode=0o700, exist_ok=True)
    with open(LOG_DUMP, "w", encoding="utf-8") as fh:
        fh.write("".join(_captured_logs))


def assert_no_secrets(blobs, secrets_list, label):
    joined = blobs if isinstance(blobs, str) else "\n".join(blobs)
    for secret in secrets_list:
        if secret and secret in joined:
            raise Failure("%s leaked a secret" % label)


def step(name, fn):
    log("== %s" % name)
    fn()


def official_profile_default_off():
    env = dict(os.environ)
    for key in (
        "BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN",
        "BOTVILLE_PAIR_REDEEM_SYNAPSE_URL",
        "PAIRING_ADMIN_TOKEN",
    ):
        env.pop(key, None)
    base = [
        "docker", "compose", "-f", OFFICIAL_COMPOSE,
        "--env-file", os.path.join(ROOT, "server", "compose", ".env.example"),
    ]
    off = run(
        base + ["--profile", "synapse", "config", "--format", "json"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    expect(off.returncode == 0, "official compose config (default) failed")
    services = json.loads(off.stdout.decode("utf-8"))["services"]
    expect("pair-redeem-edge" not in services, "edge rendered without profile")
    expect("pair-redeem" not in services, "pair-redeem rendered without profile")

    on = run(
        base + [
            "--profile", "synapse", "--profile", "pair-redeem",
            "--profile", "pair-redeem-edge", "config", "--format", "json",
        ],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    expect(on.returncode == 0, "official compose config (edge on) failed")
    services = json.loads(on.stdout.decode("utf-8"))["services"]
    edge = services["pair-redeem-edge"]
    ports = edge.get("ports") or []
    expect(ports, "edge has no published ports")
    expect(all(p.get("host_ip") == "127.0.0.1" for p in ports),
           "edge host bind is not loopback")
    expect(all(int(p.get("published") or 0) == 8443 for p in ports),
           "edge published unexpected host port")
    expect(all(int(p.get("target") or 0) == 8443 for p in ports),
           "edge target port is not 8443")


def main():
    if not shutil.which("docker"):
        raise SystemExit("docker is not on PATH")
    probe = run(["docker", "compose", "version"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        raise SystemExit("docker compose v2 is required")
    if not shutil.which("openssl"):
        raise SystemExit("openssl is required to mint a throwaway mock cert")

    shared = secrets.token_hex(32)
    macaroon = secrets.token_hex(32)
    form = secrets.token_hex(32)
    postgres_pw = secrets.token_hex(16)
    admin_password = secrets.token_hex(12)
    env_map = {
        "BOTVILLE_SERVER_NAME": "hs.example.org",
        "POSTGRES_USER": "botville",
        "POSTGRES_PASSWORD": postgres_pw,
        "POSTGRES_DB": "synapse",
        "SYNAPSE_MACAROON_SECRET": macaroon,
        "SYNAPSE_FORM_SECRET": form,
        "SYNAPSE_REGISTRATION_SHARED_SECRET": shared,
        "BOTVILLE_OPEN_REGISTRATION": "true",
        "BOTVILLE_HTTP_PORT": str(SYNAPSE_PORT),
        "BOTVILLE_PAIR_REDEEM_PORT": str(REDEEM_PORT),
        "BOTVILLE_PAIR_REDEEM_EDGE_PORT": str(EDGE_PORT),
        "BOTVILLE_PAIR_REDEEM_SYNAPSE_URL": "http://synapse:8008",
        "BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN": "",
        "BOTVILLE_PAIR_REDEEM_IP_LIMIT": "10",
        "BOTVILLE_PAIR_REDEEM_GLOBAL_LIMIT": "60",
        "PAIR_REDEEM_E2E_CONFIG": GENERATED,
        "PAIR_REDEEM_EDGE_CADDYFILE": CADDYFILE,
    }
    os.makedirs(GENERATED, mode=0o700, exist_ok=True)
    render_homeserver(env_map, HOMESERVER_FILE)
    generate_mock_tls()
    write_env(env_map)

    synapse = "http://127.0.0.1:%d" % SYNAPSE_PORT
    started = False
    admin_token = ""
    issued_codes = []
    issued_reg_tokens = []
    torn_down = False

    def teardown():
        nonlocal torn_down
        if torn_down:
            return
        torn_down = True
        try:
            if started:
                snapshot_logs("teardown")
        except Exception:
            pass
        run(compose_cmd("pair-redeem", "pair-redeem-edge") + [
            "down", "-v", "--remove-orphans"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for path in (ENV_FILE, HOMESERVER_FILE, CERT_FILE, KEY_FILE):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(CERT_DIR)
        except OSError:
            pass

    def handle_sig(_signum, _frame):
        teardown()
        raise SystemExit(128 + _signum)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        step("official compose default-off and loopback publish",
             official_profile_default_off)

        log("starting isolated stack without edge profile")
        up = run(compose_cmd() + ["up", "-d"], stdout=subprocess.PIPE,
                 stderr=subprocess.STDOUT)
        if up.returncode != 0:
            raise Failure("compose up (no edge) failed: %s" %
                          up.stdout.decode("utf-8", "replace"))
        started = True
        wait_http(synapse + "/health", timeout=180)
        expect(not port_open(EDGE_PORT),
               "edge port open while pair-redeem-edge profile is off")
        expect(not port_open(REDEEM_PORT),
               "redeem port open while pair-redeem profile is off")

        log("registering isolated Synapse admin (not through the edge)")
        admin_token = register_admin(
            synapse, shared, "edgeadmin", admin_password)
        env_map["BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN"] = admin_token
        write_env(env_map)

        log("enabling pair-redeem + pair-redeem-edge")
        up = run(
            compose_cmd("pair-redeem", "pair-redeem-edge") + ["up", "-d"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if up.returncode != 0:
            raise Failure("compose up (edge on) failed: %s" %
                          up.stdout.decode("utf-8", "replace"))
        wait_http("http://127.0.0.1:%d/health" % REDEEM_PORT, timeout=90)
        wait_http(
            "https://127.0.0.1:%d/health" % EDGE_PORT,
            timeout=90, expect_status=200,
            ssl_context=mock_ssl_context())

        def health_over_https():
            status, body, _headers, raw = https_edge("GET", "/health")
            expect(status == 200, "edge /health status %s %r" % (status, raw))
            # pair_redeem /health is a plain OK (not a Synapse admin body).
            text = raw.decode("utf-8", "replace")
            expect("access_token" not in text, "health body looks like a token")

        step("enabled: GET /health over HTTPS", health_over_https)

        def redeem_over_https():
            code = make_code("ok")
            issued_codes.append(code)
            issue_in_store(code, HS_PLACEHOLDER, gw=GW_PLACEHOLDER)
            status, body, _headers, raw = https_edge(
                "POST", REDEEM_PATH,
                {"code": code, "device_display_name": DEVICE})
            expect(status == 200, "edge redeem status %s %r" % (status, raw))
            validated = contract.validate_redeem_response(body, HS_PLACEHOLDER)
            expect(validated["auth"]["type"] == "registration_token", "auth type")
            expect(validated["gateway"] == GW_PLACEHOLDER, "gateway")
            expect(validated["features_url"] == GW_PLACEHOLDER + "/v1/capabilities",
                   "features_url")
            token = validated["auth"]["token"]
            expect(token, "redeem missing token: %r" % body)
            issued_reg_tokens.append(token)
            for key in ("features_url", "gateway"):
                value = validated.get(key)
                if isinstance(value, str) and value.startswith("http:"):
                    raise Failure("redeem returned plaintext http URL for %s" % key)

        step("enabled: POST /_botville/pair/redeem over HTTPS",
             redeem_over_https)

        def denied_paths():
            for path in DENIED_PATHS:
                for method in ("GET", "POST"):
                    payload = {} if method == "POST" else None
                    status, body, headers, raw = https_edge(method, path, body=payload)
                    expect(status == 404,
                           "%s %s expected 404, got %s %r" %
                           (method, path, status, raw[:200]))
                    text = raw.decode("utf-8", "replace")
                    expect(text.strip() == "Not Found",
                           "%s %s was proxied (body %r)" %
                           (method, path, text[:200]))
                    # Synapse admin / client APIs would not use this body.
                    expect("nonce" not in text and "flows" not in text,
                           "%s %s looks like a Synapse response" %
                           (method, path))
                    expect("errcode" not in text,
                           "%s %s looks like a Matrix/JSON error" %
                           (method, path))

        step("denied internal paths stay at the edge 404", denied_paths)

        def assert_edge_logs_omit_sentinels(edge_logs, context):
            for secret, label in (
                (QUERY_SENTINEL, "query"),
                (AUTH_SENTINEL, "authorization"),
                (COOKIE_SENTINEL, "cookie"),
            ):
                if secret in edge_logs:
                    raise Failure(
                        "pair-redeem-edge logs leaked %s (%s)" % (
                            label, context))
            if "/health" not in edge_logs:
                raise Failure(
                    "edge logs dropped the path along with the query (%s)" %
                    context)
            if "?code=" in edge_logs:
                raise Failure("edge logs kept a query string (%s)" % context)

        def access_log_omits_query():
            https_edge(
                "GET", "/health?code=%s" % QUERY_SENTINEL,
                extra_headers={
                    "Authorization": "Bearer %s" % AUTH_SENTINEL,
                    "Cookie": "session=%s" % COOKIE_SENTINEL,
                })
            https_edge("POST", REDEEM_PATH + "?code=%s" % QUERY_SENTINEL,
                       body={"code": "not-the-sentinel"})
            snapshot_logs("after-query")
            assert_edge_logs_omit_sentinels(
                compose_logs("pair-redeem-edge"), "200 access log")

        step("access log omits query string, authorization, and cookie",
             access_log_omits_query)

        def error_log_omits_query_on_502():
            stop = run(
                compose_cmd("pair-redeem", "pair-redeem-edge") + [
                    "stop", "pair-redeem"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            expect(stop.returncode == 0,
                   "failed to stop pair-redeem for 502 probe")
            try:
                status, _, _, _ = https_edge(
                    "GET", "/health?code=%s" % QUERY_SENTINEL,
                    extra_headers={
                        "Authorization": "Bearer %s" % AUTH_SENTINEL,
                        "Cookie": "session=%s" % COOKIE_SENTINEL,
                    })
                expect(status == 502,
                       "expected 502 when pair-redeem is down, got %s" %
                       status)
                snapshot_logs("after-502")
                assert_edge_logs_omit_sentinels(
                    compose_logs("pair-redeem-edge"), "502 error log")
            finally:
                start = run(
                    compose_cmd("pair-redeem", "pair-redeem-edge") + [
                        "start", "pair-redeem"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if start.returncode != 0:
                    raise Failure(
                        "failed to restart pair-redeem after 502 probe")
                wait_http(
                    "https://127.0.0.1:%d/health" % EDGE_PORT,
                    timeout=90, expect_status=200,
                    ssl_context=mock_ssl_context())

        step("error log omits query string when upstream returns 502",
             error_log_omits_query_on_502)

        def logs_have_no_secrets():
            snapshot_logs("after-cases")
            blobs = "".join(_captured_logs)
            secrets_list = list(issued_codes) + list(issued_reg_tokens)
            if admin_token:
                secrets_list.append(admin_token)
            secrets_list.extend([postgres_pw, macaroon, form, shared, admin_password])
            secrets_list.extend([QUERY_SENTINEL, AUTH_SENTINEL, COOKIE_SENTINEL])
            assert_no_secrets(blobs, secrets_list, "edge e2e compose logs")

        step("logs omit codes, tokens, and admin credentials",
             logs_have_no_secrets)

        log("pair-redeem-edge e2e passed")
        return 0
    finally:
        teardown()
        # Confirm the isolated project left no containers or volumes.
        leftover = run(
            ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=" + PROJECT,
             "--format", "{{.ID}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        leftover_ids = leftover.stdout.decode("utf-8").strip()
        if leftover_ids:
            log("WARNING: leftover containers for %s: %s" % (PROJECT, leftover_ids))
        vols = run(
            ["docker", "volume", "ls",
             "--filter", "label=com.docker.compose.project=" + PROJECT,
             "--format", "{{.Name}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        leftover_vols = vols.stdout.decode("utf-8").strip()
        if leftover_vols:
            log("WARNING: leftover volumes for %s: %s" % (PROJECT, leftover_vols))
        if os.path.exists(CERT_FILE) or os.path.exists(KEY_FILE) or os.path.exists(ENV_FILE):
            raise Failure("teardown left cert or env files behind")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        log("FAIL: %s" % exc)
        sys.exit(1)

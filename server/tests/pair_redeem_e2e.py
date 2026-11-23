#!/usr/bin/env python3
"""Isolated App QR redeem acceptance test (A-R7 / #128).

This is an isolated Docker Compose test against a real Synapse and the
pair_redeem service. It is NOT a real HTTPS entry (no Caddy edge) and
NOT a phone camera scan.

Project name: botville-pair-redeem-e2e
Host ports bind 127.0.0.1 only (Synapse :18208, redeem :18292).
Does not read or write server/compose/.env, and does not touch the
default `botville` compose project. Always `down -v` this project.

Coverage:
  1. Official compose: profile off => service absent; profile on =>
     loopback-only publish (also asserted here: e2e compose with the
     profile off leaves :18292 closed).
  2. Enabled: issue a code, redeem for a real Synapse registration_token,
     register a user; chat-only omits features_url; gateway path matches
     the contract.
  3. Replay 409, expired 410, revoked 410, invalid 400, rate-limited 429,
     issuer failure (bad admin token) does not consume the code.
  4. Compose logs and redeem audit events contain no plaintext code,
     no registration_token, no admin access token.

Run: PYTHONDONTWRITEBYTECODE=1 python3 server/tests/pair_redeem_e2e.py
  or: make pair-redeem-e2e
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

PROJECT = "botville-pair-redeem-e2e"
COMPOSE_FILE = os.path.join(ROOT, "server", "compose", "pair-redeem-e2e.yml")
OFFICIAL_COMPOSE = os.path.join(ROOT, "server", "compose", "docker-compose.yml")
GENERATED = os.path.join(ROOT, "server", "compose", "generated", "pair-redeem-e2e")
ENV_FILE = os.path.join(ROOT, "server", "compose", "generated", "pair-redeem-e2e.env")
TEMPLATE = os.path.join(ROOT, "server", "compose", "synapse", "homeserver.yaml.tmpl")
HS_PLACEHOLDER = "https://hs.example.org"
GW_PLACEHOLDER = "https://gw.example.org"
DEVICE = "e2e-isolated-device"
SYNAPSE_PORT = 18208
REDEEM_PORT = 18292
REDEEM_PATH = "/_botville/pair/redeem"


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def run(cmd, **kwargs):
    kwargs.setdefault("cwd", ROOT)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **kwargs)


def compose_cmd(profile_on=False):
    cmd = [
        "docker", "compose", "-p", PROJECT, "-f", COMPOSE_FILE,
        "--env-file", ENV_FILE,
    ]
    if profile_on:
        cmd.extend(["--profile", "pair-redeem"])
    return cmd


def make_code(seed):
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
    # Isolated stack: registration is token-gated so a successful
    # redeem can be proven by actually registering a user.
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


def wait_http(url, timeout=90, expect_status=None):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
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


def http_json(method, url, body=None, token=None, timeout=10):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return resp.status, parsed, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeError):
            parsed = {"_raw": raw.decode("utf-8", "replace")}
        return exc.code, parsed, dict(exc.headers)


def register_admin(synapse, shared_secret, username, password):
    status, body, _ = http_json("GET", synapse + "/_synapse/admin/v1/register")
    expect(status == 200 and body.get("nonce"), "admin nonce: %s %r" % (status, body))
    nonce = body["nonce"]
    mac = hmac.new(
        shared_secret.encode("utf-8"),
        ("%s\0%s\0%s\0admin" % (nonce, username, password)).encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    status, body, _ = http_json("POST", synapse + "/_synapse/admin/v1/register", {
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


def register_with_token(synapse, username, password, registration_token):
    """UIA: registration_token + dummy, against the isolated Synapse."""
    session = None
    auth = None
    body = None
    for _ in range(6):
        payload = {
            "username": username,
            "password": password,
            "auth": auth or {"type": "m.login.dummy"},
            "initial_device_display_name": DEVICE,
        }
        if session:
            payload["auth"]["session"] = session
        status, body, _ = http_json("POST", synapse + "/_matrix/client/v3/register", payload)
        if status == 200 and body.get("access_token"):
            return body
        if status != 401:
            raise Failure("register unexpected %s %r" % (status, body))
        session = body.get("session") or session
        flows = body.get("flows") or []
        stages = []
        if flows:
            candidates = [f.get("stages") or [] for f in flows]
            candidates.sort(key=lambda s: "m.login.registration_token" not in s)
            stages = candidates[0]
        remaining = []
        completed = set(body.get("completed") or [])
        for stage in stages:
            if stage not in completed:
                remaining.append(stage)
        if not remaining:
            remaining = ["m.login.registration_token"]
        stage = remaining[0]
        if stage == "m.login.registration_token":
            auth = {"type": "m.login.registration_token", "token": registration_token,
                    "session": session}
        elif stage == "m.login.dummy":
            auth = {"type": "m.login.dummy", "session": session}
        else:
            raise Failure("unsupported UIA stage %s" % stage)
    raise Failure("register did not complete: %r" % (body,))


def exec_store(py, stdin_obj=None):
    cmd = compose_cmd(True) + ["exec", "-T", "pair-redeem", "python", "-c", py]
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


def revoke_in_store(code):
    script = (
        "import json,sys\n"
        "from pair_redeem.store import PairRedeemStore, code_sha256\n"
        "spec=json.load(sys.stdin)\n"
        "store=PairRedeemStore('/data/pair_redeem.db')\n"
        "store.revoke(code_sha256(spec['code']))\n"
        "store.close()\n"
        "print('{}')\n"
    )
    exec_store(script, {"code": code})


def code_status(code):
    script = (
        "import json,sys\n"
        "from pair_redeem.store import PairRedeemStore, code_sha256\n"
        "spec=json.load(sys.stdin)\n"
        "store=PairRedeemStore('/data/pair_redeem.db')\n"
        "row=store.get(code_sha256(spec['code']))\n"
        "store.close()\n"
        "print(json.dumps(row))\n"
    )
    return exec_store(script, {"code": code})


def audit_events():
    script = (
        "import json\n"
        "from pair_redeem.store import PairRedeemStore\n"
        "store=PairRedeemStore('/data/pair_redeem.db')\n"
        "print(json.dumps(store.events()))\n"
        "store.close()\n"
    )
    return exec_store(script) or []


def redeem(code, name=DEVICE):
    return http_json(
        "POST", "http://127.0.0.1:%d%s" % (REDEEM_PORT, REDEEM_PATH),
        {"code": code, "device_display_name": name},
    )


def compose_logs(service=None):
    cmd = compose_cmd(True) + ["logs", "--no-color"]
    if service:
        cmd.append(service)
    proc = run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return proc.stdout.decode("utf-8", "replace")


def assert_no_secrets(blobs, secrets_list, label):
    for item in secrets_list:
        if not item:
            continue
        for blob in blobs:
            if item in blob:
                raise Failure("%s leaked a secret (%d chars)" % (label, len(item)))


def step(name, fn):
    log("== " + name)
    fn()
    log("PASS " + name)


def main():
    if not shutil.which("docker"):
        raise SystemExit("docker is not on PATH")
    probe = run(["docker", "compose", "version"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        raise SystemExit("docker compose v2 is required")

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
        "BOTVILLE_PAIR_REDEEM_SYNAPSE_URL": "http://synapse:8008",
        "BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN": "",
        "BOTVILLE_PAIR_REDEEM_IP_LIMIT": "10",
        "BOTVILLE_PAIR_REDEEM_GLOBAL_LIMIT": "60",
        "PAIR_REDEEM_E2E_CONFIG": GENERATED,
    }
    os.makedirs(GENERATED, mode=0o700, exist_ok=True)
    render_homeserver(env_map, os.path.join(GENERATED, "synapse", "homeserver.yaml"))
    write_env(env_map)

    synapse = "http://127.0.0.1:%d" % SYNAPSE_PORT
    started = False
    admin_token = ""
    issued_codes = []
    issued_reg_tokens = []

    def teardown():
        run(compose_cmd(True) + ["down", "-v", "--remove-orphans"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def handle_sig(_signum, _frame):
        teardown()
        raise SystemExit(128 + _signum)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        def official_profile_default_off():
            # Config-only check of the official compose file: no Docker start.
            example = os.path.join(ROOT, "server", "compose", ".env.example")
            cmd = [
                "docker", "compose", "-f", OFFICIAL_COMPOSE,
                "--env-file", example, "--profile", "synapse",
                "config", "--format", "json",
            ]
            env = dict(os.environ)
            for key in ("BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN",
                        "PAIRING_ADMIN_TOKEN", "PAIRING_SERVICE_TOKEN"):
                env.pop(key, None)
            env["POSTGRES_PASSWORD"] = "ci-placeholder-not-a-secret"
            proc = subprocess.run(
                cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            expect(proc.returncode == 0, "official compose config failed")
            services = json.loads(proc.stdout.decode("utf-8"))["services"]
            expect("pair-redeem" not in services,
                   "pair-redeem must be absent without its profile")
            expect("synapse" in services, "synapse profile missing")

            cmd = [
                "docker", "compose", "-f", OFFICIAL_COMPOSE,
                "--env-file", example, "--profile", "synapse",
                "--profile", "pair-redeem", "config", "--format", "json",
            ]
            proc = subprocess.run(
                cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            expect(proc.returncode == 0, "official compose config + profile failed")
            services = json.loads(proc.stdout.decode("utf-8"))["services"]
            svc = services["pair-redeem"]
            expect(all(p.get("host_ip") == "127.0.0.1" for p in svc.get("ports") or []),
                   "pair-redeem host port must be 127.0.0.1")

        step("official compose default-off and loopback publish",
             official_profile_default_off)

        def start_without_redeem():
            nonlocal started
            proc = run(compose_cmd(False) + ["up", "-d"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            started = True
            if proc.returncode != 0:
                raise Failure("compose up (no profile) failed:\n%s"
                              % proc.stdout.decode("utf-8", "replace"))
            wait_http(synapse + "/health", timeout=120)
            time.sleep(0.5)
            expect(not port_open(REDEEM_PORT),
                   "redeem port should be closed when the profile is off")

        step("isolated stack starts with redeem profile off (port closed)",
             start_without_redeem)

        def enable_and_redeem_success():
            nonlocal admin_token
            admin_token = register_admin(
                synapse, shared, "redeemadmin", admin_password)
            # Recreate env with the live admin token and start the service.
            env_map["BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN"] = admin_token
            write_env(env_map)
            proc = run(compose_cmd(True) + ["up", "-d"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if proc.returncode != 0:
                raise Failure("compose up (pair-redeem) failed:\n%s"
                              % proc.stdout.decode("utf-8", "replace"))
            wait_http("http://127.0.0.1:%d/health" % REDEEM_PORT, timeout=60)

            code = make_code(1)
            issued_codes.append(code)
            issue_in_store(code, HS_PLACEHOLDER, gw=GW_PLACEHOLDER)
            status, body, _ = redeem(code)
            expect(status == 200, "redeem 200 expected, got %s %r" % (status, body))
            validated = contract.validate_redeem_response(body, HS_PLACEHOLDER)
            expect(validated["auth"]["type"] == "registration_token", "auth type")
            expect(validated["gateway"] == GW_PLACEHOLDER, "gateway")
            expect(validated["features_url"] == GW_PLACEHOLDER + "/v1/capabilities",
                   "features_url")
            token = validated["auth"]["token"]
            issued_reg_tokens.append(token)
            user = "redeemed" + secrets.token_hex(3)
            registered = register_with_token(synapse, user, secrets.token_hex(8), token)
            expect(registered.get("user_id", "").endswith(":" + "hs.example.org")
                   or registered.get("user_id", "").startswith("@"),
                   "registered user_id missing")

            chat = make_code(2)
            issued_codes.append(chat)
            issue_in_store(chat, HS_PLACEHOLDER, gw=None)
            status, body, _ = redeem(chat)
            expect(status == 200, "chat-only redeem: %s %r" % (status, body))
            validated = contract.validate_redeem_response(body, HS_PLACEHOLDER)
            expect(validated["gateway"] is None, "chat-only gateway must be null")
            expect("features_url" not in body, "chat-only must omit features_url")
            issued_reg_tokens.append(validated["auth"]["token"])

        step("enabled: redeem succeeds and registration_token registers a user",
             enable_and_redeem_success)

        def negatives():
            used = issued_codes[0]
            status, body, _ = redeem(used)
            expect(status == 409 and body.get("errcode") == "BV_PAIR_USED",
                   "replay: %s %r" % (status, body))

            expired = make_code(3)
            issued_codes.append(expired)
            issue_in_store(expired, HS_PLACEHOLDER, gw=None, ttl=1)
            time.sleep(1.2)
            status, body, _ = redeem(expired)
            expect(status == 410 and body.get("errcode") == "BV_PAIR_EXPIRED",
                   "expired: %s %r" % (status, body))
            expect(code_status(expired)["status"] == "unused", "expired must stay unused")

            revoked = make_code(4)
            issued_codes.append(revoked)
            issue_in_store(revoked, HS_PLACEHOLDER, gw=None)
            revoke_in_store(revoked)
            status, body, _ = redeem(revoked)
            expect(status == 410 and body.get("errcode") == "BV_PAIR_REVOKED",
                   "revoked: %s %r" % (status, body))

            status, body, _ = redeem(make_code(99))
            expect(status == 400 and body.get("errcode") == "BV_PAIR_INVALID",
                   "invalid: %s %r" % (status, body))

            # Rate limit: lower the IP cap and recreate.
            env_map["BOTVILLE_PAIR_REDEEM_IP_LIMIT"] = "2"
            write_env(env_map)
            proc = run(
                compose_cmd(True) + ["up", "-d", "--force-recreate", "--no-deps",
                                     "pair-redeem"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            expect(proc.returncode == 0, "recreate for rate limit failed")
            wait_http("http://127.0.0.1:%d/health" % REDEEM_PORT, timeout=60)
            # Two attempts consume the cap (used + unknown both count).
            redeem(make_code(80))
            redeem(make_code(81))
            status, body, _ = redeem(make_code(82))
            expect(status == 429 and body.get("errcode") == "BV_PAIR_RATE_LIMITED",
                   "rate limit: %s %r" % (status, body))
            expect(int(body.get("retry_after_ms") or 0) >= 1, "retry_after_ms")

        step("replay / expired / revoked / invalid / rate-limited", negatives)

        def issuer_failure_rolls_back():
            env_map["BOTVILLE_PAIR_REDEEM_IP_LIMIT"] = "10"
            env_map["BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN"] = "invalid-admin-token-not-a-secret"
            write_env(env_map)
            proc = run(
                compose_cmd(True) + ["up", "-d", "--force-recreate", "--no-deps",
                                     "pair-redeem"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            expect(proc.returncode == 0, "recreate with bad token failed")
            wait_http("http://127.0.0.1:%d/health" % REDEEM_PORT, timeout=60)
            pending = make_code(5)
            issued_codes.append(pending)
            issue_in_store(pending, HS_PLACEHOLDER, gw=None)
            status, body, _ = redeem(pending)
            expect(status == 503, "issuer failure should be 503, got %s %r" % (status, body))
            expect("errcode" not in body, "5xx must not invent an errcode")
            expect(code_status(pending)["status"] == "unused",
                   "code must stay unused when issuer fails")

        step("issuer failure does not consume the code", issuer_failure_rolls_back)

        def no_secret_leak():
            redeem_logs = compose_logs("pair-redeem")
            all_logs = compose_logs()
            events = audit_events()
            event_text = json.dumps(events, ensure_ascii=False)
            # pair_redeem logs + audit must not contain codes, issued
            # registration tokens, or the admin token.
            redeem_secrets = list(issued_codes) + list(issued_reg_tokens)
            redeem_secrets.append(admin_token)
            redeem_secrets.append(env_map["BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN"])
            assert_no_secrets(
                [redeem_logs, event_text],
                [s for s in redeem_secrets if s],
                "pair_redeem logs/audit")
            # Other containers (especially Synapse) may log a token they
            # themselves issued. Still forbid pairing codes, the admin
            # access token, and stack secrets in the combined log stream.
            stack_secrets = list(issued_codes)
            stack_secrets.append(admin_token)
            stack_secrets.append(shared)
            stack_secrets.append(postgres_pw)
            assert_no_secrets(
                [all_logs], [s for s in stack_secrets if s],
                "compose logs")
            for ev in events:
                detail = ev.get("detail") or {}
                for key in ("code", "token", "auth", "admin_token"):
                    expect(key not in detail, "audit detail has %s" % key)

        step("logs and audit omit codes, registration tokens, and admin token",
             no_secret_leak)

        log("All isolated pair_redeem e2e steps passed.")
        return 0
    finally:
        teardown()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        log("FAIL %s" % exc)
        sys.exit(1)

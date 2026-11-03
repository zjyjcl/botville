#!/usr/bin/env bash
# continuwuity first-boot bootstrap (#68).
#
# On a database with no users yet continuwuity boots into bootstrap mode:
#   - it prints a one-time registration token to the container log
#     ("... register an account on <server_name> using the registration token
#     <TOKEN> . Pick your own username and password!");
#   - the configured CONTINUWUITY_REGISTRATION_TOKEN stays invalid until the
#     first account exists (UIA answers 401 M_FORBIDDEN "Invalid registration
#     token", flows = ["m.login.registration_token"]);
#   - the first registered user becomes the server admin.
#
# This script waits for the client API, scrapes that one-time token from the
# container logs and registers BOTVILLE_ADMIN_USER / BOTVILLE_ADMIN_PASSWORD
# (server/compose/.env) through the standard UIA m.login.registration_token
# flow, making that account the first (admin) user. Standard Client-Server
# API only: no admin room commands, no /_synapse/admin.
#
# Exit 0 = bootstrap complete (registered now, user already existed, or no
# token in the logs = already bootstrapped); exit 1 = failure.
#
# The admin password is passed to python3 via the environment and is never
# printed. Run from the repo root, or override COMPOSE / BOTVILLE_BASE_URL.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${BOTVILLE_ENV_FILE:-${script_dir}/../compose/.env}"
# Command prefix for docker compose; intentional word splitting.
COMPOSE="${COMPOSE:-docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env}"

if [[ ! -f "${env_file}" ]]; then
    echo "bootstrap-continuwuity.sh: ${env_file} not found; run server/scripts/init.sh first" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "bootstrap-continuwuity.sh: python3 is required" >&2
    exit 1
fi

# First assignment of each key wins, mirroring compose's --env-file handling.
env_value() {
    sed -n "s/^$1=//p" "${env_file}" | head -n 1
}

admin_user="$(env_value BOTVILLE_ADMIN_USER)"
admin_user="${admin_user:-botville-admin}"
admin_password="$(env_value BOTVILLE_ADMIN_PASSWORD)"
http_port="$(env_value BOTVILLE_HTTP_PORT)"
http_port="${http_port:-8008}"
base_url="${BOTVILLE_BASE_URL:-http://127.0.0.1:${http_port}}"
timeout_secs="${BOTVILLE_BOOTSTRAP_TIMEOUT:-180}"

if [[ -z "${admin_password}" || "${admin_password}" == "<change-me>" ]]; then
    echo "bootstrap-continuwuity.sh: BOTVILLE_ADMIN_PASSWORD is missing or still '<change-me>' in ${env_file};" >&2
    echo "  re-run server/scripts/init.sh (it appends missing keys with fresh secrets) or set it manually" >&2
    exit 1
fi

echo "bootstrap-continuwuity.sh: waiting for ${base_url}/_matrix/client/versions"
python3 - "${base_url}" "${timeout_secs}" <<'PY'
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

base_url, timeout_secs = sys.argv[1], float(sys.argv[2])
deadline = time.monotonic() + timeout_secs
last = "no response"
while True:
    try:
        req = urllib.request.Request(
            base_url + "/_matrix/client/versions", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("bootstrap-continuwuity.sh: client API is up")
                sys.exit(0)
            last = "HTTP {}".format(resp.status)
    except urllib.error.HTTPError as exc:
        last = "HTTP {}".format(exc.code)
    except Exception as exc:  # noqa: BLE001 - keep polling until the deadline
        last = str(exc)
    if time.monotonic() >= deadline:
        print(
            "bootstrap-continuwuity.sh: client API not ready within {}s (last: {})".format(
                int(timeout_secs), last
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    time.sleep(2)
PY

# --no-color only disables docker's own decoration; the container output still
# carries ANSI colors, so strip CSI escape sequences before matching. The most
# recent match wins: every boot on a still-userless database mints a fresh
# one-time token and invalidates older ones.
# shellcheck disable=SC2086  # ${COMPOSE} is an intentionally word-split prefix
compose_logs="$(${COMPOSE} --profile continuwuity logs --no-color continuwuity 2>&1)" || {
    echo "bootstrap-continuwuity.sh: could not read continuwuity logs via:" >&2
    echo "  ${COMPOSE} --profile continuwuity logs --no-color continuwuity" >&2
    printf '%s\n' "${compose_logs}" >&2
    exit 1
}

token="$(printf '%s\n' "${compose_logs}" \
    | LC_ALL=C sed $'s/\x1b\\[[0-9;:?]*[ -/]*[@-~]//g' \
    | LC_ALL=C sed -nE 's/.*using the registration token ([^[:space:]]+).*/\1/p' \
    | tail -n 1)"

if [[ -z "${token}" ]]; then
    echo "bootstrap-continuwuity.sh: no one-time bootstrap token in the continuwuity logs;"
    echo "  the first account was most likely already created (bootstrap done)"
    exit 0
fi
echo "bootstrap-continuwuity.sh: found one-time bootstrap token in the logs (not echoed)"

# Standard UIA: request a session first, then complete the
# m.login.registration_token stage with the one-time token. The password
# travels via the environment (never argv, never printed).
BOTVILLE_BOOTSTRAP_PASSWORD="${admin_password}" \
    python3 - "${base_url}" "${admin_user}" "${token}" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

base_url, username, reg_token = sys.argv[1:4]
password = os.environ.pop("BOTVILLE_BOOTSTRAP_PASSWORD")
PREFIX = "bootstrap-continuwuity.sh: "


def post_register(body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/_matrix/client/v3/register",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, None


def fail(message):
    print(PREFIX + message, file=sys.stderr)
    sys.exit(1)


already = "{} already registered; bootstrap was already complete".format(username)

# Idempotency: GET /register/available (standard CS API) answers 400
# M_USER_IN_USE once the admin exists, even when stale token lines remain in
# the container logs.
try:
    with urllib.request.urlopen(
        base_url + "/_matrix/client/v3/register/available?username="
        + urllib.parse.quote(username), timeout=30) as r:
        pass
except urllib.error.HTTPError as exc:
    try:
        body = json.loads(exc.read() or b"{}")
    except json.JSONDecodeError:
        body = {}
    if body.get("errcode") == "M_USER_IN_USE":
        print(PREFIX + already)
        sys.exit(0)

# UIA step 1: no auth -> 401 with a session (flows: m.login.registration_token).
status, resp = post_register({"username": username, "password": password})
if status == 200:
    print(PREFIX + already)
    sys.exit(0)
if status == 400 and (resp or {}).get("errcode") == "M_USER_IN_USE":
    print(PREFIX + already)
    sys.exit(0)
if status != 401:
    fail("register (no auth) returned HTTP {}: {!r}".format(status, resp))
session = (resp or {}).get("session")
if not session:
    fail("register 401 without a session: {!r}".format(resp))

# UIA step 2: complete the token stage.
status, resp = post_register(
    {
        "username": username,
        "password": password,
        "auth": {
            "type": "m.login.registration_token",
            "token": reg_token,
            "session": session,
        },
    }
)
if status == 200:
    user_id = (resp or {}).get("user_id")
    if not user_id:
        fail("register 200 without user_id: {!r}".format(resp))
    print(PREFIX + "registered {} as the first (admin) account".format(user_id))
    sys.exit(0)
errcode = (resp or {}).get("errcode")
if errcode == "M_USER_IN_USE":
    print(PREFIX + already)
    sys.exit(0)
if errcode == "M_FORBIDDEN":
    fail(
        "the one-time bootstrap token was rejected ({}); it was most likely already "
        "consumed by another first account. Check the existing users, or on a "
        "throwaway setup wipe the continuwuity-data volume and re-run "
        "'make up HS=continuwuity'".format((resp or {}).get("error"))
    )
fail("register (UIA) returned HTTP {}: {!r}".format(status, resp))
PY

echo "bootstrap-continuwuity.sh: done"

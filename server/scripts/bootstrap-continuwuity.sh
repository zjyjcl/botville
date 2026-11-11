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
# Exit 0 = bootstrap complete (registered now, or the first account already
# exists — including databases whose first/admin user has a different name);
# exit 1 = bootstrap could not be completed or verified. This script never
# deletes or resets any data; wiping the continuwuity-data volume is a manual
# operator decision (see server/README.md, "重置（仅手动）" section).
#
# The admin password and both registration tokens travel to python3 via the
# environment and are never printed or passed as argv. Run from the repo
# root, or override COMPOSE / BOTVILLE_BASE_URL / BOTVILLE_BOOTSTRAP_TIMEOUT.
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

# The last assignment of each key wins, matching docker compose's --env-file
# handling of duplicate keys (verified with compose v2.40.x) and init.sh's
# renderer.
env_value() {
    sed -n "s/^$1=//p" "${env_file}" | tail -n 1
}

admin_user="$(env_value BOTVILLE_ADMIN_USER)"
admin_user="${admin_user:-botville-admin}"
admin_password="$(env_value BOTVILLE_ADMIN_PASSWORD)"
configured_token="$(env_value BOTVILLE_REGISTRATION_TOKEN)"
http_port="$(env_value BOTVILLE_HTTP_PORT)"
http_port="${http_port:-8008}"
base_url="${BOTVILLE_BASE_URL:-http://127.0.0.1:${http_port}}"
base_url="${base_url%/}"
timeout_secs="${BOTVILLE_BOOTSTRAP_TIMEOUT:-180}"

echo "bootstrap-continuwuity.sh: waiting for ${base_url}/_matrix/client/versions"
python3 - "${base_url}" "${timeout_secs}" <<'PY'
import sys
import time
import urllib.error
import urllib.request

base_url, timeout_secs = sys.argv[1].rstrip("/"), float(sys.argv[2])
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
    except Exception as exc:  # keep polling until the deadline
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
# one-time token and invalidates older ones. The logs are streamed through a
# pipe instead of buffered into a variable (--tail keeps the scan bounded on
# long-running servers); on failure only the command to re-run is printed,
# never the logs themselves (they may contain live one-time tokens).
#
# The client API can answer /versions before continuwuity prints the one-time
# token, so scrape more than once (BOTVILLE_BOOTSTRAP_TOKEN_WAIT seconds,
# default 30; 0 = a single attempt). The token value is never printed.
token=""
logs_status=0
token_wait="${BOTVILLE_BOOTSTRAP_TOKEN_WAIT:-30}"
token_deadline=$((SECONDS + token_wait))
while :; do
    logs_status=0
    # shellcheck disable=SC2086  # ${COMPOSE} is an intentionally word-split prefix
    token="$(${COMPOSE} --profile continuwuity logs --no-color --tail 2000 continuwuity 2>/dev/null \
        | LC_ALL=C sed $'s/\x1b\\[[0-9;:?]*[ -/]*[@-~]//g' \
        | LC_ALL=C sed -nE 's/.*using the registration token ([^[:space:]]+).*/\1/p' \
        | tail -n 1)" || logs_status=$?
    if [[ "${logs_status}" -ne 0 ]]; then
        echo "bootstrap-continuwuity.sh: could not read the continuwuity logs (exit ${logs_status}); to inspect them run:" >&2
        # shellcheck disable=SC2086
        echo "  ${COMPOSE} --profile continuwuity logs --no-color --tail 2000 continuwuity" >&2
        exit 1
    fi
    if [[ -n "${token}" ]]; then
        echo "bootstrap-continuwuity.sh: found one-time bootstrap token in the logs (not echoed)"
        break
    fi
    if (( SECONDS >= token_deadline )); then
        echo "bootstrap-continuwuity.sh: no one-time bootstrap token in the last 2000 log lines;"
        echo "  verifying the bootstrap state with the configured registration token"
        break
    fi
    sleep 2
done

# One program handles both branches: with a scraped one-time token it
# registers the admin account (idempotently); without one it verifies via the
# standard token-validity endpoint that the first account exists, instead of
# silently assuming "already bootstrapped".
if ! BOTVILLE_BOOTSTRAP_PASSWORD="${admin_password}" \
    BOTVILLE_BOOTSTRAP_REG_TOKEN="${token}" \
    BOTVILLE_BOOTSTRAP_CONFIG_TOKEN="${configured_token}" \
    BOTVILLE_BOOTSTRAP_ENV_FILE="${env_file}" \
    BOTVILLE_BOOTSTRAP_RESET_CONFIRM="${BOTVILLE_BOOTSTRAP_RESET_CONFIRM:-}" \
    python3 - "${base_url}" "${admin_user}" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

base_url = sys.argv[1].rstrip("/")
username = sys.argv[2]
password = os.environ.pop("BOTVILLE_BOOTSTRAP_PASSWORD", "")
reg_token = os.environ.pop("BOTVILLE_BOOTSTRAP_REG_TOKEN", "")
config_token = os.environ.pop("BOTVILLE_BOOTSTRAP_CONFIG_TOKEN", "")
env_file = os.environ.pop("BOTVILLE_BOOTSTRAP_ENV_FILE", "server/compose/.env")
reset_confirm = os.environ.pop("BOTVILLE_BOOTSTRAP_RESET_CONFIRM", "")
PREFIX = "bootstrap-continuwuity.sh: "


class NetFailure(Exception):
    """Transport-level failure (connection refused, timeout, ...)."""


def fail(message):
    print(PREFIX + message, file=sys.stderr)
    sys.exit(1)


def http_json(method, path, body=None):
    """Request returning (status, decoded body or None). HTTP errors come
    back as (code, body); transport errors raise NetFailure."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
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
    except Exception as exc:  # transport errors, not HTTP statuses
        raise NetFailure(str(exc))


def configured_token_valid():
    """True iff the configured registration token is currently accepted.

    Standard unauthenticated endpoint (spec v1.2, MSC3231):
    GET /_matrix/client/v1/register/m.login.registration_token/validity ->
    200 {"valid": bool}; 403 means registration is not permitted at all, so
    no token is valid (continuwuity similarly rejects the configured token
    while no first user exists). Anything else is a hard error, not a guess.
    """
    if not config_token:
        fail(
            "BOTVILLE_REGISTRATION_TOKEN is not set in {}; cannot verify the "
            "bootstrap state".format(env_file)
        )
    path = (
        "/_matrix/client/v1/register/m.login.registration_token/validity?"
        + urllib.parse.urlencode({"token": config_token})
    )
    try:
        status, body = http_json("GET", path)
    except NetFailure as exc:
        fail("registration token validity check failed: {}".format(exc))
    if status == 200:
        return bool((body or {}).get("valid"))
    if status in (401, 403):
        return False
    fail(
        "registration token validity check returned HTTP {}: {!r}".format(status, body)
    )


already = "{} already registered; bootstrap was already complete".format(username)

if not reg_token:
    # No one-time token in the logs: do not silently assume "already
    # bootstrapped". The configured registration token only becomes valid
    # once the first account exists, so it decides.
    if configured_token_valid():
        print(PREFIX + "the configured registration token is valid, so the first")
        print(PREFIX + "account already exists (bootstrap complete)")
        sys.exit(0)
    fail(
        "bootstrap state unclear: no one-time token in the continuwuity logs\n"
        "  and the configured BOTVILLE_REGISTRATION_TOKEN is not accepted ({}).\n"
        "  Either the database still has no users and the token line is older\n"
        "  than the log tail, or the configured token changed. Nothing was\n"
        "  registered; inspect the logs with the command printed below and\n"
        "  re-run 'make up HS=continuwuity'".format(env_file)
    )

# Idempotency: GET /register/available (standard CS API) answers 400
# M_USER_IN_USE once the admin exists, even when stale token lines remain in
# the container logs.
try:
    status, body = http_json(
        "GET",
        "/_matrix/client/v3/register/available?username="
        + urllib.parse.quote(username),
    )
except NetFailure as exc:
    fail("register/availability check failed: {}".format(exc))
if status == 400 and (body or {}).get("errcode") == "M_USER_IN_USE":
    print(PREFIX + already)
    sys.exit(0)
if status != 200:
    fail("register/available returned HTTP {}: {!r}".format(status, body))

# The password is only needed from here on: a bootstrapped server with a
# stale token line in its logs must not fail just because the password is
# still unset.
if not password or password == "<change-me>":
    fail(
        "BOTVILLE_ADMIN_PASSWORD is missing or still '<change-me>' in {};\n"
        "  re-run server/scripts/init.sh (it appends missing keys and replaces\n"
        "  leftover '<change-me>' values with fresh secrets) or set it "
        "manually".format(env_file)
    )


def post_register(payload):
    try:
        return http_json("POST", "/_matrix/client/v3/register", payload)
    except NetFailure as exc:
        fail("register request failed: {}".format(exc))


# UIA step 1: no auth -> 401 with a session (flows: m.login.registration_token).
status, body = post_register({"username": username, "password": password})
if status == 200:
    user_id = (body or {}).get("user_id")
    if not user_id:
        fail("register 200 without user_id: {!r}".format(body))
    print(PREFIX + "registered {} (no UIA was required)".format(user_id))
    sys.exit(0)
if status == 400 and (body or {}).get("errcode") == "M_USER_IN_USE":
    print(PREFIX + already)
    sys.exit(0)
if status != 401:
    fail("register (no auth) returned HTTP {}: {!r}".format(status, body))
session = (body or {}).get("session")
if not session:
    fail("register 401 without a session: {!r}".format(body))

# UIA step 2: complete the token stage.
status, body = post_register(
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
    user_id = (body or {}).get("user_id")
    if not user_id:
        fail("register 200 without user_id: {!r}".format(body))
    print(PREFIX + "registered {} as the first (admin) account".format(user_id))
    sys.exit(0)
errcode = (body or {}).get("errcode")
if errcode == "M_USER_IN_USE":
    print(PREFIX + already)
    sys.exit(0)
if errcode == "M_FORBIDDEN":
    # The one-time token was rejected. If the configured token is valid
    # meanwhile, another first user exists (e.g. an upgraded deployment
    # whose admin was registered manually): bootstrapped, not failed. This
    # script never suggests or performs a data reset.
    if configured_token_valid():
        print(PREFIX + "the one-time bootstrap token was rejected, and the configured")
        print(PREFIX + "registration token is valid: this server was already")
        print(PREFIX + "bootstrapped with a different first user. To manage that")
        print(PREFIX + "admin from this stack, set BOTVILLE_ADMIN_USER in {} to".format(env_file))
        print(PREFIX + "the existing admin username; otherwise no action is needed.")
        if reset_confirm == "yes":
            print(PREFIX + "BOTVILLE_BOOTSTRAP_RESET_CONFIRM=yes acknowledged: a reset")
            print(PREFIX + "destroys ALL server data. This script only prints the manual")
            print(PREFIX + "steps and never runs them:")
            print("    docker compose -f server/compose/docker-compose.yml \\")
            print("      --env-file server/compose/.env \\")
            print("      --profile continuwuity --profile gateway down -v")
            print("    # make down does not pass -v and deletes no volumes.")
            print("    # down -v with --profile gateway deletes gateway-data")
            print("    # (whether or not a gateway container is running).")
            print("    # Without that profile, or to delete it alone:")
            print("    #   docker volume rm -f <project>_gateway-data")
            print("    make up HS=continuwuity")
        sys.exit(0)
    fail(
        "the one-time bootstrap token was rejected and the configured token is\n"
        "  still not accepted; the token line found in the logs is probably\n"
        "  stale. Nothing was registered; inspect the logs and re-run\n"
        "  'make up HS=continuwuity'"
    )
fail("register (UIA) returned HTTP {}: {!r}".format(status, body))
PY
then
    if [[ -z "${token}" ]]; then
        echo "bootstrap-continuwuity.sh: to inspect the full logs, run:" >&2
        # shellcheck disable=SC2086
        echo "  ${COMPOSE} --profile continuwuity logs --no-color continuwuity | grep -a 'registration token'" >&2
    fi
    exit 1
fi

echo "bootstrap-continuwuity.sh: done"

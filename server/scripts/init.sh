#!/usr/bin/env bash
# Botville server bootstrap (A0.2):
#   0. WARN (never fail, issue #134) when a host proxy env var or
#      ~/.docker/config.json "proxies" points at a loopback address: docker
#      injects host proxies into every new container, where 127.0.0.1 is the
#      container itself. compose's x-noproxy-env already keeps in-stack
#      traffic off any proxy; the WARNING only names the offending setting
#      (host:port, credentials stripped) and points at
#      docs/deploy/push-sygnal.md §11 (主机有代理时).
#   1. create server/compose/.env from .env.example, replacing every
#      <change-me> with a locally generated random value (openssl rand -hex 32).
#      When .env already exists, keep every current value but:
#      - append keys that .env.example has and .env lacks (new keys from
#        upgrades), generating their <change-me> values the same way;
#      - replace values that are still the literal '<change-me>' placeholder
#        for keys .env.example marks as secrets, so a hand-copied .env.example
#        never keeps placeholder secrets around;
#   2. render server/compose/synapse/homeserver.yaml.tmpl into
#      server/compose/generated/synapse/homeserver.yaml, appending
#      synapse/ip-range-whitelist.part only while BOTVILLE_SYGNAL_IP is set
#      (the push allow is exactly that /32; empty = no whitelist at all);
#   3. render server/compose/appservice/botville-gateway.yaml.tmpl (A1.3,
#      the AI gateway's appservice registration) into
#      server/compose/generated/appservice/botville-gateway.yaml (0600 — it
#      carries the as_token, which is full control over the @ai_* namespace).
#      Its `url:` (where the homeserver pushes AS transactions) comes from
#      BOTVILLE_APPSERVICE_URL, which must be exactly `null` or
#      `http://gateway:8080`. `make up` sets it (http://gateway:8080 only with
#      GATEWAY=1 on synapse/tuwunel, null otherwise). When the variable is
#      unset (e.g. `make smoke`, which also runs init) the url already in the
#      generated file is kept, defaulting to null. When the url changes,
#      init.sh drops generated/appservice/.url-changed so `make up` restarts
#      the homeserver (it reads the registration only at startup);
#   4. render server/compose/sygnal/sygnal.yaml.tmpl into
#      server/compose/generated/sygnal/sygnal.yaml, appending app parts:
#      sygnal/mock-app.yaml.part (org.botville.mock) unless BOTVILLE_PUSH_MOCK
#      is false/0/no (default true), and — only when BOTVILLE_APNS_ENABLED is
#      truthy AND every APNs prerequisite is real — sygnal/apns-app.yaml.part
#      (issue #4, single-app plan): BOTVILLE_APNS_BUNDLE_ID must be a real
#      reverse-domain id (APNS_TOPIC defaults to it, APNS_APP_ID to
#      "<bundle-id>.apns"), APNS_KEY_ID/APNS_TEAM_ID real 10-char uppercase
#      alphanumeric ids, APNS_PLATFORM sandbox|production, and
#      server/compose/secrets/apns.p8 a non-empty regular file with mode
#      600/400 (checked via stat only — the key's contents are never read).
#      Missing/placeholder prerequisites skip the APNs app with a WARNING
#      (the mock pushkin stays usable); values that are present but invalid,
#      or a key file with wrong permissions, abort with exit 1 (the error
#      names the field and the rule, never the value); with no app at all
#      init.sh warns (sygnal could deliver nothing);
#   5. render generated/push-denylist.env via scripts/render_push_denylist.py:
#      the tuwunel v1.9.4 upstream default IP denylist (incl. 0.0.0.0/8 and
#      ::/128) minus the sygnal container's /32 (BOTVILLE_SYGNAL_IP; empty =
#      no carve-out). The script also validates BOTVILLE_DOCKER_SUBNET /
#      BOTVILLE_SYGNAL_IP and exits with a friendly error (no traceback).
#
# All outputs (.env and everything under server/compose/generated/) are
# gitignored and must never be committed. Existing .env files are kept as-is
# so user secrets survive re-runs; *_IMAGE values that drifted from the repo
# defaults only get a WARNING here (never rewritten).
set -euo pipefail
# Everything written below is secret (.env, the appservice registration with
# the as_token): create it 0600 from the start instead of chmod-ing after a
# brief world-readable window.
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_dir="${script_dir}/../compose"
env_file="${compose_dir}/.env"
env_example="${compose_dir}/.env.example"
template="${compose_dir}/synapse/homeserver.yaml.tmpl"
whitelist_template="${compose_dir}/synapse/ip-range-whitelist.part"
generated_dir="${compose_dir}/generated/synapse"
generated="${generated_dir}/homeserver.yaml"
sygnal_template="${compose_dir}/sygnal/sygnal.yaml.tmpl"
sygnal_mock_template="${compose_dir}/sygnal/mock-app.yaml.part"
sygnal_apns_template="${compose_dir}/sygnal/apns-app.yaml.part"
sygnal_generated_dir="${compose_dir}/generated/sygnal"
sygnal_generated="${sygnal_generated_dir}/sygnal.yaml"
sygnal_data_dir="${compose_dir}/generated/sygnal-data"
secrets_dir="${compose_dir}/secrets"
appservice_template="${compose_dir}/appservice/botville-gateway.yaml.tmpl"
appservice_dir="${compose_dir}/generated/appservice"
appservice_generated="${appservice_dir}/botville-gateway.yaml"


# The last line for a key wins everywhere (docker compose --env-file, the
# renderer below); .env should not carry duplicate keys in the first place.
example_value() {
    grep "^$1=" "${env_example}" | tail -n 1 || true
}

env_value() {
    sed -n "s/^$1=//p" "${env_file}" | tail -n 1 || true
}

# Normalize a boolean-ish .env value (case-insensitive): true/1/yes resolve
# to true, false/0/no to false, an unset/empty value to $2 (the default).
# Any other value prints a WARNING and resolves to true — a typo like
# BOTVILLE_APNS_ENABLED=True (once silently ignored by a strict == "true")
# must be visible, never a quiet skip.
env_flag() {
    local key="$1" default="$2" value
    value="$(env_value "${key}")"
    if [[ -z "${value}" ]]; then
        printf '%s\n' "${default}"
        return 0
    fi
    case "$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')" in
        true|1|yes) printf 'true\n' ;;
        false|0|no) printf 'false\n' ;;
        *)
            echo "WARNING: ${env_file}: ${key}=${value} is not a boolean (true/1/yes/false/0/no); treating it as true." >&2
            printf 'true\n'
            ;;
    esac
}


# True when an APNs-related .env value is usable at all: non-empty and not
# left as a '<...>' placeholder (the APNs block of .env.example ships
# <bundle-id>-style placeholders, not <change-me>). "Usable" is about
# presence only — format checks live in the APNs section below.
is_real_value() {
    [[ -n "$1" && "$1" != '<'* ]]
}

# Apple Key ID / Team ID: exactly 10 uppercase alphanumeric characters.
is_apns_id() {
    [[ "$1" =~ ^[A-Z0-9]{10}$ ]]
}

# Reverse-domain identifier (iOS Bundle ID, APNs topic): at least two
# dot-separated labels, each starting/ending alphanumeric (hyphens allowed
# inside), e.g. com.example.app.
is_reverse_domain() {
    [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]]
}


# True when the key's value in .env is still the literal '<change-me>'
# placeholder and .env.example marks the same key as a secret (also
# '<change-me>') — only those are (re)generated.
is_leftover_placeholder() {
    [[ "$(env_value "$1")" == "<change-me>" && "$(example_value "$1")" == *"<change-me>"* ]]
}

# Rewrite one KEY=VALUE line in .env: existing KEY= lines are dropped and the
# new assignment is appended (the last assignment wins everywhere anyway —
# docker compose --env-file, the renderer, env_value). Portable: no sed -i.
set_env_value() {
    local key="$1" value="$2" tmp_file
    tmp_file="${env_file}.tmp.$$"
    trap 'rm -f "${tmp_file}"' EXIT
    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${line}" != "${key}="* ]]; then
            printf '%s\n' "${line}"
        fi
    done < "${env_file}" > "${tmp_file}"
    printf '%s=%s\n' "${key}" "${value}" >> "${tmp_file}"
    mv "${tmp_file}" "${env_file}"
    trap - EXIT
}

# APPSERVICE_*_TOKEN gate (PR #99 review, blocker 2): render only valid
# registrations. An empty value or the '<change-me>' placeholder is
# regenerated randomly and written back with a notice; any other invalid
# value (not 64 lowercase hex characters) aborts. Hand-filled values are
# never silently overwritten: only the two regenerable states are rewritten,
# anything else is the operator's to fix. as_token == hs_token is rejected
# by the caller (they must differ).
ensure_appservice_token() {
    local key="$1" value
    value="$(env_value "$1")"
    if [[ -z "${value}" || "${value}" == "<change-me>" ]]; then
        if ! command -v openssl >/dev/null 2>&1; then
            echo "init.sh: openssl is required to generate ${key}" >&2
            exit 1
        fi
        set_env_value "${key}" "$(openssl rand -hex 32)"
        echo "init.sh: ${key} in ${env_file} was empty/placeholder; generated a random 64-hex value" >&2
        return 0
    fi
    if [[ "${value}" =~ ^[0-9a-f]{64}$ ]]; then
        return 0
    fi
    echo "init.sh: ${key} in ${env_file} is not 64 lowercase hex characters;" >&2
    echo "init.sh: refusing to render the appservice registration (fix the value, or empty the line to have it regenerated)" >&2
    exit 1
}


# An existing .env keeps overriding the compose image defaults, so a stale
# *_IMAGE value (older pin, bare tag without digest, or :latest) silently
# defeats the repo pin for that deployment. Warn only; the upgrade path —
# including the mandatory backup — lives in docs/ops/upgrade-images.md.
warn_image_overrides() {
    local key default_value env_line env_value
    while IFS='=' read -r key default_value; do
        env_value=""
        while IFS= read -r env_line || [[ -n "${env_line}" ]]; do
            if [[ "${env_line}" == "${key}="* ]]; then
                env_value="${env_line#"${key}="}"
                break
            fi
        done < "${env_file}"
        if [[ -z "${env_value}" || "${env_value}" == "${default_value}" ]]; then
            continue
        fi
        if [[ "${env_value}" == *':latest' ]]; then
            echo "WARNING: ${env_file}: ${key}=${env_value} follows :latest (not reproducible, not covered by CI)." >&2
        elif [[ "${env_value}" != *'@sha256:'* ]]; then
            echo "WARNING: ${env_file}: ${key}=${env_value} has no @sha256 digest; it can drift with upstream re-tags." >&2
        else
            echo "WARNING: ${env_file}: ${key} differs from the current default in ${env_example}." >&2
        fi
        echo "WARNING: ${key} is NOT updated automatically — back up, then update it per docs/ops/upgrade-images.md (§ existing deployments)." >&2
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*_IMAGE=' "${env_example}")
}


# Pure advisory check (issue #134): docker injects the host's proxy settings
# into every NEW container — from ~/.docker/config.json's "proxies" section,
# from Docker Desktop, and from the docker client's own environment. A proxy
# listening on 127.0.0.1:<port> (or localhost) is unreachable inside a
# container, where 127.0.0.1 is the container itself; e.g. a Synapse whose
# pusher honors *_PROXY then fails every push to http://sygnal:5000.
# docker-compose.yml's x-noproxy-env already pins NO_PROXY/no_proxy for all
# in-stack service names, so this only WARNS about the loopback proxy itself.
# Never fails (a missing python3 or an unreadable config.json skips the
# check silently — later steps need python3 anyway) and never blocks
# startup. Proxy URLs are echoed as host:port only: scheme, path and
# userinfo (user:password@) are stripped so no credentials reach the log.
warn_loopback_proxy() {
    if ! python3 - >&2 <<'PY'
import json
import os

WARN = (
    "WARNING: %s points at the loopback proxy %s — docker injects host"
    " proxies into new containers, where 127.0.0.1/localhost is the"
    " container itself, so containers cannot use that proxy."
)
HINT = (
    "WARNING: in-stack traffic already bypasses proxies via NO_PROXY"
    " (docker-compose.yml x-noproxy-env); a container that should use a"
    " proxy needs a container-reachable address such as"
    " http://host.docker.internal:<port> instead — see"
    " docs/deploy/push-sygnal.md §11 (主机有代理时)."
)


def strip_url(url):
    """host:port of a proxy URL: drop scheme, path and userinfo."""
    rest = url.strip().split("://", 1)[-1]
    rest = rest.split("/", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[-1]
    return rest


def is_loopback(url):
    rest = url.strip().lower().split("://", 1)[-1]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[-1]
    return rest.startswith(("127.", "localhost", "::1", "[::1]"))


def warn(source, url):
    print(WARN % (source, strip_url(url)))
    print(HINT)


for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    url = os.environ.get(var)
    if url and is_loopback(url):
        warn("environment variable %s" % var, url)

try:
    with open(
        os.path.join(os.path.expanduser("~"), ".docker", "config.json"),
        encoding="utf-8",
    ) as f:
        proxies = json.load(f).get("proxies")
except (OSError, ValueError):
    proxies = None
if isinstance(proxies, dict):
    for scope, entry in sorted(proxies.items()):
        if not isinstance(entry, dict):
            continue
        for key in ("httpProxy", "httpsProxy", "allProxy"):
            url = entry.get(key)
            if isinstance(url, str) and is_loopback(url):
                warn("~/.docker/config.json proxies.%s.%s" % (scope, key), url)
PY
    then
        : # advisory only — never let this check fail the bootstrap
    fi
}

warn_loopback_proxy

if [[ ! -f "${env_file}" ]]; then
    # openssl is only required when a secret actually has to be generated;
    # a fresh .env always does (the example ships <change-me> secrets).
    if ! command -v openssl >/dev/null 2>&1; then
        echo "init.sh: openssl is required to generate local secrets" >&2
        exit 1
    fi
    # Each secret placeholder sits on its own line; give every line a fresh
    # random value instead of reusing one secret for all of them.
    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${line}" == *'<change-me>'* ]]; then
            line="${line//<change-me>/$(openssl rand -hex 32)}"
        fi
        printf '%s\n' "${line}"
    done < "${env_example}" > "${env_file}"
    chmod 600 "${env_file}"
    echo "init.sh: created ${env_file} with locally generated secrets"
else
    # Missing keys are appended in .env.example order; current values win.
    missing_keys=()
    placeholder_keys=()
    while IFS= read -r line || [[ -n "${line}" ]]; do
        case "${line}" in ''|\#*) continue ;; esac
        [[ "${line}" == *=* ]] || continue
        key="${line%%=*}"
        if ! grep -q "^${key}=" "${env_file}"; then
            missing_keys+=("${key}")
        elif is_leftover_placeholder "${key}"; then
            placeholder_keys+=("${key}")
        fi
    done < "${env_example}"

    # openssl only matters when this run actually has to generate a secret.
    needs_secret=0
    if [[ "${#placeholder_keys[@]}" -gt 0 ]]; then
        needs_secret=1
    fi
    if [[ "${#missing_keys[@]}" -gt 0 ]]; then
        for key in "${missing_keys[@]}"; do
            [[ "$(example_value "${key}")" == *'<change-me>'* ]] && needs_secret=1
        done
    fi
    if [[ "${needs_secret}" -eq 1 ]] && ! command -v openssl >/dev/null 2>&1; then
        echo "init.sh: openssl is required to generate local secrets" >&2
        exit 1
    fi

    modified=0
    if [[ "${#placeholder_keys[@]}" -gt 0 ]]; then
        # Rewrite placeholder values through a temp file (portable: no sed -i,
        # which differs between BSD and GNU).
        tmp_file="${env_file}.tmp.$$"
        trap 'rm -f "${tmp_file}"' EXIT
        while IFS= read -r line || [[ -n "${line}" ]]; do
            case "${line}" in
                *=*)
                    key="${line%%=*}"
                    value="${line#*=}"
                    if [[ "${value}" == "<change-me>" ]] && is_leftover_placeholder "${key}"; then
                        line="${key}=$(openssl rand -hex 32)"
                    fi
                    ;;
            esac
            printf '%s\n' "${line}"
        done < "${env_file}" > "${tmp_file}"
        mv "${tmp_file}" "${env_file}"
        trap - EXIT
        modified=1
        echo "init.sh: replaced leftover '<change-me>' values in ${env_file}: ${placeholder_keys[*]}"
    fi

    if [[ "${#missing_keys[@]}" -gt 0 ]]; then
        # Guard the append below: add a newline when .env lacks a trailing one.
        if [[ -n "$(tail -c 1 "${env_file}")" ]]; then
            printf '\n' >> "${env_file}"
        fi
        for key in "${missing_keys[@]}"; do
            line="$(example_value "${key}")"
            if [[ "${line}" == *'<change-me>'* ]]; then
                line="${line//<change-me>/$(openssl rand -hex 32)}"
            fi
            printf '%s\n' "${line}" >> "${env_file}"
        done
        modified=1
        echo "init.sh: appended missing keys to ${env_file}: ${missing_keys[*]} (<change-me> values randomly generated)"
    fi

    if [[ "${modified}" -eq 1 ]]; then
        chmod 600 "${env_file}"
    else
        echo "init.sh: ${env_file} exists, keeping current values"
    fi
    warn_image_overrides
fi

# --- appservice tokens: validate BEFORE rendering anything ------------------
# Empty as_token/hs_token would render `as_token: ""`, which Synapse happily
# accepts — and then POST /register?access_token= creates @ai_* accounts with
# no auth at all (reproduced in the PR #99 review). Fail closed here instead.
ensure_appservice_token APPSERVICE_AS_TOKEN
ensure_appservice_token APPSERVICE_HS_TOKEN
if [[ "$(env_value APPSERVICE_AS_TOKEN)" == "$(env_value APPSERVICE_HS_TOKEN)" ]]; then
    echo "init.sh: APPSERVICE_AS_TOKEN and APPSERVICE_HS_TOKEN in ${env_file} must be different values" >&2
    exit 1
fi
# Unconditional: a hand-created .env may be 0644 even when nothing above
# changed it.
chmod 600 "${env_file}"

mkdir -p "${generated_dir}" "${sygnal_generated_dir}" "${appservice_dir}"
# generated/ holds 0600 secrets (rendered configs with tokens, the gateway
# smoke fixture); keep the directory itself private too. Bind mounts of its
# subpaths are resolved by the docker daemon, so containers are unaffected.
chmod 700 "${compose_dir}/generated"

# Push denylist for tuwunel/continuwuity: their outbound HTTP (pushes to
# sygnal) refuses private ranges by default. render_push_denylist.py writes
# the FULL upstream default denylist (tuwunel v1.9.4, incl. 0.0.0.0/8 and
# ::/128) minus exactly the sygnal container's /32 (BOTVILLE_SYGNAL_IP;
# empty = no carve-out at all) and validates BOTVILLE_DOCKER_SUBNET /
# BOTVILLE_DOCKER_IP_RANGE / BOTVILLE_SYGNAL_IP — bad input exits with a
# friendly one-line error instead of a Python traceback.
python3 "${script_dir}/render_push_denylist.py" \
    "${env_file}" "${compose_dir}/generated/push-denylist.env"

# Real APNs key lives here (secrets/apns.p8, mode 600, gitignored). Created
# with 700 up front so a first `up` on Linux does not leave Docker creating
# a root-owned, more permissive directory; docs/deploy/push-sygnal.md §3
# then only has to copy the key in.
mkdir -p "${secrets_dir}"
chmod 700 "${secrets_dir}"
# docker-compose.yml bind-mounts the key as a single file (./secrets/apns.p8
# with create_host_path: false), so `up` with the push profile fails outright
# when the file is absent. Keep an empty placeholder (0600) whenever no key
# file exists yet; the APNs gate below only accepts a NON-EMPTY regular file,
# so the placeholder is never mistaken for a real key.
if [[ ! -e "${secrets_dir}/apns.p8" ]]; then
    : > "${secrets_dir}/apns.p8"
    chmod 600 "${secrets_dir}/apns.p8"
fi

# Scratch dir for the mock pushkin's JSONL, bind-mounted at /data. The sygnal
# image runs as root (verified against the pinned v0.17.0), so 755 is enough
# (it only ever holds mock push records for tests; gitignored like the rest
# of generated/).
mkdir -p "${sygnal_data_dir}"
chmod 0755 "${sygnal_data_dir}"

# The renderer reads .env itself (no shell sourcing), so values with special
# characters cannot break anything.
render_template() {
    python3 - "${env_file}" "$1" "$2" <<'PY'
import re
import sys

env_path, template_path, out_path = sys.argv[1:4]

values = {}
# Appservice registration url (resolved and validated by init.sh in bash,
# never read from .env): `null` or `http://gateway:8080`.
import os
_appservice_url = os.environ.get("BOTVILLE_APPSERVICE_URL_RESOLVED", "null")
with open(env_path, encoding="utf-8") as f:
    for n, raw in enumerate(f, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key:
            sys.exit(f"init.sh: {env_path}:{n}: not a KEY=VALUE line: {raw!r}")
        values[key] = value

# Derived value for the appservice namespace regex (A1.3): escape every regex
# metacharacter in the server name so e.g. "example.com" cannot also match
# "examplexcom". [A-Za-z0-9_] stay as-is (re.escape semantics). The server
# name is validated and lowercased first: Matrix server names are case-
# insensitive (Synapse lowercases its own config), so a mixed-case value in
# .env must not fork the @ai_* namespace between homeserver and gateway.
_dns_label = re.compile(r"^[0-9a-z]([0-9a-z-]*[0-9a-z])?$")


def validate_server_name(raw):
    """Valid Matrix server name (hostname or IP, optional :port), lowercased.

    Exits with a message when invalid so a broken namespace regex can never
    be rendered."""
    name = raw.strip().lower()

    def bad():
        sys.exit(
            "init.sh: BOTVILLE_SERVER_NAME must be a Matrix server name "
            f"(hostname or IP, optionally with :port), got {raw!r}"
        )

    port = None
    host = name
    if name.startswith("["):
        end = name.find("]")
        if end < 0:
            bad()
        host = name[: end + 1]
        rest = name[end + 1:]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                bad()
            port = int(rest[1:])
        if not re.fullmatch(r"\[[0-9a-f:.]+\]", host):
            bad()
    else:
        left, sep, maybe_port = name.rpartition(":")
        if sep:
            host = left
            if not maybe_port.isdigit():
                bad()
            port = int(maybe_port)
        if not host or len(host) > 253:
            bad()
        for label in host.split("."):
            # DNS labels and IPv4 segments alike.
            if not label or len(label) > 63 or not _dns_label.fullmatch(label):
                bad()
    if port is not None and not 1 <= port <= 65535:
        bad()
    return name


values["BOTVILLE_APPSERVICE_URL"] = _appservice_url

server_name = values.get("BOTVILLE_SERVER_NAME")
if server_name:
    server_name = validate_server_name(server_name)
    values["BOTVILLE_SERVER_NAME"] = server_name
    values["BOTVILLE_SERVER_NAME_REGEX"] = re.escape(server_name)

with open(template_path, encoding="utf-8") as f:
    template = f.read()


def substitute(match):
    var = match.group(1)
    if var not in values:
        sys.exit(
            f"init.sh: template references ${{{var}}} which is not set in {env_path}"
        )
    return values[var]


rendered = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, template)

leftover = re.findall(r"\$\{[^}]*\}", rendered)
if leftover:
    sys.exit(f"init.sh: unsubstituted placeholders remain in {out_path}: {leftover}")

# Belt and braces (PR #99 review, blocker 2): refuse to write any rendered
# registration whose as_token/hs_token is empty or malformed.
for line in rendered.splitlines():
    key, sep, rest = line.partition(":")
    if sep and key.strip() in ("as_token", "hs_token"):
        token = rest.strip().strip('"').strip("'")
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            sys.exit(
                f"init.sh: {out_path}: {key.strip()} is empty/invalid; "
                "refusing to write the registration"
            )

with open(out_path, "w", encoding="utf-8") as f:
    f.write(rendered)
print(f"init.sh: rendered {out_path}")
PY
}

render_template "${template}" "${generated}"
# Synapse's ip_range_whitelist is appended (not templated) so it can be
# omitted entirely: with BOTVILLE_SYGNAL_IP empty no private range is
# allow-listed (production with a public push gateway). render_push_denylist
# above has already validated the value by this point.
if [[ -n "$(env_value BOTVILLE_SYGNAL_IP)" ]]; then
    whitelist_part="${compose_dir}/generated/.ip-range-whitelist.part"
    render_template "${whitelist_template}" "${whitelist_part}"
    cat "${whitelist_part}" >> "${generated}"
    rm -f "${whitelist_part}"
fi

# Registration url (A2.2 + #192): only `null` or `http://gateway:8080` (no
# path prefix: the gateway serves /_matrix/app/v1/* at the root even with
# GATEWAY_HTTP_PATH_PREFIX set). Explicit BOTVILLE_APPSERVICE_URL wins;
# otherwise keep what the generated file already has, defaulting to null.
appservice_url_marker="${appservice_dir}/.url-changed"
appservice_url_prev="null"
if [[ -f "${appservice_generated}" ]]; then
    prev_line="$(sed -n '/^url:[[:space:]]*/{s///p;q;}' "${appservice_generated}")"
    case "${prev_line}" in
        null|http://gateway:8080) appservice_url_prev="${prev_line}" ;;
    esac
fi
if [[ -n "${BOTVILLE_APPSERVICE_URL+x}" ]]; then
    case "${BOTVILLE_APPSERVICE_URL}" in
        null|http://gateway:8080) appservice_url="${BOTVILLE_APPSERVICE_URL}" ;;
        *)
            echo "init.sh: BOTVILLE_APPSERVICE_URL must be exactly 'null' or 'http://gateway:8080'" >&2
            exit 1
            ;;
    esac
else
    appservice_url="${appservice_url_prev}"
fi
BOTVILLE_APPSERVICE_URL_RESOLVED="${appservice_url}" \
    render_template "${appservice_template}" "${appservice_generated}"
if [[ "${appservice_url}" != "${appservice_url_prev}" ]]; then
    : > "${appservice_url_marker}"
    echo "init.sh: appservice registration url changed (${appservice_url_prev} -> ${appservice_url});" \
        "restart the homeserver to apply (make up does this)"
fi
echo "init.sh: appservice registration url: ${appservice_url}"
# The registration file carries the as_token (full control over the @ai_*
# namespace); keep it 0600 like .env. The homeserver containers read it as
# root through the read-only bind mount.
chmod 600 "${appservice_generated}"

render_template "${sygnal_template}" "${sygnal_generated}"

# --- optional sygnal apps (A1.2) ----------------------------------------
# App parts are appended under the top-level `apps:` mapping of sygnal.yaml
# (part templates' first app key is indented accordingly) and render to
# dotfiles outside the /config bind mount so sygnal never sees a stray
# half-finished config file.

# The org.botville.mock test pushkin: on by default (BOTVILLE_PUSH_MOCK),
# off for a production sygnal that only serves the real APNs app.
apps_rendered=0
if [[ "$(env_flag BOTVILLE_PUSH_MOCK true)" == "true" ]]; then
    mock_part="${compose_dir}/generated/.mock-app.part"
    render_template "${sygnal_mock_template}" "${mock_part}"
    cat "${mock_part}" >> "${sygnal_generated}"
    rm -f "${mock_part}"
    apps_rendered=1
fi

# The APNs app (issue #4, single-app plan: one APNs app, sandbox↔production
# switched via APNS_PLATFORM + re-run). Rendered only when BOTVILLE_APNS_ENABLED
# is truthy AND everything real is in place. Two failure classes, on purpose:
#   - MISSING/placeholder prerequisites (incl. no real key file): WARNING +
#     skip the APNs app — a half-configured app would make sygnal fail to
#     start and take the mock pushkin down with it;
#   - PRESENT-but-invalid values: exit 1 — a typo'd Key ID, a bad platform,
#     an APNS_APP_ID that breaks the "<bundle-id>.apns" convention, or a key
#     file with permissions wider than 600/400 is never silently skipped.
# Error messages name the field and the rule only, never the value (the IDs
# are not secrets, but echoing them buys nothing and trains bad habits).
if [[ "$(env_flag BOTVILLE_APNS_ENABLED false)" == "true" ]]; then
    missing=()

    bundle_id="$(env_value BOTVILLE_APNS_BUNDLE_ID)"
    if ! is_real_value "${bundle_id}"; then
        missing+=("BOTVILLE_APNS_BUNDLE_ID")
    elif ! is_reverse_domain "${bundle_id}"; then
        echo "init.sh: BOTVILLE_APNS_BUNDLE_ID in ${env_file} must be a reverse-domain id (like com.example.app); refusing to continue." >&2
        exit 1
    fi

    for key in APNS_KEY_ID APNS_TEAM_ID; do
        value="$(env_value "${key}")"
        if ! is_real_value "${value}"; then
            missing+=("${key}")
        elif ! is_apns_id "${value}"; then
            echo "init.sh: ${key} in ${env_file} must be exactly 10 uppercase alphanumeric characters (the Apple Key ID / Team ID format); refusing to continue." >&2
            exit 1
        fi
    done

    platform="$(env_value APNS_PLATFORM)"
    if ! is_real_value "${platform}"; then
        missing+=("APNS_PLATFORM")
    elif [[ "${platform}" != "sandbox" && "${platform}" != "production" ]]; then
        echo "init.sh: APNS_PLATFORM in ${env_file} must be sandbox or production; refusing to continue." >&2
        exit 1
    fi

    apns_key_file="${secrets_dir}/apns.p8"
    apns_key_real=0
    if [[ -e "${apns_key_file}" && ! -f "${apns_key_file}" ]]; then
        echo "init.sh: ${apns_key_file} exists but is not a regular file; remove it and place the APNs key there as a regular file." >&2
        exit 1
    fi
    if [[ -f "${apns_key_file}" ]]; then
        # stat only — the key's contents are never read, hashed or echoed
        # here (mode/size are metadata, not key material). GNU stat first
        # (on Linux `stat -f` means filesystem status and would succeed with
        # the wrong data), BSD stat as fallback.
        key_mode="$(stat -c '%a' "${apns_key_file}" 2>/dev/null || stat -f '%Lp' "${apns_key_file}" 2>/dev/null || echo '')"
        key_size="$(stat -c '%s' "${apns_key_file}" 2>/dev/null || stat -f '%z' "${apns_key_file}" 2>/dev/null || echo 0)"
        if [[ "${key_size}" =~ ^[0-9]+$ && "${key_size}" -gt 0 ]]; then
            if [[ "${key_mode}" != "600" && "${key_mode}" != "400" ]]; then
                echo "init.sh: ${apns_key_file} must have mode 600 or 400 (found ${key_mode}); refusing to continue." >&2
                exit 1
            fi
            apns_key_real=1
        fi
        # size 0: the placeholder kept around for the compose bind mount —
        # no real key placed yet, falls through to the missing list below.
    fi
    if [[ "${apns_key_real}" -eq 0 ]]; then
        missing+=("secrets/apns.p8 (place the real key there, mode 600)")
    fi

    if [[ "${#missing[@]}" -gt 0 ]]; then
        echo "WARNING: BOTVILLE_APNS_ENABLED=true but ${missing[*]} missing or placeholder." >&2
        echo "WARNING: the APNs app is NOT rendered; sygnal runs with the mock pushkin only." >&2
        echo "WARNING: see docs/deploy/push-sygnal.md (place the real key, fill APNS_* in .env)." >&2
    else
        # APNS_TOPIC defaults to the bundle id; APNS_APP_ID must be
        # "<bundle-id>.apns" (derived when left empty/placeholder).
        topic="$(env_value APNS_TOPIC)"
        if ! is_real_value "${topic}"; then
            set_env_value APNS_TOPIC "${bundle_id}"
            echo "init.sh: APNS_TOPIC in ${env_file} was empty/placeholder; derived from BOTVILLE_APNS_BUNDLE_ID" >&2
        elif ! is_reverse_domain "${topic}"; then
            echo "init.sh: APNS_TOPIC in ${env_file} must be a reverse-domain id (like com.example.app); refusing to continue." >&2
            exit 1
        fi

        app_id="$(env_value APNS_APP_ID)"
        if ! is_real_value "${app_id}"; then
            set_env_value APNS_APP_ID "${bundle_id}.apns"
            echo "init.sh: APNS_APP_ID in ${env_file} was empty/placeholder; derived as BOTVILLE_APNS_BUNDLE_ID with .apns appended" >&2
        elif [[ "${app_id}" != "${bundle_id}.apns" ]]; then
            echo "init.sh: APNS_APP_ID in ${env_file} must equal BOTVILLE_APNS_BUNDLE_ID with .apns appended (single-app convention, issue #4); refusing to continue." >&2
            exit 1
        fi

        apns_part="${compose_dir}/generated/.apns-app.part"
        render_template "${sygnal_apns_template}" "${apns_part}"
        cat "${apns_part}" >> "${sygnal_generated}"
        rm -f "${apns_part}"
        apps_rendered=1
        echo "init.sh: rendered APNs app into ${sygnal_generated}"
    fi
fi

if [[ "${apps_rendered}" -eq 0 ]]; then
    echo "WARNING: generated sygnal.yaml has no apps at all (BOTVILLE_PUSH_MOCK off, no APNs app)." >&2
    echo "WARNING: sygnal cannot deliver anything as rendered; see docs/deploy/push-sygnal.md." >&2
fi

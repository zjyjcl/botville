#!/usr/bin/env bash
# Botville server bootstrap (A0.2):
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
#      carries the as_token, which is full control over the @ai_* namespace);
#   4. render server/compose/sygnal/sygnal.yaml.tmpl into
#      server/compose/generated/sygnal/sygnal.yaml, appending app parts:
#      sygnal/mock-app.yaml.part (org.botville.mock) unless BOTVILLE_PUSH_MOCK
#      is false/0/no (default true), and — only when BOTVILLE_APNS_ENABLED is
#      truthy AND every APNS_* value is real (no <placeholder>) AND
#      server/compose/secrets/apns.p8 exists — sygnal/apns-app.yaml.part.
#      With APNs prerequisites missing the APNs app is skipped with a WARNING;
#      with no app at all init.sh warns (sygnal could deliver nothing);
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
# "examplexcom". The server name is validated and lowercased first.
_dns_label = re.compile(r"^[0-9a-z]([0-9a-z-]*[0-9a-z])?$")


def validate_server_name(raw):
    """Valid Matrix server name (hostname or IP, optional :port), lowercased."""
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
            if not label or len(label) > 63 or not _dns_label.fullmatch(label):
                bad()
    if port is not None and not 1 <= port <= 65535:
        bad()
    return name


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

render_template "${appservice_template}" "${appservice_generated}"
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

# The APNs app: only rendered when BOTVILLE_APNS_ENABLED is truthy AND
# everything real is in place. A half-configured APNs app (placeholder key
# ids, missing .p8) would make sygnal fail to start and take the mock
# pushkin down with it, so it is skipped with a WARNING instead — see
# docs/deploy/push-sygnal.md.
if [[ "$(env_flag BOTVILLE_APNS_ENABLED false)" == "true" ]]; then
    missing=()
    for key in APNS_APP_ID APNS_KEY_ID APNS_TEAM_ID APNS_TOPIC; do
        value="$(env_value "${key}")"
        # Empty or still a <placeholder> (incl. <change-me>): not usable.
        if [[ -z "${value}" || "${value}" == '<'* ]]; then
            missing+=("${key}")
        fi
    done
    platform="$(env_value APNS_PLATFORM)"
    if [[ "${platform}" != "sandbox" && "${platform}" != "production" ]]; then
        missing+=("APNS_PLATFORM (must be sandbox or production)")
    fi
    apns_key_file="${secrets_dir}/apns.p8"
    if [[ ! -f "${apns_key_file}" ]]; then
        missing+=("secrets/apns.p8")
    fi
    if [[ "${#missing[@]}" -gt 0 ]]; then
        echo "WARNING: BOTVILLE_APNS_ENABLED=true but ${missing[*]} missing or placeholder." >&2
        echo "WARNING: the APNs app is NOT rendered; sygnal runs with the mock pushkin only." >&2
        echo "WARNING: see docs/deploy/push-sygnal.md (place the real key, fill APNS_* in .env)." >&2
    else
        apns_part="${compose_dir}/generated/.apns-app.part"
        render_template "${sygnal_apns_template}" "${apns_part}"
        cat "${apns_part}" >> "${sygnal_generated}"
        rm -f "${apns_part}"
        apps_rendered=1
        echo "init.sh: rendered APNs app ($(env_value APNS_APP_ID)) into ${sygnal_generated}"
    fi
fi

if [[ "${apps_rendered}" -eq 0 ]]; then
    echo "WARNING: generated sygnal.yaml has no apps at all (BOTVILLE_PUSH_MOCK off, no APNs app)." >&2
    echo "WARNING: sygnal cannot deliver anything as rendered; see docs/deploy/push-sygnal.md." >&2
fi

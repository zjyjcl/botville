#!/usr/bin/env bash
# Gateway preflight (run by `make up GATEWAY=1` and `make config` before any
# compose call): fail early on gateway settings compose would only trip over
# at `up` time -- or worse, silently "fix".
#
#   - BOTVILLE_GATEWAY_PERSONAS_DIR (default ./gateway-personas, relative to
#     server/compose like compose itself) must be an existing directory. With
#     the short bind syntax docker would otherwise create a missing host path
#     as an empty root-owned directory and the gateway would start with no
#     personas.
#   - GATEWAY_PERSONAS_FILE under /etc/botville-gateway/ whose file is missing
#     in that directory only gets a warning (the gateway decides how to treat
#     it; the AI stays silent without personas).
#
# Value precedence matches docker compose: the shell environment wins over the
# env file, and the last assignment in the env file wins. The env file is read
# the way compose's dotenv parser reads it: an optional `export ` prefix,
# optional blanks around `=`, "double" or 'single' quotes stripped, and for an
# unquoted value an inline ` # comment` (a space right before #) and trailing
# blanks dropped -- checked against `docker compose config`. Writes nothing; prints paths only, never any other .env value.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_dir="$(cd "${script_dir}/../compose" && pwd)"
env_file="${1:-${BOTVILLE_ENV_FILE:-${compose_dir}/.env}}"

dotenv_unquote() { # dotenv_unquote RAW -> value as compose reads it
    local raw="$1" v
    v="${raw#"${raw%%[![:space:]]*}"}"             # leading blanks
    case "${v}" in
        \"*)
            v="${v#\"}"
            printf '%s' "${v%%\"*}" ;;
        \'*)
            v="${v#\'}"
            printf '%s' "${v%%\'*}" ;;
        *)
            # Like compose: cut at the FIRST " #" (space + #) after the
            # leading blanks; `KEY=#x`, `KEY= #x`, `a#b`, tab+# stay literal.
            v="${v%% #*}"
            printf '%s' "${v%"${v##*[![:space:]]}"}" ;;  # trailing blanks
    esac
}

env_file_value() { # env_file_value KEY -> last assignment in the env file
    [[ -f "${env_file}" ]] || return 0
    local line raw found="" value=""
    while IFS= read -r line || [[ -n "${line}" ]]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        [[ "${line}" == export[[:space:]]* ]] && line="${line#export}" \
            && line="${line#"${line%%[![:space:]]*}"}"
        if [[ "${line}" =~ ^$1[[:space:]]*=(.*)$ ]]; then
            raw="${BASH_REMATCH[1]}"
            found=1
        fi
    done < "${env_file}"
    [[ -n "${found}" ]] && value="$(dotenv_unquote "${raw}")"
    printf '%s' "${value}"
}

value_of() { # value_of KEY DEFAULT
    local key="$1" default="$2" value
    if [[ -n "${!key+x}" ]]; then
        value="${!key}"
    else
        value="$(env_file_value "${key}")"
    fi
    printf '%s' "${value:-${default}}"
}

personas_dir="$(value_of BOTVILLE_GATEWAY_PERSONAS_DIR ./gateway-personas)"
case "${personas_dir}" in
    \~) host_dir="${HOME}" ;;
    \~/*) host_dir="${HOME}/${personas_dir#\~/}" ;;
    /*) host_dir="${personas_dir}" ;;
    *) host_dir="${compose_dir}/${personas_dir#./}" ;;
esac

if [[ ! -d "${host_dir}" ]]; then
    echo "gateway-preflight: BOTVILLE_GATEWAY_PERSONAS_DIR=${personas_dir} is not an existing directory (resolved: ${host_dir})." >&2
    echo "gateway-preflight: create it (read-only mounted at /etc/botville-gateway) or unset the variable to use server/compose/gateway-personas." >&2
    exit 2
fi

personas_file="$(value_of GATEWAY_PERSONAS_FILE "")"
case "${personas_file}" in
    /etc/botville-gateway/*)
        rel="${personas_file#/etc/botville-gateway/}"
        if [[ ! -f "${host_dir}/${rel}" ]]; then
            echo "gateway-preflight: WARNING: GATEWAY_PERSONAS_FILE=${personas_file} but ${host_dir}/${rel} does not exist; the AI will not speak without personas." >&2
        fi
        ;;
esac

echo "gateway-preflight: OK (personas dir ${host_dir})"

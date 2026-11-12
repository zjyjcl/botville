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
# env file, and the last assignment in the env file wins. Prints paths only,
# never any other .env value.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_dir="$(cd "${script_dir}/../compose" && pwd)"
env_file="${1:-${BOTVILLE_ENV_FILE:-${compose_dir}/.env}}"

env_file_value() { # env_file_value KEY -> last assignment in the env file
    [[ -f "${env_file}" ]] || return 0
    sed -n "s/^$1=//p" "${env_file}" | tail -n 1
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

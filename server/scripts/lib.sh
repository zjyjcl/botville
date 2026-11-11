#!/usr/bin/env bash
# Shared helpers for the Botville server scripts (A1.1). This file is
# SOURCED, never executed:
#
#   script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=lib.sh
#   source "${script_dir}/lib.sh"
#
# It only defines functions/variables (no side effects). Callers are expected
# to cd to $(BOTVILLE_REPO_ROOT) before running the compose default (paths in
# it are repo-root relative, like the Makefile's).

# Repo root (parent of server/scripts) and the stack's env file, resolved
# from this file's location so they work from any cwd.
BOTVILLE_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOTVILLE_ENV_FILE="${BOTVILLE_REPO_ROOT}/server/compose/.env"

# Run the compose command selected by the caller's COMPOSE variable (see
# default_compose). Callers must set COMPOSE before using this.
compose() {
    # shellcheck disable=SC2086  # COMPOSE is an intentional word-split prefix
    ${COMPOSE} "$@"
}

# Last value of KEY from an arbitrary env file (empty if unset/no file).
# Last assignment wins — same as docker compose --env-file and init.sh's
# env_value (issue #110 / PR #108 nit). Duplicate keys in .env must not make
# restore's instance check disagree with the project compose actually uses.
env_value_from() { # env_value_from <file> <KEY>
    sed -n "s/^$2=//p" "$1" | tail -n 1
}

# Value of KEY from server/compose/.env (empty if unset/no file); last
# assignment wins (see env_value_from).
env_value() {
    env_value_from "${BOTVILLE_ENV_FILE}" "$1"
}

# .env value usable as a quoted SQL identifier (defaults mirror compose).
identifier_value() {
    local value
    value="$(env_value "$1")"
    value="${value:-$2}"
    if [[ ! "${value}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "${0##*/}: ${1} must be a plain identifier, got: ${value}" >&2
        return 1
    fi
    printf '%s' "${value}"
}

# Compose project name for this checkout: COMPOSE_PROJECT_NAME from .env,
# else botville. Must be unique per instance on a machine — it prefixes every
# container/volume/network (<project>_synapse-data etc.), and a duplicate
# makes compose treat two checkouts as one stack (down -v deletes the shared
# volumes). Validated because it is used with -p.
project_name() {
    local name
    name="$(env_value COMPOSE_PROJECT_NAME)"
    name="${name:-botville}"
    if [[ ! "${name}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
        echo "${0##*/}: COMPOSE_PROJECT_NAME must start with a lowercase letter/digit and use only [a-z0-9_-], got: ${name}" >&2
        return 1
    fi
    printf '%s' "${name}"
}

# Default compose command prefix: explicit -p project (wins over the compose
# file's built-in name and anything in the ambient environment), repo compose
# file, .env, synapse profile. Mirrors the Makefile.
default_compose() {
    printf 'docker compose -p %s -f server/compose/docker-compose.yml --env-file server/compose/.env --profile synapse' "$(project_name)"
}

# sha256 hex digest of a file; openssl is the documented fallback (the manual
# lists openssl, not sha256sum/shasum, as a prerequisite).
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d" " -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d" " -f1
    else
        # "SHA2-256 (file) = <hex>" (OpenSSL 3) / "SHA256(file)= <hex>" (1.x):
        # the digest is always the last whitespace-separated field.
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    fi
}

# Octal mode of a file/directory, portable across GNU (stat -c) and
# BSD/macOS (stat -f) coreutils.
file_mode() {
    if stat -c %a "$1" >/dev/null 2>&1; then
        stat -c %a "$1"
    else
        stat -f %Lp "$1"
    fi
}

# Wait until `compose exec postgres pg_isready` succeeds (120s budget).
# Requires the caller's compose() plus POSTGRES_USER/POSTGRES_DB variables.
wait_for_postgres() {
    local ready=0
    for _ in $(seq 1 60); do
        if compose exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 2
    done
    if [[ "${ready}" -ne 1 ]]; then
        echo "${0##*/}: postgres not ready after 120s" >&2
        return 1
    fi
}

# Named volume for gateway local data (A2.2 / #7). Compose declares
# `gateway-data` but no service mounts it yet; docker names it
# <project>_gateway-data. Used by backup-synapse.sh / restore-synapse.sh.
gateway_data_volume() {
    printf '%s_gateway-data' "$(project_name)"
}

docker_volume_exists() {
    docker volume inspect "$1" >/dev/null 2>&1
}

# Create the volume with compose labels so `docker compose down -v` removes it.
# No-op if it already exists (labels on an existing volume are left as-is).
ensure_gateway_data_volume() {
    local vol
    vol="$(gateway_data_volume)"
    if docker_volume_exists "${vol}"; then
        return 0
    fi
    docker volume create \
        --label "com.docker.compose.project=$(project_name)" \
        --label "com.docker.compose.volume=gateway-data" \
        "${vol}" >/dev/null
}

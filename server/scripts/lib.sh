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

# Same prefix with --profile gateway instead of synapse, for the gateway-data
# scripts that run on any homeserver profile (backup-gateway-data.sh /
# restore-gateway-data.sh): their one-off container is the gateway service.
default_gateway_compose() {
    printf 'docker compose -p %s -f server/compose/docker-compose.yml --env-file server/compose/.env --profile gateway' "$(project_name)"
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

# --- gateway-data volume (A2.2 / #7) ---------------------------------------
# Compose declares `gateway-data`; the gateway service (optional `gateway`
# profile) mounts it at /var/lib/botville-gateway. Docker names it
# <project>_gateway-data. Shared by backup-synapse.sh / restore-synapse.sh
# (synapse profile) and backup-gateway-data.sh / restore-gateway-data.sh (any
# profile) so every homeserver profile archives/restores it the same way.
#
# Deletion: `make down` never passes -v. `docker compose ... --profile
# gateway down -v` deletes the volume (whether or not a gateway container
# is running); without the gateway profile active compose does not consider
# it in use and down -v leaves it. `docker volume rm -f <project>_gateway-data`
# always works.

# uid/gid the gateway image runs as (gateway/Dockerfile USER). Restored
# files are chowned to it so the non-root gateway can open them.
GATEWAY_DATA_UID=10001

gateway_data_volume() {
    printf '%s_gateway-data' "$(project_name)"
}

docker_volume_exists() {
    docker volume inspect "$1" >/dev/null 2>&1
}

# Create the volume with the same labels compose would put on it, so compose
# adopts it without a "volume already exists but was not created by compose"
# warning and `down -v` with the gateway profile removes it. No-op if the
# volume already exists (labels on an existing volume are left as-is).
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

# IDs of this project's running gateway service container(s), found by
# compose labels so it works whatever profiles the caller's COMPOSE carries
# (one-off `compose run` containers are excluded). Empty when not running.
gateway_running_containers() {
    docker ps -q \
        --filter "label=com.docker.compose.project=$(project_name)" \
        --filter "label=com.docker.compose.service=gateway" \
        --filter "label=com.docker.compose.oneoff=False" \
        --filter "status=running"
}

# Shell run inside the one-off container by archive_gateway_data. Before tar,
# best-effort WAL checkpoint of TOP-LEVEL /gateway-data/*.db only (not
# recursive; the agreed path is /var/lib/botville-gateway/moments.db at the
# volume root) with the sqlite3 CLI if present, else python3's sqlite3
# module (the gateway and synapse images both ship python3). Any -wal/-shm
# left behind is chowned back to the db's owner so the non-root gateway can
# reopen it. Then a full-volume tar (everything under the root, not only
# .db/-wal/-shm), chowned to the invoking user and chmod 600 inside the
# container (it runs as root; a host-side chmod would fail).
# shellcheck disable=SC2016  # expands inside the container shell
GATEWAY_ARCHIVE_SH='for db in /gateway-data/*.db; do
    [ -f "$db" ] || continue
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" || true
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(\"PRAGMA wal_checkpoint(TRUNCATE)\"); c.close()" "$db" || true
    fi
    for side in "$db-wal" "$db-shm"; do
        [ -e "$side" ] && chown "$(stat -c %u:%g "$db")" "$side" || true
    done
done
tar -czf /backup/gateway-data.tar.gz -C /gateway-data .
chown "$HOST_UID:$HOST_GID" /backup/gateway-data.tar.gz
chmod 600 /backup/gateway-data.tar.gz'

# Shell run inside the one-off container by restore_gateway_data: empty the
# volume root, extract, then hand everything to the gateway's uid.
# shellcheck disable=SC2016  # expands inside the container shell
GATEWAY_RESTORE_SH='cd /gateway-data && find . -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -xzf /backup/gateway-data.tar.gz && chown -R "$GATEWAY_UID:$GATEWAY_UID" /gateway-data'

# archive_gateway_data <backup_dir> <runner_service>
# Writes <backup_dir>/gateway-data.tar.gz from the existing gateway-data
# volume via `compose run --rm --no-deps` on <runner_service> (any service of
# the caller's COMPOSE whose image has sh + tar: synapse for the synapse
# profile, gateway otherwise -- tuwunel/continuwuity images are OS-less).
# A running gateway container is stopped for the archive (it holds the
# SQLite files open) and started again afterwards, also on failure.
archive_gateway_data() {
    local dir="$1" runner="$2" running rc=0
    running="$(gateway_running_containers)"
    if [[ -n "${running}" ]]; then
        echo "==> stopping the gateway container briefly for a consistent gateway-data archive"
        # shellcheck disable=SC2086  # one id per word
        docker stop ${running} >/dev/null
    fi
    compose run --rm --no-deps --user 0:0 --entrypoint sh \
        -e "HOST_UID=$(id -u)" -e "HOST_GID=$(id -g)" \
        -v "$(cd "${dir}" && pwd):/backup" \
        -v "$(gateway_data_volume):/gateway-data" \
        "${runner}" \
        -c "${GATEWAY_ARCHIVE_SH}" || rc=$?
    if [[ -n "${running}" ]]; then
        echo "==> starting the gateway container again"
        # shellcheck disable=SC2086
        docker start ${running} >/dev/null || rc=1
    fi
    return "${rc}"
}

# restore_gateway_data <backup_dir> <runner_service>
# Replaces the gateway-data volume's contents with <backup_dir>/
# gateway-data.tar.gz (creating the volume with compose labels if missing).
# Same runner rules and gateway stop/start handling as archive_gateway_data.
# Callers verify the artifact (verify_gateway_data_artifact) BEFORE calling.
restore_gateway_data() {
    local dir="$1" runner="$2" running rc=0
    ensure_gateway_data_volume
    running="$(gateway_running_containers)"
    if [[ -n "${running}" ]]; then
        echo "==> stopping the gateway container for the gateway-data restore"
        # shellcheck disable=SC2086
        docker stop ${running} >/dev/null
    fi
    compose run --rm --no-deps --user 0:0 --entrypoint sh \
        -e "GATEWAY_UID=${GATEWAY_DATA_UID}" \
        -v "$(cd "${dir}" && pwd):/backup:ro" \
        -v "$(gateway_data_volume):/gateway-data" \
        "${runner}" \
        -c "${GATEWAY_RESTORE_SH}" || rc=$?
    if [[ -n "${running}" ]]; then
        echo "==> starting the gateway container again"
        # shellcheck disable=SC2086
        docker start ${running} >/dev/null || rc=1
    fi
    return "${rc}"
}

# sha256 recorded for <name> in <backup_dir>/manifest.txt (empty if absent).
manifest_sha() { # manifest_sha <backup_dir> <name>
    [[ -f "$1/manifest.txt" ]] || return 0
    # First match only, via sed's own q (no `| head`: an early-exit consumer
    # under pipefail can fail the producer with EPIPE).
    sed -n "/^\([0-9a-f]\{64\}\)  [0-9][0-9]*  $2\$/{s//\1/p;q;}" "$1/manifest.txt"
}

# verify_gateway_data_artifact <backup_dir>
# Returns 0 when <backup_dir>/gateway-data.tar.gz exists and matches the
# manifest, 1 when the backup simply has no gateway-data (old backups / the
# volume did not exist at backup time: leave the volume untouched). Exits
# non-zero on any inconsistency, BEFORE anything destructive happens: the
# file present without a manifest entry, a sha256 mismatch, or the manifest
# listing it while the file is missing. A backup without any manifest skips
# the checksum (callers warn about that).
verify_gateway_data_artifact() {
    local dir="$1" expected got
    expected="$(manifest_sha "${dir}" gateway-data.tar.gz)"
    if [[ -f "${dir}/gateway-data.tar.gz" ]]; then
        if [[ -f "${dir}/manifest.txt" && -z "${expected}" ]]; then
            echo "${0##*/}: manifest.txt has no sha256 entry for gateway-data.tar.gz" >&2
            exit 1
        fi
        if [[ -n "${expected}" ]]; then
            got="$(sha256_of "${dir}/gateway-data.tar.gz")"
            if [[ "${expected}" != "${got}" ]]; then
                echo "${0##*/}: sha256 mismatch for gateway-data.tar.gz: manifest ${expected}, file ${got}" >&2
                exit 1
            fi
            echo "==> checksum OK: gateway-data.tar.gz"
        fi
        return 0
    fi
    if [[ -n "${expected}" ]]; then
        echo "${0##*/}: manifest.txt lists gateway-data.tar.gz but the file is missing" >&2
        exit 1
    fi
    return 1
}

#!/usr/bin/env bash
# Botville Synapse backup (A1.1).
#
# Backs up the running compose stack (synapse profile) into BACKUP_DIR
# (default ./backups/<UTC timestamp>/ under the repo root):
#
#   synapse-db.dump      pg_dump -Fc snapshot, taken on the live stack via
#                        `docker compose exec -T postgres`
#   synapse-data.tar.gz  media_store + signing.key from the synapse-data
#                        volume; synapse is stopped briefly so the archive
#                        cannot drift from the dump
#   homeserver.yaml      copy of server/compose/generated/synapse/homeserver.yaml
#   env                  copy of server/compose/.env -- CONTAINS SECRETS
#   manifest.txt         backup time, images, sha256 + size of every file
#
# The whole backup directory is treated as a key store: the script runs with
# umask 077, forces BACKUP_DIR to mode 700 and every artifact to 600, and
# verifies both at the end (aborting on any deviation).
#
# If synapse was running when the backup started, it is started again at the
# end -- and pulled back up by an EXIT trap if any step fails in between.
# Run from anywhere; all relative paths resolve from the repo root.
#
# Overrides (environment):
#   COMPOSE     compose command prefix (default: docker compose with -p
#               <COMPOSE_PROJECT_NAME from .env, else botville> -f
#               server/compose/docker-compose.yml --env-file
#               server/compose/.env --profile synapse)
#   BACKUP_DIR  target directory (default: ./backups/<UTC timestamp>)
#
# On success the backup directory path is printed on the last line and
# recorded in backups/.last-backup for scripts/CI to pick up.
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "${script_dir}/lib.sh"
repo_root="${BOTVILLE_REPO_ROOT}"
cd "${repo_root}"

COMPOSE="${COMPOSE:-$(default_compose)}"
BACKUP_DIR="${BACKUP_DIR:-./backups/$(date -u +%Y%m%d-%H%M%S)}"
env_file="server/compose/.env"
generated_config="server/compose/generated/synapse/homeserver.yaml"

if [[ ! -f "${env_file}" ]]; then
    echo "backup-synapse.sh: ${env_file} not found; run 'make init' first" >&2
    exit 1
fi
if [[ ! -f "${generated_config}" ]]; then
    echo "backup-synapse.sh: ${generated_config} not found; run 'make up' first" >&2
    exit 1
fi

POSTGRES_USER="$(identifier_value POSTGRES_USER botville)"
POSTGRES_DB="$(identifier_value POSTGRES_DB synapse)"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
echo "==> backup target: ${BACKUP_DIR} (dir 700, files 600)"

# 1. Database dump on the live stack (pg_dump -Fc is transactionally
#    consistent, so synapse can keep serving while it runs).
compose up -d postgres
wait_for_postgres
images_block="$(compose images postgres synapse || true)"
compose exec -T postgres pg_dump -U "${POSTGRES_USER}" -Fc -d "${POSTGRES_DB}" \
    >"${BACKUP_DIR}/synapse-db.dump"

# 2. Quiesce synapse and archive the data volume through a one-off container
#    on the same image (no extra image needs to be pulled). The backup
#    directory is bind-mounted and tar writes the archive by path, so no
#    stdin/stdout piping is involved. The one-off container runs as root, so
#    the archive is chowned to the invoking user and chmod 600 INSIDE the
#    container (a later host-side chmod would fail on a root-owned file).
#    If any step below fails after the stop, the EXIT trap starts synapse
#    again (unless it was already down before the backup).
# Assume synapse was running when ps cannot tell (older compose without
# --status): restarting a stopped synapse is the previously documented
# behavior, so it is the safe default.
synapse_was_running=yes
if compose ps --quiet --status running synapse >/dev/null 2>&1 \
    && [[ -z "$(compose ps --quiet --status running synapse)" ]]; then
    synapse_was_running=no
fi
restart_synapse() {
    local rc=$?
    trap - EXIT
    if [[ "${synapse_was_running}" == yes ]]; then
        echo "==> backup failed; starting synapse again" >&2
        compose up -d synapse || true
    fi
    exit "${rc}"
}
echo "==> stopping synapse briefly for a consistent volume archive"
compose stop synapse
trap restart_synapse EXIT
# shellcheck disable=SC2016  # $f/$files expand inside the container shell
compose run --rm --no-deps --entrypoint sh \
    -e "HOST_UID=$(id -u)" -e "HOST_GID=$(id -g)" \
    -v "$(cd "${BACKUP_DIR}" && pwd):/backup" \
    synapse \
    -c 'cd /data && files=""; for f in media_store signing.key; do [ -e "$f" ] && files="$files $f"; done; [ -n "$files" ] || { echo "no data files found in /data" >&2; exit 1; }; tar -czf /backup/synapse-data.tar.gz $files && chown "$HOST_UID:$HOST_GID" /backup/synapse-data.tar.gz && chmod 600 /backup/synapse-data.tar.gz'

# 3. Config copies. Everything below is secret-adjacent: force mode 600
#    regardless of what umask or a pre-existing file would give.
cp "${generated_config}" "${BACKUP_DIR}/homeserver.yaml"
cp "${env_file}" "${BACKUP_DIR}/env"

# 4. Manifest: when, which images, and verifiable checksums.
{
    echo "# Botville synapse backup manifest"
    echo "backup_time_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "compose_command=${COMPOSE}"
    echo
    echo "# images:"
    echo "${images_block}"
    echo
    echo "# files (sha256  bytes  name):"
    for f in synapse-db.dump synapse-data.tar.gz homeserver.yaml env; do
        printf '%s  %s  %s\n' \
            "$(sha256_of "${BACKUP_DIR}/${f}")" \
            "$(wc -c <"${BACKUP_DIR}/${f}" | tr -d " ")" \
            "${f}"
    done
} >"${BACKUP_DIR}/manifest.txt"

chmod 600 "${BACKUP_DIR}/synapse-db.dump" \
    "${BACKUP_DIR}/synapse-data.tar.gz" \
    "${BACKUP_DIR}/homeserver.yaml" \
    "${BACKUP_DIR}/env" \
    "${BACKUP_DIR}/manifest.txt"

# 5. Self-check: the backup directory must be a real key store before the
#    script reports success.
[[ "$(file_mode "${BACKUP_DIR}")" == 700 ]] || {
    echo "backup-synapse.sh: ${BACKUP_DIR} has mode $(file_mode "${BACKUP_DIR}"), expected 700" >&2
    exit 1
}
for f in synapse-db.dump synapse-data.tar.gz homeserver.yaml env manifest.txt; do
    mode="$(file_mode "${BACKUP_DIR}/${f}")"
    [[ "${mode}" == 600 ]] || {
        echo "backup-synapse.sh: ${BACKUP_DIR}/${f} has mode ${mode}, expected 600" >&2
        exit 1
    }
done

# 6. Bring synapse back before reporting success (only if it was running;
#    a stack that was already down stays down).
if [[ "${synapse_was_running}" == yes ]]; then
    compose up -d synapse
fi
trap - EXIT

# 7. Record the path for scripts/CI (BACKUP_DIR may live outside ./backups,
#    in which case ./backups was never created above).
mkdir -p backups
printf '%s\n' "${BACKUP_DIR}" >backups/.last-backup

echo "==> backup complete: ${BACKUP_DIR} (keep it secret: it contains the .env and signing key)"
# Plain path on the last line for scripts to capture: dir=$(backup-synapse.sh | tail -n 1)
echo "${BACKUP_DIR}"

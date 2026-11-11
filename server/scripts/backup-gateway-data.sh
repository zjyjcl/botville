#!/usr/bin/env bash
# Botville gateway-data backup, any homeserver profile (A2.2 / #7).
#
# backup-synapse.sh already includes gateway-data for the synapse profile.
# tuwunel and continuwuity have no homeserver backup script, so this script
# covers the gateway-data volume on its own for them (it works with synapse
# too). Same semantics as the synapse backup's gateway-data step, through the
# same lib.sh helper (archive_gateway_data):
#
#   gateway-data.tar.gz  full-volume tar of <project>_gateway-data; a running
#                        gateway container is stopped around the archive and
#                        top-level /gateway-data/*.db get a WAL checkpoint
#                        (not recursive) first
#   manifest.txt         backup time, project, sha256 + size
#
# If the volume does not exist (the gateway profile never ran), nothing is
# written and the script exits 0 -- same "skip, don't fail" rule as
# backup-synapse.sh. The volume holds no secrets (tokens/HMAC secret live in
# server/compose/.env only); this script does NOT copy .env -- keep a copy of
# it with your other backups, or use backup-synapse.sh on synapse.
#
# The one-off container runs on the gateway service image (built locally
# from gateway/Dockerfile; tuwunel/continuwuity images are OS-less and have
# no sh/tar), so the compose command carries --profile gateway.
#
# Overrides (environment):
#   COMPOSE     compose command prefix (default: docker compose -p <project>
#               -f server/compose/docker-compose.yml --env-file
#               server/compose/.env --profile gateway)
#   BACKUP_DIR  target directory (default: ./backups/<UTC timestamp>, same
#               place as backup-synapse.sh so one retention rule covers both)
#
# On success the backup directory path is printed on the last line and
# recorded in backups/.last-backup.
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "${script_dir}/lib.sh"
cd "${BOTVILLE_REPO_ROOT}"

env_file="server/compose/.env"
if [[ ! -f "${env_file}" ]]; then
    echo "backup-gateway-data.sh: ${env_file} not found; run 'make init' first" >&2
    exit 1
fi

COMPOSE="${COMPOSE:-$(default_gateway_compose)}"
BACKUP_DIR="${BACKUP_DIR:-./backups/$(date -u +%Y%m%d-%H%M%S)}"

gateway_vol="$(gateway_data_volume)"
if ! docker_volume_exists "${gateway_vol}"; then
    echo "==> gateway-data volume ${gateway_vol} not found; skipping (optional, nothing written)"
    exit 0
fi

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
echo "==> backup target: ${BACKUP_DIR} (dir 700, files 600)"
echo "==> archiving gateway-data volume ${gateway_vol}"
archive_gateway_data "${BACKUP_DIR}" gateway

{
    echo "# Botville gateway-data backup manifest"
    echo "backup_time_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "compose_project=$(project_name)"
    echo "compose_command=${COMPOSE}"
    echo
    echo "# files (sha256  bytes  name):"
    printf '%s  %s  %s\n' \
        "$(sha256_of "${BACKUP_DIR}/gateway-data.tar.gz")" \
        "$(wc -c <"${BACKUP_DIR}/gateway-data.tar.gz" | tr -d " ")" \
        gateway-data.tar.gz
} >"${BACKUP_DIR}/manifest.txt"
chmod 600 "${BACKUP_DIR}/gateway-data.tar.gz" "${BACKUP_DIR}/manifest.txt"

[[ "$(file_mode "${BACKUP_DIR}")" == 700 ]] || {
    echo "backup-gateway-data.sh: ${BACKUP_DIR} has mode $(file_mode "${BACKUP_DIR}"), expected 700" >&2
    exit 1
}
for f in gateway-data.tar.gz manifest.txt; do
    mode="$(file_mode "${BACKUP_DIR}/${f}")"
    [[ "${mode}" == 600 ]] || {
        echo "backup-gateway-data.sh: ${BACKUP_DIR}/${f} has mode ${mode}, expected 600" >&2
        exit 1
    }
done

mkdir -p backups
printf '%s\n' "${BACKUP_DIR}" >backups/.last-backup

echo "==> gateway-data backup complete: ${BACKUP_DIR}"
echo "${BACKUP_DIR}"

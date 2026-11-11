#!/usr/bin/env bash
# Botville gateway-data restore, any homeserver profile (A2.2 / #7).
#
# usage: server/scripts/restore-gateway-data.sh <backup_dir> --yes
#
# Restores ONLY the gateway-data volume from <backup_dir>/gateway-data.tar.gz
# (made by backup-gateway-data.sh or backup-synapse.sh); the homeserver,
# its volumes and server/compose/.env are not touched. Same semantics as the
# gateway-data step of restore-synapse.sh, through the same lib.sh helpers:
#
#   - the artifact is checked against manifest.txt before anything is
#     changed (present-but-unlisted, sha256 mismatch and listed-but-missing
#     all abort);
#   - a backup WITHOUT gateway-data.tar.gz (older backups, or the volume did
#     not exist at backup time) leaves an existing volume untouched (exit 0);
#   - otherwise the volume is created with compose labels if missing, a
#     running gateway container is stopped, the volume root is emptied and
#     the archive extracted (files chowned to the gateway's uid), and the
#     gateway container is started again.
#
# Instance check: a manifest from backup-gateway-data.sh records
# compose_project=; it must match this checkout's COMPOSE_PROJECT_NAME
# (BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes overrides, as in
# restore-synapse.sh). Synapse backups carry the project in their env copy.
#
# Override (environment): COMPOSE, same default as backup-gateway-data.sh.
set -euo pipefail

usage() {
    cat <<'USAGE'
usage: server/scripts/restore-gateway-data.sh <backup_dir> --yes

Restores the gateway-data volume from a backup made by backup-gateway-data.sh
or backup-synapse.sh. Destructive for that volume only. --yes is required.
USAGE
}

backup_dir=""
confirmed=no
for arg in "$@"; do
    case "${arg}" in
    --yes) confirmed=yes ;;
    -h | --help) usage; exit 0 ;;
    -*) echo "restore-gateway-data.sh: unknown option: ${arg}" >&2; usage >&2; exit 2 ;;
    *)
        if [[ -n "${backup_dir}" ]]; then
            echo "restore-gateway-data.sh: unexpected extra argument: ${arg}" >&2
            usage >&2
            exit 2
        fi
        backup_dir="${arg}"
        ;;
    esac
done
if [[ -z "${backup_dir}" ]]; then
    usage >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "${script_dir}/lib.sh"
cd "${BOTVILLE_REPO_ROOT}"

env_file="server/compose/.env"
if [[ ! -d "${backup_dir}" ]]; then
    echo "restore-gateway-data.sh: backup directory not found: ${backup_dir}" >&2
    exit 1
fi
if [[ ! -f "${env_file}" ]]; then
    echo "restore-gateway-data.sh: ${env_file} not found; run 'make init' first" >&2
    exit 1
fi
COMPOSE="${COMPOSE:-$(default_gateway_compose)}"

if [[ "${confirmed}" != "yes" ]]; then
    cat >&2 <<MSG
restore-gateway-data.sh: refusing to run without --yes

This would REPLACE the contents of the gateway-data volume $(gateway_data_volume)
with ${backup_dir}/gateway-data.tar.gz (if present). A backup without it
leaves the volume untouched. Re-run with --yes to proceed.
MSG
    exit 2
fi

# Instance check (only when the backup says which project it came from).
current_project="$(project_name)"
backup_project=""
if [[ -f "${backup_dir}/manifest.txt" ]]; then
    backup_project="$(sed -n 's/^compose_project=//p' "${backup_dir}/manifest.txt" | tail -n 1)"
fi
if [[ -z "${backup_project}" && -f "${backup_dir}/env" ]]; then
    backup_project="$(env_value_from "${backup_dir}/env" COMPOSE_PROJECT_NAME)"
    backup_project="${backup_project:-botville}"
fi
if [[ -n "${backup_project}" && "${backup_project}" != "${current_project}" ]]; then
    if [[ "${BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE:-}" == "yes" ]]; then
        echo "restore-gateway-data.sh: WARNING: COMPOSE_PROJECT_NAME mismatch (current '${current_project}', backup '${backup_project}'); restoring anyway (BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes)" >&2
    else
        echo "restore-gateway-data.sh: COMPOSE_PROJECT_NAME mismatch: current '${current_project}', backup '${backup_project}'" >&2
        echo "restore-gateway-data.sh: refusing to restore a backup from a different instance (BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes overrides)" >&2
        exit 1
    fi
fi

if [[ ! -f "${backup_dir}/manifest.txt" && -f "${backup_dir}/gateway-data.tar.gz" ]]; then
    echo "restore-gateway-data.sh: warning: no manifest.txt in ${backup_dir}, skipping checksum verification" >&2
fi
if ! verify_gateway_data_artifact "${backup_dir}"; then
    echo "==> no gateway-data.tar.gz in backup; leaving gateway-data volume untouched"
    exit 0
fi

echo "==> restoring gateway-data volume $(gateway_data_volume)"
restore_gateway_data "${backup_dir}" gateway
echo "==> gateway-data restore complete from ${backup_dir}"

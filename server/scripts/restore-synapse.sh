#!/usr/bin/env bash
# Botville Synapse restore (A1.1).
#
# usage: server/scripts/restore-synapse.sh <backup_dir> --yes
#
# DESTRUCTIVE: drops and recreates the synapse database and replaces the
# synapse-data volume, server/compose/.env and the generated homeserver.yaml
# with the copies from <backup_dir>. If the backup contains
# gateway-data.tar.gz, the gateway-data volume is also replaced (created
# with compose labels if missing); if it does not, an existing gateway-data
# volume is left untouched. --yes is required to run; without it the
# script only prints what it would do.
#
# Steps: check the backup belongs to this instance (COMPOSE_PROJECT_NAME and
# BOTVILLE_HTTP_PORT of the current .env must match the backup's) -> verify
# the sha256 of every artifact against the manifest (when a manifest exists,
# every artifact must be listed; a missing entry aborts) -> stop synapse ->
# install the backed-up .env/homeserver.yaml -> ensure postgres is healthy
# -> DROP/CREATE DATABASE (UTF8, C locale, template0) -> pg_restore -> untar
# the volume -> start synapse -> wait for GET /health. A backup missing any
# of the four artifacts (env included) is rejected: the backed-up
# homeserver.yaml carries the DB password, so a partial restore would leave
# synapse unable to log in.
#
# On a fresh machine run `make init` first (the generated random .env is
# replaced by the backup copy before postgres is first started).
#
# Override (environment): COMPOSE, same default as backup-synapse.sh
# (docker compose with -p <COMPOSE_PROJECT_NAME from .env, else botville>);
# it is recomputed from the restored .env once installed, unless the caller
# exported COMPOSE. BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes skips the
# instance consistency check (disaster recovery onto a renamed instance).
set -euo pipefail

usage() {
    cat <<'EOF'
usage: server/scripts/restore-synapse.sh <backup_dir> --yes

Restores a backup made by backup-synapse.sh. Destructive: the current
synapse database and data volume are replaced. --yes is required.
EOF
}

backup_dir=""
confirmed=no
for arg in "$@"; do
    case "${arg}" in
    --yes) confirmed=yes ;;
    -h | --help) usage; exit 0 ;;
    -*) echo "restore-synapse.sh: unknown option: ${arg}" >&2; usage >&2; exit 2 ;;
    *)
        if [[ -n "${backup_dir}" ]]; then
            echo "restore-synapse.sh: unexpected extra argument: ${arg}" >&2
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
repo_root="${BOTVILLE_REPO_ROOT}"
cd "${repo_root}"

# COMPOSE first addresses the CURRENT stack (`compose stop` below); after the
# backup's .env is installed it is recomputed from the new file so later
# commands target the restored project. A COMPOSE exported by the caller wins
# throughout (COMPOSE_OVERRIDE tells set from unset; empty counts as unset,
# matching the :- default below).
COMPOSE_OVERRIDE="${COMPOSE-}"
COMPOSE="${COMPOSE_OVERRIDE:-$(default_compose)}"

env_file="server/compose/.env"
generated_config="server/compose/generated/synapse/homeserver.yaml"

# Checksum line from manifest.txt for the given artifact name, if present.
expected_sha() {
    [[ -f "${backup_dir}/manifest.txt" ]] || return 1
    sed -n "s/^\([0-9a-f]\{64\}\)  [0-9][0-9]*  $1\$/\1/p" "${backup_dir}/manifest.txt" | head -n 1
}

wait_for_health() {
    python3 - "$1" <<'PY'
import sys
import time
import urllib.request

url = sys.argv[1]
deadline = time.monotonic() + 300
while True:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status == 200:
                print("synapse /health OK: " + url)
                sys.exit(0)
    except Exception:
        pass
    if time.monotonic() >= deadline:
        sys.exit("synapse /health did not return 200 within 300s: " + url)
    time.sleep(3)
PY
}

# --- checks ------------------------------------------------------------

if [[ ! -d "${backup_dir}" ]]; then
    echo "restore-synapse.sh: backup directory not found: ${backup_dir}" >&2
    exit 1
fi
# All four artifacts are required: the .env and homeserver.yaml are what make
# the restored dump usable (DB password/credentials), so a backup without
# them is incomplete, not something to work around.
for f in synapse-db.dump synapse-data.tar.gz homeserver.yaml env; do
    if [[ ! -f "${backup_dir}/${f}" ]]; then
        echo "restore-synapse.sh: ${backup_dir}/${f} missing (created by backup-synapse.sh)" >&2
        exit 1
    fi
done
if [[ ! -f "${backup_dir}/manifest.txt" ]]; then
    echo "restore-synapse.sh: warning: no manifest.txt in ${backup_dir}, skipping checksum verification" >&2
fi
if [[ ! -f "${env_file}" ]]; then
    echo "restore-synapse.sh: ${env_file} not found; run 'make init' first" >&2
    exit 1
fi

if [[ "${confirmed}" != "yes" ]]; then
    cat >&2 <<EOF
restore-synapse.sh: refusing to run without --yes

This would REPLACE on this machine, using ${backup_dir}:
  - the synapse database (drop + recreate + pg_restore)
  - the synapse-data volume (media_store, signing.key)
  - the gateway-data volume (only if ${backup_dir}/gateway-data.tar.gz exists)
  - ${env_file}
  - ${generated_config}
Old backups without gateway-data.tar.gz leave any existing gateway-data volume untouched.
Re-run with --yes to proceed.
EOF
    exit 2
fi

# --- instance consistency ----------------------------------------------

# The backup must belong to THIS instance: the compose project name prefixes
# every container/volume/network, and the HTTP port feeds the health check
# below. Restoring another instance's backup would write the wrong project's
# volumes while replacing .env/homeserver.yaml with the foreign copies, so
# compare the current .env with the backup's copy before anything is touched.
# BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes skips the check for disaster
# recovery onto a renamed/re-hosted instance (the operator owns the outcome).
current_project="$(env_value_from "${env_file}" COMPOSE_PROJECT_NAME)"
current_project="${current_project:-botville}"
backup_project="$(env_value_from "${backup_dir}/env" COMPOSE_PROJECT_NAME)"
backup_project="${backup_project:-botville}"
current_port="$(env_value_from "${env_file}" BOTVILLE_HTTP_PORT)"
current_port="${current_port:-8008}"
backup_port="$(env_value_from "${backup_dir}/env" BOTVILLE_HTTP_PORT)"
backup_port="${backup_port:-8008}"

# Instance check reads .env (and the backup's env copy) only. An explicit
# COMPOSE / COMPOSE_OVERRIDE can point compose at a different -p project than
# .env describes (#110): warn so operators do not confuse "env matches" with
# "compose target matches". The check itself stays .env-scoped by design.
if [[ -n "${COMPOSE_OVERRIDE}" ]]; then
    echo "restore-synapse.sh: WARNING: COMPOSE is explicitly set; instance consistency still compares .env project/port only (compose may target a different -p). Confirm COMPOSE matches the instance you intend to restore into." >&2
fi

mismatch=0
if [[ "${current_project}" != "${backup_project}" ]]; then
    echo "restore-synapse.sh: COMPOSE_PROJECT_NAME mismatch: current '${current_project}', backup '${backup_project}'" >&2
    mismatch=1
fi
if [[ "${current_port}" != "${backup_port}" ]]; then
    echo "restore-synapse.sh: BOTVILLE_HTTP_PORT mismatch: current '${current_port}', backup '${backup_port}'" >&2
    mismatch=1
fi
if [[ "${mismatch}" -ne 0 ]]; then
    if [[ "${BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE:-}" == "yes" ]]; then
        echo "restore-synapse.sh: WARNING: BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes, restoring a cross-instance backup at the operator's request" >&2
    else
        cat >&2 <<EOF
restore-synapse.sh: refusing to restore a backup from a different instance.
Confirm this is the same instance first: align the values above in ${env_file}
with the backup (or restore on a matching checkout), or acknowledge the
rename/re-host explicitly with BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes.
EOF
        exit 1
    fi
fi

# --- restore -----------------------------------------------------------

# Verify every artifact against the manifest before destroying anything
# (manifests from older backups list the same four files). With a manifest
# present, every artifact MUST have an entry: a missing line means a truncated
# or hand-edited manifest, and silently skipping that file's check is how a
# swapped artifact goes unnoticed. Only a backup without any manifest falls
# back to skipping verification (warned about above).
for f in synapse-db.dump synapse-data.tar.gz homeserver.yaml env; do
    expected="$(expected_sha "${f}")" || expected=""
    if [[ -f "${backup_dir}/manifest.txt" && -z "${expected}" ]]; then
        echo "restore-synapse.sh: manifest.txt has no sha256 entry for ${f}" >&2
        exit 1
    fi
    [[ -n "${expected}" ]] || continue
    got="$(sha256_of "${backup_dir}/${f}")"
    if [[ "${expected}" != "${got}" ]]; then
        echo "restore-synapse.sh: sha256 mismatch for ${f}: manifest ${expected}, file ${got}" >&2
        exit 1
    fi
    echo "==> checksum OK: ${f}"
done

# Optional gateway-data artifact (A2.2 / #7). If the file is present it must
# match the manifest (when one exists); if the manifest lists it, the file
# must be present. Either way we do this before destroying anything.
if [[ -f "${backup_dir}/gateway-data.tar.gz" ]]; then
    expected="$(expected_sha gateway-data.tar.gz)" || expected=""
    if [[ -f "${backup_dir}/manifest.txt" && -z "${expected}" ]]; then
        echo "restore-synapse.sh: manifest.txt has no sha256 entry for gateway-data.tar.gz" >&2
        exit 1
    fi
    if [[ -n "${expected}" ]]; then
        got="$(sha256_of "${backup_dir}/gateway-data.tar.gz")"
        if [[ "${expected}" != "${got}" ]]; then
            echo "restore-synapse.sh: sha256 mismatch for gateway-data.tar.gz: manifest ${expected}, file ${got}" >&2
            exit 1
        fi
        echo "==> checksum OK: gateway-data.tar.gz"
    fi
elif [[ -f "${backup_dir}/manifest.txt" ]]; then
    expected="$(expected_sha gateway-data.tar.gz)" || expected=""
    if [[ -n "${expected}" ]]; then
        echo "restore-synapse.sh: manifest.txt lists gateway-data.tar.gz but the file is missing" >&2
        exit 1
    fi
fi

echo "==> stopping synapse"
compose stop synapse

# Install the backup's config copies first so every later step (postgres
# bootstrap included) runs with the backed-up credentials.
echo "==> restoring ${env_file} from backup (contains secrets, mode 600)"
cp "${backup_dir}/env" "${env_file}"
chmod 600 "${env_file}"
# The restored .env may carry a different COMPOSE_PROJECT_NAME than this
# checkout had: recompute COMPOSE so the postgres/synapse commands below
# target the backup's project (an explicit caller COMPOSE still wins).
if [[ -n "${COMPOSE_OVERRIDE}" ]]; then
    COMPOSE="${COMPOSE_OVERRIDE}"
else
    COMPOSE="$(default_compose)"
fi
echo "==> restoring ${generated_config} from backup"
mkdir -p "$(dirname "${generated_config}")"
cp "${backup_dir}/homeserver.yaml" "${generated_config}"
chmod 600 "${generated_config}"

POSTGRES_USER="$(identifier_value POSTGRES_USER botville)"
POSTGRES_DB="$(identifier_value POSTGRES_DB synapse)"
http_port="$(env_value BOTVILLE_HTTP_PORT)"
http_port="${http_port:-8008}"

echo "==> starting postgres"
compose up -d postgres
wait_for_postgres

# If the postgres volume predates the backup's .env (restore onto a server
# that kept running after the backup), the stored role password may differ
# from POSTGRES_PASSWORD. Local socket auth is trust, so reconcile it here.
pg_password="$(env_value POSTGRES_PASSWORD)"
if [[ "${pg_password}" =~ ^[A-Za-z0-9/_+=:.@-]+$ ]]; then
    compose exec -T postgres psql -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 \
        -c "ALTER USER \"${POSTGRES_USER}\" WITH PASSWORD '${pg_password}';" >/dev/null
else
    echo "restore-synapse.sh: note: POSTGRES_PASSWORD has unusual characters, skipping ALTER USER (set it manually if synapse cannot log in)" >&2
fi

echo "==> dropping and recreating database ${POSTGRES_DB} (UTF8, C locale)"
compose exec -T postgres psql -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"${POSTGRES_DB}\" WITH (FORCE);" \
    -c "CREATE DATABASE \"${POSTGRES_DB}\" WITH OWNER \"${POSTGRES_USER}\" ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;" \
    >/dev/null

echo "==> pg_restore ${POSTGRES_DB}"
compose exec -T postgres pg_restore -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --no-owner \
    <"${backup_dir}/synapse-db.dump"

echo "==> restoring synapse-data volume"
compose run --rm --no-deps --entrypoint sh \
    -v "$(cd "${backup_dir}" && pwd):/backup:ro" \
    synapse \
    -c 'cd /data && rm -rf media_store signing.key homeserver.pid && tar -xzf /backup/synapse-data.tar.gz'

if [[ -f "${backup_dir}/gateway-data.tar.gz" ]]; then
    echo "==> restoring gateway-data volume $(gateway_data_volume)"
    ensure_gateway_data_volume
    compose run --rm --no-deps --entrypoint sh \
        -v "$(cd "${backup_dir}" && pwd):/backup:ro" \
        -v "$(gateway_data_volume):/gateway-data" \
        synapse \
        -c 'cd /gateway-data && find . -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -xzf /backup/gateway-data.tar.gz'
else
    echo "==> no gateway-data.tar.gz in backup; leaving gateway-data volume untouched"
fi

echo "==> starting synapse"
compose up -d synapse
wait_for_health "http://127.0.0.1:${http_port}/health"

echo "==> restore complete from ${backup_dir}"
echo "    verify with: make e2e E2E_PHASE=verify E2E_STATE=backups/e2e-state.json"

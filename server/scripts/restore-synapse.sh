#!/usr/bin/env bash
# Botville Synapse restore (A1.1).
#
# usage: server/scripts/restore-synapse.sh <backup_dir> --yes
#
# DESTRUCTIVE: drops and recreates the synapse database and replaces the
# synapse-data volume, server/compose/.env and the generated homeserver.yaml
# with the copies from <backup_dir>. --yes is required to run; without it the
# script only prints what it would do.
#
# Steps: verify the sha256 of every artifact against the manifest (when it
# has them) -> stop synapse -> install the backed-up .env/homeserver.yaml ->
# ensure postgres is healthy -> DROP/CREATE DATABASE (UTF8, C locale,
# template0) -> pg_restore -> untar the volume -> start synapse -> wait for
# GET /health. A backup missing any of the four artifacts (env included) is
# rejected: the backed-up homeserver.yaml carries the DB password, so a
# partial restore would leave synapse unable to log in.
#
# On a fresh machine run `make init` first (the generated random .env is
# replaced by the backup copy before postgres is first started).
#
# Override (environment): COMPOSE, same default as backup-synapse.sh
# (docker compose with -p <COMPOSE_PROJECT_NAME from .env, else botville>).
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

COMPOSE="${COMPOSE:-$(default_compose)}"

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
  - ${env_file}
  - ${generated_config}
Re-run with --yes to proceed.
EOF
    exit 2
fi

# --- restore -----------------------------------------------------------

# Verify every artifact against the manifest before destroying anything
# (manifests from older backups list the same four files).
for f in synapse-db.dump synapse-data.tar.gz homeserver.yaml env; do
    if expected="$(expected_sha "${f}")" && [[ -n "${expected}" ]]; then
        got="$(sha256_of "${backup_dir}/${f}")"
        if [[ "${expected}" != "${got}" ]]; then
            echo "restore-synapse.sh: sha256 mismatch for ${f}: manifest ${expected}, file ${got}" >&2
            exit 1
        fi
        echo "==> checksum OK: ${f}"
    fi
done

echo "==> stopping synapse"
compose stop synapse

# Install the backup's config copies first so every later step (postgres
# bootstrap included) runs with the backed-up credentials.
echo "==> restoring ${env_file} from backup (contains secrets, mode 600)"
cp "${backup_dir}/env" "${env_file}"
chmod 600 "${env_file}"
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

echo "==> starting synapse"
compose up -d synapse
wait_for_health "http://127.0.0.1:${http_port}/health"

echo "==> restore complete from ${backup_dir}"
echo "    verify with: make e2e E2E_PHASE=verify E2E_STATE=backups/e2e-state.json"

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
#      server/compose/generated/synapse/homeserver.yaml.
#
# Both outputs are gitignored and must never be committed. Existing .env files
# are kept as-is so user secrets survive re-runs; *_IMAGE values that drifted
# from the repo defaults only get a WARNING here (never rewritten).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_dir="${script_dir}/../compose"
env_file="${compose_dir}/.env"
env_example="${compose_dir}/.env.example"
template="${compose_dir}/synapse/homeserver.yaml.tmpl"
generated_dir="${compose_dir}/generated/synapse"
generated="${generated_dir}/homeserver.yaml"

# The last line for a key wins everywhere (docker compose --env-file, the
# renderer below); .env should not carry duplicate keys in the first place.
example_value() {
    grep "^$1=" "${env_example}" | tail -n 1 || true
}

env_value() {
    sed -n "s/^$1=//p" "${env_file}" | tail -n 1 || true
}

# True when the key's value in .env is still the literal '<change-me>'
# placeholder and .env.example marks the same key as a secret (also
# '<change-me>') — only those are (re)generated.
is_leftover_placeholder() {
    [[ "$(env_value "$1")" == "<change-me>" && "$(example_value "$1")" == *"<change-me>"* ]]
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

mkdir -p "${generated_dir}"

# The renderer reads .env itself (no shell sourcing), so values with special
# characters cannot break anything.
python3 - "${env_file}" "${template}" "${generated}" <<'PY'
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
    sys.exit(f"init.sh: unsubstituted placeholders remain: {leftover}")

with open(out_path, "w", encoding="utf-8") as f:
    f.write(rendered)
print(f"init.sh: rendered {out_path}")
PY

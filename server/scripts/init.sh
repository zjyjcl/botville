#!/usr/bin/env bash
# Botville server bootstrap (A0.2):
#   1. create server/compose/.env from .env.example, replacing every
#      <change-me> with a locally generated random value (openssl rand -hex 32).
#      When .env already exists, keep every current value but append keys that
#      .env.example has and .env lacks (new keys from upgrades), generating
#      their <change-me> values the same way;
#   2. render server/compose/synapse/homeserver.yaml.tmpl into
#      server/compose/generated/synapse/homeserver.yaml.
#
# Both outputs are gitignored and must never be committed. Existing .env files
# are kept as-is so user secrets survive re-runs.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_dir="${script_dir}/../compose"
env_file="${compose_dir}/.env"
env_example="${compose_dir}/.env.example"
template="${compose_dir}/synapse/homeserver.yaml.tmpl"
generated_dir="${compose_dir}/generated/synapse"
generated="${generated_dir}/homeserver.yaml"

if ! command -v openssl >/dev/null 2>&1; then
    echo "init.sh: openssl is required to generate local secrets" >&2
    exit 1
fi

if [[ ! -f "${env_file}" ]]; then
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
    # Missing keys are appended in .env.example order; current values always win.
    missing_keys=()
    while IFS= read -r line || [[ -n "${line}" ]]; do
        case "${line}" in ''|\#*) continue ;; esac
        [[ "${line}" == *=* ]] || continue
        key="${line%%=*}"
        if ! grep -q "^${key}=" "${env_file}"; then
            missing_keys+=("${key}")
        fi
    done < "${env_example}"

    if [[ "${#missing_keys[@]}" -gt 0 ]]; then
        # Guard the append below: add a newline when .env lacks a trailing one.
        if [[ -n "$(tail -c 1 "${env_file}")" ]]; then
            printf '\n' >> "${env_file}"
        fi
        for key in "${missing_keys[@]}"; do
            line="$(grep "^${key}=" "${env_example}" | head -n 1)"
            if [[ "${line}" == *'<change-me>'* ]]; then
                line="${line//<change-me>/$(openssl rand -hex 32)}"
            fi
            printf '%s\n' "${line}" >> "${env_file}"
        done
        echo "init.sh: appended missing keys to ${env_file}: ${missing_keys[*]} (<change-me> values randomly generated)"
    else
        echo "init.sh: ${env_file} exists, keeping current values"
    fi
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

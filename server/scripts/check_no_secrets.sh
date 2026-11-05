#!/usr/bin/env bash
# Secret-leak guard for issue #4 (APNs push gateway).
#
# Fails (exit 1) when any of the following holds in the git work tree:
#   1. a *.p8 file (APNs auth key) is tracked by git;
#   2. any tracked file contains a PEM private-key header
#      ("-----BEGIN [RSA |EC |DSA |OPENSSH |ENCRYPTED ]PRIVATE KEY-----");
#   3. .gitignore no longer ignores *.p8 files and the secrets/ dir.
#
# The PEM header patterns are assembled at RUN TIME from fragments on
# purpose: a literal header inside this script (or its tests) would make the
# scanner match itself. Matched file paths are printed, never the matched
# line (it may be key material).
#
# Usage: check_no_secrets.sh [workdir]   (default: the repo root)
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workdir="${1:-${script_dir}/../..}"
cd "${workdir}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "check_no_secrets.sh: ${workdir} is not a git work tree" >&2
    exit 2
fi

failed=0

# 1. Tracked *.p8 files (git ls-files lists index paths; untracked local
# keys in server/compose/secrets/ are fine — that is exactly where they
# are supposed to live, ignored).
while IFS= read -r tracked; do
    [[ -n "${tracked}" ]] || continue
    echo "check_no_secrets.sh: tracked APNs key file (.p8): ${tracked}" >&2
    failed=1
done < <(git ls-files -- '*.p8')

# 2. PEM private-key headers in tracked files. Patterns built from parts.
pem_prefix="-----BEGIN "
pem_suffix="PRIVATE KEY-----"
for kind in "" "RSA " "EC " "DSA " "OPENSSH " "ENCRYPTED "; do
    pattern="${pem_prefix}${kind}${pem_suffix}"
    while IFS= read -r hit; do
        [[ -n "${hit}" ]] || continue
        # File path only — never the matched line.
        echo "check_no_secrets.sh: ${kind:-PKCS#8}private-key header found in tracked file: ${hit}" >&2
        failed=1
    done < <(git grep -I -l -F -e "${pattern}" -- . || true)
done

# 3. .gitignore must keep ignoring *.p8 and secrets/ so a key placed per
# docs/deploy/push-sygnal.md can never be committed by accident.
for probe in "server/compose/secrets/apns.p8" "anywhere.p8"; do
    if ! git check-ignore -q "${probe}"; then
        echo "check_no_secrets.sh: ${probe} is NOT ignored by .gitignore (expected *.p8 and secrets/ rules)" >&2
        failed=1
    fi
done

if [[ "${failed}" -ne 0 ]]; then
    echo "check_no_secrets.sh: FAILED — private key material must never be tracked; see docs/deploy/push-sygnal.md" >&2
    exit 1
fi
echo "check_no_secrets.sh: OK (no tracked .p8 files, no private-key headers, ignore rules in place)"

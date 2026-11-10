#!/usr/bin/env bash
# Copyright 2026 botville contributors
# SPDX-License-Identifier: Apache-2.0
#
# Self-test for scripts/sync-pairing-qr-fixtures.sh.
#
# Rebuilds a minimal repo layout in a mktemp throwaway directory (the sync
# script + the docs examples dir) and asserts the sync / --check contract:
#
#   a) sync copies every source file byte-identically EXCEPT README.md,
#      which must not exist in the destination; --check passes afterwards;
#   b) a hand-added README.md in the destination fails --check and is
#      deleted by the next sync;
#   c) a drifted fixture byte fails --check;
#   d) an extra destination file fails --check and is removed by sync;
#   e) editing the source README.md never fails --check and is never synced.
#
# Run: scripts/test-sync-pairing-qr-fixtures.sh   (exit 0 = all passed)
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
sync_script=$repo_root/scripts/sync-pairing-qr-fixtures.sh
examples=$repo_root/docs/contracts/examples/pairing-qr

[ -f "$sync_script" ] || { echo "missing sync script: $sync_script" >&2; exit 2; }
[ -d "$examples" ] || { echo "missing examples dir: $examples" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/scripts" "$work/docs/contracts/examples"
cp "$sync_script" "$work/scripts/sync-pairing-qr-fixtures.sh"
cp -a "$examples" "$work/docs/contracts/examples/pairing-qr"

# The sync script derives the repo root from its own path, so the throwaway
# layout only needs scripts/, docs/contracts/examples/pairing-qr/ and (created
# by sync) the app-apple fixture destination.
src=$work/docs/contracts/examples/pairing-qr
dest=$work/app-apple/Tests/BotvilleCoreTests/Fixtures/pairing-qr-contract
sync_in_work() { bash "$work/scripts/sync-pairing-qr-fixtures.sh" "$@"; }
expect_check_ok() {
  sync_in_work --check || die "$1 (expected --check to pass)"
}
expect_check_fail() {
  if sync_in_work --check >/dev/null 2>&1; then
    die "$1 (expected --check to fail)"
  fi
}

pass=0
step() {
  pass=$((pass + 1))
  printf 'ok %d - %s\n' "$pass" "$1"
}
die() {
  echo "FAIL: $*" >&2
  exit 1
}

# a) sync omits README.md, copies the rest byte-identically, --check passes.
sync_in_work >/dev/null
[ ! -e "$dest/README.md" ] || die "a) sync must not create $dest/README.md"
src_count=$(find "$src" -type f ! -name 'README.md' | wc -l | tr -d ' ')
dest_count=$(find "$dest" -type f | wc -l | tr -d ' ')
[ "$src_count" = "$dest_count" ] \
  || die "a) destination file count ($dest_count) != source minus README ($src_count)"
while IFS= read -r f; do
  cmp -s "$f" "$dest/${f#"$src"/}" || die "a) drifted bytes: $f"
done < <(find "$src" -type f ! -name 'README.md')
expect_check_ok "a) --check must pass right after sync"
step "sync omits README.md, copies the rest byte-identically; --check passes"

# b) a README.md in the destination is drift; sync deletes it.
printf 'hand-added readme\n' > "$dest/README.md"
expect_check_fail "b) destination README.md must fail --check"
sync_in_work >/dev/null
[ ! -e "$dest/README.md" ] || die "b) sync must delete a hand-added destination README.md"
expect_check_ok "b) --check must pass again after sync"
step "destination README.md fails --check; sync deletes it"

# c) a drifted fixture byte fails --check.
printf 'x' >> "$dest/uri-valid.txt"
expect_check_fail "c) a drifted fixture byte must fail --check"
sync_in_work >/dev/null
expect_check_ok "c) --check must pass after re-sync"
step "drifted fixture byte fails --check; re-sync restores it"

# d) an extra destination file fails --check; sync removes it.
: > "$dest/extra-file.txt"
expect_check_fail "d) an extra destination file must fail --check"
sync_in_work >/dev/null
[ ! -e "$dest/extra-file.txt" ] || die "d) sync must delete extra destination files"
expect_check_ok "d) --check must pass after sync removed the extra file"
step "extra destination file fails --check; sync removes it"

# e) source README.md edits are invisible to --check and never synced.
printf 'docs-only readme, edited\n' > "$src/README.md"
expect_check_ok "e) source README.md edits must not fail --check"
sync_in_work >/dev/null
[ ! -e "$dest/README.md" ] || die "e) the source README.md must never be synced"
step "source README.md edits never fail --check nor get synced"

echo "PASS: $pass checks — sync-pairing-qr-fixtures.sh behaves as documented"

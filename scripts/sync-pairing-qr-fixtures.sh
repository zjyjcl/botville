#!/usr/bin/env bash
# Copyright 2026 botville contributors
# SPDX-License-Identifier: Apache-2.0
#
# Sync the pairing-qr contract examples into the App test fixtures
# (#128 / #132 Option A). docs/contracts/examples/pairing-qr/ is the single
# source of truth; the App-side copy is generated — never edit it by hand.
#
#   scripts/sync-pairing-qr-fixtures.sh            # sync (rewrites the copy)
#   scripts/sync-pairing-qr-fixtures.sh --check    # CI guard: exit 1 on drift,
#                                                  # write nothing
#
# Sync is destructive on the destination (extra files are removed) so the
# destination holds the same file set as the source with identical bytes —
# except README.md, which is docs-only and never synced: its relative links
# (../../pairing-qr.md) would break on the App side. A README.md found in the
# destination is drift: --check fails, sync deletes it. See
# docs/contracts/examples/pairing-qr/README.md and pairing-qr.md §7.
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
source_dir=$repo_root/docs/contracts/examples/pairing-qr
dest_dir=$repo_root/app-apple/Tests/BotvilleCoreTests/Fixtures/pairing-qr-contract

mode=${1:-sync}
if [ "$#" -gt 1 ] || { [ "$mode" != "--check" ] && [ "$mode" != "sync" ]; }; then
  echo "usage: $0 [--check]" >&2
  exit 2
fi

[ -d "$source_dir" ] || { echo "missing source dir: $source_dir" >&2; exit 2; }

check() {
  # README.md is docs-only (relative links break on the App side), so it is
  # excluded from the diff — but it must not exist in the destination either.
  if [ -e "$dest_dir/README.md" ]; then
    echo "unexpected file: $dest_dir/README.md (README.md is docs-only;" \
      "the App-side copy must not contain it)" >&2
    return 1
  fi
  # No -N: it treats a missing file as empty, so an extra empty file on either
  # side would compare equal and slip through. Plain -ru reports "Only in"
  # for any file present on just one side, which is the file-set guard.
  diff -ru -x README.md "$source_dir" "$dest_dir"
}

if [ "$mode" = "--check" ]; then
  if check; then
    echo "OK: $dest_dir is byte-identical to $source_dir (README.md excluded," \
      "docs-only)"
    exit 0
  fi
  echo "ERROR: $dest_dir drifted from $source_dir (the docs dir is the" \
    "single source of truth)." >&2
  echo "Fix: edit docs/contracts/examples/pairing-qr/, then run" \
    "scripts/sync-pairing-qr-fixtures.sh and commit both sides together." >&2
  exit 1
fi

if check >/dev/null 2>&1; then
  echo "already in sync; refreshing the copy anyway (idempotent)"
else
  echo "drift detected; replacing the App-side copy (run with --check for details)"
fi
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cp -a "$source_dir/." "$stage/"
rm -f "$stage/README.md"  # docs-only; never lands in the App-side copy
rm -rf "$dest_dir"
mkdir -p "$dest_dir"
cp -a "$stage/." "$dest_dir/"
check >/dev/null  # must now match byte-for-byte; set -e fails otherwise
count=$(find "$dest_dir" -type f | wc -l | tr -d ' ')
echo "synced $count files: $source_dir -> $dest_dir"

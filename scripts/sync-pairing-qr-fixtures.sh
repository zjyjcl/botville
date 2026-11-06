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
# Sync is destructive on the destination (extra files are removed) so both
# directories always hold the same file set with identical bytes. See
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
  diff -ruN "$source_dir" "$dest_dir"
}

if [ "$mode" = "--check" ]; then
  if check; then
    echo "OK: $dest_dir is byte-identical to $source_dir"
    exit 0
  fi
  echo "ERROR: $dest_dir drifted from $source_dir (the docs dir is the" \
    "single source of truth)." >&2
  echo "Fix: edit docs/contracts/examples/pairing-qr/, then run" \
    "scripts/sync-pairing-qr-fixtures.sh and commit both sides together." >&2
  exit 1
fi

if check; then
  echo "already in sync; refreshing the copy anyway (idempotent)"
else
  echo "drift detected; replacing the App-side copy:"
fi
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cp -a "$source_dir/." "$stage/"
rm -rf "$dest_dir"
mkdir -p "$dest_dir"
cp -a "$stage/." "$dest_dir/"
check >/dev/null  # must now match byte-for-byte; set -e fails otherwise
count=$(find "$dest_dir" -type f | wc -l | tr -d ' ')
echo "synced $count files: $source_dir -> $dest_dir"

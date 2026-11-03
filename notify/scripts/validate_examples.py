#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Validate the botville notify domain model (D1.1) and its example payloads.

Runs fully offline. The notify layer (``notify/openapi/notify.v1.yaml``) is a
thin, additive wrapper over the gateway contract: it ``$ref``s gateway schemas
and extends them with ``allOf``. This script therefore:

1. Loads ``notify.v1.yaml`` (duplicate keys rejected) and **bundles** external
   ``$ref``s (``../../gateway/openapi/gateway.v1.yaml#/...``) by inlining them,
   so validation runs on local pointers only.
2. Checks OpenAPI 3.1 shape, Apache-2.0 license, and that no secrets are
   committed (reusing the gateway scanner).
3. Validates every file in ``notify/openapi/examples/`` against its declared
   notify schema target, then cross-checks each example against the gateway
   schema it ultimately derives from — proof that the notify layer is additive.

The JSON-Schema validator is imported from ``gateway/scripts/validate_openapi.py``
(no third-party schema library needed beyond PyYAML).

Usage:
    python3 notify/scripts/validate_examples.py

Exit code 0 on success, 1 on any failure. Requires PyYAML.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NOTIFY_OPENAPI = os.path.join(REPO_ROOT, "notify", "openapi", "notify.v1.yaml")
EXAMPLES_DIR = os.path.join(REPO_ROOT, "notify", "openapi", "examples")
GATEWAY_VALIDATOR = os.path.join(REPO_ROOT, "gateway", "scripts", "validate_openapi.py")

_spec = importlib.util.spec_from_file_location("gateway_validate_openapi", GATEWAY_VALIDATOR)
gv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gv)

yaml = gv.yaml

# Example file -> JSON pointer into notify.v1.yaml (post-bundling).
EXAMPLE_TARGETS = {
    "notification-card.json": "#/components/schemas/NotificationCard",
    "reminder-create-request.json": "#/components/schemas/ReminderCreateRequest",
    "approval-create-request.json": "#/components/schemas/ApprovalCreateRequest",
    "card-delivery-states.json": "#/components/schemas/Card",
    "digest-collapsed.json": "#/components/schemas/NotificationCard",
}

# Same example -> gateway schema it must ALSO satisfy directly (additivity check).
GATEWAY_CROSS_CHECK = {
    "notification-card.json": "#/components/schemas/Card",
    "reminder-create-request.json": "#/components/schemas/CreateCardRequest",
    "approval-create-request.json": "#/components/schemas/CreateCardRequest",
    "card-delivery-states.json": "#/components/schemas/Card",
    "digest-collapsed.json": "#/components/schemas/Card",
}


class Report:
    def __init__(self):
        self.failures = 0
        self.passes = 0

    def ok(self, label):
        self.passes += 1
        print("  \033[32mPASS\033[0m  %s" % label)

    def fail(self, label, detail=""):
        self.failures += 1
        print("  \033[31mFAIL\033[0m  %s" % label)
        if detail:
            for line in str(detail).splitlines():
                print("        %s" % line)


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.load(handle.read(), Loader=gv.DuplicateKeySafeLoader)


def bundle_external_refs(node, base_dir, cache, depth=0):
    """Inline every ``$ref`` that points at another file.

    Local pointers (``#/...``) are left untouched — the gateway validator
    resolves them against the bundled document.
    """
    if depth > 64:
        raise ValueError("external $ref nesting too deep")
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            file_part, _, pointer = ref.partition("#")
            target_path = os.path.normpath(os.path.join(base_dir, file_part))
            if target_path not in cache:
                cache[target_path] = load_yaml(target_path)
            target_doc = cache[target_path]
            resolved = gv.resolve_pointer(target_doc, "#" + pointer if pointer else "#")
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if siblings:
                if not isinstance(resolved, dict):
                    raise ValueError("$ref with siblings targets a non-object: %s" % ref)
                resolved = dict(resolved)
                resolved.update(siblings)
            return bundle_external_refs(
                resolved, os.path.dirname(target_path), cache, depth + 1
            )
        return {
            key: bundle_external_refs(value, base_dir, cache, depth)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [bundle_external_refs(item, base_dir, cache, depth) for item in node]
    return node


def check_document(document, raw_text, report):
    print("\n[1/3] notify.v1.yaml structure")

    version = document.get("openapi")
    if isinstance(version, str) and version.startswith("3.1."):
        report.ok("openapi version is 3.1.x (%s)" % version)
    else:
        report.fail("openapi version is 3.1.x", "found %r" % (version,))

    info = document.get("info", {})
    if info.get("title") and info.get("version"):
        report.ok("info.title and info.version present (%s)" % info.get("version"))
    else:
        report.fail("info.title and info.version present", "info = %r" % (info,))

    license_ = info.get("license", {})
    if license_.get("identifier") == "Apache-2.0":
        report.ok("info.license is Apache-2.0")
    else:
        report.fail(
            "info.license is Apache-2.0",
            "expected identifier 'Apache-2.0', got %r" % (license_,),
        )

    schemas = document.get("components", {}).get("schemas", {})
    if schemas:
        report.ok("components.schemas present (%d schemas)" % len(schemas))
    else:
        report.fail("components.schemas present", "no schemas found")

    # Every aggregated/ref'd schema must resolve locally after bundling.
    refs = []
    gv.collect_refs(document, refs)
    broken = []
    for target, trail in refs:
        try:
            gv.resolve_pointer(document, target)
        except (KeyError, ValueError, IndexError) as exc:
            broken.append("%s (at %s): %s" % (target, trail, exc))
    if broken:
        report.fail("all $refs resolve (%d found)" % len(refs), "\n".join(broken[:12]))
    else:
        report.ok("all $refs resolve (%d found)" % len(refs))

    hits = gv.scan_secrets(raw_text)
    if hits:
        report.fail("no committed secrets in notify.v1.yaml", "\n".join(hits[:10]))
    else:
        report.ok("no committed secrets in notify.v1.yaml")


def check_examples(document, report):
    print("\n[2/3] Example payloads against notify schemas (+ gateway cross-check)")

    present = sorted(f for f in os.listdir(EXAMPLES_DIR) if f.endswith(".json"))
    untracked = [f for f in present if f not in EXAMPLE_TARGETS]
    missing = [f for f in EXAMPLE_TARGETS if f not in present]
    if untracked:
        report.fail("every example has a schema target", "unmapped: %s" % ", ".join(untracked))
    if missing:
        report.fail("every mapped example file exists", "missing: %s" % ", ".join(missing))

    example_hits = []
    texts = {}
    for name in present:
        path = os.path.join(EXAMPLES_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                texts[name] = handle.read()
        except OSError as exc:
            example_hits.append("%s: %s" % (name, exc))
            continue
        for hit in gv.scan_secrets(texts[name]):
            example_hits.append("%s: %s" % (name, hit))
    if example_hits:
        report.fail("no committed secrets in examples", "\n".join(example_hits[:10]))
    else:
        report.ok("no committed secrets in examples (%d files)" % len(present))

    failures = 0
    for name in present:
        pointer = EXAMPLE_TARGETS.get(name)
        if not pointer:
            continue
        try:
            payload = json.loads(texts[name])
        except ValueError as exc:
            report.fail("%s is valid JSON" % name, exc)
            failures += 1
            continue
        try:
            schema = gv.resolve_pointer(document, pointer)
        except (KeyError, ValueError, IndexError) as exc:
            report.fail("%s: schema pointer resolves" % name, "%s -> %s" % (pointer, exc))
            failures += 1
            continue
        errors = gv.validate(payload, schema, document)
        if errors:
            report.fail("%s matches %s" % (name, pointer), "\n".join(errors[:8]))
            failures += 1
            continue
        cross = GATEWAY_CROSS_CHECK.get(name)
        if cross:
            cross_errors = gv.validate(
                payload, gv.resolve_pointer(document, cross), document
            )
            if cross_errors:
                report.fail(
                    "%s still matches gateway %s" % (name, cross),
                    "\n".join(cross_errors[:8]),
                )
                failures += 1
                continue
            report.ok("%s matches %s (and gateway %s)" % (name, _short(pointer), _short(cross)))
        else:
            report.ok("%s matches %s" % (name, pointer))

    if failures:
        report.fail("all examples validate (%d of %d failed)" % (failures, len(present)))
    else:
        report.ok("all examples validate (%d files)" % len(EXAMPLE_TARGETS))


def _short(pointer):
    name = pointer.rstrip("/").rsplit("/", 1)[-1]
    return name or pointer


def main(argv=None):
    del argv  # no CLI flags; kept for symmetry with the gateway validator
    print("botville notify domain model validation")
    print("  contract: %s" % NOTIFY_OPENAPI)
    print("  examples: %s" % EXAMPLES_DIR)

    try:
        with open(NOTIFY_OPENAPI, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
    except OSError as exc:
        print("\nCannot read notify contract: %s" % exc)
        return 2

    try:
        document = yaml.load(raw_text, Loader=gv.DuplicateKeySafeLoader)
    except yaml.YAMLError as exc:
        print("\n\033[31mFAIL\033[0m  YAML parses: %s" % exc)
        return 1

    report = Report()
    report.ok("YAML parses (duplicate keys rejected)")

    try:
        document = bundle_external_refs(
            document, os.path.dirname(NOTIFY_OPENAPI), cache={}
        )
    except (KeyError, ValueError, IndexError, yaml.YAMLError) as exc:
        report.fail("external $refs bundle", exc)
        print("\n[3/3] Summary")
        print("  \033[31m1 check(s) failed\033[0m. Notify model is NOT ready for review.")
        return 1
    report.ok("external $refs bundled (gateway schemas inlined)")

    check_document(document, raw_text, report)
    check_examples(document, report)

    print("\n[3/3] Summary")
    total = report.passes + report.failures
    if report.failures:
        print(
            "  \033[31m%d check(s) failed\033[0m of %d. Notify model is NOT ready for review."
            % (report.failures, total)
        )
        return 1
    print(
        "  \033[32mAll %d checks passed.\033[0m Notify model is consistent with the gateway contract."
        % total
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

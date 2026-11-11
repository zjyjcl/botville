#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Validate the botville gateway OpenAPI contract and its example payloads.

Runs fully offline. Two layers of checks:

1. Document checks on ``gateway/openapi/gateway.v1.yaml``:
   OpenAPI 3.1 shape, resolvable ``$ref``s, unique operationIds, path parameters,
   the placeholder bearer scheme, Apache-2.0 license, RFC 9457 error responses,
   and a conservative scan for committed secrets.
2. Example checks: every file in ``gateway/openapi/examples/`` is validated against
   the schema the contract declares for it (response body or request body).

No third-party schema library is required; the subset of JSON Schema 2020-12 used
by this contract (type/array-of-types, required, properties, additionalProperties,
items, enum, pattern, min/max, minLength/maxLength, minItems, minProperties,
allOf, anyOf, $ref, format date-time/uri) is implemented below. Unknown keywords
are ignored, matching JSON Schema semantics.

Usage:
    python3 gateway/scripts/validate_openapi.py
    python3 gateway/scripts/validate_openapi.py --contract path/to/gateway.v1.yaml

Exit code 0 on success, 1 on any failure. Requires PyYAML (``pip install pyyaml``).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import urllib.parse

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "error: PyYAML is required.\n"
        "  python3 -m pip install --user pyyaml\n"
        "  (or run: npx --yes @redocly/cli lint gateway/openapi/gateway.v1.yaml)\n"
    )
    sys.exit(2)


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CONTRACT = os.path.join(REPO_ROOT, "gateway", "openapi", "gateway.v1.yaml")
EXAMPLES_DIR = os.path.join(REPO_ROOT, "gateway", "openapi", "examples")

# Media type used for every error body (RFC 9457).
PROBLEM_MEDIA_TYPE = "application/problem+json"

# example file -> JSON pointer to the schema it must satisfy.
# Pointers target the contract itself, so a schema change breaks examples too.
EXAMPLE_TARGETS = {
    "health.json": "#/paths/~1health/get/responses/200/content/application~1json/schema",
    "capability-manifest.json": "#/paths/~1capabilities/get/responses/200/content/application~1json/schema",
    "persona.json": "#/paths/~1personas~1{persona_id}/get/responses/200/content/application~1json/schema",
    "create-persona-request.json": "#/paths/~1personas/post/requestBody/content/application~1json/schema",
    "persona-state.json": "#/paths/~1personas~1{persona_id}~1state/get/responses/200/content/application~1json/schema",
    "conversation.json": "#/paths/~1conversations~1{conversation_id}/get/responses/200/content/application~1json/schema",
    "message.json": "#/components/schemas/Message",
    "send-message-request.json": "#/paths/~1conversations~1{conversation_id}~1messages/post/requestBody/content/application~1json/schema",
    "message-receipt.json": "#/paths/~1conversations~1{conversation_id}~1messages/post/responses/200/content/application~1json/schema",
    "message-receipt-deferred.json": "#/paths/~1conversations~1{conversation_id}~1messages/post/responses/200/content/application~1json/schema",
    "message-receipt-blocked.json": "#/paths/~1conversations~1{conversation_id}~1messages/post/responses/200/content/application~1json/schema",
    "message-receipt-queued.json": "#/paths/~1conversations~1{conversation_id}~1messages/post/responses/200/content/application~1json/schema",
    "policy-evaluate-request.json": "#/paths/~1policy~1evaluate/post/requestBody/content/application~1json/schema",
    "policy-decision.json": "#/paths/~1policy~1evaluate/post/responses/200/content/application~1json/schema",
    "context-assemble-request.json": "#/paths/~1context~1assemble/post/requestBody/content/application~1json/schema",
    "context-assemble-result.json": "#/paths/~1context~1assemble/post/responses/200/content/application~1json/schema",
    "context-assemble-blocked.json": "#/paths/~1context~1assemble/post/responses/200/content/application~1json/schema",
    "effective-limits.json": "#/paths/~1limits/get/responses/200/content/application~1json/schema",
    "usage-summary.json": "#/paths/~1usage/get/responses/200/content/application~1json/schema",
    "usage-event.json": "#/components/schemas/UsageEvent",
    "card.json": "#/paths/~1cards~1{card_id}/get/responses/200/content/application~1json/schema",
    "create-card-request.json": "#/paths/~1cards/post/requestBody/content/application~1json/schema",
    "card-action-receipt.json": "#/paths/~1cards~1{card_id}~1actions~1{action_id}/post/responses/200/content/application~1json/schema",
    "card-action-request.json": "#/paths/~1cards~1{card_id}~1actions~1{action_id}/post/requestBody/content/application~1json/schema",
    "card-action-challenge.json": "#/paths/~1cards~1{card_id}~1actions~1{action_id}~1challenge/post/responses/201/content/application~1json/schema",
    "audit-record.json": "#/paths/~1audit~1{audit_id}/get/responses/200/content/application~1json/schema",
    "session-token.json": "#/paths/~1capabilities~1{capability_id}~1session-tokens/post/responses/201/content/application~1json/schema",
    "problem.json": "#/components/responses/Forbidden/content/application~1problem+json/schema",
    # #124 OpenID App 接入：/moments* 终端用户身份校验的实际输出
    "problem-auth-missing.json": "#/components/responses/Unauthorized/content/application~1problem+json/schema",
    "problem-auth-invalid-token.json": "#/components/responses/Unauthorized/content/application~1problem+json/schema",
    "problem-homeserver-unavailable.json": "#/components/responses/InternalError/content/application~1problem+json/schema",
    "problem-conversation-e2ee.json": "#/paths/~1conversations~1{conversation_id}~1messages/post/responses/403/content/application~1problem+json/schema",
    "problem-create-conversation-e2ee.json": "#/paths/~1conversations/post/responses/422/content/application~1problem+json/schema",
    # A2.2 moments (issue #7)
    "moment.json": "#/paths/~1moments~1{moment_id}/get/responses/200/content/application~1json/schema",
    "create-moment-request.json": "#/paths/~1moments/post/requestBody/content/application~1json/schema",
    "update-moment-request.json": "#/paths/~1moments~1{moment_id}/patch/requestBody/content/application~1json/schema",
    "moment-page.json": "#/paths/~1moments/get/responses/200/content/application~1json/schema",
    "moment-comment.json": "#/components/schemas/MomentComment",
    "create-comment-request.json": "#/paths/~1moments~1{moment_id}~1comments/post/requestBody/content/application~1json/schema",
    "comment-page.json": "#/paths/~1moments~1{moment_id}~1comments/get/responses/200/content/application~1json/schema",
    "moment-like-state.json": "#/paths/~1moments~1{moment_id}~1likes~1me/put/responses/200/content/application~1json/schema",
    "moment-like-page.json": "#/paths/~1moments~1{moment_id}~1likes/get/responses/200/content/application~1json/schema",
    "problem-moment-not-found.json": "#/components/responses/NotFound/content/application~1problem+json/schema",
}

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "OpenAI-style key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
]

# key = value lines that carry a suspiciously long literal
SECRET_ASSIGNMENT = re.compile(
    r"""^[\s"'`{,-]*(?P<key>password|passwd|secret|api[_-]?key|access[_-]?token|"""
    r"""client[_-]?secret|private[_-]?key|signing[_-]?key)["'`]?\s*[:=]\s*"""
    r"""["'`]?(?P<value>[^\s"'`,}#]{12,})""",
    re.IGNORECASE,
)


class DuplicateKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys instead of silently overwriting."""


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key %r" % (key,),
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


class Report:
    def __init__(self):
        self.failures = []
        self.passes = 0

    def ok(self, label):
        self.passes += 1
        print("  \033[32mPASS\033[0m  %s" % label)

    def fail(self, label, detail=""):
        self.failures.append((label, detail))
        print("  \033[31mFAIL\033[0m  %s" % label)
        if detail:
            for line in str(detail).splitlines():
                print("        %s" % line)

    @property
    def failed(self):
        return bool(self.failures)


# --------------------------------------------------------------------------- #
# JSON pointer + $ref resolution
# --------------------------------------------------------------------------- #
def resolve_pointer(document, pointer):
    if pointer in ("", "#"):
        return document
    if not pointer.startswith("#"):
        raise KeyError("only local pointers are supported: %s" % pointer)
    node = document
    body = pointer[1:]
    if body.startswith("/"):
        body = body[1:]
    if body == "":
        return node
    for raw in body.split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        elif isinstance(node, dict):
            node = node[token]
        else:
            raise KeyError("cannot descend into %r at %r" % (type(node).__name__, token))
    return node


def deref(document, node):
    """Follow a chain of $ref nodes."""
    guard = 0
    while isinstance(node, dict) and "$ref" in node:
        node = resolve_pointer(document, node["$ref"])
        guard += 1
        if guard > 64:
            raise KeyError("$ref chain too deep")
    return node


# --------------------------------------------------------------------------- #
# Minimal JSON Schema 2020-12 validation
# --------------------------------------------------------------------------- #
class SchemaError(Exception):
    pass


def _type_matches(value, name):
    if name == "null":
        return value is None
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True  # unknown type keyword: be permissive


def _check_format(value, fmt):
    if fmt == "date-time":
        try:
            parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return "not a valid RFC 3339 date-time"
        if parsed.tzinfo is None:
            return "date-time must carry a timezone (UTC 'Z' required)"
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
            return "date-time must be UTC with a trailing 'Z'; offsets such as +08:00 are not allowed"
        return None
    if fmt in ("uri", "uri-reference"):
        if " " in value:
            return "URI must not contain spaces"
        parsed = urllib.parse.urlsplit(value)
        if fmt == "uri" and not parsed.scheme:
            return "URI must have a scheme"
        return None
    return None


def validate(instance, schema, document, path="$", depth=0):
    """Return a list of human readable error strings."""
    if depth > 64:
        return ["%s: schema recursion limit" % path]
    schema = deref(document, schema)
    if schema is True or schema == {}:
        return []
    if schema is False:
        return ["%s: no value allowed here" % path]
    if not isinstance(schema, dict):
        return ["%s: invalid schema node %r" % (path, schema)]

    errors = []

    if "allOf" in schema:
        for i, sub in enumerate(schema["allOf"]):
            errors.extend(validate(instance, sub, document, path, depth + 1))
    if "anyOf" in schema:
        sub_errors = [
            validate(instance, sub, document, path, depth + 1) for sub in schema["anyOf"]
        ]
        if all(sub_errors):
            errors.append(
                "%s: does not match anyOf; first branch: %s"
                % (path, sub_errors[0][0] if sub_errors[0] else "?")
            )
    if "oneOf" in schema:
        sub_errors = [
            validate(instance, sub, document, path, depth + 1) for sub in schema["oneOf"]
        ]
        if sum(1 for e in sub_errors if not e) != 1:
            errors.append("%s: must match exactly one oneOf branch" % path)

    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_type_matches(instance, n) for n in names):
            return errors + [
                "%s: expected %s, got %s"
                % (path, "/".join(str(n) for n in names), _json_type(instance))
            ]

    if instance is None:
        if "enum" in schema and None not in schema["enum"]:
            errors.append("%s: null not in enum" % path)
        return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: %r not in enum %r" % (path, instance, schema["enum"]))
    if "const" in schema and instance != schema["const"]:
        errors.append("%s: expected const %r" % (path, schema["const"]))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s: shorter than %s" % (path, schema["minLength"]))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append("%s: longer than %s" % (path, schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("%s: does not match pattern %s" % (path, schema["pattern"]))
        if "format" in schema:
            problem = _check_format(instance, schema["format"])
            if problem:
                errors.append("%s: %s (%r)" % (path, problem, instance))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: below minimum %s" % (path, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: above maximum %s" % (path, schema["maximum"]))
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append("%s: must exceed %s" % (path, schema["exclusiveMinimum"]))
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append("%s: must be below %s" % (path, schema["exclusiveMaximum"]))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append("%s: fewer than %s items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append("%s: more than %s items" % (path, schema["maxItems"]))
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                errors.extend(
                    validate(item, items, document, "%s[%d]" % (path, index), depth + 1)
                )

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("%s: missing required property %r" % (path, key))
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors.extend(
                    validate(value, props[key], document, "%s.%s" % (path, key), depth + 1)
                )
            else:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    errors.append("%s: unexpected property %r" % (path, key))
                elif isinstance(additional, dict):
                    errors.extend(
                        validate(value, additional, document, "%s.%s" % (path, key), depth + 1)
                    )
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            errors.append("%s: fewer than %s properties" % (path, schema["minProperties"]))
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            errors.append("%s: more than %s properties" % (path, schema["maxProperties"]))
    return errors


def _json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# --------------------------------------------------------------------------- #
# Document checks
# --------------------------------------------------------------------------- #
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
PATH_PARAM = re.compile(r"\{([^{}]+)\}")


def collect_refs(node, found, trail="#"):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append((value, trail))
            else:
                collect_refs(value, found, "%s/%s" % (trail, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            collect_refs(value, found, "%s/%d" % (trail, index))


def iter_operations(document):
    for path, item in document.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                yield path, method, operation, item


def scan_secrets(raw_text):
    """Return a list of suspected committed secrets in ``raw_text``.

    Placeholders (``<change-me>``, ``env:``/``ref:`` references, example.invalid
    hosts) are allowed; anything else that looks like a literal credential fails.
    """
    hits = []
    for pattern, label in SECRET_PATTERNS:
        for match in pattern.finditer(raw_text):
            hits.append("%s near %r" % (label, match.group(0)[:24]))
    for line_no, line in enumerate(raw_text.splitlines(), 1):
        match = SECRET_ASSIGNMENT.search(line)
        if not match:
            continue
        value = match.group("value")
        if value.startswith("<") or "change-me" in value or value.startswith(("env:", "ref:")):
            continue
        if ".example.invalid" in value or value.startswith("https://"):
            continue
        hits.append("possible literal secret at line %d (%s)" % (line_no, match.group("key")))
    return hits


def check_document(document, raw_text, report):
    print("\n[1/3] OpenAPI document structure")

    # --- top level ---
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

    servers = document.get("servers", [])
    if servers and all(str(s.get("url", "")).rstrip("/").endswith("/v1") for s in servers):
        report.ok("servers are versioned under /v1")
    else:
        report.fail("servers are versioned under /v1", "servers = %r" % (servers,))

    # --- bearer placeholder scheme ---
    scheme = document.get("components", {}).get("securitySchemes", {}).get("bearerAuth", {})
    if (
        scheme.get("type") == "http"
        and str(scheme.get("scheme", "")).lower() == "bearer"
    ):
        report.ok("securitySchemes.bearerAuth is http/bearer placeholder")
    else:
        report.fail("securitySchemes.bearerAuth is http/bearer placeholder", "got %r" % (scheme,))
    # document-level security should reference the scheme by name; an explicit
    # empty list ("security: []") disables the default and is also acceptable.
    # Never index [0] blindly: an empty list would raise IndexError.
    default_security = document.get("security", [])
    if not isinstance(default_security, list):
        report.fail("default security uses bearerAuth", "security = %r" % (default_security,))
    elif not default_security:
        report.ok("default security is explicitly empty (security: [])")
    elif any(isinstance(entry, dict) and "bearerAuth" in entry for entry in default_security):
        report.ok("default security uses bearerAuth")
    else:
        report.fail("default security uses bearerAuth", "security = %r" % (default_security,))

    # --- refs resolve ---
    refs = []
    collect_refs(document, refs)
    broken = []
    for target, trail in refs:
        try:
            resolve_pointer(document, target)
        except (KeyError, ValueError, IndexError) as exc:
            broken.append("%s (at %s): %s" % (target, trail, exc))
    if broken:
        report.fail("all $refs resolve (%d found)" % len(refs), "\n".join(broken[:12]))
    else:
        report.ok("all $refs resolve (%d found)" % len(refs))

    # --- operations ---
    operations = list(iter_operations(document))
    problems = []
    seen_ids = {}
    for path, method, operation, path_item in operations:
        label = "%s %s" % (method.upper(), path)
        op_id = operation.get("operationId")
        if not op_id:
            problems.append("%s: missing operationId" % label)
        elif op_id in seen_ids:
            problems.append("%s: duplicate operationId %r" % (label, op_id))
        else:
            seen_ids[op_id] = label
        if not operation.get("summary"):
            problems.append("%s: missing summary" % label)
        if not operation.get("description"):
            problems.append("%s: missing description" % label)
        if not operation.get("tags"):
            problems.append("%s: missing tags" % label)
        for tag in operation.get("tags", []):
            if not any(t.get("name") == tag for t in document.get("tags", [])):
                problems.append("%s: tag %r not declared in top-level tags" % (label, tag))

        responses = operation.get("responses") or {}
        if not responses:
            problems.append("%s: no responses" % label)
        if not any(str(code).startswith("2") for code in responses):
            problems.append("%s: no 2xx response" % label)
        for code, response in responses.items():
            response = deref(document, response)
            if not response.get("description"):
                problems.append("%s %s: response missing description" % (label, code))
            content = response.get("content") or {}
            if str(code).startswith(("4", "5")):
                if path != "/health" and PROBLEM_MEDIA_TYPE not in content:
                    problems.append(
                        "%s %s: error response must use %s" % (label, code, PROBLEM_MEDIA_TYPE)
                    )
            elif str(code).startswith("2") and content:
                if "application/json" not in content:
                    problems.append("%s %s: success body must be application/json" % (label, code))
                else:
                    schema = content["application/json"].get("schema")
                    if not schema:
                        problems.append("%s %s: JSON body without schema" % (label, code))

        # path parameters declared and required
        template_params = PATH_PARAM.findall(path)
        declared = []
        for param in (path_item.get("parameters") or []) + (operation.get("parameters") or []):
            param = deref(document, param)
            if param.get("in") == "path":
                declared.append(param.get("name"))
                if param.get("required") is not True:
                    problems.append(
                        "%s: path parameter %r must be required: true" % (label, param.get("name"))
                    )
        for name in template_params:
            if name not in declared:
                problems.append("%s: path parameter {%s} is not declared" % (label, name))
        for name in declared:
            if name not in template_params:
                problems.append(
                    "%s: parameter %r declared as path but not in the path template" % (label, name)
                )

        # request bodies must name their schema
        body = operation.get("requestBody")
        if isinstance(body, dict):
            body = deref(document, body)
            for media, media_obj in (body.get("content") or {}).items():
                if not media_obj.get("schema"):
                    problems.append("%s: request body %s has no schema" % (label, media))

    if problems:
        report.fail("path operations are well formed (%d ops)" % len(operations), "\n".join(problems[:15]))
    else:
        report.ok("path operations are well formed (%d ops, unique operationIds)" % len(operations))

    # --- secrets ---
    hits = scan_secrets(raw_text)
    if hits:
        report.fail("no committed secrets in contract (placeholder only)", "\n".join(hits[:10]))
    else:
        report.ok("no committed secrets in contract (placeholder only)")


# --------------------------------------------------------------------------- #
# Examples
# --------------------------------------------------------------------------- #
def check_examples(document, report):
    print("\n[2/3] Example payloads against contract schemas")
    if not os.path.isdir(EXAMPLES_DIR):
        report.fail("examples directory exists", EXAMPLES_DIR)
        return

    present = sorted(f for f in os.listdir(EXAMPLES_DIR) if f.endswith(".json"))
    untracked = [f for f in present if f not in EXAMPLE_TARGETS]
    missing = [f for f in EXAMPLE_TARGETS if f not in present]
    if untracked:
        report.fail("every example has a schema target", "unmapped: %s" % ", ".join(untracked))
    if missing:
        report.fail("every mapped example file exists", "missing: %s" % ", ".join(missing))

    # Secret placeholders must hold in examples too, not just in the contract.
    example_hits = []
    for name in present:
        path = os.path.join(EXAMPLES_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            example_hits.append("%s: %s" % (name, exc))
            continue
        for hit in scan_secrets(text):
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
        path = os.path.join(EXAMPLES_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            report.fail("%s is valid JSON" % name, exc)
            failures += 1
            continue
        try:
            schema = resolve_pointer(document, pointer)
        except (KeyError, ValueError, IndexError) as exc:
            report.fail("%s: schema pointer resolves" % name, "%s -> %s" % (pointer, exc))
            failures += 1
            continue
        errors = validate(payload, schema, document)
        if errors:
            report.fail("%s matches %s" % (name, pointer), "\n".join(errors[:8]))
            failures += 1
        else:
            report.ok("%s matches %s" % (name, _short_pointer(pointer)))

    if failures:
        report.fail("all examples validate (%d of %d failed)" % (failures, len(present)))
    else:
        report.ok("all examples validate (%d files)" % (len(EXAMPLE_TARGETS),))

    check_assemble_nonce_binding(report)


# `id="<nonce>"` on both the opening (`<untrusted id=...>`) and closing
# (`</untrusted id=...>`) of every untrusted-data segment.
UNTRUSTED_TAG_ID = re.compile(r"</?untrusted\s+id=\"([^\"]*)\"")


def check_assemble_nonce_binding(report):
    """Cross-check nonce tag ids against ``nonces[]`` (B2.3 B5).

    contract.md §7.2 requires every ``<untrusted id="{nonce}">…</untrusted id="{nonce}">``
    segment to carry the segment's real nonce as its ``id``, and ``ContextNonce.segment_id``
    must equal that nonce. The allowed-context example therefore has to satisfy:

      * the set of ``id`` values found in ``zones.untrusted_data`` equals the set of
        ``nonces[].nonce`` values (one-to-one, no extras on either side), and
      * each ``nonces[].segment_id`` equals its ``nonce``.
    """
    print("\n[2b/3] Context assemble nonce binding")
    name = "context-assemble-result.json"
    path = os.path.join(EXAMPLES_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        report.fail("%s is readable for nonce checks" % name, exc)
        return

    if not isinstance(payload, dict) or payload.get("outcome") != "allowed":
        report.fail("%s has outcome=allowed" % name, "outcome = %r" % (payload.get("outcome"),))
        return

    zones = payload.get("zones") or {}
    nonces = payload.get("nonces") or []
    if not isinstance(zones, dict) or not isinstance(nonces, list) or not nonces:
        report.fail("%s has zones and nonces" % name, "zones/nonces missing or empty")
        return

    untrusted = zones.get("untrusted_data") or ""
    tag_ids = UNTRUSTED_TAG_ID.findall(untrusted) if isinstance(untrusted, str) else []
    listed = [
        entry.get("nonce") for entry in nonces if isinstance(entry, dict)
    ]

    problems = []
    if not tag_ids:
        problems.append("no `<untrusted id=\"...\">` tags found in zones.untrusted_data")
    if any(not isinstance(n, str) or not n for n in listed):
        problems.append("every nonces[] entry needs a non-string-empty `nonce`")
    for entry in nonces:
        if not isinstance(entry, dict):
            problems.append("nonces[] entry is not an object: %r" % (entry,))
            continue
        if entry.get("segment_id") != entry.get("nonce"):
            problems.append(
                "segment_id %r != nonce %r (contract §7.2 requires equality)"
                % (entry.get("segment_id"), entry.get("nonce"))
            )
    # Opening and closing tags both repeat the id: an odd count means a dangling tag.
    if tag_ids and len(tag_ids) % 2 != 0:
        problems.append("odd number of `<untrusted id=...>` tags (%d): unclosed segment" % len(tag_ids))
    unique_tags = set(tag_ids)
    unique_nonces = {n for n in listed if isinstance(n, str) and n}
    if tag_ids and unique_tags != unique_nonces:
        missing = unique_nonces - unique_tags
        extra = unique_tags - unique_nonces
        if missing:
            problems.append("nonces missing from untrusted_data tag ids: %s" % ", ".join(sorted(missing)))
        if extra:
            problems.append("tag ids with no matching nonce: %s" % ", ".join(sorted(extra)))
    # Each nonce must appear exactly twice: once opening, once closing.
    for nonce in sorted(unique_nonces):
        count = tag_ids.count(nonce)
        if count != 2:
            problems.append("nonce %r appears %d times as a tag id (expected 2: open + close)" % (nonce, count))

    if problems:
        report.fail("%s nonce ids bind to nonces[]" % name, "\n".join(problems[:10]))
    else:
        report.ok(
            "%s nonce ids bind to nonces[] (%d segments, segment_id == nonce)"
            % (name, len(unique_nonces))
        )


def _short_pointer(pointer):
    """Human readable label for a pointer, e.g. GET /personas/{persona_id} 200."""
    match = re.match(r"#/paths/([^/]+)/([a-z]+)/responses/([0-9X]+)", pointer)
    if match:
        path = match.group(1).replace("~1", "/")
        return "%s %s %s" % (match.group(2).upper(), path, match.group(3))
    match = re.match(r"#/paths/([^/]+)/([a-z]+)/requestBody", pointer)
    if match:
        path = match.group(1).replace("~1", "/")
        return "%s %s request body" % (match.group(2).upper(), path)
    return pointer


# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate botville gateway OpenAPI contract")
    parser.add_argument("--contract", default=DEFAULT_CONTRACT, help="path to gateway.v1.yaml")
    args = parser.parse_args(argv)

    print("botville gateway contract validation")
    print("  contract: %s" % args.contract)
    print("  examples: %s" % EXAMPLES_DIR)

    try:
        with open(args.contract, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
    except OSError as exc:
        print("\nCannot read contract: %s" % exc)
        return 2

    try:
        document = yaml.load(raw_text, Loader=DuplicateKeySafeLoader)
    except yaml.YAMLError as exc:
        print("\n\033[31mFAIL\033[0m  YAML parses: %s" % exc)
        return 1

    report = Report()
    report.ok("YAML parses (duplicate keys rejected)")

    check_document(document, raw_text, report)
    check_examples(document, report)

    print("\n[3/3] Summary")
    total = report.passes + len(report.failures)
    if report.failed:
        print(
            "  \033[31m%d check(s) failed\033[0m of %d. Contract is NOT ready for review."
            % (len(report.failures), total)
        )
        return 1
    print("  \033[32mAll %d checks passed.\033[0m Contract is consistent with its examples." % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Offline owner administration for PR3a memory.

``--as-owner`` is a trusted local operator assertion, never remote
authentication. The command emits JSON only; failures use stable body-free
codes and do not print exception details.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, Optional, Sequence

from ..engine.personas import PersonaConfigError, load_personas
from .api import MemoryPermissionError, OwnerMemoryAPI
from .store import MemoryStore


class _ArgumentError(ValueError):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _ArgumentError()


def _emit(value: Dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), file=stream)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="Manage local gateway memory")
    parser.add_argument(
        "command", choices=("list", "show", "delete", "pin", "unpin", "set-fact"),
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--persona", required=True)
    parser.add_argument("--as-owner", required=True)
    parser.add_argument("--personas-file", required=True)
    parser.add_argument(
        "--server-name", default=os.environ.get("GATEWAY_MATRIX_SERVER_NAME"),
    )
    parser.add_argument("--kind", choices=("item", "fact"))
    parser.add_argument("--id", type=int)
    parser.add_argument("--key")
    parser.add_argument("--counterpart")
    parser.add_argument("--value")
    parser.add_argument("--visibility", default="dm_only")
    return parser


def _argument_problem(args: argparse.Namespace) -> bool:
    if not args.server_name:
        return True
    if args.command in ("show", "delete", "pin", "unpin"):
        # Omitted --kind preserves the early PR3a item-only spelling; facts
        # require an explicit selector to avoid accidentally changing an item.
        if args.kind is None:
            args.kind = "item"
        if args.kind == "item":
            return args.id is None
        return not args.key or args.counterpart is None
    if args.command == "set-fact":
        return not args.key or args.value is None or args.counterpart is None
    return args.kind is not None


def _resolve_api(args: argparse.Namespace) -> OwnerMemoryAPI:
    # Validate the persona file before opening the DB, so malformed config never
    # creates a new sensitive DB file as a side effect.
    personas = load_personas(args.personas_file, server_name=args.server_name)
    owners = {
        persona_id: persona.owner_user_id
        for persona_id, persona in personas.items()
    }
    # Authorization precedes database creation/opening. A typo, non-owner, or
    # ownerless persona cannot cause a local database side effect.
    if not owners.get(args.persona) or owners[args.persona] != args.as_owner:
        raise MemoryPermissionError("memory owner permission denied")
    return OwnerMemoryAPI(MemoryStore(args.db), owners)


def _dispatch(api: OwnerMemoryAPI, args: argparse.Namespace) -> Dict[str, Any]:
    persona_id = args.persona
    caller = args.as_owner
    if args.command == "list":
        return {
            "items": api.list_items(persona_id, caller),
            "facts": api.list_facts(persona_id, caller),
        }
    if args.command == "show":
        if args.kind == "fact":
            return {"fact": api.show_fact(
                persona_id, caller, args.key, args.counterpart,
            )}
        return {"item": api.show_item(persona_id, caller, args.id)}
    if args.command == "set-fact":
        return {"changed": api.set_fact(
            persona_id, caller, args.key, args.value, args.counterpart,
            args.visibility,
        )}
    if args.kind == "fact":
        operation = {
            "delete": api.delete_fact,
            "pin": api.pin_fact,
            "unpin": api.unpin_fact,
        }[args.command]
        return {"changed": operation(persona_id, caller, args.key, args.counterpart)}
    operation = {
        "delete": api.delete_item,
        "pin": api.pin_item,
        "unpin": api.unpin_item,
    }[args.command]
    return {"changed": operation(persona_id, caller, args.id)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if _argument_problem(args):
            raise _ArgumentError()
        api = _resolve_api(args)
    except MemoryPermissionError:
        _emit({"error": {"code": "memory.owner_permission_denied"}}, stream=sys.stderr)
        return 3
    except (_ArgumentError, PersonaConfigError, ValueError, OSError, sqlite3.Error,
            RuntimeError):
        _emit({"error": {"code": "memory.cli.invalid_arguments"}}, stream=sys.stderr)
        return 2

    try:
        _emit(_dispatch(api, args))
        return 0
    except MemoryPermissionError:
        _emit({"error": {"code": "memory.owner_permission_denied"}}, stream=sys.stderr)
        return 3
    except (ValueError, OSError, sqlite3.Error, RuntimeError):
        _emit({"error": {"code": "memory.cli.operation_failed"}}, stream=sys.stderr)
        return 1
    finally:
        try:
            api.close()
        except sqlite3.Error:
            # Close errors have no useful operator action and must not bypass
            # the JSON-only error boundary with a traceback.
            pass


if __name__ == "__main__":
    raise SystemExit(main())

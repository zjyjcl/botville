"""Authenticated admin list/filter/bulk-revoke for issuance records.

Default off and auth rules match POST /_botville/pair/issue: an empty
BOTVILLE_PAIR_REDEEM_ISSUE_TOKEN leaves these routes 404; a configured
token must be at least 32 characters, distinct from the Synapse admin
token, and sent as Authorization: Bearer. These paths must never be
added to the public HTTPS edge allowlist.

The frozen App contract (pairing-qr.md §8 / pairing-redeem.openapi.yaml)
does not yet define a management-page API. This module documents the
minimal HTTP convention for this cut, in the same style as the issue
route: JSON object bodies, no query string, Bearer auth, and
{"error": "..."} on 400/401. List and revoke responses never include a
plaintext code, a pairing URI, or code_sha256.

This cut does not add a web UI, a second admin token, a one-time short
TTL management token, or login_token issuance.
"""

import json

from .issue import IssueError
from .store import ALLOWED_STATUS, require_https_url

ISSUE_LIST_PATH = "/_botville/pair/issue/list"
ISSUE_REVOKE_PATH = "/_botville/pair/issue/revoke"

LIST_FIELDS = frozenset({"status", "hs", "expired", "limit", "after_id"})
REVOKE_FIELDS = frozenset({"ids"})
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 100
REVOKE_MAX_IDS = 100

PUBLIC_RECORD_KEYS = (
    "id",
    "hs",
    "gw",
    "fp",
    "exp",
    "status",
    "auth_type",
    "issued_at",
    "used_at",
    "revoked_at",
    "device_display_name",
    "expired",
)


def _json_media_type(content_type):
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json"


def _parse_object(raw, content_type, allowed):
    if raw is None or len(raw) > 64 * 1024:
        raise IssueError(400, "invalid request")
    if not _json_media_type(content_type):
        raise IssueError(400, "invalid request")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise IssueError(400, "invalid request") from None
    if not isinstance(body, dict) or set(body) - allowed:
        raise IssueError(400, "invalid request")
    return body


def _require_int(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise IssueError(400, "invalid request")
    if value < minimum or value > maximum:
        raise IssueError(400, "invalid request")
    return value


def parse_list_request(raw, content_type):
    """Parse the admin list JSON body. All fields are optional."""
    body = _parse_object(raw, content_type, LIST_FIELDS)
    status = body.get("status")
    if "status" in body:
        if status not in ALLOWED_STATUS:
            raise IssueError(400, "invalid request")
    else:
        status = None
    hs = body.get("hs")
    if "hs" in body:
        if not isinstance(hs, str):
            raise IssueError(400, "invalid request")
        try:
            require_https_url(hs, "hs")
        except ValueError:
            raise IssueError(400, "invalid request") from None
    else:
        hs = None
    expired = body.get("expired")
    if "expired" in body:
        if not isinstance(expired, bool):
            raise IssueError(400, "invalid request")
    else:
        expired = None
    if "limit" in body:
        limit = _require_int(body["limit"], 1, LIST_MAX_LIMIT)
    else:
        limit = LIST_DEFAULT_LIMIT
    after_id = body.get("after_id")
    if "after_id" in body:
        after_id = _require_int(after_id, 1, 2 ** 31 - 1)
    else:
        after_id = None
    return {
        "status": status,
        "hs": hs,
        "expired": expired,
        "limit": limit,
        "after_id": after_id,
    }


def parse_revoke_request(raw, content_type):
    """Parse the admin bulk-revoke JSON body: {\"ids\": [positive int, ...]}."""
    body = _parse_object(raw, content_type, REVOKE_FIELDS)
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise IssueError(400, "invalid request")
    if len(ids) > REVOKE_MAX_IDS:
        raise IssueError(400, "invalid request")
    out = []
    for item in ids:
        out.append(_require_int(item, 1, 2 ** 31 - 1))
    return {"ids": out}


def record_is_expired(row, now):
    return row["status"] == "unused" and float(row["exp"]) <= float(now)


def public_record(row, now):
    """Admin-visible record. No code, URI, or code_sha256."""
    item = {
        "id": row["id"],
        "hs": row["hs"],
        "gw": row.get("gw"),
        "fp": row.get("fp"),
        "exp": row["exp"],
        "status": row["status"],
        "auth_type": row["auth_type"],
        "issued_at": row["issued_at"],
        "used_at": row.get("used_at"),
        "revoked_at": row.get("revoked_at"),
        "device_display_name": row.get("device_display_name"),
        "expired": record_is_expired(row, now),
    }
    unexpected = set(item) - set(PUBLIC_RECORD_KEYS)
    if unexpected:
        raise ValueError("unexpected public record fields")
    return item


def public_list_payload(rows, limit, now):
    items = [public_record(row, now) for row in rows]
    payload = {"items": items}
    if len(items) == limit and items:
        payload["next_after_id"] = items[-1]["id"]
    return payload

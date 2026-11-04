"""Shared validation rules for the botville pairing components (A2.1).

Both the pairing service and fedgate must agree on what a valid
server_name is (and on request-path hygiene), so the rules live here once:

- Matrix server names are case-insensitive; the canonical form is
  lower-case and every write/compare in the stack normalises to it first
  (store writes and lookups, QR payloads, fedgate's origin check);
- paired federation only supports bare DNS names: no ports, no IPv4/IPv6
  literals, no localhost — see docs/deploy/paired-federation.md.
"""

import re

MAX_SERVER_NAME_LENGTH = 253  # RFC 1123 hostname limit

# One DNS label: alnum, inner hyphens, no leading/trailing hyphen, 1-63
# characters (the {0,61} bounds below enforce the label length).
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Digits-and-dots-only strings are IPv4 literals (or invalid DNS); both
# are refused for pairing.
_DIGITS_AND_DOTS_RE = re.compile(r"[0-9.]+")


class ServerNameError(ValueError):
    """The value is not a valid DNS-style server_name."""


def normalize_server_name(name):
    """Return the canonical lower-case form of `name`.

    Raises ServerNameError for anything that is not a bare DNS hostname:
    non-string/empty values, total length over 253, labels that are empty
    / over 63 characters / start or end with '-', digits-and-dots-only
    forms (IPv4 literals), 'localhost' / '*.localhost', or names carrying
    any other character (ports, '@', '/', '#', IPv6 brackets, spaces,
    underscores, ...). Trailing dots are rejected (empty label).
    """
    if not isinstance(name, str) or not name:
        raise ServerNameError("server_name must be a non-empty string")
    lowered = name.lower()
    if len(lowered) > MAX_SERVER_NAME_LENGTH:
        raise ServerNameError(
            "server_name longer than {} characters".format(MAX_SERVER_NAME_LENGTH))
    if _DIGITS_AND_DOTS_RE.fullmatch(lowered):
        raise ServerNameError("server_name must be a DNS name, not an IP literal")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise ServerNameError("localhost is not a valid federation peer")
    for label in lowered.split("."):
        if not _LABEL_RE.fullmatch(label):
            raise ServerNameError(
                "server_name label {!r} is not a valid DNS label".format(label))
    return lowered


def is_valid_server_name(name):
    """True when `name` is valid *and* already in canonical lower-case.

    fedgate's X-Matrix check uses this: origins that are valid but not
    lower-case are refused (fail-closed) rather than silently normalised,
    so a difference between the gate's view and the stored peer rows is
    always a rejection, never an accidental allow.
    """
    try:
        return normalize_server_name(name) == name
    except ServerNameError:
        return False


def path_is_suspicious(path):
    """True for request paths that must be refused before any routing.

    Covers traversal segments ('..'), empty segments ('//') and
    percent-encoded separators/dots (%2e / %2f / %5c, any case): prefix
    matching must never be fed a path an upstream router could normalise
    into something else (review: bypass-via-path).
    """
    if "//" in path:
        return True
    if ".." in path.split("/"):
        return True
    lowered = path.lower()
    return "%2e" in lowered or "%2f" in lowered or "%5c" in lowered

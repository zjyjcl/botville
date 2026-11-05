#!/usr/bin/env python3
"""Render runtime configs/secrets for the paired-federation compose stack (A2.1).

Writes into server/compose/federation/generated/ (gitignored, recreated on
every run — no secrets or rendered configs ever ship in the repo):

  hs-a/homeserver.yaml, hs-b/homeserver.yaml  Synapse test instances
                                               (SQLite, throwaway)
  federation.env                              random pairing admin tokens
                                               consumed by docker compose
                                               --env-file and server/tests/
                                               federation_e2e.py
                                               (+ BOTVILLE_NO_PROXY_EXTRA,
                                               propagated so the federation
                                               compose sees it — see below)

The Synapse configs deliberately stack CI-only relaxations on top of the
standard surface (see the header comment in the template below): open
registration for the e2e bootstrap, no certificate verification and no
private-range blacklist because federation goes through fedgate over
self-signed certs on the compose network. The gate itself is fedgate; the
homeserver runs no whitelist (docs/compat/homeserver-compat.md §2.4).
"""

import os
import secrets

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATED_DIR = os.path.join(
    SCRIPT_DIR, "..", "compose", "federation", "generated")

# Same image pin as server/compose/docker-compose.yml (kept in sync here so
# the federation stack tests exactly the Synapse the main stack ships).
INSTANCES = ("hs-a", "hs-b")

# The federation compose interpolates ONLY from generated/federation.env
# (Makefile FED_COMPOSE passes --env-file), never from server/compose/.env
# where issue #134 PR1 documents BOTVILLE_NO_PROXY_EXTRA. docker-compose
# .federation.yml pins NO_PROXY/no_proxy on every service and appends this
# variable to the fixed list, so it must reach that env file: propagate it
# from the shell environment first (docker compose lets process env
# override --env-file values anyway), then server/compose/.env.
NO_PROXY_EXTRA_KEY = "BOTVILLE_NO_PROXY_EXTRA"
MAIN_ENV_PATH = os.path.join(SCRIPT_DIR, "..", "compose", ".env")

HOMESERVER_TEMPLATE = """\
# Rendered by server/scripts/federation-setup.py (A2.1) — throwaway paired-
# federation test instance; gitignored, regenerated per run.
# CI-only relaxations on purpose:
#   federation_verify_certificates: false  (fedgate TLS is self-signed here)
#   federation_ip_range_blacklist: []      (fedgate lives on the compose net)
#   enable_registration: true              (e2e registers alice/bob via UIA)
# The pairing gate is fedgate in front of this listener; no homeserver-side
# federation whitelist (docs/compat/homeserver-compat.md §2.4).
server_name: {server_name}
pid_file: /data/homeserver.pid
listeners:
  - port: 8008
    tls: false
    type: http
    bind_addresses: ["0.0.0.0"]
    resources:
      - names: [client, federation]
        compress: false
# SQLite keeps the test stack single-container per homeserver; the main
# stack's Postgres profile stays untouched.
database:
  name: sqlite3
  args:
    database: /data/homeserver.db
media_store_path: /data/media_store
signing_key_path: /data/signing.key
report_stats: false
enable_registration: true
enable_registration_without_verification: true
macaroon_secret_key: "{macaroon}"
form_secret: "{form_secret}"
registration_shared_secret: "{registration_shared_secret}"
trusted_key_servers: []
suppress_key_server_warning: true
federation_verify_certificates: false
federation_ip_range_blacklist: []
"""


def write_private(path, text):
    """Write 0600 from the start (values are secrets)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def resolve_no_proxy_extra():
    """BOTVILLE_NO_PROXY_EXTRA value for the federation env file.

    Shell environment first, then the last assignment in
    server/compose/.env if that file exists (plain KEY=VALUE lines, same
    shape .env.example ships; surrounding quotes stripped like compose
    does). Missing file/entry or unset var all resolve to "" — which
    docker-compose.federation.yml renders as the bare default list.
    """
    value = ""
    try:
        with open(MAIN_ENV_PATH, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith(NO_PROXY_EXTRA_KEY + "="):
                    value = line.partition("=")[2].strip()
    except OSError:
        pass
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return os.environ.get(NO_PROXY_EXTRA_KEY, value).strip()


def main():
    os.umask(0o077)
    os.makedirs(GENERATED_DIR, exist_ok=True)

    tokens = {}
    for name in INSTANCES:
        config = HOMESERVER_TEMPLATE.format(
            server_name=name,
            macaroon=secrets.token_hex(32),
            form_secret=secrets.token_hex(32),
            registration_shared_secret=secrets.token_hex(32),
        )
        instance_dir = os.path.join(GENERATED_DIR, name)
        os.makedirs(instance_dir, exist_ok=True)
        write_private(os.path.join(instance_dir, "homeserver.yaml"), config)
        # hs-a -> PAIRING_ADMIN_TOKEN_A (docker-compose interpolation key).
        tokens[name.partition("-")[2].upper()] = secrets.token_hex(32)
        print("federation-setup.py: rendered {}/homeserver.yaml".format(name))

    env = "".join(
        "PAIRING_ADMIN_TOKEN_{}={}\n".format(suffix, token)
        for suffix, token in sorted(tokens.items())
    )
    # Issue #134 PR2: the only other key compose interpolates from this
    # file; hostnames only, never a secret.
    no_proxy_extra = resolve_no_proxy_extra()
    env += "{}={}\n".format(NO_PROXY_EXTRA_KEY, no_proxy_extra)
    write_private(os.path.join(GENERATED_DIR, "federation.env"), env)
    print("federation-setup.py: wrote federation.env with fresh pairing admin"
          " tokens (gitignored; never commit)")
    if no_proxy_extra:
        print("federation-setup.py: {}={} propagated (appended to the fixed"
              " NO_PROXY list by docker-compose.federation.yml)".format(
                  NO_PROXY_EXTRA_KEY, no_proxy_extra))


if __name__ == "__main__":
    main()

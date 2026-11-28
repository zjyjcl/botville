"""Run the App QR redeem server, or the loopback issue CLI.

Default bind is 127.0.0.1:8092. Public unicast binds are refused.
0.0.0.0 / private addresses are allowed for an isolated compose
network; the host port must still be published on 127.0.0.1 only.

  python -m pair_redeem              # serve redeem (+ issue if token set)
  python -m pair_redeem issue --hs https://hs.example.org

Issuance is default off and must never be exposed on a public edge.
Local or mock TLS is not a substitute for public HTTPS or a phone scan.
"""

import logging
import os
import sys

from .issue import cli_main as issue_cli
from .service import (
    ConfigError,
    PairRedeemApp,
    PairRedeemConfig,
    SynapseRegistrationTokenIssuer,
    allow_bind_host,
    start_pruner,
)
from .store import PairRedeemStore


def serve(env=None):
    env = os.environ if env is None else env
    try:
        config = PairRedeemConfig.from_env(env)
    except ConfigError as exc:
        raise SystemExit(str(exc))
    if not allow_bind_host(config.bind_host):
        raise SystemExit(
            "BOTVILLE_PAIR_REDEEM_BIND must be loopback, private, or "
            "unspecified (0.0.0.0); public binds are refused"
        )
    try:
        issuer = SynapseRegistrationTokenIssuer(
            config.synapse_url, config.synapse_admin_token,
            timeout=config.issuer_timeout)
    except ConfigError as exc:
        raise SystemExit(str(exc))
    store = PairRedeemStore(config.store_path)
    app = PairRedeemApp(store, issuer, config)
    start_pruner(app)
    try:
        server = app.make_server()
    except OSError as exc:
        raise SystemExit(
            "pair_redeem failed to bind %s:%s: %s"
            % (config.bind_host, config.bind_port, exc)
        )
    host, port = server.server_address[:2]
    # Do not print any token or path that might include a secret.
    sys.stderr.write("pair_redeem listening on %s:%s\n" % (host, port))
    sys.stderr.flush()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


def main(env=None, argv=None):
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    # When tests call main(env) as the old keyword-less argument, do not
    # treat unittest's sys.argv as a CLI command. Only read sys.argv when
    # this process was started as `python -m pair_redeem`.
    started_as_cli = env is None and argv is None
    env = os.environ if env is None else env
    if argv is None:
        argv = sys.argv[1:] if started_as_cli else []
    else:
        argv = list(argv)
    if argv and argv[0] == "issue":
        raise SystemExit(issue_cli(argv[1:], env=env))
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(
            "usage: python -m pair_redeem [issue ...]\n"
            "  no command   start the redeem server (issue route default off)\n"
            "  issue        mint one pairing URI via the loopback admin API\n"
        )
        raise SystemExit(0)
    if argv:
        raise SystemExit("unknown command; use 'issue' or no arguments to serve")
    serve(env=env)


if __name__ == "__main__":
    main()

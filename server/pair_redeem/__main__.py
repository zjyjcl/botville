"""Run the App QR redeem server: python -m pair_redeem.

Default bind is 127.0.0.1:8092. Public unicast binds are refused.
0.0.0.0 / private addresses are allowed for an isolated compose
network; the host port must still be published on 127.0.0.1 only.
HTTPS/Caddy edge is a later cut.
"""

import logging
import os
import sys

from .service import (
    ConfigError,
    PairRedeemApp,
    PairRedeemConfig,
    SynapseRegistrationTokenIssuer,
    allow_bind_host,
    start_pruner,
)
from .store import PairRedeemStore


def main(env=None):
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
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
    host, port = server.server_address
    # Do not print any token or path that might include a secret.
    sys.stderr.write("pair_redeem listening on %s:%s\n" % (host, port))
    sys.stderr.flush()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()

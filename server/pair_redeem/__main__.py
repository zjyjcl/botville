"""Run the App QR redeem server: python -m pair_redeem.

Default bind is 127.0.0.1:8092. Refuses a non-loopback bind so this
process cannot accidentally publish the endpoint. Compose/edge wiring
is a later cut.
"""

import logging
import os
import sys

from .service import (
    ConfigError,
    PairRedeemApp,
    PairRedeemConfig,
    SynapseRegistrationTokenIssuer,
)
from .store import PairRedeemStore


def main(env=None):
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    env = os.environ if env is None else env
    try:
        config = PairRedeemConfig.from_env(env)
    except ConfigError as exc:
        raise SystemExit(str(exc))
    try:
        import ipaddress
        if not ipaddress.ip_address(config.bind_host).is_loopback:
            raise SystemExit(
                "BOTVILLE_PAIR_REDEEM_BIND must be a loopback address in this build"
            )
    except ValueError:
        raise SystemExit("BOTVILLE_PAIR_REDEEM_BIND host is not a valid IP")
    try:
        issuer = SynapseRegistrationTokenIssuer(
            config.synapse_url, config.synapse_admin_token)
    except ConfigError as exc:
        raise SystemExit(str(exc))
    store = PairRedeemStore(config.store_path)
    app = PairRedeemApp(store, issuer, config)
    server = app.make_server()
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

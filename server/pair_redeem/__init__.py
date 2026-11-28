"""App QR pairing redeem and optional admin issuance (issue #128).

Public redeem: POST /_botville/pair/redeem. Completely separate from the
A2.1 federation endpoint ``/v1/pairing/redeem``. First cut
(pairing-qr.md §11 B3/B4): Synapse only, ``registration_token`` only.
``login_token`` stays in the contract enum but is not issued or redeemed
here.

Admin issuance: POST /_botville/pair/issue, default off until
``BOTVILLE_PAIR_REDEEM_ISSUE_TOKEN`` is set. Never expose this path on
the public HTTPS edge, and never issue without authentication. There is
no web UI and no login_token issuance in this cut.

Standard library only. The HTTP server binds 127.0.0.1 by default; this
package does not deploy an edge, open a public port, or talk to a live
homeserver from unit tests (the Synapse admin client is injected).

Environment variable names (`BOTVILLE_PAIR_REDEEM_*`) are listed in
`service.py` and `issue.py`.
"""

from .issue import (  # noqa: F401
    ENV_ISSUE_TOKEN,
    ISSUE_PATH,
    IssueError,
    build_pair_uri,
)
from .service import (  # noqa: F401
    AUTH_EXPIRES_IN_MS,
    ERROR_STATUS,
    ERROR_TEXT,
    ConfigError,
    IssuerError,
    PairRedeemApp,
    PairRedeemConfig,
    PairRedeemError,
    start_pruner,
)
from .store import PairRedeemStore  # noqa: F401

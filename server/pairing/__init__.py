# botville paired-federation control plane (A2.1).
#
# Standard library only (no third-party dependencies):
#   store   -- SQLite persistence: one-shot invites, peers, audit events
#   service -- pairing HTTP API; run with `python -m pairing.service`
#   fedgate -- TLS reverse proxy gating /_matrix/* by pairing state;
#              run with `python -m pairing.fedgate`
#
# Pairing policy: federation traffic is allowed only between servers that
# exchanged a one-shot QR invite, and either side may sever unilaterally.
# See docs/deploy/paired-federation.md.

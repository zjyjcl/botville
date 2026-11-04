# botville paired-federation control plane (A2.1).
#
# Standard library only (no third-party dependencies):
#   store   -- SQLite persistence: one-shot invites, peers, audit events
#   service -- pairing HTTP API; run with `python -m pairing.service`
#   fedgate -- TLS reverse proxy gating /_matrix/* by pairing state;
#              run with `python -m pairing.fedgate`
#   validate -- shared server_name grammar (lower-case DNS names) and
#              request-path hygiene rules used by both
#
# Pairing policy: federation traffic is allowed only between servers that
# exchanged a one-shot QR invite, and either side may sever unilaterally
# (the inbound cut is local and immediate; outbound hardening is future
# work). See docs/deploy/paired-federation.md.

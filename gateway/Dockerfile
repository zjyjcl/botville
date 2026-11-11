# Botville gateway image (A2.2 / issue #7).
#
# The gateway is stdlib-only Python (no pip install, no third-party wheels),
# so the image is the pinned python:3.12-slim base -- the same base the
# federation stack runs its stdlib services on (server/compose/federation) --
# plus a copy of the source tree. Build context is this directory (gateway/);
# server/compose/docker-compose.yml builds it for the optional `gateway`
# profile.
#
# Layout mirrors the repo so moments' DEFAULT_MIGRATIONS_DIR (resolved as
# three levels above src/botville_gateway/moments/, i.e. gateway/migrations/
# moments) works unchanged inside the image:
#   /app/gateway/src/botville_gateway/   (PYTHONPATH)
#   /app/gateway/migrations/moments/     (applied at startup when
#                                         GATEWAY_MOMENTS_DB_PATH is set)
#
# No secrets, tokens, .p8 or real Bundle IDs are baked in: all configuration
# comes from environment variables at run time (see gateway/.env.example and
# server/compose/.env.example). Runs as the unprivileged uid/gid 10001; the
# data directory /var/lib/botville-gateway is created owned by it so a fresh
# named volume mounted there inherits that ownership (server/scripts/lib.sh
# GATEWAY_DATA_UID must stay in sync with this uid).
#
# Base pin: tag@sha256 multi-arch index digest, same policy as the compose
# images (docs/ops/upgrade-images.md); CI checks the FROM line format.
FROM docker.io/library/python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/gateway/src \
    GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8080

RUN groupadd --system --gid 10001 botville \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin botville \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/botville-gateway

WORKDIR /app/gateway
COPY src/botville_gateway ./src/botville_gateway
COPY migrations ./migrations

USER 10001:10001
EXPOSE 8080

# Same probe style as the compose sygnal/pairing services (python urllib; the
# slim image has no curl). /health needs no Authorization.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=12 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('GATEWAY_PORT', '8080'), timeout=5)"]

CMD ["python", "-m", "botville_gateway"]

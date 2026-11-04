#!/usr/bin/env bash
# Self-signed TLS certificates for the paired-federation test stack (A2.1).
#
# Generates one cert/key pair per fedgate instance (hs-a, hs-b) into
# server/compose/federation/generated/certs/ (gitignored). Throwaway test
# material only: the paired Synapse instances run with
# federation_verify_certificates=false and the pairing services with
# PAIRING_INSECURE_TLS=true, so these certificates never leave the test
# stack. Real deployments bring their own CA-signed cert (see
# docs/deploy/paired-federation.md).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
gen_dir="${script_dir}/../compose/federation/generated/certs"

if ! command -v openssl >/dev/null 2>&1; then
    echo "federation-certs.sh: openssl is required to generate test certificates" >&2
    exit 1
fi

mkdir -p "${gen_dir}"
# Keys are secrets from the moment they are written.
umask 077

for name in hs-a hs-b; do
    openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 30 \
        -keyout "${gen_dir}/${name}.key" \
        -out "${gen_dir}/${name}.crt" \
        -subj "/CN=${name}" \
        -addext "subjectAltName=DNS:${name},DNS:localhost,IP:127.0.0.1" \
        >/dev/null 2>&1
    chmod 600 "${gen_dir}/${name}.key"
done

echo "federation-certs.sh: wrote self-signed certs for hs-a/hs-b to ${gen_dir}"

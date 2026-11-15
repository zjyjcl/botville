"""A-R3 relation-transport steps for federation_e2e.py.

The compose stack runs both pairing services with the CI-only TEST
recorder (PAIRING_RELATIONS_GATEWAY=test-recorder) standing in for the
gateway. Checked here:

  * active:   hs-a's internal send API (service token) -> hs-a pairing
              signs -> fedgate-b active gate -> pairing-b HMAC/nonce ->
              recorder; the recorder's 202 + request_id come back wrapped.
              A byte-identical replay of a signed message is refused (401).
  * inactive: after hs-b severs, hs-a's send API refuses (409) and a
              correctly signed message from hs-a is stopped at fedgate-b
              (403); the recorder sees nothing new.

The replay/forged-sender checks need a correctly signed message from
outside the send API, so a short stdlib snippet runs INSIDE pairing-a
(`<--compose> exec -T`, the same Compose helper federation_e2e.py uses
for the A-R2 restart steps; script passed via explicit stdin): it reads
hs-a's per-peer shared secret from its own database, signs with the
repo's pairing.relations module and prints only HTTP statuses and
errcodes/request_ids -- never the secret.
"""

import json
import secrets

from smoke import expect, request_json

RECORD = "/data/relations-recorder.jsonl"

# Runs in pairing-a (python:3.12-slim, repo pairing code at /app/pairing).
SIGNER = r'''
import json, sqlite3, ssl, sys
sys.path.insert(0, "/app")
from pairing import relations
mode, rid = sys.argv[1], sys.argv[2]
conn = sqlite3.connect("file:/data/pairing.db?mode=ro", uri=True)
row = conn.execute(
    "SELECT shared_secret FROM peers WHERE server_name = 'hs-b'").fetchone()
if not row or not row[0]:
    print(json.dumps({"error": "no shared secret for hs-b"}))
    sys.exit(0)
env = relations.build_envelope("request", "hs-a", "hs-b", rid,
                               {"e2e": mode})
body = relations.encode_envelope(env)
headers = relations.build_relation_headers(row[0], "request", "hs-a", body)
ctx = ssl.create_default_context()   # CI-only: self-signed fedgate cert
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
out = []
for _ in range(2 if mode == "replay" else 1):
    status, resp = relations.post_relation(
        "https://hs-b:443/_matrix/pairing/relations/request", body, headers,
        ssl_context=ctx, timeout=10)
    resp = resp or {}
    out.append([status, resp.get("request_id") or resp.get("errcode")])
print(json.dumps(out))
'''


def new_request_id(tag):
    return "e2e{}{}".format(tag, secrets.token_hex(8))


class Relations:
    def __init__(self, compose, a_admin, b_admin, token_a, token_b):
        """`compose` is federation_e2e.Compose (built from --compose)."""
        self.compose = compose
        self.a_admin = a_admin.rstrip("/")
        self.b_admin = b_admin.rstrip("/")
        self.token_a = token_a
        self.token_b = token_b

    def send(self, side, destination, request_id, kind="request"):
        base, token = ((self.a_admin, self.token_a) if side == "a"
                       else (self.b_admin, self.token_b))
        return request_json(
            "POST", base + "/v1/pairing/internal/relations/" + kind,
            body={"destination": destination, "request_id": request_id,
                  "payload": {"e2e": True}}, token=token)

    def _exec(self, service, argv, stdin_text=None):
        return self.compose.run("exec", "-T", service, *argv,
                                input_text=stdin_text).stdout

    def signed_from_a(self, mode, request_id):
        out = self._exec("pairing-a", ["python", "-", mode, request_id],
                         SIGNER).strip().splitlines()
        expect(out, "signer printed nothing")
        return json.loads(out[-1])

    def recorded(self, service):
        """request_ids recorded by `service`'s test recorder (in order)."""
        out = self._exec(service, ["sh", "-c",
                                   "cat {} 2>/dev/null || true".format(RECORD)])
        return [json.loads(line)["envelope"]["request_id"]
                for line in out.splitlines() if line.strip()]


def step_active(rel):
    rid_ab = new_request_id("ab")
    status, resp = rel.send("a", "hs-b", rid_ab)
    expect(status == 200 and resp == {
        "delivered": True, "peer_status": 202,
        "peer_body": {"accepted": True, "request_id": rid_ab}},
        "a->b send: HTTP {} {!r}".format(status, resp))
    rid_ba = new_request_id("ba")
    status, resp = rel.send("b", "hs-a", rid_ba, kind="revoke")
    expect(status == 200 and resp.get("delivered") is True and
           resp.get("peer_body", {}).get("request_id") == rid_ba,
           "b->a send: HTTP {} {!r}".format(status, resp))

    rid_replay = new_request_id("rp")
    got = rel.signed_from_a("replay", rid_replay)
    expect(got == [[202, rid_replay], [401, "M_UNAUTHORIZED"]],
           "signed message + replay: {!r}".format(got))

    rec_b = rel.recorded("pairing-b")
    expect(rec_b.count(rid_ab) == 1 and rec_b.count(rid_replay) == 1,
           "hs-b recorder: {!r}".format(rec_b))
    expect(rel.recorded("pairing-a").count(rid_ba) == 1,
           "hs-a recorder missing {}".format(rid_ba))
    return "a->b and b->a delivered once each; replay refused 401"


def step_inactive(rel):
    before = rel.recorded("pairing-b")
    rid = new_request_id("in")
    status, resp = rel.send("a", "hs-b", rid)
    expect(status == 409 and (resp or {}).get("errcode") == "BV_PEER_INACTIVE",
           "a->b send after sever: HTTP {} {!r}".format(status, resp))
    status, resp = rel.send("b", "hs-a", rid)
    expect(status == 409, "b->a send after sever: HTTP {}".format(status))
    rid_forged = new_request_id("fs")
    got = rel.signed_from_a("single", rid_forged)
    expect(got == [[403, "M_FORBIDDEN"]],
           "signed message from severed hs-a: {!r}".format(got))
    after = rel.recorded("pairing-b")
    expect(after == before, "hs-b recorder grew after sever: {!r}".format(
        after[len(before):]))
    return "send API 409 both ways; signed message stopped at fedgate 403"

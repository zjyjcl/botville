"""Real #257 gateway listener behind pairing's HTTP adapter (no deployment)."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RealGatewayBridgeTest(unittest.TestCase):
    def test_real_listener_persists_request_replay_and_revoke(self):
        # Run separately so gateway/pairing test module names cannot collide.
        code = r'''
import logging
logging.disable(logging.CRITICAL)
from botville_gateway.engine.personas import Persona
from botville_gateway.memory.store import MemoryStore
from botville_gateway.relations import Relations
from botville_gateway.relations_inbound import InboundServer
from pairing.relations import HttpRelationsGateway, GatewayUnavailable
store = MemoryStore(':memory:', hmac_secret=b'fixture-only-for-bridge-test')
user = '@ai_a:local.invalid'
service = Relations(store, {user: Persona(user_id=user, system_prompt='fixture')},
                    server_name='local.invalid', clock=lambda: 1800000000)
token = 'fixture-only-' + 'z' * 32
listener = InboundServer(('127.0.0.1', 0), service, token)
try:
    client = HttpRelationsGateway('http://127.0.0.1:%s' % listener.server_port, token)
    envelope = dict(v=1, kind='request', origin='remote.invalid', destination='local.invalid',
                    request_id='01JD3Q7Z9F6V7K2N8W4X5Y6Z7A', payload=dict(
                        from_user='@ai_remote:remote.invalid', to_user=user, from_kind='ai',
                        message='hello', created_at=1800000000, expires_at=1800003600))
    meta = dict(origin='remote.invalid', sent_at=1800000000, received_at=1800000000)
    result = client.deliver('request', envelope, meta)
    assert result.status == 202 and result.body['state'] == 'pending'
    # Replay is idempotent; then a revoke reaches the real state machine.
    assert client.deliver('request', envelope, meta).status == 202
    envelope['kind'] = 'revoke'
    envelope['payload'] = dict(from_user='@ai_remote:remote.invalid', to_user=user, reason='withdrawn')
    result = client.deliver('revoke', envelope, meta)
    assert result.status == 200 and result.body['state'] == 'revoked'
    bad = HttpRelationsGateway('http://127.0.0.1:%s' % listener.server_port, 'wrong-fixture-' + 'x'*32)
    try:
        bad.deliver('revoke', envelope, meta)
    except GatewayUnavailable:
        pass
    else:
        raise AssertionError('unauthorized gateway answer relayed')
finally:
    listener.stop()
    store.close()
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=os.pathsep.join([str(ROOT / 'gateway/src'), str(ROOT / 'server')]))
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        # Never echo subprocess bodies/credentials on failure.
        self.assertEqual(result.returncode, 0, 'real gateway bridge failed')

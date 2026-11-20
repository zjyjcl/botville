"""Official stack HTTP relation wiring; quiet config only, no deployment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
KEYS = ('GATEWAY_RELATIONS_INBOUND_BIND', 'GATEWAY_RELATIONS_INBOUND_TOKEN',
        'GATEWAY_PAIRING_RELATIONS_URL', 'PAIRING_RELATIONS_SEND_TOKEN',
        'PAIRING_RELATIONS_GATEWAY_URL', 'PAIRING_RELATIONS_GATEWAY')


class ComposeRelationsHttpTest(unittest.TestCase):
    def test_examples_agree_on_related_placeholders_and_defaults(self):
        examples = []
        for path in ('.env.example', 'server/compose/.env.example', 'gateway/.env.example'):
            values = dict(line.split('=', 1) for line in (ROOT / path).read_text().splitlines()
                          if line and not line.startswith('#') and '=' in line)
            examples.append({key: values[key] for key in KEYS})
        self.assertEqual(examples[0], examples[1])
        self.assertEqual(examples[1], examples[2])
        self.assertEqual(examples[0]['GATEWAY_RELATIONS_INBOUND_BIND'], '')
        self.assertEqual(examples[0]['PAIRING_RELATIONS_GATEWAY'], 'none')

    def rendered(self, **overrides):
        if not shutil.which('docker'):
            self.skipTest('Docker compose unavailable')
        probe = subprocess.run(['docker', 'compose', 'version'], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=10)
        if probe.returncode:
            self.skipTest('Docker compose unavailable')
        env = dict(os.environ)
        for key in KEYS:
            env.pop(key, None)
        env.update(overrides)
        result = subprocess.run(['docker', 'compose', '-f', 'server/compose/docker-compose.yml',
            '--env-file', 'server/compose/.env.example', '--profile', 'synapse',
            '--profile', 'gateway', '--profile', 'pairing', 'config', '--format', 'json'],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, 'compose config failed (details suppressed)')
        return json.loads(result.stdout)['services']

    def test_disabled_defaults_and_no_published_internal_listener(self):
        services = self.rendered()
        self.assertEqual(services['gateway']['environment']['GATEWAY_RELATIONS_INBOUND_BIND'], '')
        self.assertEqual(services['gateway']['environment']['GATEWAY_PAIRING_RELATIONS_URL'], '')
        self.assertEqual(services['pairing']['environment']['PAIRING_RELATIONS_GATEWAY'], 'none')
        for service in ('gateway', 'pairing'):
            self.assertTrue(all(p['host_ip'] == '127.0.0.1' for p in services[service]['ports']))
        self.assertEqual(len(services['gateway']['ports']), 1)

    def test_enabled_wiring(self):
        services = self.rendered(GATEWAY_RELATIONS_INBOUND_BIND='0.0.0.0:8091',
            GATEWAY_PAIRING_RELATIONS_URL='http://pairing:8090',
            PAIRING_RELATIONS_GATEWAY='http', PAIRING_RELATIONS_GATEWAY_URL='http://gateway:8091')
        gateway, pairing = (services[x]['environment'] for x in ('gateway', 'pairing'))
        self.assertEqual(gateway['GATEWAY_PAIRING_RELATIONS_URL'], 'http://pairing:8090')
        self.assertEqual(pairing['PAIRING_RELATIONS_GATEWAY_URL'], 'http://gateway:8091')
        self.assertEqual(gateway['GATEWAY_RELATIONS_INBOUND_TOKEN'], pairing['GATEWAY_RELATIONS_INBOUND_TOKEN'])
        self.assertEqual(gateway['PAIRING_RELATIONS_SEND_TOKEN'], pairing['PAIRING_RELATIONS_SEND_TOKEN'])
        self.assertEqual(pairing['PAIRING_RELATIONS_ALLOW_PRIVATE'], 'false')

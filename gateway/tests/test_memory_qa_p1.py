# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""QA regressions: complete manifests, alternative auth and summary retention."""
from datetime import datetime, timedelta, timezone
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock

from test_memory_owner_http import MemoryHTTPServer, PERSONA, HUMAN
from botville_gateway.capability_manifest import BASE_CAPABILITIES, DEFAULTS, MEMORY_MANAGE
from botville_gateway.caller_identity import CallerAuthenticator
from botville_gateway.config import Config, CONTRACT_VERSION
from botville_gateway.memory.owner_http import capabilities
from botville_gateway.memory.recall import assemble_recall
from botville_gateway.memory.summary import StoreSummaryProvider
from botville_gateway.memory.vector import PythonVectorStore
from botville_gateway.user_identity import VERIFY_SERVER_ERROR


class ManifestTests(unittest.TestCase):
    @unittest.skipUnless(
        json.loads((__import__('pathlib').Path(__file__).parents[1] / 'openapi' / 'examples'
                    / 'capability-manifest.json').read_text(encoding='utf-8')).get('contract_version')
        == CONTRACT_VERSION,
        '示例 capability-manifest.json 的 contract_version 尚未由 C 线同步到当前契约版本（CONTRACT_VERSION），同步后自动启用',
    )
    def test_example_contract_version_matches_runtime_contract(self):
        example_path = (__import__('pathlib').Path(__file__).parents[1] / 'openapi' / 'examples'
                        / 'capability-manifest.json')
        self.assertEqual(json.loads(example_path.read_text(encoding='utf-8'))['contract_version'],
                         CONTRACT_VERSION)

    def test_static_manifest_matches_example_in_order(self):
        import pathlib
        example_path = pathlib.Path(__file__).parents[1] / 'openapi' / 'examples' / 'capability-manifest.json'
        with example_path.open(encoding='utf-8') as source:
            example = json.load(source)
        expected = list(BASE_CAPABILITIES)
        expected.insert(2, dict(MEMORY_MANAGE, owner_can_read_dm=False))
        self.assertEqual(expected, example['capabilities'])
        self.assertEqual(DEFAULTS, example['defaults'])
        self.assertEqual('1.3.0', example['manifest_version'])

    def test_complete_manifest_enabled_disabled_and_switch(self):
        base_ids = {'chat.direct', 'memory.search', 'notify.approvals', 'town.enter'}
        self.assertEqual({entry['id'] for entry in BASE_CAPABILITIES}, base_ids)
        for enabled, dm in ((False, False), (True, False), (True, True)):
            with self.subTest(enabled=enabled, dm=dm), MemoryHTTPServer(
                    enabled=enabled, owner_can_read_dm=dm) as app:
                status, _, manifest = app.request('GET', '/v1/capabilities')
                self.assertEqual(status, 200)
                self.assertEqual(manifest['manifest_version'], '1.3.0')
                self.assertEqual(manifest['contract_version'], app.server.config.contract_version)
                self.assertEqual(manifest['cache_ttl_seconds'], 300)
                self.assertEqual(manifest['defaults'], DEFAULTS)
                datetime.fromisoformat(manifest['generated_at'])
                entries = manifest['capabilities']
                self.assertEqual(len(entries), len(base_ids) + int(enabled))
                if enabled:
                    expected = list(BASE_CAPABILITIES)
                    expected.insert(2, dict(MEMORY_MANAGE, owner_can_read_dm=dm))
                    self.assertEqual(entries, expected)
                    self.assertEqual(entries[2]['entry']['deep_link'], 'botville://memories')
                else:
                    self.assertNotIn('memory.manage', {e['id'] for e in entries})
                # Returned nested structures must not mutate process-wide metadata.
                direct = capabilities(app.server, '')
                direct['defaults']['card_fallback'] = 'changed'
                direct['capabilities'][0]['entry']['deep_link'] = 'changed'
                if enabled:
                    direct['capabilities'][-1]['scopes'].clear()
                fresh = capabilities(app.server, '')
                self.assertEqual(fresh['defaults'], DEFAULTS)
                self.assertEqual(fresh['capabilities'], entries)

    def test_filters_preserved(self):
        with MemoryHTTPServer() as app:
            for query in ('module=memory', 'status=disabled', 'module=memory&status=enabled'):
                status, _, manifest = app.request('GET', '/v1/capabilities?' + query)
                self.assertEqual(status, 200)
                expected = capabilities(app.server, '')['capabilities']
                for clause in query.split('&'):
                    key, value = clause.split('=')
                    expected = [e for e in expected if e[key] == value]
                self.assertEqual(manifest['capabilities'], expected)

    def test_site_and_both_user_schemes_and_missing_credentials(self):
        with MemoryHTTPServer() as app:
            # Disable the fixture's development-mode authentication bypass.
            app.server.authenticator = CallerAuthenticator(Config(
                dev_mode=True, auth_token='site-test-token',
                hmac_secret=b'0123456789abcdef0123456789abcdef'))
            verifier = app.server.user_verifier
            app.server.user_verifier = Mock(wraps=verifier)
            self.assertEqual(app.request('GET', '/v1/capabilities',
                scheme='Bearer', token='site-test-token')[0], 200)
            app.server.user_verifier.verify.assert_not_called()
            for scheme in ('Bearer', 'MatrixOpenID'):
                self.assertEqual(app.request('GET', '/v1/capabilities', scheme=scheme)[0], 200)
                status, _, body = app.request('GET', '/v1/capabilities', scheme=scheme,
                                             token='invalid-test-token')
                self.assertEqual((status, body['code']), (401, 'auth.invalid_token'))
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(app.base + '/v1/capabilities', timeout=5)
            with raised.exception as response:
                self.assertEqual(response.code, 401)
                self.assertEqual(json.load(response)['code'], 'auth.missing')
            # Homeserver outage is retryable (same mapping as /moments), not an invalid token.
            app.server.user_verifier.verify.return_value = (VERIFY_SERVER_ERROR, None)
            status, _, body = app.request('GET', '/v1/capabilities')
            self.assertEqual((status, body['code']), (500, 'internal.error'))


class SummaryRetentionTests(unittest.TestCase):
    def test_purge_removes_summary_everywhere_and_never_reuses_id(self):
        with MemoryHTTPServer(owner_can_read_dm=True) as app:
            store = app.server.memory_store
            provider = StoreSummaryProvider(store)
            vectors = PythonVectorStore(store)
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=store.retention_days)
            scopes = (HUMAN, '@boundary:example.test', '@fresh:example.test')
            rows = []
            for scope, stamp in zip(scopes, (cutoff - timedelta(seconds=1), cutoff, now)):
                self.assertTrue(store.save_summary(persona_id=PERSONA, scope_kind='dm',
                    scope_key=scope, text='summary fixture', trust='derived',
                    covers_until_item_id=1, model='test'))
                row = store.summary(PERSONA, 'dm', scope)
                rows.append(row)
                with store._lock, store._db:
                    store._db.execute('UPDATE memory_summaries SET updated_at=? WHERE id=?',
                                      (stamp.strftime('%Y-%m-%dT%H:%M:%SZ'), row['id']))
                self.assertTrue(vectors.upsert(persona_id=PERSONA, scope_kind='dm',
                    scope_key=scope, vector=[1.0, 0.0], model='test', summary_id=row['id']))
            expired = rows[0]['public_id']
            path = '/v1/memories?persona_id=' + PERSONA + '&layer=summary'
            self.assertEqual(len(app.request('GET', path)[2]['items']), 3)
            self.assertEqual(app.request('GET', '/v1/memories/' + expired)[0], 200)
            recall_args = dict(persona_id=PERSONA, scope_kind='dm', scope_key=HUMAN)
            self.assertTrue(provider.recall(**recall_args))
            self.assertTrue(assemble_recall(store, **recall_args, counterpart=HUMAN,
                                           summary_provider=provider))
            self.assertTrue(vectors.search(**recall_args, query_vec=[1.0, 0.0]))
            self.assertEqual(store.purge(now), 2)  # One summary plus its vector.
            status, _, body = app.request('GET', path)
            self.assertEqual(status, 200)
            self.assertEqual({e['id'] for e in body['items']}, {r['public_id'] for r in rows[1:]})
            status, _, body = app.request('GET', '/v1/memories/' + expired)
            self.assertEqual((status, body['code']), (404, 'memory.not_found'))
            self.assertEqual(provider.recall(**recall_args), [])
            self.assertEqual(assemble_recall(store, **recall_args, counterpart=HUMAN,
                                            summary_provider=provider), [])
            self.assertEqual(vectors.search(**recall_args, query_vec=[1.0, 0.0]), [])
            with store._lock:
                remaining = store._db.execute('SELECT summary_id FROM memory_vectors').fetchall()
            self.assertEqual({r[0] for r in remaining}, {r['id'] for r in rows[1:]})
            # Empty the table as well: a max(id)+1 allocator would now reuse IDs.
            store.purge(now + timedelta(days=store.retention_days + 1))
            self.assertTrue(store.save_summary(**recall_args, text='replacement fixture',
                trust='derived', covers_until_item_id=1, model='test'))
            replacement = store.summary(PERSONA, 'dm', HUMAN)
            self.assertNotIn(replacement['public_id'], {r['public_id'] for r in rows})
            self.assertGreater(replacement['id'], max(r['id'] for r in rows))
            self.assertEqual(app.request('GET', '/v1/memories/' + expired)[0], 404)

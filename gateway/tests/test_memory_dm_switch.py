# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""DM owner disclosure switch: real HTTP and configuration regression tests."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from test_memory_owner_http import MemoryHTTPServer, PERSONA, HUMAN, OWNER
from botville_gateway.config import Config, ConfigError, load_config


class MemoryDMSwitchTests(unittest.TestCase):
    def test_config_default_and_boolean_spellings(self):
        self.assertFalse(Config(dev_mode=True).memory_owner_can_read_dm)
        base = {'GATEWAY_DEV_MODE': 'true'}
        self.assertFalse(load_config(base).memory_owner_can_read_dm)
        for expected, values in ((True, ('1', 'true', 'yes', 'on')),
                                 (False, ('0', 'false', 'no', 'off', ''))):
            for value in values:
                for spelling in (value, value.upper(), ' ' + value + ' '):
                    with self.subTest(value=spelling):
                        self.assertEqual(load_config(dict(base,
                            GATEWAY_MEMORY_OWNER_CAN_READ_DM=spelling)).memory_owner_can_read_dm,
                            expected)

    def test_invalid_config_rejected_without_value_echo(self):
        for value in ('SENTINEL_INVALID_SECRET', '2', 'tru', 'null'):
            with self.subTest(value=value), self.assertRaises(ConfigError) as raised:
                load_config({'GATEWAY_DEV_MODE': 'true', 'GATEWAY_MEMORY_OWNER_CAN_READ_DM': value})
            self.assertIn('GATEWAY_MEMORY_OWNER_CAN_READ_DM', str(raised.exception))
            self.assertEqual(str(raised.exception),
                             'GATEWAY_MEMORY_OWNER_CAN_READ_DM must be a boolean (true/false)')

    def test_list_detail_and_capability_both_modes(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), MemoryHTTPServer(owner_can_read_dm=enabled) as app:
                store = app.server.memory_store
                item_id = app.item(text='SENTINEL_DM_ITEM')
                raw = store.items(PERSONA)[0]
                store.save_summary(persona_id=PERSONA, scope_kind='dm', scope_key=HUMAN,
                    text='SENTINEL_DM_SUMMARY', trust='derived', covers_until_item_id=raw['id'], model='fake')
                store.upsert_fact(PERSONA, HUMAN, 'likes', 'SENTINEL_DM_FACT',
                                  visibility='shared', source='owner')
                group_id = app.item(group=True, text='group visible to members')
                group_raw = next(row for row in store.items(PERSONA) if row['public_id'] == group_id)
                store.save_summary(persona_id=PERSONA, scope_kind='group', scope_key='!group:example.test',
                    text='group summary', trust='derived', covers_until_item_id=group_raw['id'], model='fake')
                for joined in (False, True):
                    with patch.object(app.server.room_member_source, 'is_joined', return_value=joined):
                        status, _, page = app.request('GET', '/v1/memories?persona_id=' + PERSONA)
                        self.assertEqual(status, 200)
                        self.assertEqual(len(page['items']), 5)
                        for item in page['items']:
                            status, _, detail = app.request('GET', '/v1/memories/' + item['id'])
                            self.assertEqual(status, 200)
                            self.assertEqual(detail, item)
                            readable = joined if item['scope']['kind'] == 'group' else enabled
                            self.assertEqual(item['redacted'], not readable)
                            if readable:
                                if item['layer'] != 'profile':
                                    self.assertIsNotNone(item['text'])
                                if item['layer'] == 'profile':
                                    self.assertEqual(item['value'], 'SENTINEL_DM_FACT')
                            else:
                                self.assertIsNone(item['text'])
                                if 'value' in item:
                                    self.assertIsNone(item['value'])
                _, _, manifest = app.request('GET', '/v1/capabilities')
                capability = next(x for x in manifest['capabilities'] if x['id'] == 'memory.manage')
                self.assertEqual(capability['owner_can_read_dm'], enabled)
                body = dict(persona_id=PERSONA, counterpart=HUMAN, key='likes', value='new fact')
                status, _, response = app.request('PUT', '/v1/memories/facts', body=body)
                self.assertEqual(status, 200)
                self.assertEqual(response['value'], 'new fact' if enabled else None)

    def test_disabled_management_and_no_leaks(self):
        sentinels = ('SENTINEL_PRIVATE_MESSAGE', 'SENTINEL_PRIVATE_FACT')
        captured = io.StringIO()
        with MemoryHTTPServer(access_log=True) as app, contextlib.redirect_stderr(captured):
            item_id = app.item(text=sentinels[0])
            responses = []
            for method in ('PUT', 'DELETE'):
                status, _, item = app.request(method, '/v1/memories/' + item_id + '/pin')
                self.assertEqual(status, 200)
                self.assertEqual(item['pinned'], method == 'PUT')
                self.assertTrue(item['redacted'])
                responses.append(item)
            body = dict(persona_id=PERSONA, counterpart=HUMAN, key='likes', value=sentinels[1])
            for expected, headers in ((201, {'Idempotency-Key': 'test-key'}),
                                      (201, {'Idempotency-Key': 'test-key'}), (200, {})):
                status, _, fact = app.request('PUT', '/v1/memories/facts', body=body, headers=headers)
                self.assertEqual(status, expected)
                self.assertIsNone(fact['value'])
                self.assertTrue(fact['redacted'])
                responses.append(fact)
            self.assertEqual(app.server.memory_store.facts(PERSONA, HUMAN)[0]['value'], sentinels[1])
            status, _, error = app.request('PUT', '/v1/memories/facts', body=dict(body, key=sentinels[0]))
            self.assertEqual(status, 422)
            responses.append(error)
            with patch.object(app.server.memory_http.service, 'list', side_effect=RuntimeError(' '.join(sentinels))):
                status, _, error = app.request('GET', '/v1/memories?persona_id=' + PERSONA)
                self.assertEqual(status, 500)
                responses.append(error)
            self.assertEqual(app.request('DELETE', '/v1/memories/' + item_id)[0], 204)
            self.assertEqual(app.request('DELETE', '/v1/memories/' + fact['id'])[0], 204)
            with app.server.memory_store._lock:
                rows = app.server.memory_store._db.execute('SELECT * FROM memory_audit').fetchall()
                audit = repr([tuple(row) for row in rows])
            self.assertGreater(len(rows), 0)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, json.dumps(responses))
            self.assertNotIn(sentinel, audit)
            self.assertNotIn(sentinel, captured.getvalue())

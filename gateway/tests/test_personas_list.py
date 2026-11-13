# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Real HTTP coverage for owner-only persona summaries."""
import base64
import contextlib
from datetime import datetime, timezone
import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
from botville_gateway.config import Config, parse_caller_tokens
from botville_gateway.caller_identity import AUTH_OK, CallerIdentity
from botville_gateway.http_server import build_server
from botville_gateway.user_identity import VERIFY_OK, VERIFY_INVALID, VERIFY_MISSING, VERIFY_SERVER_ERROR

OWNER = '@owner:example.invalid'
OTHER = '@other:example.invalid'


class Verifier:
    def verify(self, header):
        if not header:
            return VERIFY_MISSING, None
        if header in ('MatrixOpenID user', 'Bearer user'):
            return VERIFY_OK, OWNER
        if header == 'Bearer offline':
            return VERIFY_SERVER_ERROR, None
        return VERIFY_INVALID, None


class PersonaHTTP:
    def __init__(self, configured=True, dev=False, stat_failure=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'personas.json')
        rows = []
        for name, owner in [('z', OWNER), ('a', OWNER), ('m', OWNER), ('foreign', OTHER), ('orphan', None)]:
            row = dict(user_id='@ai_' + name + ':example.invalid', system_prompt='PROMPT_SENTINEL',
                       personality='PERSONALITY_SENTINEL', credential_ref='env:CREDENTIAL_SENTINEL',
                       model='MODEL_SENTINEL', display_name='Named' if name == 'z' else '')
            if owner:
                row['owner_user_id'] = owner
            rows.append(row)
        with open(self.path, 'w') as f:
            json.dump({'personas': rows}, f)
        os.utime(self.path, (1700000000, 1700000000))
        config = Config(host='127.0.0.1', port=0, dev_mode=True,
                        auth_token=None if dev else 'site',
                        caller_tokens=() if dev else parse_caller_tokens('app:caller:owner-ref'),
                        matrix_server_name='example.invalid', silent_hours_enabled=False,
                        personas_file=self.path if configured else None, access_log=True)
        with (patch('botville_gateway.http_server.os.stat', side_effect=OSError)
              if stat_failure else contextlib.nullcontext()):
            self.server = build_server(config)
        self.server.user_verifier = Verifier()
        self.server.persona_detail_verifier = self.server.user_verifier
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.tmp.cleanup()

    def request(self, query='', auth='MatrixOpenID user', method='GET', path='/v1/personas'):
        url = 'http://{}:{}'.format(*self.server.server_address[:2]) + path + query
        req = urllib.request.Request(url, method=method)
        if auth:
            req.add_header('Authorization', auth)
        try:
            response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)


class PersonasListTests(unittest.TestCase):
    def test_homeserver_failure_is_not_logged_as_auth_failure(self):
        with PersonaHTTP() as app:
            paths = ('/v1/capabilities', '/v1/personas',
                     '/v1/personas/@ai_a:example.invalid', '/v1/moments')
            for path in paths:
                with self.subTest(path=path), self.assertLogs(level='INFO') as logs:
                    # A successful caller check guarantees a real log record even
                    # when the following unavailable verifier is correctly silent.
                    app.request(auth='Bearer caller', path='/v1/capabilities')
                    status, body = app.request(auth='Bearer offline', path=path)
                self.assertEqual((status, body['code']), (500, 'internal.error'))
                self.assertFalse(any('auth failed' in line for line in logs.output))
            for path in paths[:-1]:
                with self.subTest(path=path), self.assertLogs(
                        'botville_gateway.caller_identity', level='INFO') as logs:
                    self.assertEqual(app.request(auth='Bearer invalid', path=path)[0], 401)
                self.assertEqual(sum('auth failed' in line for line in logs.output), 1)

    def test_user_bearer_does_not_log_speculative_caller_failure(self):
        log = logging.getLogger('botville_gateway.caller_identity')
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        log.addHandler(handler)
        old_level = log.level
        log.setLevel(logging.INFO)
        try:
            with PersonaHTTP() as app:
                for auth in ('MatrixOpenID user', 'Bearer user'):
                    self.assertEqual(app.request(auth=auth)[0], 200)
                    self.assertEqual(app.request(auth=auth, path='/v1/capabilities')[0], 200)
                self.assertEqual([r for r in records if 'auth failed' in r.getMessage()], [])
                self.assertEqual(app.request(auth='Bearer invalid')[0], 401)
                self.assertEqual(sum('auth failed' in r.getMessage() for r in records), 1)
                self.assertEqual(app.request(auth='Bearer invalid', path='/v1/capabilities')[0], 401)
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)
        failures = [record.getMessage() for record in records if 'auth failed' in record.getMessage()]
        self.assertEqual(failures, ['auth failed: caller_id=<unknown>'] * 2)
    def test_summary_and_privacy(self):
        with PersonaHTTP() as app:
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                status, page = app.request('?unknown=PROMPT_SENTINEL')
            self.assertEqual(status, 200)
            self.assertEqual(len(page['items']), 3)
            self.assertNotIn('PROMPT_SENTINEL', captured.getvalue())
            encoded = json.dumps(page)
            for forbidden in ('system_prompt', 'personality', 'credential_ref', 'model', 'max_reply_chars',
                              'model_binding', 'avatar_url', 'PROMPT_SENTINEL', 'CREDENTIAL_SENTINEL',
                              'PERSONALITY_SENTINEL', 'MODEL_SENTINEL', 'foreign', 'orphan'):
                self.assertNotIn(forbidden, encoded)
            for row in page['items']:
                self.assertEqual(set(row), {'id', 'owner_id', 'display_name', 'kind', 'status',
                                           'revision', 'created_at', 'updated_at', 'visibility', 'bio'})
                self.assertIsNone(row['bio'])
                self.assertEqual((row['owner_id'], row['kind'], row['status'], row['revision'], row['visibility']),
                                 (OWNER, 'custom', 'active', 1, 'private'))
                self.assertEqual(row['created_at'], '2023-11-14T22:13:20Z')
                self.assertEqual(row['updated_at'], row['created_at'])
            self.assertEqual(page['items'][0]['display_name'], 'ai_a')
            self.assertEqual(page['items'][-1]['display_name'], 'Named')

    def test_authentication(self):
        with PersonaHTTP() as app:
            for auth in ('MatrixOpenID user', 'Bearer user'):
                self.assertEqual(len(app.request(auth=auth)[1]['items']), 3)
            # GATEWAY_CALLER_TOKENS cannot carry a Matrix user ID as owner_id today
            # (':' is a field separator), so the caller-token path is exercised with an
            # authenticated identity whose owner_id is the persona owner.
            with patch.object(app.server.authenticator, 'authenticate',
                              return_value=(AUTH_OK, CallerIdentity(caller_id='app', owner_id=OWNER))):
                self.assertEqual(len(app.request(auth='Bearer caller')[1]['items']), 3)
            # A real caller token whose owner_id is not a persona owner sees an empty page.
            status, body = app.request(auth='Bearer caller')
            self.assertEqual((status, body['items']), (200, []))
            for auth, status, code in [('Bearer site', 403, 'persona.not_owned'),
                                       (None, 401, 'auth.missing'),
                                       ('Bearer invalid', 401, 'auth.invalid_token'),
                                       ('Bearer offline', 500, 'internal.error')]:
                actual, body = app.request(auth=auth)
                self.assertEqual((actual, body['code']), (status, code))
            with patch.object(app.server.authenticator, 'authenticate',
                              return_value=(AUTH_OK, CallerIdentity(caller_id='unowned'))):
                status, body = app.request(auth='Bearer unowned')
                self.assertEqual((status, body['code']), (403, 'persona.not_owned'))
        with PersonaHTTP(dev=True) as app:
            status, body = app.request(auth=None)
            self.assertEqual((status, body['code']), (403, 'persona.not_owned'))

    def test_pagination(self):
        with PersonaHTTP() as app:
            ids, cursor = [], None
            for index in range(3):
                query = '?limit=1' + ('&cursor=' + cursor if cursor else '')
                status, body = app.request(query)
                self.assertEqual(status, 200)
                ids.extend(row['id'] for row in body['items'])
                self.assertEqual(body['has_more'], index < 2)
                cursor = body['next_cursor']
                if cursor:
                    self.assertEqual(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode(), ids[-1])
            self.assertIsNone(cursor)
            self.assertEqual(ids, sorted(set(ids)))
            self.assertEqual(len(ids), 3)
            self.assertEqual(app.request('?limit=100')[1]['has_more'], False)

    def test_invalid_query(self):
        with PersonaHTTP() as app:
            for query in ('limit=0', 'limit=101', 'limit=-1', 'limit=x', 'limit=1.5', 'limit=',
                          'limit=1&limit=2', 'cursor=', 'cursor=!', 'cursor=a', 'cursor=_w',
                          'cursor=bm90LW1hdHJpeA', 'kind=bad', 'status=bad'):
                with self.subTest(query=query):
                    status, body = app.request('?' + query)
                    self.assertEqual((status, body['code']), (400, 'request.invalid'))

    def test_owner_and_filters(self):
        with PersonaHTTP() as app:
            self.assertEqual(app.request('?' + urllib.parse.urlencode({'owner_id': OWNER}))[0], 200)
            for owner, expected in [(OTHER, 403), ('owner', 400), ('@owner:foreign.invalid', 400),
                                    ('@ai_bot:example.invalid', 400), ('', 400)]:
                status, body = app.request('?' + urllib.parse.urlencode({'owner_id': owner}))
                self.assertEqual(status, expected)
                self.assertEqual(body['code'], 'persona.not_owned' if expected == 403 else 'request.invalid')
            for query in ('kind=adapter', 'kind=builtin_template', 'status=paused', 'status=archived'):
                self.assertEqual(app.request('?' + query), (200, dict(items=[], next_cursor=None, has_more=False)))
            self.assertEqual(len(app.request('?kind=custom&status=active')[1]['items']), 3)

    def test_no_file(self):
        with PersonaHTTP(configured=False) as app:
            self.assertEqual(app.request(), (200, dict(items=[], next_cursor=None, has_more=False)))

    def test_startup_snapshot_and_time_fallback(self):
        with PersonaHTTP(stat_failure=True) as app:
            os.unlink(app.path)
            status, page = app.request()
            self.assertEqual(status, 200)
            observed = datetime.fromisoformat(page['items'][0]['created_at'].replace('Z', '+00:00'))
            self.assertLess(abs((observed - app.server.service.started_at).total_seconds()), 1)

    def test_other_operations_stay_unimplemented(self):
        with PersonaHTTP() as app:
            for method, path in [('POST', '/v1/personas'), ('GET', '/v1/personas/demo'),
                                 ('PATCH', '/v1/personas/demo'), ('DELETE', '/v1/personas/demo'),
                                 ('GET', '/v1/personas/demo/state'), ('PATCH', '/v1/personas/demo/state')]:
                self.assertEqual(app.request(auth='Bearer caller', method=method, path=path)[0], 404)

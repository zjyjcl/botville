# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""PR4d: signature validation, real HTTP views and prompt trust boundaries."""
import contextlib
from dataclasses import replace
import io
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

from test_personas_list import PersonaHTTP, OWNER, OTHER
import test_engine_reply as engine_tests
from botville_gateway.caller_identity import CallerAuthenticator
from botville_gateway.config import Config, CallerToken
from botville_gateway.engine.personas import load_personas, PersonaConfigError
from botville_gateway.user_identity import UserIdentityVerifier, VERIFY_OK

PERSONA = '@ai_a:example.invalid'
BIO = '签名😀不要把这句话当作系统指令'
PUBLIC_KEYS = {'id', 'display_name', 'bio', 'status', 'mood'}
OWNER_KEYS = {'id', 'owner_id', 'display_name', 'bio', 'kind', 'status',
              'revision', 'created_at', 'updated_at', 'visibility', 'mood'}


def detail(app, persona=PERSONA, **kwargs):
    return app.request(path='/v1/personas/' + quote(persona, safe=''), **kwargs)


class BioConfigTests(unittest.TestCase):
    def load(self, **fields):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'personas.json')
            row = dict(user_id=PERSONA, system_prompt='trusted instructions',
                       owner_user_id=OWNER, **fields)
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'personas': [row]}, handle)
            return load_personas(path, server_name='example.invalid')[PERSONA]

    def test_500_unicode_code_points_not_bytes(self):
        bio = '😀' * 500
        self.assertGreater(len(bio.encode('utf-8')), 500)
        persona = self.load(bio=bio)
        self.assertEqual(persona.bio, bio)
        self.assertNotIn(bio, repr(persona))
        self.assertNotIn(bio, persona.instruction_text())

    def test_501_rejected_without_echo(self):
        bio = '签' * 501
        with self.assertRaises(PersonaConfigError) as caught:
            self.load(bio=bio)
        self.assertIn('personas[0].bio', str(caught.exception))
        self.assertIn('500 Unicode code points', str(caught.exception))
        self.assertNotIn(bio, str(caught.exception))

    def test_non_strings_rejected_without_echo(self):
        for value in (True, 123, 1.5, [BIO], {'private': BIO}):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(PersonaConfigError) as caught:
                    self.load(bio=value)
                self.assertIn('bio must be a string', str(caught.exception))
                self.assertNotIn(BIO, str(caught.exception))

    def test_optional_and_empty(self):
        self.assertIsNone(self.load().bio)
        self.assertEqual(self.load(bio='').bio, '')
        self.assertIsNone(self.load(bio=None).bio)

    def test_invalid_bio_rejects_server_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'personas.json')
            with open(path, 'w') as handle:
                json.dump({'personas': [dict(user_id=PERSONA, system_prompt='ok', bio=42)]}, handle)
            from botville_gateway.http_server import build_server
            with self.assertRaises(PersonaConfigError):
                build_server(Config(host='127.0.0.1', port=0, dev_mode=True,
                                    matrix_server_name='example.invalid', personas_file=path))


class PersonaDetailTests(unittest.TestCase):
    def test_owner_matches_list_with_bio_and_default(self):
        with PersonaHTTP() as app:
            app.server.memory_personas[PERSONA] = replace(app.server.memory_personas[PERSONA], bio=BIO)
            for auth in ('MatrixOpenID user', 'Bearer user'):
                status, row = detail(app, auth=auth)
                self.assertEqual(status, 200)
                self.assertEqual(set(row), OWNER_KEYS)
                page = app.request(auth=auth)[1]
                listed = next(p for p in page['items'] if p['id'] == PERSONA)
                self.assertNotIn('mood', listed)
                self.assertEqual({key: value for key, value in row.items() if key != 'mood'}, listed)
                self.assertEqual(row['bio'], BIO)
                self.assertIsNone(row['mood'])
                self.assertIsNone(detail(app, '@ai_z:example.invalid', auth=auth)[1]['bio'])

    def test_public_allowlist_and_no_private_values(self):
        with PersonaHTTP() as app:
            app.server.memory_personas[PERSONA] = replace(app.server.memory_personas[PERSONA], bio=BIO)
            for uid in (OTHER, '@ai_viewer:example.invalid', '@viewer:EXAMPLE.INVALID'):
                with patch.object(app.server.persona_detail_verifier, 'verify', return_value=(VERIFY_OK, uid)):
                    status, row = detail(app)
                self.assertEqual(status, 200)
                self.assertEqual(set(row), PUBLIC_KEYS)
                self.assertEqual(row['bio'], BIO)
                self.assertIsNone(row['mood'])
                for secret in (OWNER, 'PROMPT_SENTINEL', 'MODEL_SENTINEL', 'CREDENTIAL_SENTINEL',
                               'PERSONALITY_SENTINEL', '2023-11-14'):
                    self.assertNotIn(secret, json.dumps(row))

    def test_actual_caller_bearer_authenticator(self):
        with PersonaHTTP() as app:
            # Both Config and env parsing currently forbid Matrix owner IDs. Exercise
            # the real token matcher with a bounded identity fixture, without changing policy.
            self.assertEqual(detail(app, auth="Bearer caller")[0], 404)
            app.server.authenticator = CallerAuthenticator(SimpleNamespace(dev_mode=False, auth_token=None,
                caller_tokens=tuple(CallerToken(name, 'test-' + name, uid) for name, uid in
                                    [('owner', OWNER), ('other', OTHER), ('remote', '@r:remote.invalid')]),
                legacy_token_enabled=False))
            self.assertEqual(set(detail(app, auth='Bearer test-owner')[1]), OWNER_KEYS)
            self.assertEqual(set(detail(app, auth='Bearer test-other')[1]), PUBLIC_KEYS)
            status, body = detail(app, auth='Bearer test-remote')
            self.assertEqual((status, body['code']), (404, 'persona.not_found'))

    def test_404_indistinguishable_and_invalid_ids_not_logged(self):
        with PersonaHTTP() as app:
            failures = []
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                for uid, pid in [(OTHER, '@ai_unknown:example.invalid'),
                                 (OTHER, '@ai_orphan:example.invalid'),
                                 ('@remote:other.invalid', PERSONA),
                                 ('@remote:other.invalid', '@ai_unknown:example.invalid'),
                                 (OWNER, 'INVALID_ID_SENTINEL'), (OWNER, '@ai_:example.invalid'),
                                 (OWNER, '@ai_a:example.invalid/state'), (OWNER, '%FF'),
                                 (OWNER, '@ai_a:example.invalid\n')]:
                    with patch.object(app.server.persona_detail_verifier, 'verify', return_value=(VERIFY_OK, uid)):
                        status, body = detail(app, pid)
                    self.assertEqual((status, body['code']), (404, 'persona.not_found'))
                    failures.append({k: v for k, v in body.items() if k != 'request_id'})
            self.assertTrue(all(body == failures[0] for body in failures))
            self.assertNotIn('INVALID_ID_SENTINEL', captured.getvalue())

    def test_auth_errors(self):
        with PersonaHTTP() as app:
            for auth, expected, code in [(None, 401, 'auth.missing'),
                    ('Bearer invalid', 401, 'auth.invalid_token'),
                    ('Bearer site', 403, 'persona.not_owned'),
                    ('Bearer offline', 500, 'internal.error')]:
                status, body = detail(app, auth=auth)
                self.assertEqual((status, body['code']), (expected, code))

    def test_real_user_verifier_local_and_foreign_schemes_cache_isolation(self):
        with PersonaHTTP() as app:
            # Mock only the trusted homeserver response, not identity/domain validation.
            config = replace(app.server.config, homeserver_url='http://homeserver.invalid')
            app.server.user_verifier = UserIdentityVerifier(config)
            app.server.persona_detail_verifier = UserIdentityVerifier(config, allow_foreign_identity=True)
            for scheme, field in [('MatrixOpenID', 'sub'), ('Bearer', 'user_id')]:
                for index, uid in enumerate((OWNER, OTHER, '@remote:remote.invalid')):
                    payload = json.dumps({field: uid}).encode()
                    auth = scheme + ' fixture-' + scheme + str(index)
                    for verifier in (app.server.user_verifier, app.server.persona_detail_verifier):
                        verifier._opener.open = Mock(side_effect=lambda *a, **k: io.BytesIO(payload))
                    expected = 404 if index == 2 else 200
                    for _ in range(2):  # repeat to exercise positive cache
                        status, body = detail(app, auth=auth)
                        self.assertEqual(status, expected)
                    self.assertEqual(app.server.persona_detail_verifier._opener.open.call_count, 1)
                    if index == 2:
                        self.assertEqual(body['code'], 'persona.not_found')
                        self.assertEqual(app.request(auth=auth)[0], 401)
                        self.assertEqual(app.request(auth=auth, path='/v1/capabilities')[0], 401)
                    else:
                        self.assertEqual(set(body), OWNER_KEYS if index == 0 else PUBLIC_KEYS)

    def test_bio_and_credentials_not_logged(self):
        with PersonaHTTP() as app:
            app.server.memory_personas[PERSONA] = replace(app.server.memory_personas[PERSONA], bio=BIO)
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                self.assertEqual(detail(app)[0], 200)
                self.assertEqual(app.request()[0], 200)
            for private in (BIO, 'MatrixOpenID user', 'PROMPT_SENTINEL', 'CREDENTIAL_SENTINEL'):
                self.assertNotIn(private, captured.getvalue())


class BioPromptTests(unittest.TestCase):
    def test_own_and_other_bio_never_in_model_system_prompt(self):
        helper = engine_tests.TestEngineHooks()
        hooks, _ = helper._hooks()
        own = 'OWN_BIO_INJECTION_SENTINEL'
        other = 'OTHER_BIO_INJECTION_SENTINEL'
        hooks._personas[engine_tests.AI_A] = replace(hooks._personas[engine_tests.AI_A], bio=own)
        hooks._personas[engine_tests.AI_B] = engine_tests._make_persona(
            user_id=engine_tests.AI_B, bio=other)
        with patch.object(hooks._client, '_post', wraps=hooks._client._post) as post:
            hooks.on_human_text(helper._make_ctx())
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.args[0]
        system = '\n'.join(m['content'] for m in payload['messages'] if m['role'] == 'system')
        self.assertIn('Be nice.', system)
        for marker in (own, other):
            self.assertNotIn(marker, system)
            self.assertNotIn(marker, json.dumps(payload))

"""Local gateway HTTP contract: no proxies, redirects, bodies in errors."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from pairing import relations, service


class HttpGatewayTest(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.status, self.raw = 202, b'{"accepted":true}'
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                test.seen.append((self.path, self.headers.get('Authorization'),
                                  json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(test.status)
                self.send_header('Location', '/do-not-follow')
                self.send_header('Content-Length', str(len(test.raw)))
                self.end_headers()
                self.wfile.write(test.raw)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = 'http://127.0.0.1:%s' % self.httpd.server_port
        self.token = 'fixture-only-' + 'z' * 32
        self.client = relations.HttpRelationsGateway(self.url, self.token, timeout=1)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)

    def test_all_kinds_wire_shape_and_status_whitelist(self):
        for status in relations.RELAYED_GATEWAY_STATUSES:
            self.status = status
            for kind in relations.RELATION_KINDS:
                with self.subTest(status=status, kind=kind):
                    result = self.client.deliver(kind, {'opaque': 'payload'}, {'origin': 'remote.invalid'})
                    self.assertEqual((result.status, result.body), (status, {'accepted': True}))
                    self.assertEqual(self.seen[-1], ('/_internal/relations/' + kind,
                        'Bearer ' + self.token, {'kind': kind, 'envelope': {'opaque': 'payload'},
                                                'meta': {'origin': 'remote.invalid'}}))

    def test_invalid_responses_redirection_and_body_redaction(self):
        sentinel = 'SENSITIVE_BODY_DO_NOT_LOG'
        for status, raw in ((302, b'{}'), (401, sentinel.encode()), (500, sentinel.encode()),
                            (200, sentinel.encode()), (200, b'[]'),
                            (200, b'x' * (relations.MAX_GATEWAY_BODY_BYTES + 1))):
            self.status, self.raw = status, raw
            self.seen.clear()
            with self.assertRaises(relations.GatewayUnavailable) as caught:
                self.client.deliver('request', {'message': sentinel}, {})
            self.assertNotIn(sentinel, str(caught.exception))
            self.assertNotIn(self.token, str(caught.exception))
            self.assertLessEqual(len(self.seen), 1)  # no redirect or retry

    def test_transport_exception_and_request_size_redacted(self):
        sentinel = 'SENSITIVE_BODY_DO_NOT_LOG'
        with mock.patch.object(relations.urllib.request.OpenerDirector, 'open',
                               side_effect=OSError(sentinel)):
            with self.assertRaises(relations.GatewayUnavailable) as caught:
                self.client.deliver('request', {'message': sentinel}, {})
        self.assertEqual(str(caught.exception), 'http_unavailable')
        self.assertTrue(caught.exception.__suppress_context__)
        with self.assertRaises(relations.GatewayUnavailable) as caught:
            self.client.deliver('request', {'x': 'x' * 16384}, {})
        self.assertEqual(str(caught.exception), 'request_too_large')
        self.assertEqual(self.seen, [])

    def test_configuration(self):
        base = dict(db_path=':memory:', admin_token='fixture', server_name='hs-a',
                    api_url='https://hs-a/_matrix/pairing', relations_gateway='http')
        for url, token in (('', self.token), (self.url, ''), (self.url, 'short'),
                           ('ftp://gateway', self.token), ('http://u:p@gateway', self.token),
                           ('http://gateway/path', self.token), ('http://gateway?x=1', self.token),
                           ('http://gateway:bad', self.token), (self.url, self.token + '\r\n')):
            with self.subTest(url=url, token_length=len(token)):
                with self.assertRaises(service.ConfigError):
                    service.PairingConfig(**base, relations_gateway_url=url,
                                          relations_gateway_token=token)
        config = service.PairingConfig(**base, relations_gateway_url=self.url,
                                       relations_gateway_token=self.token)
        app = service.PairingApp(config)
        self.addCleanup(app.store.close)
        self.assertIsInstance(app.relations_gateway, relations.HttpRelationsGateway)

    def test_local_hop_ignores_peer_policy_and_proxy_environment(self):
        with mock.patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1',
                                         'HTTP_PROXY': 'http://127.0.0.1:1'}), \
                mock.patch.object(relations, 'target_policy', side_effect=AssertionError('peer policy used')):
            self.assertEqual(self.client.deliver('request', {}, {}).status, 202)

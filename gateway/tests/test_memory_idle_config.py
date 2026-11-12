# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""PR3b configuration defaults and fail-closed validation."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from botville_gateway.config import ConfigError, load_config


class MemoryIdleConfigTests(unittest.TestCase):
    def test_embedding_inherits_chat_endpoint_and_secret_is_hidden(self):
        config = load_config({
            'GATEWAY_DEV_MODE': 'true',
            'GATEWAY_MODEL_BASE_URL': 'https://models.example/v1',
            'GATEWAY_MODEL_NAME': 'chat',
            'GATEWAY_MODEL_API_KEY': 'fake-secret',
            'GATEWAY_EMBEDDING_MODEL': 'embed',
        })
        self.assertEqual(config.memory_summary_model, 'chat')
        self.assertEqual(config.embedding_base_url, 'https://models.example/v1')
        self.assertEqual(config.embedding_api_key, 'fake-secret')
        self.assertNotIn('fake-secret', repr(config))

    def test_embedding_requires_effective_base_url(self):
        with self.assertRaises(ConfigError):
            load_config({
                'GATEWAY_DEV_MODE': 'true',
                'GATEWAY_EMBEDDING_MODEL': 'embed',
            })

    def test_backend_and_dimension_are_validated_even_when_disabled(self):
        for name, value in (
            ('GATEWAY_MEMORY_VECTOR_BACKEND', 'bad'),
            ('GATEWAY_EMBEDDING_DIM', '-1'),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ConfigError):
                    load_config({'GATEWAY_DEV_MODE': 'true', name: value})


if __name__ == '__main__':
    unittest.main()

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Small, deliberately boring OpenAI-compatible embeddings client."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import threading
import http.client
import math
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from ..as_consumer import TransientUnavailable
from ..engine.model_client import ModelError, _NoRedirectHandler, _drain


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str = ''
    model: str = ''
    dim: int = 0
    _api_key: Optional[str] = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def api_key(self) -> Optional[str]:
        return self._api_key

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> 'EmbeddingConfig':
        source = os.environ if env is None else env
        def get(name: str) -> str:
            value = source.get(name)
            return value.strip() if isinstance(value, str) else ''
        model = get('GATEWAY_EMBEDDING_MODEL')
        base = get('GATEWAY_EMBEDDING_BASE_URL') or get('GATEWAY_MODEL_BASE_URL')
        key = get('GATEWAY_EMBEDDING_API_KEY') or get('GATEWAY_MODEL_API_KEY')
        raw_dim = get('GATEWAY_EMBEDDING_DIM')
        try:
            dim = int(raw_dim or '0')
        except ValueError as exc:
            raise ValueError('GATEWAY_EMBEDDING_DIM must be a non-negative integer') from exc
        if dim < 0:
            raise ValueError('GATEWAY_EMBEDDING_DIM must be a non-negative integer')
        if model:
            parsed = urllib.parse.urlparse(base)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError('GATEWAY_EMBEDDING_MODEL requires an http:// or https:// embedding base URL')
        return cls(base.rstrip('/'), model, dim, key or None)


class EmbeddingClient:
    """No redirects/proxy environment and no bodies/URLs in exception messages."""
    def __init__(self, config: EmbeddingConfig, timeout_seconds: float = 30) -> None:
        self.config = config
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirectHandler, urllib.request.ProxyHandler({}))
        self._dimension = config.dim
        self._dimension_lock = threading.Lock()

    @property
    def dimension(self) -> int:
        """Configured dimension, or the first provider response pinned process-wide."""
        return self._dimension

    def embed(self, text: str) -> list[float]:
        if not self.config.configured:
            raise ModelError('embedding model is not configured')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self.config.api_key:
            headers['Authorization'] = 'Bearer ' + self.config.api_key
        request = urllib.request.Request(
            self.config.base_url + '/embeddings',
            data=json.dumps({'model': self.config.model, 'input': text}).encode('utf-8'),
            headers=headers, method='POST')
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                payload = response.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    raise ModelError('embedding provider response is too large')
                raw = json.loads(payload.decode('utf-8'))
        except urllib.error.HTTPError as exc:
            _drain(exc)
            if exc.code >= 500 or exc.code in (408, 429):
                raise TransientUnavailable('embedding provider unavailable (HTTP %d)' % exc.code) from None
            raise ModelError('embedding provider rejected the request (HTTP %d)' % exc.code) from None
        except urllib.error.URLError as exc:
            raise TransientUnavailable('embedding provider unreachable (%s)' % type(exc.reason).__name__) from None
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise TransientUnavailable('embedding provider request failed (%s)' % type(exc).__name__) from None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ModelError('embedding provider returned invalid JSON') from None
        try:
            vector = raw['data'][0]['embedding']
            if not isinstance(vector, list) or not vector or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)) or abs(float(x)) > 3.4e38 for x in vector):
                raise ValueError
            result = [float(x) for x in vector]
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError('embedding provider response has no embedding') from None
        with self._dimension_lock:
            if self._dimension and len(result) != self._dimension:
                raise ModelError('embedding provider returned unexpected dimension')
            # Default zero deliberately accepts, then pins, a provider dimension.
            self._dimension = len(result)
        return result

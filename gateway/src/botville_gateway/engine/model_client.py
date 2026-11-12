# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""OpenAI 兼容的模型客户端（B2.4 简单接入，contract §1.7.8 ⑫）。

只用标准库 `urllib`：`POST {BASE_URL}/chat/completions`。

配置（启动时由 `ModelConfig.from_env` 校验，非法 → 拒绝启动）：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_MODEL_BASE_URL` | 空 | 空 = **未配置模型**：引擎不发言，只记录/计数，`/health` 不 degraded |
| `GATEWAY_MODEL_NAME` | 空 | 配置了 BASE_URL 时**必填**（否则拒绝启动） |
| `GATEWAY_MODEL_API_KEY` | 空 | 可选密钥；`Authorization: Bearer`；**永不写日志、不进 `/health`、不进任何异常消息** |
| `GATEWAY_MODEL_TIMEOUT_SECONDS` | 30 | 正数；非正数 / 非数字拒绝启动 |

人设可用 `credential_ref: "env:NAME"` 覆盖全局密钥：调用时读 `os.environ[NAME]`，
缺失即**永久失败**（错误消息只点名变量，不含值）。

错误映射（与 `as_consumer` 对接）：
  - HTTP 5xx / 408 / 429 / 超时 / 连接错误 / DNS 失败 → `TransientUnavailable`
    （**不计 attempts**，短退避后重试）；
  - 其余 4xx、2xx 但响应结构不对、空 choices → 普通失败（语义错误，重试无益）。

异常消息只含**状态码与异常类型名**：不含 URL、请求体、响应体或密钥。重定向一律
不跟随（与 `matrix_appservice` 一致：`Authorization` 不能被转去跨源地址）；代理
处理同样与该模块保持一致（忽略 `http_proxy` 等环境变量）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from ..as_consumer import TransientUnavailable

DEFAULT_MODEL_TIMEOUT_SECONDS = 30

# 瞬时失败的状态码：服务端错误 + 限流 + 请求超时。
_TRANSIENT_STATUS = frozenset({408, 429})
# 疑似明文密钥的字面量前缀（`sk-...` 等）——只用于**拒绝**这类配置，不用于日志。
_KEY_LOOKING_PREFIXES = ("sk-", "sk_", "Bearer ")


class ModelConfigError(ValueError):
    """模型配置非法（启动即失败）。消息不含任何密钥值。"""


@dataclass(frozen=True)
class ModelConfig:
    """模型连接配置（不含密钥值，密钥单独传递，不进 repr / 日志）。"""

    base_url: str = ""
    model: str = ""
    timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    _api_key: Optional[str] = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        """是否配置了模型（BASE_URL 非空且模型名非空）。"""
        return bool(self.base_url and self.model)

    @property
    def api_key(self) -> Optional[str]:
        return self._api_key

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ModelConfig":
        """从环境变量读取并校验。非法值抛 `ModelConfigError`（拒绝启动）。"""
        source: Mapping[str, str] = os.environ if env is None else env

        def _get(name: str) -> str:
            raw = source.get(name)
            return "" if raw is None else raw.strip()

        base_url = _get("GATEWAY_MODEL_BASE_URL")
        model = _get("GATEWAY_MODEL_NAME")
        api_key_raw = source.get("GATEWAY_MODEL_API_KEY")
        api_key = api_key_raw.strip() if isinstance(api_key_raw, str) else ""
        timeout_raw = _get("GATEWAY_MODEL_TIMEOUT_SECONDS")

        if base_url:
            parsed = urllib.parse.urlparse(base_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ModelConfigError(
                    "GATEWAY_MODEL_BASE_URL must be an http:// or https:// URL, e.g. "
                    "https://api.example.com/v1"
                )
            if not model:
                raise ModelConfigError(
                    "GATEWAY_MODEL_BASE_URL is set but GATEWAY_MODEL_NAME is not; "
                    "the model name is required when a base URL is configured"
                )

        timeout = float(DEFAULT_MODEL_TIMEOUT_SECONDS)
        if timeout_raw:
            try:
                timeout = float(timeout_raw)
            except ValueError as exc:
                raise ModelConfigError(
                    "GATEWAY_MODEL_TIMEOUT_SECONDS must be a number of seconds"
                ) from exc
            if not (timeout > 0) or timeout != timeout:  # NaN / 负数 / 零
                raise ModelConfigError(
                    "GATEWAY_MODEL_TIMEOUT_SECONDS must be a positive number of seconds"
                )

        return cls(
            base_url=base_url.rstrip("/"),
            model=model,
            timeout_seconds=timeout,
            _api_key=api_key or None,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向：Bearer 密钥在头里，3xx 可能把它转去跨源地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(
            req.full_url, code, "redirects are not followed", headers, fp
        )


class ModelError(Exception):
    """模型调用永久失败（4xx / 响应结构不对）。消息只含状态码与类型名。"""


class ModelClient:
    """OpenAI 兼容 chat completions 客户端（线程安全：无共享可变状态）。"""

    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        # 与 matrix_appservice 同一套传输约定：不跟随重定向、不吃代理环境变量。
        self._opener = urllib.request.build_opener(
            _NoRedirectHandler, urllib.request.ProxyHandler({})
        )

    @property
    def config(self) -> ModelConfig:
        return self._config

    def _api_key_for(self, persona: Optional["object"]) -> Optional[str]:
        """该人设生效的密钥：`credential_ref: env:NAME` 覆盖全局密钥。

        人设引用的变量缺失 → `ModelError`（永久失败），消息只点名变量名。
        """
        env_name = getattr(persona, "credential_env_name", None) if persona else None
        if env_name:
            value = os.environ.get(env_name)
            if not value:
                raise ModelError(
                    f"credential environment variable {env_name} is not set "
                    "(referenced by the persona's credential_ref)"
                )
            return value
        return self._config.api_key

    def complete(
        self,
        *,
        system_text: str,
        messages: List[Dict[str, str]],
        persona: Optional["object"] = None,
        model: Optional[str] = None,
    ) -> str:
        """一次 chat completion；返回助手消息正文。

        参数：
          - `system_text`：**指令区**正文（system role），只允许可信人设内容；
          - `messages`：对话/数据区消息（`[{"role": "user", "content": ...}]`），
            其中的不可信内容必须已由调用方按 §7.2 隔离包裹；
          - `persona`：有 `credential_env_name` 属性时用于取该人设的密钥；
          - `model`：模型名覆盖（缺省用配置里的 `GATEWAY_MODEL_NAME`）。

        失败：`TransientUnavailable`（5xx / 429 / 408 / 超时 / 连接错误）或
        `ModelError`（其余 4xx / 响应结构不对 / 密钥变量缺失）。
        """
        if not self._config.configured:
            raise ModelError("the model is not configured")
        payload = self._build_payload(system_text, messages, persona, model)
        raw = self._post(payload, persona)
        return self._extract_text(raw)

    # ------------------------------------------------------------------ http
    def _build_payload(
        self,
        system_text: str,
        messages: List[Dict[str, str]],
        persona: Optional["object"],
        model: Optional[str],
    ) -> Dict[str, Any]:
        resolved_model = model or getattr(persona, "model", None) or self._config.model
        body: List[Dict[str, str]] = []
        if system_text:
            body.append({"role": "system", "content": system_text})
        for message in messages:
            # 只透传已校验的 role/content（调用方负责数据区隔离）。
            role = message.get("role")
            content = message.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                body.append({"role": role, "content": content})
        return {"model": resolved_model, "messages": body}

    def _post(self, payload: Dict[str, Any], persona: Optional["object"]) -> Any:
        url = self._config.base_url + "/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        api_key = self._api_key_for(persona)
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with self._opener.open(
                request, timeout=self._config.timeout_seconds
            ) as response:
                return _decode(response.read())
        except urllib.error.HTTPError as exc:
            # 只读状态码：响应体可能回显请求内容，绝不进异常消息。
            _drain(exc)
            if exc.code >= 500 or exc.code in _TRANSIENT_STATUS:
                raise TransientUnavailable(
                    f"model provider unavailable (HTTP {exc.code})"
                ) from None
            raise ModelError(f"model provider rejected the request (HTTP {exc.code})") from None
        except urllib.error.URLError as exc:
            # DNS / 连接拒绝 / 握手失败等；消息里只放内部异常类型名。
            raise TransientUnavailable(
                f"model provider unreachable ({type(exc.reason).__name__})"
            ) from None
        except (TimeoutError, OSError) as exc:
            # socket.timeout 是 OSError 子类；读超时 / 连接被重置都算瞬态。
            raise TransientUnavailable(
                f"model provider request failed ({type(exc).__name__})"
            ) from None

    @staticmethod
    def _extract_text(raw: Any) -> str:
        """从 OpenAI 兼容响应里取 `choices[0].message.content`。"""
        if not isinstance(raw, dict):
            raise ModelError("model provider returned a non-object response")
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError("model provider response has no choices")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ModelError("model provider response has no message content")
        return content


def _decode(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ModelError("model provider response is not valid UTF-8") from None
    try:
        return json.loads(text) if text.strip() else None
    except (json.JSONDecodeError, ValueError):
        raise ModelError("model provider response is not valid JSON") from None


def _drain(exc: urllib.error.HTTPError) -> None:
    """读完错误响应体（连接复用）但不保留内容、不解析。"""
    try:
        exc.read()
    except Exception:  # noqa: BLE001 - 读错误体失败无关紧要
        pass


def looks_like_plaintext_key(value: str) -> bool:
    """值是否「看起来就是明文密钥」（用于配置/人设的拒绝规则）。

    只做形状判断，不记录、不回显值。
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return any(stripped.startswith(prefix) for prefix in _KEY_LOOKING_PREFIXES)

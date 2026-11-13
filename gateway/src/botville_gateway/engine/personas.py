# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""角色人设（只读 JSON，B2.4 简单接入，contract §1.7.8 ②）。

人设文件由部署侧维护（`GATEWAY_PERSONAS_FILE`），仓库只放**示例**
（`gateway/examples/personas.example.json`）。真实人设不进仓库。

文件结构：

```json
{
  "personas": [
    {
      "user_id": "@ai_luna:example.org",
      "display_name": "Luna",
      "system_prompt": "你是 Luna，一个耐心、爱提问的伙伴……",
      "personality": "说话简短，喜欢用比喻（可选）",
      "model": "gpt-4o-mini",
      "credential_ref": "env:LUNA_MODEL_API_KEY",
      "max_reply_chars": 800
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 含义 |
|---|---|---|
| `user_id` | 是 | **本服** `@ai_*` Matrix 用户 ID（domain 必须等于 `GATEWAY_MATRIX_SERVER_NAME`，大小写不敏感） |
| `display_name` | 否 | 展示名；用于摘要与系统提示 |
| `bio` | 否 | owner 文件签名，最多 500 Unicode 码点；不进入任何 system 指令 |
| `system_prompt` | 是 | 人设指令正文（进入 **system/instructions 区**，不是数据区） |
| `personality` | 否 | 追加的性格描述（同样属指令区） |
| `model` | 否 | 该人设的模型名覆盖；缺省用 `GATEWAY_MODEL_NAME` |
| `credential_ref` | 否 | 只接受 `env:NAME` 形式，调用时读 `os.environ[NAME]`；覆盖全局 `GATEWAY_MODEL_API_KEY`。任何其他值一律拒绝 |
| `max_reply_chars` | 否 | 该人设单条回复上限（正整数，且不超过平台 `MAX_MESSAGE_CHARS`） |

校验在**启动期**完成（`load_personas`）：文件读不到、JSON 非法、结构不对、
`user_id` 不是本服 `@ai_*`、重复 `user_id`、`credential_ref` 不是 `env:NAME`、
`max_reply_chars` 越界 —— 一律抛 `PersonaConfigError` 拒绝启动。

**错误消息永不回显文件内容或字段原值**（人设文件可能含部署侧写的密钥），
只报「第几个条目 / 哪个字段 / 哪类问题」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# `env:NAME` 的 NAME 形状（POSIX 环境变量名）。
_ENV_REF_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_MATRIX_LOCALPART_RE = re.compile(r"^[A-Za-z0-9._=/-]+$")
# 人设条目的允许字段（未知字段拒绝：配置拼错不应静默忽略）。
_PERSONA_FIELDS = frozenset(
    {
        "user_id",
        "display_name",
        "bio",
        "system_prompt",
        "personality",
        "model",
        "credential_ref",
        "max_reply_chars",
        "owner_user_id",
        "proactive",
    }
)
# 人设文件允许的顶层字段。
_TOP_FIELDS = frozenset({"personas"})

DEFAULT_MAX_REPLY_CHARS = 2000
_PROACTIVE_FIELDS = frozenset({"enabled", "max_per_day", "require_reply", "quiet_hours"})
_QUIET_HOURS_FIELDS = frozenset({"enabled", "start", "end", "timezone"})
_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class PersonaConfigError(ValueError):
    """人设文件不可用（启动即失败；消息只含位置与字段名，不含内容）。"""


@dataclass(frozen=True)
class Persona:
    """一个本服 AI 的人设（不可变；不含任何密钥值，只含引用）。"""

    user_id: str
    system_prompt: str
    display_name: str = ""
    personality: str = ""
    model: Optional[str] = None
    # `env:NAME` 形式的引用（**不是**密钥本身）。密钥在调用时从环境读取。
    credential_ref: Optional[str] = field(default=None, repr=False)
    max_reply_chars: int = DEFAULT_MAX_REPLY_CHARS
    # Optional local human account authorized for the internal memory API.
    owner_user_id: Optional[str] = None
    # Validated deployment-side values.  It remains data, never system text.
    proactive: Mapping[str, Any] = field(default_factory=dict, repr=False)

    # Owner-authored display data; never part of instructions or logs.
    bio: Optional[str] = field(default=None, repr=False)

    @property
    def credential_env_name(self) -> Optional[str]:
        """`credential_ref` 里的环境变量名（不是 `env:` 形式时为 None）。"""
        if not self.credential_ref:
            return None
        return self.credential_ref[len("env:"):]

    def instruction_text(self) -> str:
        """拼进 **system/instructions 区**的指令正文（人设属可信指令来源）。"""
        parts = [p for p in (self.system_prompt, self.personality) if p]
        if self.display_name:
            parts.insert(0, f"你的名字是 {self.display_name}。")
        return "\n".join(parts)


def _is_local_ai(user_id: str, server_name: Optional[str]) -> bool:
    """`@ai_*:<server_name>`（大小写不敏感）；未配置 server_name → False。"""
    if not isinstance(user_id, str) or not user_id.startswith("@"):
        return False
    localpart, sep, domain = user_id[1:].partition(":")
    if not sep or not localpart or not domain:
        return False
    if not localpart.startswith("ai_"):
        return False
    if not server_name:
        return False
    return domain.lower() == server_name.strip().lower()


def _is_local_owner(user_id: str, server_name: Optional[str]) -> bool:
    """A memory owner is a local non-AI Matrix account."""
    if not isinstance(user_id, str) or not user_id.startswith("@") or not server_name:
        return False
    localpart, sep, domain = user_id[1:].partition(":")
    return bool(
        sep and localpart and domain and _MATRIX_LOCALPART_RE.fullmatch(localpart)
        and not localpart.lower().startswith("ai_")
        and domain.lower() == server_name.strip().lower()
    )


def _validate_proactive(value: Any, where: str) -> Mapping[str, Any]:
    """Validate the persona subset of OpenAPI ProactiveSettings without echoing values."""
    if not isinstance(value, dict):
        raise PersonaConfigError(f"{where}.proactive must be an object")
    unknown = set(value) - _PROACTIVE_FIELDS
    if unknown:
        # Do not disclose a deployment-provided key name: it may itself carry
        # sensitive material in a malformed persona file.
        raise PersonaConfigError(f"{where}.proactive has unexpected field(s)")
    result: Dict[str, Any] = {"enabled": False}
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise PersonaConfigError(f"{where}.proactive.enabled must be a boolean")
    result["enabled"] = enabled
    if "max_per_day" in value:
        max_per_day = value["max_per_day"]
        if not isinstance(max_per_day, int) or isinstance(max_per_day, bool) or not 0 <= max_per_day <= 100:
            raise PersonaConfigError(f"{where}.proactive.max_per_day is outside its supported range")
        result["max_per_day"] = max_per_day
    if "require_reply" in value:
        if not isinstance(value["require_reply"], bool):
            raise PersonaConfigError(f"{where}.proactive.require_reply must be a boolean")
        result["require_reply"] = value["require_reply"]
    quiet = value.get("quiet_hours")
    if "quiet_hours" in value:
        if not isinstance(quiet, dict):
            raise PersonaConfigError(f"{where}.proactive.quiet_hours must be an object")
        if set(quiet) - _QUIET_HOURS_FIELDS:
            raise PersonaConfigError(f"{where}.proactive.quiet_hours has unexpected field(s)")
        if not isinstance(quiet.get("enabled", True), bool):
            raise PersonaConfigError(f"{where}.proactive.quiet_hours.enabled must be a boolean")
        # SilentHours requires these fields.  Validate them here so the worker
        # cannot start successfully and later fail for every scheduled tick.
        for key in ("start", "end", "timezone"):
            if key not in quiet or not isinstance(quiet[key], str):
                raise PersonaConfigError(f"{where}.proactive.quiet_hours.{key} is required")
        if not _HHMM_RE.fullmatch(quiet["start"]):
            raise PersonaConfigError(f"{where}.proactive.quiet_hours.start must be HH:MM")
        if not _HHMM_RE.fullmatch(quiet["end"]):
            raise PersonaConfigError(f"{where}.proactive.quiet_hours.end must be HH:MM")
        try:
            ZoneInfo(quiet["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise PersonaConfigError(f"{where}.proactive.quiet_hours.timezone is unknown") from None
        result["quiet_hours"] = dict(quiet, enabled=quiet.get("enabled", True))
    return result


def load_personas(
    path: Optional[str],
    *,
    server_name: Optional[str],
    max_message_chars: int = 8000,
) -> Dict[str, Persona]:
    """读取并校验人设文件；`path` 为空 → 空表（引擎不发言）。

    返回 `user_id -> Persona`。任何校验失败抛 `PersonaConfigError`。
    """
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        raise PersonaConfigError(
            f"GATEWAY_PERSONAS_FILE is not readable ({type(exc).__name__}); "
            "check the path and file permissions"
        ) from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PersonaConfigError(
            "GATEWAY_PERSONAS_FILE is not valid JSON "
            f"({type(exc).__name__}); see gateway/examples/personas.example.json"
        ) from exc
    if not isinstance(payload, dict):
        raise PersonaConfigError(
            "GATEWAY_PERSONAS_FILE must contain a JSON object with a 'personas' array"
        )
    unknown_top = set(payload) - _TOP_FIELDS
    if unknown_top:
        raise PersonaConfigError(
            "GATEWAY_PERSONAS_FILE has unexpected top-level field(s): "
            f"{', '.join(sorted(unknown_top))}"
        )
    entries = payload.get("personas")
    if not isinstance(entries, list):
        raise PersonaConfigError(
            "GATEWAY_PERSONAS_FILE must contain a 'personas' array"
        )
    if not server_name:
        raise PersonaConfigError(
            "GATEWAY_PERSONAS_FILE requires GATEWAY_MATRIX_SERVER_NAME to validate "
            "that every persona user_id is a local @ai_* account"
        )

    personas: Dict[str, Persona] = {}
    for index, entry in enumerate(entries):
        where = f"personas[{index}]"
        if not isinstance(entry, dict):
            raise PersonaConfigError(f"{where} must be an object")
        unknown = set(entry) - _PERSONA_FIELDS
        if unknown:
            raise PersonaConfigError(
                f"{where} has unexpected field(s): {', '.join(sorted(unknown))}"
            )
        user_id = entry.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise PersonaConfigError(f"{where}.user_id is required (a Matrix user ID)")
        if not _is_local_ai(user_id, server_name):
            raise PersonaConfigError(
                f"{where}.user_id is not a local @ai_* account of this homeserver "
                f"(expected localpart ai_* and domain {server_name!r})"
            )
        if user_id in personas:
            raise PersonaConfigError(f"{where}.user_id duplicates an earlier persona")
        system_prompt = entry.get("system_prompt")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise PersonaConfigError(f"{where}.system_prompt is required")
        display_name = entry.get("display_name", "")
        if not isinstance(display_name, str):
            raise PersonaConfigError(f"{where}.display_name must be a string")
        bio = entry.get("bio")
        if bio is not None and not isinstance(bio, str):
            raise PersonaConfigError(f"{where}.bio must be a string")
        if bio is not None and len(bio) > 500:
            raise PersonaConfigError(f"{where}.bio must contain at most 500 Unicode code points")
        personality = entry.get("personality", "")
        if not isinstance(personality, str):
            raise PersonaConfigError(f"{where}.personality must be a string")
        model = entry.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise PersonaConfigError(f"{where}.model must be a non-empty string")

        credential_ref = entry.get("credential_ref")
        if credential_ref is not None:
            # 只接受 `env:NAME`：人设文件里**不允许**出现明文密钥（即使是
            # 占位符形态的密钥字面量也不行），错误消息不回显该值。
            if not isinstance(credential_ref, str) or not _ENV_REF_RE.fullmatch(
                credential_ref
            ):
                raise PersonaConfigError(
                    f"{where}.credential_ref must be of the form 'env:NAME' "
                    "(a plaintext key value is not accepted in the persona file)"
                )

        max_reply_chars = entry.get("max_reply_chars", DEFAULT_MAX_REPLY_CHARS)
        if (
            not isinstance(max_reply_chars, int)
            or isinstance(max_reply_chars, bool)
            or max_reply_chars < 1
        ):
            raise PersonaConfigError(f"{where}.max_reply_chars must be a positive integer")
        if max_reply_chars > max_message_chars:
            raise PersonaConfigError(
                f"{where}.max_reply_chars must not exceed MAX_MESSAGE_CHARS "
                f"({max_message_chars})"
            )

        owner_user_id = entry.get("owner_user_id")
        if "owner_user_id" in entry and (
            not isinstance(owner_user_id, str)
            or not _is_local_owner(owner_user_id, server_name)
        ):
            raise PersonaConfigError(
                f"{where}.owner_user_id must be a local non-ai Matrix user ID"
            )
        proactive = _validate_proactive(entry.get("proactive", {}), where)

        personas[user_id] = Persona(
            user_id=user_id,
            system_prompt=system_prompt,
            display_name=display_name,
            personality=personality,
            model=model.strip() if isinstance(model, str) else None,
            credential_ref=credential_ref,
            max_reply_chars=max_reply_chars,
            owner_user_id=owner_user_id,
            proactive=proactive,
            bio=bio,
        )
    return personas

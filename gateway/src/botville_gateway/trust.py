# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""§7.1 trust 判定规则（B2.3）。

规则独立评估，全部命中取最低值（偏序 untrusted < trusted）。
system 只由规则 1 在 2–8 均不命中时产生；任何 untrusted 命中覆盖它。
forwarded_from 逐跳追溯，最多 8 跳，超限 / 查不到 / 非法值 → fail-closed untrusted。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

TRUST_UNTRUSTED = "untrusted"
TRUST_TRUSTED = "trusted"
TRUST_SYSTEM = "system"

# rule3：这两个 origin 一律 untrusted
_UNTRUSTED_ORIGINS = frozenset({"bridge", "town"})

# forwarded_from 最多追溯跳数（含本跳）
MAX_FORWARD_HOPS = 8


def _lower_trust(a: str, b: str) -> str:
    """偏序取低：untrusted < trusted；system 不参与比较（只由规则 1 产生）。"""
    if a == TRUST_UNTRUSTED or b == TRUST_UNTRUSTED:
        return TRUST_UNTRUSTED
    if a == TRUST_TRUSTED or b == TRUST_TRUSTED:
        return TRUST_TRUSTED
    return TRUST_SYSTEM


def determine_provenance_trust(
    origin: str,
    external: bool,
) -> tuple[str, str]:
    """按 §7.1 规则 1–8 对 provenance 字段判断 trust。

    Returns (trust, rule_label)。rule_label 是命中最低值的那条规则。
    """
    hits: list[tuple[str, str]] = []

    # 规则 2：external=True
    if external:
        hits.append((TRUST_UNTRUSTED, "rule2"))

    # 规则 3：origin 为 bridge / town
    if origin in _UNTRUSTED_ORIGINS:
        hits.append((TRUST_UNTRUSTED, "rule3"))

    # 规则 6：本服经鉴权的人类消息
    if origin == "human" and not external:
        hits.append((TRUST_TRUSTED, "rule6"))

    # 规则 7：本服 AI 发出的消息
    if origin == "ai" and not external:
        hits.append((TRUST_TRUSTED, "rule7"))

    if not hits:
        # 规则 1：网关自身生成（origin=system, external=False）
        if origin == "system" and not external:
            return TRUST_SYSTEM, "rule1"
        # 规则 8：来源不明 / fail-closed
        return TRUST_UNTRUSTED, "rule8"

    # 全部命中取最低
    result_trust, result_rule = hits[0]
    for trust, rule in hits[1:]:
        lower = _lower_trust(result_trust, trust)
        if lower != result_trust:
            result_trust = lower
            result_rule = rule
    return result_trust, result_rule


# --------------------------------------------------------------------------- #
# 消息存储（轻量内存实现，供 context/assemble 使用）
# --------------------------------------------------------------------------- #

@dataclass
class MessageRecord:
    """本地存储的消息副本（用于 trust 取低与正文校验）。"""

    source_id: str          # message.id 或 matrix_event_id（以 $ 开头）
    text: str               # 存储正文（以此为准）
    origin: str             # provenance.origin
    external: bool          # provenance.external
    forwarded_from: Optional[str] = None   # provenance.forwarded_from（$ 开头或 None）

    @property
    def provenance(self) -> Dict[str, Any]:
        p: Dict[str, Any] = {"origin": self.origin, "external": self.external}
        if self.forwarded_from is not None:
            p["forwarded_from"] = self.forwarded_from
        return p


class MessageStore:
    """按 source_id（message.id 或 matrix_event_id）查找消息记录。线程安全（读多写少）。"""

    def __init__(self) -> None:
        self._records: Dict[str, MessageRecord] = {}

    def register(self, record: MessageRecord) -> None:
        self._records[record.source_id] = record

    def get(self, source_id: str) -> Optional[MessageRecord]:
        return self._records.get(source_id)

    def __len__(self) -> int:
        return len(self._records)


def determine_message_trust(
    source_id: str,
    message_store: MessageStore,
) -> tuple[str, str]:
    """查本地存储，沿 forwarded_from 链逐跳取低，最多 MAX_FORWARD_HOPS 跳。

    查不到 / 超限 / forwarded_from 格式非法 → rule8 fail-closed untrusted。
    """
    visited: set[str] = set()
    current_id = source_id
    result_trust: Optional[str] = None
    result_rule = "rule8"
    forwarded = False

    for _hop in range(MAX_FORWARD_HOPS):
        if current_id in visited:
            return TRUST_UNTRUSTED, "rule8"   # 成环
        visited.add(current_id)

        record = message_store.get(current_id)
        if record is None:
            return TRUST_UNTRUSTED, "rule8"

        hop_trust, hop_rule = determine_provenance_trust(record.origin, record.external)

        if result_trust is None:
            result_trust = hop_trust
            result_rule = hop_rule
        else:
            lower = _lower_trust(result_trust, hop_trust)
            if lower != result_trust:
                result_trust = lower
                result_rule = hop_rule

        ff = record.forwarded_from
        if ff is None:
            break
        # 格式校验：必须以 $ 开头
        if not isinstance(ff, str) or not ff.startswith("$"):
            return TRUST_UNTRUSTED, "rule8"
        forwarded = True
        current_id = ff
    else:
        # 超过 MAX_FORWARD_HOPS
        return TRUST_UNTRUSTED, "rule8"

    # `system` 不参与取低，也不能由转发产生：链上若只剩 system（即每一跳都是
    # 网关系统通知），转发后的副本不再是系统指令来源 → fail-closed。
    if result_trust == TRUST_SYSTEM and forwarded:
        return TRUST_UNTRUSTED, "forwarded_from"

    return result_trust or TRUST_UNTRUSTED, result_rule

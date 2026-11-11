# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""ULID 生成（Crockford base32，26 位，时间有序）。

契约要求动态 / 评论 ID 匹配 ``^[0-9A-HJKMNP-TV-Z]{26}$``：48 bit 毫秒
时间戳（前 10 字符）+ 80 bit 随机（后 16 字符）。同一进程内同一毫秒保证
单调递增（分页游标依赖 `(created_at, id)` 的字典序与写入时间序一致），
时钟回拨时不后退（沿用上一毫秒并递增随机段）。
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Optional

# Crockford base32：去掉 I / L / O / U。
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1

_lock = threading.Lock()
_last_ms: int = 0
_last_random: int = 0


def _encode(value: int) -> str:
    """128 bit → 26 个 base32 字符（26×5=130 bit，最高 2 bit 恒为 0）。"""
    chars = []
    for shift in range(125, -1, -5):
        chars.append(ULID_ALPHABET[(value >> shift) & 0x1F])
    return "".join(chars)


def new_ulid() -> str:
    """生成一个新的 ULID（进程内时间有序、含 CSPRNG 随机段）。"""
    global _last_ms, _last_random
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            # 同一毫秒（或时钟回拨）：沿用上一时间戳并递增随机段，保序。
            ms = _last_ms
            _last_random += 1
            if _last_random > _RANDOM_MAX:  # pragma: no cover - 2^80 溢出不现实
                _last_random = secrets.randbits(_RANDOM_BITS)
        else:
            _last_random = secrets.randbits(_RANDOM_BITS)
        _last_ms = ms
        random_part = _last_random
    return _encode((ms << _RANDOM_BITS) | random_part)


def is_ulid(value: Optional[str]) -> bool:
    """是否为契约形状的 ULID（路径 id 预检用；不探测存在性）。"""
    return bool(value) and bool(ULID_RE.fullmatch(value))

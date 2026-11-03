# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""时间工具：契约要求所有 `date-time` 为 UTC 且以 `Z` 结尾。"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    """格式化为 RFC 3339 UTC，秒精度，`Z` 结尾（B0.1 校验脚本要求）。"""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_until(value: datetime, *, now: datetime) -> int:
    """向上取整的非负秒差。"""
    delta = (value - now).total_seconds()
    return max(0, math.ceil(delta))

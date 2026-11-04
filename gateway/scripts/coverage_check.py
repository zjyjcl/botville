#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""简易行覆盖率统计（零依赖）。

项目没有现成的 coverage 工具时，用 `sys.settrace` 做最小统计：记录每个
`.py` 源文件哪些行被执行，按文件汇总「已执行 / 可执行 / 覆盖率」。

用法：
    python3 gateway/scripts/coverage_check.py
    python3 gateway/scripts/coverage_check.py --module <module> [--args ...]

默认对 `gateway/tests` 运行 `unittest discover`，统计 `gateway/src`
下所有 `.py` 的行覆盖率。退出码 0 当总覆盖率 ≥ 阈值（默认 80），否则 1。

实现要点：
- 「可执行行」指非空、非纯注释行；docstring 起止行算一行（与 coverage.py
  口径一致，避免多行 docstring 虚高）。
- 只统计项目自身源码（`gateway/src/botville_gateway/`），不统计测试、
  脚本、stdlib、第三方包。
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import threading
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(REPO_ROOT, "gateway", "src")
PKG_DIR = os.path.join(SRC_DIR, "botville_gateway")
TESTS_DIR = os.path.join(REPO_ROOT, "gateway", "tests")


def _executable_lines(path: str) -> set:
    """返回该文件的可执行行号集合（1-based）。

    与 coverage.py 的口径对齐：
    - 空行、纯注释行不算；
    - 多行 docstring 只算起始行；
    - **括号/引号持续行不算**：`foo(a,\\n  b)` 的第二行不是独立语句，
      Python 3.9 的 `line` 事件也只在语句起始行触发。若把续行算进分母，
      任何多行字面量（如 `__all__ = [...]`）都会虚低覆盖率。
    """
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    execable: set = set()
    in_docstring = False
    docstring_delim = None
    depth = 0  # () [] {} 的嵌套深度
    for idx, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if in_docstring:
            if docstring_delim in stripped:
                in_docstring = False
            continue
        is_continuation = depth > 0
        if not is_continuation:
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith('"""') or stripped.startswith("'''"):
                delim = stripped[:3]
                rest = stripped[3:]
                if rest.endswith(delim) and len(rest) >= 3:
                    continue  # 单行 docstring
                in_docstring = True
                docstring_delim = delim
                continue
            execable.add(idx)
        # 更新括号深度（忽略字符串里的括号是不可能的，但对本仓库够用）
        depth += _bracket_delta(stripped, depth)
        depth = max(0, depth)
    return execable


def _bracket_delta(line: str, depth: int) -> int:
    """粗略估算一行带来的括号深度变化（跳过 `#` 之后的内容）。"""
    code = line.split("#", 1)[0]
    delta = 0
    for ch in code:
        if ch in "([{":
            delta += 1
        elif ch in ")]}":
            delta -= 1
    return delta


class _CoverageTracer:
    def __init__(self, watched_files: dict) -> None:
        self.watched = watched_files  # {path: {executable: set, hit: set}}
        self._lock = threading.Lock()

    def trace(self, frame, event, arg):
        if event != "line":
            return self.trace
        filename = frame.f_code.co_filename
        norm = os.path.normpath(filename)
        entry = self.watched.get(norm)
        if entry is None:
            return self.trace
        lineno = frame.f_lineno
        with self._lock:
            entry["hit"].add(lineno)
        return self.trace


def _collect_files() -> dict:
    files: dict = {}
    for root, _dirs, names in os.walk(PKG_DIR):
        for name in names:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                files[os.path.normpath(path)] = {
                    "executable": _executable_lines(path),
                    "hit": set(),
                }
    return files


def run_tests() -> dict:
    """跑 unittest discover，返回每个源文件的覆盖率数据。

    两个容易踩的坑：
    1. `sys.settrace` 是**线程局部**的。`ThreadingHTTPServer` 在独立线程里跑
       handler，必须同时用 `threading.settrace()` 让新建线程继承 tracer。
    2. `unittest` 的 discover 会 import 测试模块 → 间接 import 被测包，
       模块顶层行必须发生在 settrace **之后**才能被统计。所以先装 tracer
       再 discover（此时还没 import 任何 botville_gateway 模块）。
    """
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)
    if TESTS_DIR not in sys.path:
        sys.path.insert(0, TESTS_DIR)

    files = _collect_files()
    tracer = _CoverageTracer(files)

    old_trace = sys.gettrace()
    try:
        sys.settrace(tracer.trace)
        threading.settrace(tracer.trace)
        loader = unittest.TestLoader()
        suite = loader.discover(TESTS_DIR, pattern="test_*.py")
        runner = unittest.TextTestRunner(verbosity=0)
        result = runner.run(suite)
    finally:
        sys.settrace(old_trace)
        # Python 3.9 没有 threading.gettrace()，直接恢复到「不追踪新线程」。
        threading.settrace(None)

    return files, result.wasSuccessful()


def report(files: dict, threshold: float, show_missing: bool = False) -> int:
    print("\nbotville_gateway src line coverage")
    print("=" * 60)
    total_exec = 0
    total_hit = 0
    rows = []
    for path in sorted(files):
        entry = files[path]
        execable = entry["executable"]
        if not execable:
            continue
        hit = entry["hit"] & execable
        rel = os.path.relpath(path, SRC_DIR)
        rows.append((rel, len(hit), len(execable), sorted(execable - hit)))
        total_exec += len(execable)
        total_hit += len(hit)

    for rel, hit, execable, missing in rows:
        pct = (hit / execable * 100) if execable else 100.0
        bar = "#" * int(pct / 5)
        print(f"  {rel:<30} {hit:4d}/{execable:<4d} {pct:5.1f}% {bar}")
        if show_missing and missing:
            print(f"      missing: {_ranges(missing)}")

    overall = (total_hit / total_exec * 100) if total_exec else 100.0
    print("-" * 60)
    print(f"  {'TOTAL':<30} {total_hit:4d}/{total_exec:<4d} {overall:5.1f}%")
    print(f"  threshold: {threshold:.0f}%")
    if overall >= threshold:
        print(f"  PASS: coverage {overall:.1f}% >= {threshold:.0f}%")
        return 0
    print(f"  FAIL: coverage {overall:.1f}% < {threshold:.0f}%")
    return 1


def _ranges(numbers: list) -> str:
    """把行号列表压缩成 `1-5,9,12-14` 形式。"""
    if not numbers:
        return ""
    parts = []
    start = prev = numbers[0]
    for value in numbers[1:]:
        if value == prev + 1:
            prev = value
            continue
        parts.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = value
    parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Zero-dependency line coverage for gateway src")
    parser.add_argument("--threshold", type=float, default=80.0,
                        help="minimum coverage percent (default 80)")
    parser.add_argument("--show-missing", action="store_true", default=False,
                        help="print uncovered line numbers per file")
    args = parser.parse_args(argv)
    files, _ok = run_tests()
    return report(files, args.threshold, show_missing=args.show_missing)


if __name__ == "__main__":
    sys.exit(main())

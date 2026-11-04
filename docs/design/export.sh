#!/bin/bash
# 在独立、临时的浏览器配置中导出并检查静态设计稿。
set -euo pipefail
DESIGN_DIR="$(cd "$(dirname "$0")" && pwd)"
CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
if [[ ! -x "$CHROME" ]]; then
  CHROME=''
  for candidate in '/Applications/Chromium.app/Contents/MacOS/Chromium' "${HOME}/Applications/Chromium.app/Contents/MacOS/Chromium"; do
    if [[ -x "$candidate" ]]; then CHROME="$candidate"; break; fi
  done
  if [[ -z "$CHROME" ]]; then
    for command_name in chromium chromium-browser; do
      candidate="$(command -v "$command_name" || true)"
      if [[ -n "$candidate" && -x "$candidate" ]]; then CHROME="$candidate"; break; fi
    done
  fi
fi
[[ -n "$CHROME" ]] || { printf '未找到 Chrome 或 Chromium，请先安装浏览器。\n' >&2; exit 1; }
command -v node >/dev/null || { printf '需要 Node.js 22+ 以使用内建 WebSocket。\n' >&2; exit 1; }
node "$DESIGN_DIR/export.mjs" "$DESIGN_DIR" "$CHROME" "${1:-}"

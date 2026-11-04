#!/bin/bash
# 独立导出静态设计稿；所有产物、日志与临时配置均限制在本目录。
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
if [[ -z "$CHROME" ]]; then
  printf '未找到 Chrome 或 Chromium，请先安装浏览器。\n' >&2
  exit 1
fi
command -v python3 >/dev/null || { printf '需要 Python 3 读取页面尺寸。\n' >&2; exit 1; }
mkdir -p "$DESIGN_DIR/png"
EXPORT_WORK="$(mktemp -d "$DESIGN_DIR/.export.XXXXXX")"
trap 'rm -rf "$EXPORT_WORK"' EXIT
# mktemp -d 生成全新配置，不复用日常浏览器；临时根目录也在写入白名单内。
export TMPDIR="$EXPORT_WORK/"
for html in "$DESIGN_DIR"/*.html; do
  [[ -f "$html" ]] || continue
  geometry="$(python3 - "$html" <<'PY'
import sys,re
from pathlib import Path
s=Path(sys.argv[1]).read_text(encoding='utf-8')
def value(key):
    m=re.search(r'<meta\s+name="'+key+r'"\s+content="(\d+)"',s)
    if not m: raise SystemExit('页面缺少导出尺寸：'+key)
    return int(m.group(1))
n=value('design-frame-count')
w=max(value('design-export-width'),64+390*n+24*(n-1))
print(str(w)+','+str(value('design-export-height')))
PY
)"
  uri="$(python3 - "$html" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).resolve().as_uri())
PY
)"
  name="$(basename "$html" .html)"
  profile="$(mktemp -d)"
  "$CHROME" --headless --disable-gpu --no-first-run --no-default-browser-check \
    --disable-background-networking --disable-component-update --disable-sync \
    --disable-breakpad --hide-scrollbars --force-device-scale-factor=1 \
    --user-data-dir="$profile" --window-size="$geometry" \
    --screenshot="$DESIGN_DIR/png/$name.png" "$uri" >"$EXPORT_WORK/browser.log" 2>&1 || {
      printf '截图失败：%s\n' "$name" >&2
      cat "$EXPORT_WORK/browser.log" >&2
      exit 1
    }
  [[ -s "$DESIGN_DIR/png/$name.png" ]] || { printf '截图未生成：%s\n' "$name" >&2; exit 1; }
  printf '已导出：png/%s.png（%s）\n' "$name" "$geometry"
done

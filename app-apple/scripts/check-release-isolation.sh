#!/bin/sh
# Copyright 2026 botville contributors
# SPDX-License-Identifier: Apache-2.0
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
plutil -lint "$root/BotvilleApp/Info.plist" "$root/BotvilleApp/Info-Debug.plist" >/dev/null
grep -q '^#if DEBUG$' "$root/Sources/BotvilleApp/PushDiagnosticsView.swift"
grep -q '^#if DEBUG$' "$root/Sources/BotvilleApp/LoginView.swift"
BOTVILLE_APP_ROOT="$root" python3 - <<'PY'
import json, os, pathlib, subprocess
root = pathlib.Path(os.environ['BOTVILLE_APP_ROOT'])
forbidden = {'NSAppTransportSecurity', 'NSAllowsLocalNetworking', 'NSLocalNetworkUsageDescription', 'BotvilleSygnalNotifyURL'}
def json_plutil(path):
    return json.loads(subprocess.check_output(['plutil', '-convert', 'json', '-o', '-', str(path)]))
objects = json_plutil(root / 'Botville.xcodeproj/project.pbxproj')['objects']
releases = 0
for target in objects.values():
    if target.get('isa') != 'PBXNativeTarget' or target.get('productType') != 'com.apple.product-type.application': continue
    configs = objects[target['buildConfigurationList']]['buildConfigurations']
    for identifier in configs:
        config = objects[identifier]
        if config.get('name') != 'Release': continue
        releases += 1
        got = config.get('buildSettings', {}).get('INFOPLIST_FILE')
        if got != 'BotvilleApp/Info.plist': raise SystemExit(f"Release target {target.get('name')} uses {got!r}, not BotvilleApp/Info.plist")
        for key, value in config.get('buildSettings', {}).items():
            if key.startswith('INFOPLIST_FILE[') and value != 'BotvilleApp/Info.plist':
                raise SystemExit(f'Release conditional plist override: {key}={value}')
if not releases: raise SystemExit('No application Release configuration found')
def keys(value):
    if isinstance(value, dict):
        for key, nested in value.items(): yield key; yield from keys(nested)
    elif isinstance(value, list):
        for nested in value: yield from keys(nested)
release = json_plutil(root / 'BotvilleApp/Info.plist')
leaked = forbidden & set(keys(release))
if leaked: raise SystemExit('Release Info.plist contains DEBUG-only key(s): ' + ', '.join(sorted(leaked)))
def guarded(path, needle):
    stack, found = [], False
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if line.startswith('#if '): stack.append(line == '#if DEBUG'); continue
        if line.startswith('#elseif') or line == '#else':
            if not stack: raise SystemExit(f'unbalanced conditional at {path}:{number}')
            if stack[-1]: stack[-1] = False
            continue
        if line == '#endif':
            if not stack: raise SystemExit(f'unbalanced conditional at {path}:{number}')
            stack.pop(); continue
        if needle in line:
            found = True
            if not any(stack): raise SystemExit(f'{needle!r} is not DEBUG-guarded at {path}:{number}')
    if stack: raise SystemExit(f'unclosed conditional in {path}')
    if not found: raise SystemExit(f'{needle!r} not found in {path}')
sources = root / 'Sources/BotvilleApp'
guarded(sources / 'PushDiagnosticsView.swift', 'struct PushDiagnosticsView')
guarded(sources / 'LoginView.swift', 'ManualHomeserverLoginView')
for source in sources.glob('*.swift'):
    text = source.read_text()
    for token in ('PushDiagnosticsView(', 'ManualHomeserverLoginView('):
        if token in text: guarded(source, token)
print('Release isolation check passed')
PY

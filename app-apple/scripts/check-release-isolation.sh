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
for config in objects.values():
    if config.get('isa') != 'XCBuildConfiguration': continue
    settings = config.get('buildSettings', {})
    if any(key.startswith('PROVISIONING_PROFILE') for key in settings):
        raise SystemExit('Signing configuration must not pin a provisioning profile')
    for key, value in settings.items():
        if key.startswith(('CODE_SIGNING_ALLOWED', 'CODE_SIGNING_REQUIRED')) and value == 'NO':
            raise SystemExit(f'Signing must remain enabled: {key}={value}')
    if config.get('name') != 'Release': continue
    entitlements = [key for key in settings if key.startswith('CODE_SIGN_ENTITLEMENTS')]
    if entitlements:
        raise SystemExit('Release configuration contains CODE_SIGN_ENTITLEMENTS: ' + ', '.join(sorted(entitlements)))
releases = 0
for target in objects.values():
    if target.get('isa') != 'PBXNativeTarget' or target.get('productType') != 'com.apple.product-type.application': continue
    configs = objects[target['buildConfigurationList']]['buildConfigurations']
    checked = set()
    for identifier in configs:
        config = objects[identifier]
        name = config.get('name')
        settings = config.get('buildSettings', {})
        if name in ('Debug', 'Release'):
            checked.add(name)
            expected = {'CODE_SIGN_STYLE': 'Automatic', 'DEVELOPMENT_TEAM': '3Z8WP6U6F7', 'CODE_SIGN_IDENTITY[sdk=iphonesimulator*]': '-'}
            if name == 'Debug': expected['CODE_SIGN_ENTITLEMENTS'] = 'BotvilleApp/BotvilleApp.entitlements'
            for key, value in expected.items():
                if settings.get(key) != value:
                    raise SystemExit(f'{name} target {target.get("name")} requires {key}={value}')
        if config.get('name') != 'Release': continue
        releases += 1
        got = config.get('buildSettings', {}).get('INFOPLIST_FILE')
        if got != 'BotvilleApp/Info.plist': raise SystemExit(f"Release target {target.get('name')} uses {got!r}, not BotvilleApp/Info.plist")
        for key, value in config.get('buildSettings', {}).items():
            if key.startswith('INFOPLIST_FILE[') and value != 'BotvilleApp/Info.plist':
                raise SystemExit(f'Release conditional plist override: {key}={value}')
    if checked != {'Debug', 'Release'}: raise SystemExit('Application target must have Debug and Release configurations')
if not releases: raise SystemExit('No application Release configuration found')
def keys(value):
    if isinstance(value, dict):
        for key, nested in value.items(): yield key; yield from keys(nested)
    elif isinstance(value, list):
        for nested in value: yield from keys(nested)
release = json_plutil(root / 'BotvilleApp/Info.plist')
leaked = forbidden & set(keys(release))
if leaked: raise SystemExit('Release Info.plist contains DEBUG-only key(s): ' + ', '.join(sorted(leaked)))
debug = json_plutil(root / 'BotvilleApp/Info-Debug.plist')
if debug.get('BotvilleSygnalNotifyURL') != 'http://sygnal:5000/_matrix/push/v1/notify':
    raise SystemExit('Debug Info.plist must default to the compose Sygnal notify URL')
if json_plutil(root / 'BotvilleApp/BotvilleApp.entitlements').get('aps-environment') != 'development':
    raise SystemExit('Debug APNs entitlement must use the development environment')
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
core = root / 'Sources/BotvilleCore'
guarded(core / 'PushRegistrar.swift', 'private static let isDebugBuild = true')
if 'PushHTTPPolicy.permitsHTTP(allowHTTP)' not in (core / 'MatrixService.swift').read_text():
    raise SystemExit('MatrixService must delegate push HTTP policy to PushHTTPPolicy')
print('Release isolation check passed')
PY

#!/bin/sh
# Copyright 2026 botville contributors
# SPDX-License-Identifier: Apache-2.0
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
plutil -lint "$root/BotvilleApp/Info.plist" "$root/BotvilleApp/Info-Debug.plist" "$root/NotificationService/Info.plist" >/dev/null
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
def target_configurations(name, product_type):
    target = next((target for target in objects.values()
                   if target.get('isa') == 'PBXNativeTarget' and target.get('name') == name
                   and target.get('productType') == product_type), None)
    if not target: raise SystemExit(f'Missing {name} target')
    result = {}
    for identifier in objects[target['buildConfigurationList']]['buildConfigurations']:
        config = objects[identifier]
        result[config.get('name')] = config.get('buildSettings', {})
    if set(result) != {'Debug', 'Release'}: raise SystemExit(f'{name} target must have Debug and Release configurations')
    return result

app = target_configurations('BotvilleApp-iOS', 'com.apple.product-type.application')
nse = target_configurations('BotvilleNotificationService', 'com.apple.product-type.app-extension')
for target_name, configurations, bundle_id, entitlements, plists in (
    ('BotvilleApp-iOS', app, 'com.zjyjcl.botville',
     {'Debug': 'BotvilleApp/BotvilleApp.entitlements', 'Release': 'BotvilleApp/BotvilleApp-Release.entitlements'},
     {'Debug': 'BotvilleApp/Info-Debug.plist', 'Release': 'BotvilleApp/Info.plist'}),
    ('BotvilleNotificationService', nse, 'com.zjyjcl.botville.notification',
     {'Debug': 'NotificationService/NotificationService.entitlements', 'Release': 'NotificationService/NotificationService.entitlements'},
     {'Debug': 'NotificationService/Info.plist', 'Release': 'NotificationService/Info.plist'}),
):
    for name, settings in configurations.items():
        expected = {'CODE_SIGN_STYLE': 'Automatic', 'DEVELOPMENT_TEAM': '3Z8WP6U6F7',
                    'CODE_SIGN_IDENTITY[sdk=iphonesimulator*]': '-',
                    'PRODUCT_BUNDLE_IDENTIFIER': bundle_id,
                    'CODE_SIGN_ENTITLEMENTS': entitlements[name],
                    'INFOPLIST_FILE': plists[name]}
        for key, value in expected.items():
            if settings.get(key) != value:
                raise SystemExit(f'{name} target {target_name} requires {key}={value}')
        for key, value in settings.items():
            if key.startswith('INFOPLIST_FILE[') and value != plists[name]:
                raise SystemExit(f'{name} target {target_name} has unsafe plist override: {key}={value}')
            if key.startswith('CODE_SIGN_ENTITLEMENTS[') and value != entitlements[name]:
                raise SystemExit(f'{name} target {target_name} has unsafe entitlement override: {key}={value}')
def keys(value):
    if isinstance(value, dict):
        for key, nested in value.items(): yield key; yield from keys(nested)
    elif isinstance(value, list):
        for nested in value: yield from keys(nested)
for path in (root / 'BotvilleApp/Info.plist', root / 'NotificationService/Info.plist'):
    leaked = forbidden & set(keys(json_plutil(path)))
    if leaked: raise SystemExit(f'{path.name} contains DEBUG-only key(s): ' + ', '.join(sorted(leaked)))
debug = json_plutil(root / 'BotvilleApp/Info-Debug.plist')
if debug.get('BotvilleSygnalNotifyURL') != 'http://sygnal:5000/_matrix/push/v1/notify':
    raise SystemExit('Debug Info.plist must default to the compose Sygnal notify URL')
if json_plutil(root / 'BotvilleApp/BotvilleApp.entitlements').get('aps-environment') != 'development':
    raise SystemExit('Debug APNs entitlement must use the development environment')
def check_groups(path, private_group, requires_development_aps):
    value = json_plutil(path)
    if value.get('com.apple.security.application-groups') != ['group.com.zjyjcl.botville']:
        raise SystemExit(f'{path.name} must contain only the Botville App Group')
    if value.get('keychain-access-groups') != [private_group, '$(AppIdentifierPrefix)com.zjyjcl.botville.shared']:
        raise SystemExit(f'{path.name} must contain its private and shared Keychain groups')
    aps = value.get('aps-environment')
    if requires_development_aps and aps != 'development':
        raise SystemExit(f'{path.name} must use the development APNs entitlement')
    if not requires_development_aps and aps is not None:
        raise SystemExit(f'{path.name} must not contain an APNs entitlement')
check_groups(root / 'BotvilleApp/BotvilleApp.entitlements', '$(AppIdentifierPrefix)com.zjyjcl.botville', True)
check_groups(root / 'BotvilleApp/BotvilleApp-Release.entitlements', '$(AppIdentifierPrefix)com.zjyjcl.botville', False)
check_groups(root / 'NotificationService/NotificationService.entitlements', '$(AppIdentifierPrefix)com.zjyjcl.botville.notification', False)
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
guarded(core / 'PushRegistrar.swift', 'static let isDebugBuild = true')
if 'PushHTTPPolicy.permitsHTTP(allowHTTP)' not in (core / 'MatrixService.swift').read_text():
    raise SystemExit('MatrixService must delegate push HTTP policy to PushHTTPPolicy')
print('Release isolation check passed')
PY

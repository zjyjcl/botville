#!/bin/sh
# Copyright 2026 botville contributors
# SPDX-License-Identifier: Apache-2.0
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
plutil -lint "$root/BotvilleApp/Info.plist" "$root/BotvilleApp/Info-Debug.plist" "$root/NotificationService/Info.plist" "$root/NotificationService/Info-Debug.plist" >/dev/null
grep -q '^#if DEBUG$' "$root/Sources/BotvilleApp/PushDiagnosticsView.swift"
grep -q '^#if DEBUG$' "$root/Sources/BotvilleApp/LoginView.swift"
BOTVILLE_APP_ROOT="$root" python3 - <<'PY'
import json, os, pathlib, re, subprocess
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
def keys(value):
    if isinstance(value, dict):
        for key, nested in value.items(): yield key; yield from keys(nested)
    elif isinstance(value, list):
        for nested in value: yield from keys(nested)

app = target_configurations('BotvilleApp-iOS', 'com.apple.product-type.application')
nse = target_configurations('BotvilleNotificationService', 'com.apple.product-type.app-extension')
# Every native target gets a Release configuration check. Future apps and
# extensions must supply a shipping plist; optional entitlement settings and
# conditional overrides are validated when present.
release_targets = {}
for identifier, target in objects.items():
    if target.get('isa') != 'PBXNativeTarget': continue
    configurations = {}
    for config_id in objects[target['buildConfigurationList']]['buildConfigurations']:
        config = objects[config_id]
        configurations[config.get('name')] = config.get('buildSettings', {})
    release = configurations.get('Release')
    if release is None:
        raise SystemExit(f"{target.get('name')} target must have a Release configuration")
    plist_settings = [(key, value) for key, value in release.items() if key.startswith('INFOPLIST_FILE')]
    if target.get('productType') in ('com.apple.product-type.application', 'com.apple.product-type.app-extension') and not plist_settings:
        raise SystemExit(f"{target.get('name')} Release must declare an INFOPLIST_FILE")
    for key, value in plist_settings:
        if not isinstance(value, str) or not (root / value).is_file():
            raise SystemExit(f"{target.get('name')} Release has an invalid {key}")
        leaked = forbidden & set(keys(json_plutil(root / value)))
        if leaked: raise SystemExit(f'{target.get("name")} Release plist {value} contains DEBUG-only key(s): ' + ', '.join(sorted(leaked)))
    for key, value in release.items():
        if not key.startswith('CODE_SIGN_ENTITLEMENTS'): continue
        if not isinstance(value, str) or not (root / value).is_file():
            raise SystemExit(f"{target.get('name')} Release has an invalid {key}")
        if json_plutil(root / value).get('aps-environment') == 'development':
            raise SystemExit(f'{target.get("name")} Release entitlements must not use the development APNs environment')
    release_targets[identifier] = target
# Compare configured teams without embedding or printing their values.
teams = [settings.get('DEVELOPMENT_TEAM', '') for configs in (app, nse) for settings in configs.values()]
if any(not isinstance(team, str) or not re.fullmatch(r'[A-Z0-9]{10}', team) for team in teams) or len(set(teams)) != 1:
    raise SystemExit('App and extension require the same configured signing team')
for target_name, configurations, bundle_id, entitlements, plists in (
    ('BotvilleApp-iOS', app, 'com.zjyjcl.botville',
     {'Debug': 'BotvilleApp/BotvilleApp.entitlements', 'Release': 'BotvilleApp/BotvilleApp-Release.entitlements'},
     {'Debug': 'BotvilleApp/Info-Debug.plist', 'Release': 'BotvilleApp/Info.plist'}),
    ('BotvilleNotificationService', nse, 'com.zjyjcl.botville.notification',
     {'Debug': 'NotificationService/NotificationService.entitlements', 'Release': 'NotificationService/NotificationService.entitlements'},
     {'Debug': 'NotificationService/Info-Debug.plist', 'Release': 'NotificationService/Info.plist'}),
):
    for name, settings in configurations.items():
        expected = {'CODE_SIGN_STYLE': 'Automatic',
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
for path in (root / 'BotvilleApp/Info.plist', root / 'NotificationService/Info.plist'):
    leaked = forbidden & set(keys(json_plutil(path)))
    if leaked: raise SystemExit(f'{path.name} contains DEBUG-only key(s): ' + ', '.join(sorted(leaked)))
debug = json_plutil(root / 'BotvilleApp/Info-Debug.plist')
if debug.get('BotvilleSygnalNotifyURL') != 'http://sygnal:5000/_matrix/push/v1/notify':
    raise SystemExit('Debug Info.plist must default to the compose Sygnal notify URL')
if json_plutil(root / 'BotvilleApp/BotvilleApp.entitlements').get('aps-environment') != 'development':
    raise SystemExit('Debug APNs entitlement must use the development environment')
release_app_aps_environment = None  # Controlled production signing sets this to 'production'.
def check_groups(path, private_group, expected_aps_environment):
    value = json_plutil(path)
    if value.get('com.apple.security.application-groups') != ['group.com.zjyjcl.botville']:
        raise SystemExit(f'{path.name} must contain only the Botville App Group')
    if value.get('keychain-access-groups') != [private_group, '$(AppIdentifierPrefix)com.zjyjcl.botville.shared']:
        raise SystemExit(f'{path.name} must contain its private and shared Keychain groups')
    aps = value.get('aps-environment')
    if aps != expected_aps_environment:
        raise SystemExit(f'{path.name} must use APNs entitlement {expected_aps_environment!r}')
check_groups(root / 'BotvilleApp/BotvilleApp.entitlements', '$(AppIdentifierPrefix)com.zjyjcl.botville', 'development')
# Temporary release gate. Set release_app_aps_environment to 'production' for
# controlled production signing; the NSE intentionally remains without APNs.
check_groups(root / 'BotvilleApp/BotvilleApp-Release.entitlements', '$(AppIdentifierPrefix)com.zjyjcl.botville', release_app_aps_environment)
check_groups(root / 'NotificationService/NotificationService.entitlements', '$(AppIdentifierPrefix)com.zjyjcl.botville.notification', None)
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
for directory in (sources, root / 'NotificationService'):
    for source in directory.rglob('*.swift'):
        text = source.read_text()
        for token in ('PushDiagnosticsView(', 'ManualHomeserverLoginView(',
                      'registerForRemoteNotifications(', 'requestAuthorization(',
                      'didRegisterForRemoteNotificationsWithDeviceToken',
                      'didFailToRegisterForRemoteNotificationsWithError'):
            if token in text: guarded(source, token)
core = root / 'Sources/BotvilleCore'
guarded(core / 'PushRegistrar.swift', 'static let isDebugBuild = true')
if 'PushHTTPPolicy.permitsHTTP(allowHTTP)' not in (core / 'MatrixService.swift').read_text():
    raise SystemExit('MatrixService must delegate push HTTP policy to PushHTTPPolicy')

# The notification extension is a separate process boundary. It may read the
# shared keychain item and make the event GET plus one best-effort profile GET,
# but must not log tokens
# or add plaintext storage. Derive the allowlist from the extension target so
# new source files cannot silently bypass this boundary.
nse_target_id = next(identifier for identifier, target in release_targets.items()
                     if target.get('name') == 'BotvilleNotificationService')
nse_target = objects[nse_target_id]
if nse_target.get('packageProductDependencies', []):
    raise SystemExit('NSE must not link package products')
for phase_id in nse_target['buildPhases']:
    phase = objects[phase_id]
    if phase.get('isa') == 'PBXFrameworksBuildPhase' and phase.get('files', []):
        raise SystemExit('NSE must not link frameworks')
nse_sources_phase = next(objects[phase_id] for phase_id in nse_target['buildPhases']
                         if objects[phase_id].get('isa') == 'PBXSourcesBuildPhase')
nse_source_paths = []
for build_file_id in nse_sources_phase['files']:
    file_ref_id = objects[build_file_id].get('fileRef')
    file_ref = objects.get(file_ref_id, {})
    path, source_tree = file_ref.get('path'), file_ref.get('sourceTree')
    if not isinstance(path, str) or not isinstance(source_tree, str): raise SystemExit('NSE source has no path/sourceTree')
    source_paths = {
        ('NotificationService.swift', '<group>'): root / 'NotificationService/NotificationService.swift',
        ('Sources/BotvilleCore/SharedPushCredentials.swift', 'SOURCE_ROOT'): root / 'Sources/BotvilleCore/SharedPushCredentials.swift',
        ('Sources/BotvilleCore/PushNotificationContent.swift', 'SOURCE_ROOT'): root / 'Sources/BotvilleCore/PushNotificationContent.swift',
    }
    if (path, source_tree) not in source_paths or not source_paths[path, source_tree].is_file():
        raise SystemExit(f'NSE source path/sourceTree is not in the controlled allowlist: {path} {source_tree}')
    if source_tree == '<group>' and not any(file_ref_id in group.get('children', []) and group.get('path') == 'NotificationService'
                                            for group in objects.values() if group.get('isa') == 'PBXGroup'):
        raise SystemExit('NSE group-relative source must be in NotificationService group')
    nse_source_paths.append(source_paths[path, source_tree])
if {path.name for path in nse_source_paths} != {'NotificationService.swift', 'SharedPushCredentials.swift', 'PushNotificationContent.swift'}:
    raise SystemExit('NSE source set must be exactly NotificationService, SharedPushCredentials, and PushNotificationContent')
for source in nse_source_paths:
    source_text = source.read_text()
    if re.search(r'\b(?:print|debugPrint|dump|NSLog|os_log)\s*\(', source_text):
        raise SystemExit(f'NSE must use only its safe Logger diagnostics: {source}')
    for number, line in enumerate(source_text.splitlines(), 1):
        if re.search(r'\b\w+\.(?:trace|debug|info|notice|warning|error|fault|critical|log)\s*\(', line):
            safe_logs = {
                'logger.debug("Shared push credentials present: \\(present, privacy: .public)")',
                'logger.warning("Shared push credentials unavailable")',
            }
            if line.strip() not in safe_logs or 'token' in line.lower():
                raise SystemExit(f'NSE has an unsafe log statement: {source}:{number}')
    for forbidden_storage in ('UserDefaults', 'FileHandle', 'write(to:', 'Data(contentsOf:'):
        if forbidden_storage in source_text:
            raise SystemExit(f'NSE must not use plaintext credential storage ({forbidden_storage}): {source}')
    if 'import MatrixRustSDK' in source_text or 'import BotvilleCore' in source_text:
        raise SystemExit(f'NSE must not import the Matrix SDK or BotvilleCore: {source}')
    if source.name != 'PushNotificationContent.swift' and any(api in source_text for api in ('URLSession', 'URLRequest')):
        raise SystemExit(f'Only PushNotificationContent may use URLSession/URLRequest in NSE: {source}')
content_source = next(path for path in nse_source_paths if path.name == 'PushNotificationContent.swift')
content_text = content_source.read_text()
for required in ('URLSession', '/_matrix/client/v3/rooms/', '/event/', 'httpMethod = "GET"', 'Authorization', '.ephemeral', 'willPerformHTTPRedirection', 'requestTimeout', 'timeBudget'):
    if required not in content_text:
        raise SystemExit(f'NSE event fetch is missing required boundary {required!r}')
if content_text.count('/_matrix/client/v3/rooms/') != 1:
    raise SystemExit('NSE may contain only the single authorized Matrix event endpoint')
if content_text.count('/_matrix/client/v3/profile/') != 1 or content_text.count('/_matrix/client/') != 2:
    raise SystemExit('NSE may contain only the authorized event GET and single profile GET endpoints')
if content_text.count('dataTask(with:') != 2 or content_text.count('URLRequest(url:') != 2:
    raise SystemExit('NSE must construct and run only the event and optional profile requests')
if content_text.count('httpMethod =') != 2 or content_text.count('httpMethod = "GET"') != 2:
    raise SystemExit('NSE event and profile requests must use exactly two GET method assignments')
if 'components.percentEncodedPath = base + "/_matrix/client/v3/rooms/" + room + "/event/" + event' not in content_text:
    raise SystemExit('NSE must build only the percent-encoded room/event endpoint path')
if 'components.percentEncodedPath = base + "/_matrix/client/v3/profile/" + user' not in {line.strip() for line in content_text.splitlines()}:
    raise SystemExit('NSE profile request must use only the percent-encoded user profile endpoint path')

# Ensure the app embeds the NSE product and declares the matching target
# dependency; an appex reference string alone is not sufficient.
app_target_id = next(identifier for identifier, target in release_targets.items()
                     if target.get('name') == 'BotvilleApp-iOS')
app_target = objects[app_target_id]
nse_product_ref = objects[nse_target_id].get('productReference')
if not any(objects[dependency].get('isa') == 'PBXTargetDependency' and objects[dependency].get('target') == nse_target_id
           for dependency in app_target.get('dependencies', [])):
    raise SystemExit('BotvilleApp-iOS must declare a target dependency on BotvilleNotificationService')
embedded = False
for phase_id in app_target.get('buildPhases', []):
    phase = objects[phase_id]
    if phase.get('isa') != 'PBXCopyFilesBuildPhase' or str(phase.get('dstSubfolderSpec')) != '13': continue
    for build_file_id in phase.get('files', []):
        if objects[build_file_id].get('isa') == 'PBXBuildFile' and objects[build_file_id].get('fileRef') == nse_product_ref:
            embedded = True
if not embedded:
    raise SystemExit('BotvilleApp-iOS must embed BotvilleNotificationService.appex in a Copy Files phase')
print('Release isolation check passed')
PY

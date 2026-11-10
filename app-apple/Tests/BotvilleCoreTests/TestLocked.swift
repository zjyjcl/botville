// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// SDK async overrides and URLProtocol callbacks run off the test's actor.
/// Keep compound mutations (including Array.append) under the same lock as
/// reads; a separately locked get/set would still lose concurrent updates.
@propertyWrapper
final class TestLocked<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value

    init(wrappedValue: Value) { value = wrappedValue }

    var wrappedValue: Value {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
        _modify {
            lock.lock()
            defer { lock.unlock() }
            yield &value
        }
    }

    var projectedValue: TestLocked<Value> { self }

    func withValue<Result>(_ body: (inout Value) -> Result) -> Result {
        lock.withLock { body(&value) }
    }
}

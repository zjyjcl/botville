// swift-tools-version: 5.9
// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import PackageDescription

let package = Package(
    name: "Botville",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "BotvilleCore", targets: ["BotvilleCore"]),
        .executable(name: "BotvilleApp", targets: ["BotvilleApp"])
    ],
    dependencies: [
        .package(url: "https://github.com/matrix-org/matrix-rust-components-swift", exact: "26.10.27")
    ],
    targets: [
        .target(name: "BotvilleCore", dependencies: [
            .product(name: "MatrixRustSDK", package: "matrix-rust-components-swift")
        ]),
        .executableTarget(name: "BotvilleApp", dependencies: ["BotvilleCore"]),
        .testTarget(name: "BotvilleCoreTests", dependencies: ["BotvilleCore"])
    ]
)

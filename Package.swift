// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "AISubtitle",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "aisubtitle", targets: ["AISubtitle"]),
        .executable(name: "qwen3-asr-stdin", targets: ["Qwen3ASRStdin"])
    ],
    dependencies: [
        .package(url: "https://github.com/ml-explore/mlx-swift", exact: "0.31.3")
    ],
    targets: [
        .executableTarget(
            name: "AISubtitle",
            swiftSettings: [
                .enableUpcomingFeature("BareSlashRegexLiterals")
            ]
        ),
        .executableTarget(
            name: "Qwen3ASRStdin",
            dependencies: [
                .product(name: "MLX", package: "mlx-swift"),
                .product(name: "MLXNN", package: "mlx-swift"),
                .product(name: "MLXFast", package: "mlx-swift")
            ],
            swiftSettings: [
                .enableUpcomingFeature("BareSlashRegexLiterals")
            ]
        )
    ]
)

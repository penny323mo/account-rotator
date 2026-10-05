// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "AGYRotator", platforms: [.macOS(.v13)],
    products: [.executable(name: "AGYRotator", targets: ["AGYRotator"])],
    targets: [.executableTarget(name: "AGYRotator")])

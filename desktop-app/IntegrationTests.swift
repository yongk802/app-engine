import Foundation

@main
struct IntegrationTests {
    static func expect(_ condition: Bool, line: Int = #line) throws {
        if !condition { throw LauncherError(message: "Integration check failed at line \(line)") }
    }
    static func main() throws {
        guard CommandLine.arguments.count == 3 else { fatalError("Pass engine and store repository paths") }
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("launcher integration \(UUID().uuidString)")
        try fm.createDirectory(at: root.appendingPathComponent("apps"), withIntermediateDirectories: true)
        let manager = ServiceManager(serviceDirectory: root.appendingPathComponent("services"), logDirectory: root.appendingPathComponent("logs"), suffix: ".test-\(getpid())")
        defer {
            for kind in ServiceKind.allCases { try? manager.stop(kind) }
            try? fm.removeItem(at: root)
        }
        let settings = LauncherSettings(engineRepository: CommandLine.arguments[1], storeRepository: CommandLine.arguments[2],
            appsDirectory: root.appendingPathComponent("apps").path, stateDirectory: root.appendingPathComponent("state").path,
            storeDataDirectory: root.appendingPathComponent("store data").path, enginePort: 18770, storePort: 18780)
        try settings.validate()
        for kind in ServiceKind.allCases {
            let configuration = ServiceConfiguration(kind: kind, settings: settings)
            guard manager.portAvailable(configuration.port) else { throw LauncherError(message: "Test port \(configuration.port) occupied") }
            try expect(!manager.owns(kind))
            let started = try manager.start(configuration)
            try expect(started.running && started.managed)
            let reused = try manager.start(configuration)
            try expect(reused.running && reused.managed)
            // A separate launcher must recognize an external service without stopping it.
            let outsider = ServiceManager(serviceDirectory: root.appendingPathComponent("other"), suffix: ".other-\(getpid())")
            let external = outsider.status(configuration)
            try expect(external.running && !external.managed)
            try outsider.stop(kind)
            try expect(manager.status(configuration).running)
            try manager.stop(kind)
            try expect(!manager.owns(kind))
            try expect(!manager.status(configuration).running)
            print("\(kind.title): start, health, repeat start, external ownership and stop passed")
        }
    }
}

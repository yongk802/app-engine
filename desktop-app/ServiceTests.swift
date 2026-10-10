import Foundation

@main
struct ServiceTests {
    static func main() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("engine tests \(UUID().uuidString)")
        defer { try? fm.removeItem(at: root) }
        for directory in ["engine/.venv/bin", "store/.venv/bin", "store/app_store", "apps"] {
            try fm.createDirectory(at: root.appendingPathComponent(directory), withIntermediateDirectories: true)
        }
        for file in ["engine/engine.py", "store/app_store/__main__.py", "engine/.venv/bin/python", "store/.venv/bin/python"] {
            fm.createFile(atPath: root.appendingPathComponent(file).path, contents: Data())
        }
        for repo in ["engine", "store"] {
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: root.appendingPathComponent("\(repo)/.venv/bin/python").path)
        }
        var settings = LauncherSettings(engineRepository: root.appendingPathComponent("engine").path,
            storeRepository: root.appendingPathComponent("store").path, appsDirectory: root.appendingPathComponent("apps").path,
            stateDirectory: root.appendingPathComponent("state").path, storeDataDirectory: root.appendingPathComponent("store data").path,
            enginePort: 18770, storePort: 18780)
        try settings.validate()
        let service = ServiceConfiguration(kind: .engine, settings: settings)
        let plist = try PropertyListSerialization.propertyList(from: service.plistData(logDirectory: root), format: nil) as! [String: Any]
        let argv = plist["ProgramArguments"] as! [String]
        assert(argv == [root.appendingPathComponent("engine/.venv/bin/python").path, root.appendingPathComponent("engine/engine.py").path])
        assert(plist["Label"] as? String == "io.appengine.desktop.engine")
        assert(plist["KeepAlive"] as? Bool == false)
        let env = plist["EnvironmentVariables"] as! [String: String]
        assert(env["APP_ENGINE_HOST"] == "127.0.0.1")
        assert(env["APP_ENGINE_PORT"] == "18770")
        assert(env["APP_ENGINE_PUBLIC_ORIGIN"] == "")
        assert(env["APP_ENGINE_APPS_DIR"] == settings.appsDirectory)
        let store = ServiceConfiguration(kind: .store, settings: settings)
        let storePlist = try PropertyListSerialization.propertyList(from: store.plistData(logDirectory: root), format: nil) as! [String: Any]
        let storeArgs = storePlist["ProgramArguments"] as! [String]
        assert(storeArgs.contains(settings.storeDataDirectory))
        assert(storeArgs.contains("app_store"))
        assert(service.recognizes(Data(#"{"name":"app-engine","version":"0.2.1"}"#.utf8)))
        assert(!service.recognizes(Data(#"{"name":"unrelated"}"#.utf8)))
        assert(store.recognizes(Data(#"{"protocol_version":1,"name":"My Apps","apps":[]}"#.utf8)))
        assert(!store.recognizes(Data(#"{"protocol_version":2,"apps":[]}"#.utf8)))
        assert(!store.recognizes(Data("not JSON".utf8)))
        assert(!ServiceManager.defaultServiceDirectory.path.contains("LaunchAgents"))
        settings.storePort = settings.enginePort
        expectInvalid(settings)
        settings.storePort = 8780
        settings.enginePort = 0
        expectInvalid(settings)
        settings.enginePort = 8770
        settings.engineRepository = root.appendingPathComponent("missing").path
        expectInvalid(settings)
        print("Service configuration tests passed: paths, argv, identity, isolation, ports and missing runtime.")
    }
    static func expectInvalid(_ settings: LauncherSettings) {
        do { try settings.validate(); fatalError("Expected invalid configuration") }
        catch { }
    }
}

import Foundation

enum ServiceKind: String, CaseIterable, Codable {
    case engine, store
    var title: String { self == .engine ? "App Engine" : "App Store" }
    var label: String { "io.appengine.desktop.\(rawValue)" }
}

struct LauncherError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct LauncherSettings: Codable {
    var engineRepository: String
    var storeRepository: String
    var appsDirectory: String
    var stateDirectory: String
    var storeDataDirectory: String
    var enginePort: Int
    var storePort: Int

    static func defaults() -> LauncherSettings {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let engine = Bundle.main.object(forInfoDictionaryKey: "AppEngineRepository") as? String ?? home.appendingPathComponent("git/app-engine").path
        let parent = URL(fileURLWithPath: engine).deletingLastPathComponent()
        return LauncherSettings(engineRepository: engine, storeRepository: parent.appendingPathComponent("app-store").path,
            appsDirectory: parent.appendingPathComponent("personal-apps").path,
            stateDirectory: home.appendingPathComponent(".config/app-engine/app-state").path,
            storeDataDirectory: parent.appendingPathComponent("app-store/store-data").path, enginePort: 8770, storePort: 8780)
    }

    static func load() -> LauncherSettings {
        guard let data = UserDefaults.standard.data(forKey: "launcherSettings"),
              let saved = try? JSONDecoder().decode(LauncherSettings.self, from: data) else { return defaults() }
        return saved
    }
    func save() throws { UserDefaults.standard.set(try JSONEncoder().encode(self), forKey: "launcherSettings") }

    func validate() throws {
        guard (1024...65535).contains(enginePort), (1024...65535).contains(storePort), enginePort != storePort else {
            throw LauncherError(message: "Choose two different ports between 1024 and 65535.")
        }
        for kind in ServiceKind.allCases { try ServiceConfiguration(kind: kind, settings: self).validate() }
    }
}

struct ServiceConfiguration {
    let kind: ServiceKind
    let settings: LauncherSettings
    var repository: String { kind == .engine ? settings.engineRepository : settings.storeRepository }
    var python: String { repository + "/.venv/bin/python" }
    var port: Int { kind == .engine ? settings.enginePort : settings.storePort }
    var url: URL { URL(string: "http://127.0.0.1:\(port)")! }
    var probeURL: URL { url.appendingPathComponent(kind == .engine ? "api/engine" : "api/v1/catalog") }

    func validate() throws {
        let fm = FileManager.default
        guard (1024...65535).contains(port) else { throw LauncherError(message: "Invalid service port.") }
        let entry = kind == .engine ? "engine.py" : "app_store/__main__.py"
        guard repository.hasPrefix("/"), fm.fileExists(atPath: repository + "/" + entry) else {
            throw LauncherError(message: "Choose the \(kind.title) source folder in Settings.")
        }
        guard fm.isExecutableFile(atPath: python) else {
            throw LauncherError(message: "Python environment missing in \(repository)/.venv. Set up this repository with uv first.")
        }
        let paths = kind == .engine ? [settings.appsDirectory, settings.stateDirectory] : [settings.storeDataDirectory]
        guard paths.allSatisfy({ $0.hasPrefix("/") && !$0.contains("\n") && !$0.contains("\0") }) else {
            throw LauncherError(message: "Use absolute folder paths in Settings.")
        }
        if kind == .engine {
            var directory: ObjCBool = false
            guard fm.fileExists(atPath: settings.appsDirectory, isDirectory: &directory), directory.boolValue else {
                throw LauncherError(message: "Choose an existing Apps folder in Settings.")
            }
        }
    }

    func recognizes(_ data: Data) -> Bool {
        guard let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        if kind == .engine { return value["name"] as? String == "app-engine" }
        return value["protocol_version"] as? Int == 1 && value["name"] is String && value["apps"] is [Any]
    }

    func plistData(logDirectory: URL, label: String? = nil) throws -> Data {
        try validate()
        var environment = ["HOME": NSHomeDirectory(), "PATH": repository + "/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"]
        var arguments = [python]
        if kind == .engine {
            arguments += [repository + "/engine.py"]
            environment.merge(["APP_ENGINE_APPS_DIR": settings.appsDirectory, "APP_ENGINE_STATE_DIR": settings.stateDirectory,
                               "APP_ENGINE_HOST": "127.0.0.1", "APP_ENGINE_PORT": String(port), "APP_ENGINE_PUBLIC_ORIGIN": ""]) { _, new in new }
        } else {
            arguments += ["-m", "app_store", "serve", "--data-dir", settings.storeDataDirectory, "--host", "127.0.0.1", "--port", String(port), "--name", "My Apps"]
        }
        let plist: [String: Any] = ["Label": label ?? kind.label, "ProgramArguments": arguments,
            "WorkingDirectory": repository, "EnvironmentVariables": environment, "RunAtLoad": true, "KeepAlive": false,
            "ProcessType": "Background", "ExitTimeOut": 15, "AbandonProcessGroup": false,
            "StandardOutPath": logDirectory.appendingPathComponent(kind.rawValue + ".log").path,
            "StandardErrorPath": logDirectory.appendingPathComponent(kind.rawValue + ".log").path]
        return try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
    }
}

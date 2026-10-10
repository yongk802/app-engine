import Foundation
import Darwin

struct ServiceStatus {
    let running: Bool
    let managed: Bool
    let message: String
}

/// Call these synchronous operations on a background serial queue, never the UI thread.
final class ServiceManager {
    static var defaultServiceDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/App Engine/Services")
    }
    let serviceDirectory: URL
    let logDirectory: URL
    let suffix: String
    private var domain: String { "gui/\(getuid())" }
    init(serviceDirectory: URL = defaultServiceDirectory,
         logDirectory: URL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/App Engine"), suffix: String = "") {
        self.serviceDirectory = serviceDirectory
        self.logDirectory = logDirectory
        self.suffix = suffix
    }
    func label(_ kind: ServiceKind) -> String { kind.label + suffix }
    func logURL(_ kind: ServiceKind) -> URL { logDirectory.appendingPathComponent(kind.rawValue + ".log") }

    @discardableResult
    private func launchctl(_ arguments: [String]) -> (Int32, String) {
        let task = Process()
        let pipe = Pipe()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = arguments
        task.standardOutput = pipe; task.standardError = pipe
        do {
            try task.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            task.waitUntilExit()
            return (task.terminationStatus, String(data: data, encoding: .utf8) ?? "")
        } catch { return (-1, error.localizedDescription) }
    }
    func owns(_ kind: ServiceKind) -> Bool { launchctl(["print", "\(domain)/\(label(kind))"]).0 == 0 }

    private func response(_ configuration: ServiceConfiguration) -> (Int, Data)? {
        let session = URLSession(configuration: .ephemeral)
        let wait = DispatchSemaphore(value: 0)
        var result: (Int, Data)?
        var request = URLRequest(url: configuration.probeURL)
        request.timeoutInterval = 1
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let task = session.dataTask(with: request) { data, response, _ in
            if let response = response as? HTTPURLResponse, let data { result = (response.statusCode, data) }
            wait.signal()
        }
        task.resume()
        let completed = wait.wait(timeout: .now() + 2) == .success
        if !completed { task.cancel() }
        session.invalidateAndCancel()
        return completed ? result : nil
    }

    func portAvailable(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(port).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        return withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0 }
        }
    }

    func status(_ configuration: ServiceConfiguration) -> ServiceStatus {
        let managed = owns(configuration.kind)
        if let (code, data) = response(configuration), code == 200, configuration.recognizes(data) {
            return ServiceStatus(running: true, managed: managed,
                message: managed ? "Running · port \(configuration.port)" : "Running externally · port \(configuration.port)")
        }
        let message = managed ? "Not responding · check Logs or restart" : portAvailable(configuration.port) ? "Stopped" : "Port \(configuration.port) is occupied by another service"
        return ServiceStatus(running: false, managed: managed, message: message)
    }

    func start(_ configuration: ServiceConfiguration) throws -> ServiceStatus {
        try configuration.validate()
        let current = status(configuration)
        if current.running { return current }
        if current.managed { try stop(configuration.kind) }
        guard portAvailable(configuration.port) else { throw LauncherError(message: "Port \(configuration.port) is in use. Choose a different port in Settings; the other process has not been stopped.") }
        let fm = FileManager.default
        try fm.createDirectory(at: serviceDirectory, withIntermediateDirectories: true)
        try fm.createDirectory(at: logDirectory, withIntermediateDirectories: true)
        let state = configuration.kind == .engine ? configuration.settings.stateDirectory : configuration.settings.storeDataDirectory
        try fm.createDirectory(atPath: state, withIntermediateDirectories: true)
        let path = serviceDirectory.appendingPathComponent(label(configuration.kind) + ".plist")
        try configuration.plistData(logDirectory: logDirectory, label: label(configuration.kind)).write(to: path, options: .atomic)
        let launched = launchctl(["bootstrap", domain, path.path])
        guard launched.0 == 0 else { throw LauncherError(message: "Could not start \(configuration.kind.title): \(launched.1.trimmingCharacters(in: .whitespacesAndNewlines))") }
        let deadline = Date().addingTimeInterval(20)
        repeat {
            if let (code, data) = response(configuration), code == 200, configuration.recognizes(data) {
                return ServiceStatus(running: true, managed: true, message: "Running · port \(configuration.port)")
            }
            Thread.sleep(forTimeInterval: 0.25)
        } while Date() < deadline
        // A requested job that never became healthy must not linger unseen.
        try? stop(configuration.kind)
        throw LauncherError(message: "\(configuration.kind.title) did not become ready. Open Logs for details; its launcher job was stopped.")
    }

    func stop(_ kind: ServiceKind) throws {
        guard owns(kind) else { return }
        let stopped = launchctl(["bootout", "\(domain)/\(label(kind))"])
        if stopped.0 != 0 && owns(kind) { throw LauncherError(message: "Could not stop \(kind.title): \(stopped.1)") }
        // bootout can return while launchd is still terminating the process group.
        let deadline = Date().addingTimeInterval(20)
        while owns(kind) && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if owns(kind) { throw LauncherError(message: "\(kind.title) is still stopping. Check its log before trying again.") }
    }
}

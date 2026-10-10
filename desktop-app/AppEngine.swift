import AppKit

final class ServiceCard: NSBox {
    let status = NSTextField(labelWithString: "Checking…")
    let start = NSButton(title: "Start", target: nil, action: nil)
    let stop = NSButton(title: "Stop", target: nil, action: nil)
    let open = NSButton(title: "Open", target: nil, action: nil)
    let logs = NSButton(title: "Logs", target: nil, action: nil)
    init(kind: ServiceKind, index: Int, owner: AppDelegate) {
        super.init(frame: .zero)
        boxType = .custom; borderWidth = 1; cornerRadius = 12
        borderColor = .separatorColor; fillColor = .controlBackgroundColor
        contentViewMargins = NSSize(width: 18, height: 16)
        let heading = NSTextField(labelWithString: kind.title)
        heading.font = .systemFont(ofSize: 19, weight: .semibold)
        let description = NSTextField(labelWithString: kind == .engine ? "Run your apps and connect your chat." : "Browse and share your app catalog.")
        description.textColor = .secondaryLabelColor
        status.font = .systemFont(ofSize: 12, weight: .medium)
        let buttons = NSStackView(views: [start, stop, open, logs])
        buttons.orientation = .horizontal; buttons.spacing = 8
        for button in [start, stop, open, logs] { button.bezelStyle = .rounded; button.tag = index; button.target = owner }
        start.action = #selector(AppDelegate.startService(_:)); stop.action = #selector(AppDelegate.stopService(_:))
        open.action = #selector(AppDelegate.openService(_:)); logs.action = #selector(AppDelegate.openLogs(_:))
        start.setAccessibilityLabel("Start \(kind.title)"); stop.setAccessibilityLabel("Stop \(kind.title)")
        open.setAccessibilityLabel("Open \(kind.title)"); logs.setAccessibilityLabel("\(kind.title) Logs")
        let stack = NSStackView(views: [heading, description, status, buttons])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        contentView!.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: contentView!.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: contentView!.trailingAnchor), stack.topAnchor.constraint(equalTo: contentView!.topAnchor),
            stack.bottomAnchor.constraint(equalTo: contentView!.bottomAnchor)])
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unsupported") }
    func update(_ value: ServiceStatus, busy: Bool) {
        status.stringValue = value.message
        status.textColor = value.running ? .systemGreen : .secondaryLabelColor
        start.isEnabled = !busy && !value.running
        stop.isEnabled = !busy && value.managed
        open.isEnabled = !busy
        logs.isEnabled = !busy
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var cards: [ServiceCard] = []
    var settings = LauncherSettings.load()
    let manager = ServiceManager()
    let queue = DispatchQueue(label: "io.appengine.desktop.services", qos: .utility)
    var busy = false
    var polling = false
    var timer: Timer?
    var settingsButton: NSButton!
    var statuses = ServiceKind.allCases.map { _ in ServiceStatus(running: false, managed: false, message: "Checking…") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let item = NSMenuItem(); menu.addItem(item)
        let appMenu = NSMenu(); item.submenu = appMenu
        appMenu.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: ",").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit App Engine", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 500, height: 550),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "App Engine"; window.isReleasedWhenClosed = false
        let title = NSTextField(labelWithString: "Your apps, one place.")
        title.font = .systemFont(ofSize: 25, weight: .bold)
        let subtitle = NSTextField(labelWithString: "Start a service, then open it in your browser.")
        subtitle.textColor = .secondaryLabelColor
        cards = ServiceKind.allCases.enumerated().map { ServiceCard(kind: $0.element, index: $0.offset, owner: self) }
        settingsButton = NSButton(title: "Settings…", target: self, action: #selector(showSettings))
        settingsButton.bezelStyle = .rounded
        let footer = NSTextField(wrappingLabelWithString: "Services start only when requested. Closing this window keeps running services available until you stop them or log out.")
        footer.font = .systemFont(ofSize: 11); footer.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [title, subtitle] + cards + [settingsButton, footer])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24)])
        for card in cards { card.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true }
        footer.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refresh() }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); return true
    }
    func render() {
        for (index, card) in cards.enumerated() { card.update(statuses[index], busy: busy) }
        settingsButton.isEnabled = !busy
    }
    func refresh() {
        guard !busy && !polling else { return }
        polling = true
        let snapshot = settings
        queue.async {
            let values = ServiceKind.allCases.map { self.manager.status(ServiceConfiguration(kind: $0, settings: snapshot)) }
            DispatchQueue.main.async {
                self.polling = false
                if !self.busy { self.statuses = values; self.render() }
            }
        }
    }
    func operate(_ sender: NSButton, stop: Bool = false, open: Bool = false) {
        guard !busy else { return }
        let kind = ServiceKind.allCases[sender.tag]
        let config = ServiceConfiguration(kind: kind, settings: settings)
        busy = true
        statuses[sender.tag] = ServiceStatus(running: false, managed: statuses[sender.tag].managed, message: stop ? "Stopping…" : "Starting…")
        render()
        queue.async {
            var failure: Error?
            do { if stop { try self.manager.stop(kind) } else { _ = try self.manager.start(config) } }
            catch { failure = error }
            let result = self.manager.status(config)
            let error = failure
            DispatchQueue.main.async {
                self.statuses[sender.tag] = result; self.busy = false; self.render()
                if let error { self.showError(error) }
                else if open { NSWorkspace.shared.open(config.url) }
            }
        }
    }
    @objc func startService(_ sender: NSButton) { operate(sender) }
    @objc func stopService(_ sender: NSButton) { operate(sender, stop: true) }
    @objc func openService(_ sender: NSButton) { operate(sender, open: true) }
    @objc func openLogs(_ sender: NSButton) {
        let url = manager.logURL(ServiceKind.allCases[sender.tag])
        if FileManager.default.fileExists(atPath: url.path) { NSWorkspace.shared.open(url) }
        else { showError(LauncherError(message: "No log yet. Start this service to create its log.")) }
    }
    func showError(_ error: Error) {
        let alert = NSAlert(); alert.messageText = "App Engine"; alert.informativeText = error.localizedDescription
        alert.alertStyle = .warning; alert.runModal()
    }
    @objc func showSettings() {
        guard !busy else { return }
        busy = true; render()
        queue.async {
            let managed = ServiceKind.allCases.contains { self.manager.owns($0) }
            DispatchQueue.main.async {
                if managed {
                    self.busy = false; self.render()
                    self.showError(LauncherError(message: "Stop both launcher-managed services before changing Settings."))
                } else { self.editSettings() }
            }
        }
    }
    private func editSettings() {
        // Hold off polling while the modal editor runs.
        defer { busy = false; render(); refresh() }
        let alert = NSAlert(); alert.messageText = "Launcher Settings"
        alert.informativeText = "Choose local source folders with existing uv Python environments. Servers listen on this Mac only."
        alert.addButton(withTitle: "Save"); alert.addButton(withTitle: "Cancel")
        let labels = ["App Engine folder", "App Store folder", "Apps folder", "App state folder", "Store data folder", "App Engine port", "App Store port"]
        let values = [settings.engineRepository, settings.storeRepository, settings.appsDirectory, settings.stateDirectory,
                      settings.storeDataDirectory, String(settings.enginePort), String(settings.storePort)]
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 600, height: 294))
        var fields: [NSTextField] = []
        for (index, label) in labels.enumerated() {
            let y = 260 - index * 42
            let text = NSTextField(labelWithString: label); text.frame = NSRect(x: 0, y: y + 4, width: 135, height: 20)
            let field = NSTextField(string: values[index]); field.frame = NSRect(x: 140, y: y, width: 460, height: 26)
            field.setAccessibilityLabel(label); field.lineBreakMode = .byTruncatingMiddle
            view.addSubview(text); view.addSubview(field); fields.append(field)
        }
        alert.accessoryView = view
        while alert.runModal() == .alertFirstButtonReturn {
            let input = fields.map { $0.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) }
            let candidate = LauncherSettings(engineRepository: input[0], storeRepository: input[1], appsDirectory: input[2],
                stateDirectory: input[3], storeDataDirectory: input[4], enginePort: Int(input[5]) ?? 0, storePort: Int(input[6]) ?? 0)
            do { try candidate.validate(); try candidate.save(); settings = candidate; break }
            catch { showError(error) }
        }
    }
}

@main
struct AppEngineLauncher {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.setActivationPolicy(.regular); app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}

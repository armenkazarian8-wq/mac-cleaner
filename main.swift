import Cocoa
import WebKit

/// A transparent strip over the title bar area so the window can be dragged (the web view would swallow the clicks).
final class DragStrip: NSView {
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { window?.zoom(nil) } else { window?.performDrag(with: event) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var engine: Process?
    var buffer = ""

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
        buildMenu()

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1160, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Mac Cleaner"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.minSize = NSSize(width: 980, height: 660)
        window.backgroundColor = NSColor(red: 0.09, green: 0.05, blue: 0.16, alpha: 1)
        window.setFrameAutosaveName("MainWindow")

        web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.uiDelegate = self
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        web.translatesAutoresizingMaskIntoConstraints = false

        let strip = DragStrip()
        strip.translatesAutoresizingMaskIntoConstraints = false
        let root = NSView()
        root.addSubview(web)
        root.addSubview(strip)
        NSLayoutConstraint.activate([
            web.leadingAnchor.constraint(equalTo: root.leadingAnchor), web.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            web.topAnchor.constraint(equalTo: root.topAnchor), web.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            strip.leadingAnchor.constraint(equalTo: root.leadingAnchor), strip.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            strip.topAnchor.constraint(equalTo: root.topAnchor), strip.heightAnchor.constraint(equalToConstant: 38),
        ])
        window.contentView = root
        if !window.setFrameUsingName("MainWindow") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        web.loadHTMLString(loadingPage("Starting…"), baseURL: nil)
        startEngine()
    }

    /// The engine runs on the Python 3 that ships with Apple's Command Line Tools (or Xcode).
    func hasPython() -> Bool {
        let check = Process()
        check.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
        check.arguments = ["-p"]
        check.standardOutput = FileHandle.nullDevice
        check.standardError = FileHandle.nullDevice
        do { try check.run(); check.waitUntilExit() } catch { return false }
        return check.terminationStatus == 0
    }

    func startEngine() {
        if !hasPython() {
            let install = Process()
            install.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
            install.arguments = ["--install"]
            try? install.run()
            return fail("Mac Cleaner needs Apple's free <b>Command Line Tools</b>.<br><br>An installer window should have opened — " +
                        "click <b>Install</b>, wait for it to finish, then open Mac Cleaner again.")
        }
        guard let script = Bundle.main.path(forResource: "cleaner", ofType: "py") else { return fail("cleaner.py is missing from the app.") }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = ["-u", script, "--app"]
        let out = Pipe()
        p.standardOutput = out
        let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/MacCleaner.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        p.standardError = try? FileHandle(forWritingTo: logURL)
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let chunk = String(data: h.availableData, encoding: .utf8) ?? ""
            DispatchQueue.main.async { self?.gotOutput(chunk, handle: h) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                if proc.terminationReason != .exit || proc.terminationStatus != 0 {
                    self?.fail("The cleaner engine stopped unexpectedly. Details are in ~/Library/Logs/MacCleaner.log")
                }
            }
        }
        do { try p.run(); engine = p } catch { fail("Couldn't start Python: \(error.localizedDescription)") }
    }

    func gotOutput(_ chunk: String, handle: FileHandle) {
        buffer += chunk
        guard let line = buffer.split(separator: "\n").first(where: { $0.hasPrefix("PORT ") }),
              let port = Int(line.dropFirst(5).trimmingCharacters(in: .whitespaces)) else { return }
        handle.readabilityHandler = nil
        web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!))
    }

    func fail(_ msg: String) { web.loadHTMLString(loadingPage(msg), baseURL: nil) }

    func loadingPage(_ msg: String) -> String {
        "<html><body style='margin:0;height:100vh;display:grid;place-items:center;background:linear-gradient(160deg,#2a1150,#0d0718);" +
        "color:#fff;font:15px -apple-system'><div style='text-align:center;max-width:420px'><div style='font-size:28px;font-weight:700;" +
        "margin-bottom:10px'>Mac Cleaner</div>\(msg)</div></body></html>"
    }

    // Links that leave the app (none expected) open in the default browser.
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, action.navigationType == .linkActivated, url.host != "127.0.0.1" {
            NSWorkspace.shared.open(url); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.runModal(); completionHandler()
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ note: Notification) { engine?.terminate() }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Mac Cleaner", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Mac Cleaner", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Mac Cleaner", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

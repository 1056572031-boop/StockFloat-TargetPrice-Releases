import AppKit
import AVFoundation

// This probe only queries/requests TCC authorization. It never creates a capture session.
final class CameraAuthProbe: NSObject, NSApplicationDelegate {
    private var window: NSWindow?

    func record(_ status: String) {
        let environment = ProcessInfo.processInfo.environment
        guard let output = environment["SF_CAMERA_PROBE_RESULT"] else {
            fatalError("Missing isolated result path")
        }
        #if UPGRADE_RELEASE
        let variant = "changed-executable"
        #else
        let variant = "baseline-executable"
        #endif
        let report = ["version": Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion")!,
                      "status": status, "variant": variant]
        let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
        try! data.write(to: URL(fileURLWithPath: output), options: [.atomic])
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let status = AVCaptureDevice.authorizationStatus(for: .video)
        if status == .authorized {
            record("authorized_without_request")
            NSApp.terminate(nil)
            return
        }
        if status != .notDetermined {
            record(status == .denied ? "denied" : "restricted")
            NSApp.terminate(nil)
            return
        }
        record("undetermined")
        // Upgrade/restart must succeed without calling requestAccess at all.
        if ProcessInfo.processInfo.environment["SF_CAMERA_PROBE_ALLOW_REQUEST"] != "1" {
            NSApp.terminate(nil)
            return
        }
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 380, height: 140),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window!.title = "Skyrocket Camera Authorization Test"
        let button = NSButton(title: "Authorize", target: self, action: #selector(authorize))
        button.frame = NSRect(x: 110, y: 45, width: 160, height: 40)
        window!.contentView!.addSubview(button)
        window!.center()
        window!.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func authorize() {
        AVCaptureDevice.requestAccess(for: .video) { granted in
            DispatchQueue.main.async {
                self.record(granted ? "granted_by_user" : "denied_by_user")
                NSApp.terminate(nil)
            }
        }
    }
}

let delegate = CameraAuthProbe()
NSApplication.shared.delegate = delegate
NSApplication.shared.setActivationPolicy(.regular)
NSApplication.shared.run()

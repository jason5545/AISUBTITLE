import AppKit

private final class DraggableVisualEffectView: NSVisualEffectView {
    private var dragStartLocation: NSPoint?

    override func mouseDown(with event: NSEvent) {
        dragStartLocation = event.locationInWindow
    }

    override func mouseDragged(with event: NSEvent) {
        guard let window, let dragStartLocation else {
            return
        }

        let currentScreenLocation = NSEvent.mouseLocation
        let newOrigin = NSPoint(
            x: currentScreenLocation.x - dragStartLocation.x,
            y: currentScreenLocation.y - dragStartLocation.y
        )
        window.setFrameOrigin(newOrigin)
    }
}

final class OverlayWindowController {
    private let panel: NSPanel
    private let statusLabel = NSTextField(labelWithString: "Starting")
    private let textLabel = NSTextField(labelWithString: "")
    private let sourceLabel = NSTextField(labelWithString: "")

    init() {
        let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        let width = min(max(screenFrame.width * 0.42, 420), 680)
        let height: CGFloat = 88
        let frame = NSRect(
            x: screenFrame.midX - width / 2,
            y: screenFrame.maxY - height - 28,
            width: width,
            height: height
        )

        panel = NSPanel(
            contentRect: frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .screenSaver
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.ignoresMouseEvents = false
        panel.isMovableByWindowBackground = true

        let container = DraggableVisualEffectView(frame: NSRect(origin: .zero, size: frame.size))
        container.material = .hudWindow
        container.blendingMode = .withinWindow
        container.state = .active
        container.wantsLayer = true
        container.layer?.cornerRadius = 14
        container.layer?.masksToBounds = true

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.translatesAutoresizingMaskIntoConstraints = false

        statusLabel.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        statusLabel.textColor = NSColor.white.withAlphaComponent(0.7)
        statusLabel.lineBreakMode = .byTruncatingTail

        textLabel.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        textLabel.textColor = .white
        textLabel.maximumNumberOfLines = 2
        textLabel.lineBreakMode = .byWordWrapping
        textLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        sourceLabel.font = NSFont.systemFont(ofSize: 10, weight: .regular)
        sourceLabel.textColor = NSColor.white.withAlphaComponent(0.55)
        sourceLabel.lineBreakMode = .byTruncatingTail

        stack.addArrangedSubview(statusLabel)
        stack.addArrangedSubview(textLabel)
        stack.addArrangedSubview(sourceLabel)
        container.addSubview(stack)
        panel.contentView = container

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -16),
            stack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            textLabel.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
    }

    func show() {
        DispatchQueue.main.async {
            self.panel.orderFrontRegardless()
        }
    }

    func showStatus(_ status: String) {
        DispatchQueue.main.async {
            self.statusLabel.stringValue = status
            self.panel.orderFrontRegardless()
        }
    }

    func showSubtitle(_ text: String, source: String?, usage: String?) {
        DispatchQueue.main.async {
            self.statusLabel.stringValue = usage.map { "Translated · \($0)" } ?? "Translated"
            self.textLabel.stringValue = text
            self.sourceLabel.stringValue = source.map { "source: \($0)" } ?? ""
            self.panel.orderFrontRegardless()
        }
    }
}

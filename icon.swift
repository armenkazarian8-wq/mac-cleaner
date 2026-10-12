import Cocoa

// Draws the 1024×1024 app icon: a glossy purple-pink squircle with a white sparkle.
let size: CGFloat = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

let inset: CGFloat = 100
let rect = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
let shape = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)

let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
shadow.shadowOffset = NSSize(width: 0, height: -18)
shadow.shadowBlurRadius = 40
NSGraphicsContext.saveGraphicsState()
shadow.set()
NSColor.black.setFill()
shape.fill()
NSGraphicsContext.restoreGraphicsState()

NSGradient(colors: [NSColor(red: 0.43, green: 0.16, blue: 0.85, alpha: 1),
                    NSColor(red: 0.86, green: 0.15, blue: 0.47, alpha: 1)])!.draw(in: shape, angle: -60)
NSGradient(colors: [NSColor.white.withAlphaComponent(0.28), NSColor.white.withAlphaComponent(0)])!
    .draw(in: NSBezierPath(roundedRect: NSRect(x: inset, y: size / 2, width: rect.width, height: rect.height / 2),
                           xRadius: 185, yRadius: 185), angle: -90)

// Sparkle: a four-point star plus a small one.
func star(cx: CGFloat, cy: CGFloat, r: CGFloat) -> NSBezierPath {
    let p = NSBezierPath()
    let k: CGFloat = 0.22
    p.move(to: NSPoint(x: cx, y: cy + r))
    p.curve(to: NSPoint(x: cx + r, y: cy), controlPoint1: NSPoint(x: cx + r * k, y: cy + r * k), controlPoint2: NSPoint(x: cx + r * k, y: cy + r * k))
    p.curve(to: NSPoint(x: cx, y: cy - r), controlPoint1: NSPoint(x: cx + r * k, y: cy - r * k), controlPoint2: NSPoint(x: cx + r * k, y: cy - r * k))
    p.curve(to: NSPoint(x: cx - r, y: cy), controlPoint1: NSPoint(x: cx - r * k, y: cy - r * k), controlPoint2: NSPoint(x: cx - r * k, y: cy - r * k))
    p.curve(to: NSPoint(x: cx, y: cy + r), controlPoint1: NSPoint(x: cx - r * k, y: cy + r * k), controlPoint2: NSPoint(x: cx - r * k, y: cy + r * k))
    return p
}
let glow = NSShadow()
glow.shadowColor = NSColor.white.withAlphaComponent(0.6)
glow.shadowBlurRadius = 40
glow.set()
NSColor.white.setFill()
star(cx: 480, cy: 480, r: 250).fill()
star(cx: 700, cy: 700, r: 95).fill()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))

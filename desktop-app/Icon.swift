import AppKit
let destination = CommandLine.arguments[1]
let size = 1024
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSColor(calibratedRed: 0.12, green: 0.19, blue: 0.36, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 64, y: 64, width: 896, height: 896), xRadius: 200, yRadius: 200).fill()
let colors: [NSColor] = [.systemTeal, .systemBlue, .systemOrange, .systemPurple]
for row in 0..<2 { for column in 0..<2 {
    colors[row * 2 + column].setFill()
    NSBezierPath(roundedRect: NSRect(x: 238 + column * 294, y: 238 + row * 294, width: 254, height: 254), xRadius: 58, yRadius: 58).fill()
} }
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: destination))

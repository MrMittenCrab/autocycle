// ScreenCaptureKit stays behind this JSON command boundary.
import Foundation
import AppKit
import ScreenCaptureKit
import ImageIO
import UniformTypeIdentifiers
import ApplicationServices
import Vision

struct Request: Decodable {
    let pid: Int32
    let bundle_id: String
    let title: String
    let frame: [Double] // x, y, width, height in screen points
    let output: String
    var valid: Bool {
        pid > 0 && !bundle_id.isEmpty && !title.isEmpty &&
        frame.count == 4 && frame.allSatisfy { $0.isFinite } &&
        frame[2] >= 200 && frame[3] >= 150 && output.hasPrefix("/")
    }
}
struct WindowMetadata {
    let id: UInt32
    let pid: Int32
    let bundle: String
    let title: String
    let frame: [Double]
}
func matches(_ window: WindowMetadata, _ request: Request) -> Bool {
    window.pid == request.pid && window.bundle == request.bundle_id &&
    window.title == request.title && window.frame == request.frame
}
func emit(_ result: [String: Any]) {
    do {
        let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    } catch {
        FileHandle.standardOutput.write(Data("{\"status\":\"capture_failure\",\"message\":\"result encoding failed\"}\n".utf8))
    }
}
// Only the document canvas is eligible for page evidence. Ribbon, status-bar,
// navigation/search panes and selection/page labels are not rendered page proof.
func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}
func axFrame(_ element: AXUIElement) -> CGRect? {
    guard let p = attribute(element, kAXPositionAttribute),
          let s = attribute(element, kAXSizeAttribute),
          CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero; var size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point),
          AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}
func wordViewport(_ request: Request) throws -> CGRect {
    let failure = NSError(domain: "Word document viewport unavailable or ambiguous", code: 1)
    guard AXIsProcessTrusted(),
          let windows = attribute(AXUIElementCreateApplication(request.pid), kAXWindowsAttribute) as? [AXUIElement] else { throw failure }
    let frame = CGRect(x: request.frame[0], y: request.frame[1], width: request.frame[2], height: request.frame[3])
    let owned = windows.filter {
        (attribute($0, kAXTitleAttribute) as? String) == request.title && axFrame($0) == frame
    }
    guard owned.count == 1 else { throw failure }
    var areas: [CGRect] = []; var remaining = 1000
    func visit(_ element: AXUIElement, _ depth: Int) {
        guard remaining > 0, depth < 24 else { return }; remaining -= 1
        let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
        if (attribute(element, kAXRoleAttribute) as? String) == kAXScrollAreaRole,
           children.contains(where: { (attribute($0, kAXRoleAttribute) as? String) == "AXLayoutArea" }),
           let rect = axFrame(element), frame.contains(rect), rect.width >= 200, rect.height >= 150 {
            areas.append(rect)
        }
        for child in children { visit(child, depth + 1) }
    }
    visit(owned[0], 0)
    guard remaining > 0, areas.count == 1 else { throw failure }
    return areas[0]
}
func renderedWordText(_ image: CGImage, _ viewport: CGRect, _ request: Request) throws -> [String: Any] {
    let sx = Double(image.width) / request.frame[2], sy = Double(image.height) / request.frame[3]
    let rect = CGRect(x: (viewport.minX-request.frame[0])*sx, y: (viewport.minY-request.frame[1])*sy,
                      width: viewport.width*sx, height: viewport.height*sy).integral
    guard let crop = image.cropping(to: rect) else { throw NSError(domain: "Invalid Word viewport crop", code: 1) }
    let recognize = VNRecognizeTextRequest()
    recognize.recognitionLevel = .accurate
    recognize.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: crop).perform([recognize])
    let lines = (recognize.results ?? []).compactMap { observation -> [String: Any]? in
        guard let candidate = observation.topCandidates(1).first, candidate.confidence >= 0.8 else { return nil }
        let b = observation.boundingBox
        return ["text": candidate.string, "confidence": candidate.confidence,
                "box": [b.minX, b.minY, b.width, b.height]]
    }
    return ["method": "vision-document-canvas", "viewport": [viewport.minX, viewport.minY, viewport.width, viewport.height],
            "crop_pixels": [rect.minX, rect.minY, rect.width, rect.height], "lines": lines]
}
#if !CAPTURE_SELECTION_TEST
@main struct OfficeCapture {
    static func main() async {
        // A CLI must initialize its WindowServer connection before screenshot APIs.
        // This creates no windows and starts no persistent application loop.
        _ = NSApplication.shared
        let request: Request
        do {
            request = try JSONDecoder().decode(Request.self, from: FileHandle.standardInput.readDataToEndOfFile())
            guard request.valid else { throw NSError(domain: "Invalid request", code: 1) }
        } catch {
            emit(["status": "invalid_request", "message": String(describing: error)])
            return
        }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
            let candidates = content.windows.filter { window in
                guard let app = window.owningApplication else { return false }
                return matches(WindowMetadata(id: window.windowID, pid: app.processID,
                    bundle: app.bundleIdentifier, title: window.title ?? "",
                    frame: [window.frame.origin.x, window.frame.origin.y,
                            window.frame.width, window.frame.height]), request)
            }
            guard candidates.count == 1 else {
                emit(["status": candidates.isEmpty ? "no_window" : "ambiguous",
                      "message": "Expected exactly one native window", "matches": candidates.count])
                return
            }
            let window = candidates[0]
            let filter = SCContentFilter(desktopIndependentWindow: window)
            let config = SCStreamConfiguration()
            let scale = Double(filter.pointPixelScale)
            config.width = Int(ceil(filter.contentRect.width * scale))
            config.height = Int(ceil(filter.contentRect.height * scale))
            config.showsCursor = false
            config.ignoreShadowsSingleWindow = true
            guard config.width >= 200 && config.height >= 150 && config.width * config.height <= 40_000_000 else {
                emit(["status": "capture_failure", "message": "Invalid target dimensions"])
                return
            }
            let viewport = request.bundle_id == "com.microsoft.Word" ? try wordViewport(request) : nil
            let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
            var wordEvidence: [String: Any]? = nil
            if let viewport = viewport {
                guard try wordViewport(request) == viewport else { throw NSError(domain: "Word viewport changed during capture", code: 1) }
                wordEvidence = try renderedWordText(image, viewport, request)
            }
            let url = URL(fileURLWithPath: request.output)
            guard let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
                throw NSError(domain: "Cannot create PNG destination", code: 1)
            }
            CGImageDestinationAddImage(destination, image, nil)
            guard CGImageDestinationFinalize(destination) else {
                throw NSError(domain: "Cannot write PNG", code: 1)
            }
            var result: [String: Any] = ["status": "success", "window_id": window.windowID,
                  "pid": window.owningApplication!.processID,
                  "bundle_id": window.owningApplication!.bundleIdentifier,
                  "application": window.owningApplication!.applicationName,
                  "title": window.title ?? "", "frame": request.frame,
                  "width": image.width, "height": image.height, "output": request.output]
            if let wordEvidence = wordEvidence { result["word_rendered"] = wordEvidence }
            emit(result)
        } catch {
            let native = error as NSError
            let denied = native.domain == SCStreamErrorDomain && native.code == -3801
            emit(["status": denied ? "permission_failure" : "capture_failure",
                  "message": native.localizedDescription, "error_domain": native.domain,
                  "error_code": native.code])
        }
    }
}
#endif

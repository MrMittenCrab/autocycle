"""Run the actual native OCR crop against a synthetic bitmap; no Office or screen access."""
from pathlib import Path
import subprocess
import tempfile


def main():
    root=Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as d:
        d=Path(d); main=d/'main.swift'; binary=d/'word-ocr-test'
        main.write_text('''
import Foundation
import CoreGraphics
import CoreText
let context = CGContext(data: nil, width: 800, height: 600, bitsPerComponent: 8,
    bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
context.setFillColor(CGColor(gray: 1, alpha: 1)); context.fill(CGRect(x: 0,y: 0,width: 800,height: 600))
func draw(_ text: String, _ y: Double) {
    let font = CTFontCreateWithName("Helvetica" as CFString, 26, nil)
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: [
        NSAttributedString.Key(kCTFontAttributeName as String): font,
        NSAttributedString.Key(kCTForegroundColorAttributeName as String): CGColor(gray: 0, alpha: 1)]))
    context.textPosition = CGPoint(x: 40, y: 600-y)
    CTLineDraw(line,context)
}
draw("TOOLBAR FALSE PAGE ONE",60)
draw("CANVAS UNIQUE PAGE THREE",300)
draw("STATUS FALSE PAGE SIX",570)
let image = context.makeImage()!
let request = Request(pid: 12,bundle_id: "com.microsoft.Word",title: "owned",frame: [0,0,800,600],output: "/tmp/unused.png")
let result = try renderedWordText(image,CGRect(x: 0,y: 200,width: 800,height: 300),request)
let lines = result["lines"] as! [[String: Any]]
let text = lines.map { $0["text"] as! String }.joined(separator: " ")
precondition(text.contains("CANVAS UNIQUE PAGE THREE"),text)
precondition(!text.contains("TOOLBAR") && !text.contains("STATUS"),text)
print("PASS native OCR observes document crop and excludes toolbar/status text")
''')
        subprocess.run(['xcrun','swiftc','-D','CAPTURE_SELECTION_TEST',str(root/'office_capture.swift'),str(main),'-o',str(binary)],check=True)
        subprocess.run([str(binary)],check=True)

if __name__=='__main__':main()

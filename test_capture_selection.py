"""Exercise actual Swift matching code without Screen Recording or Office."""
from pathlib import Path
import subprocess,tempfile

def main():
 root=Path(__file__).resolve().parent
 with tempfile.TemporaryDirectory() as d:
  d=Path(d);main=d/'main.swift';binary=d/'selection-test'
  main.write_text('''
import Foundation
let r = Request(pid: 12, bundle_id: "com.microsoft.Excel", title: "excel-view", frame: [40,60,1200,800], output: "/tmp/view.png")
let good = WindowMetadata(id: 1, pid: 12, bundle: "com.microsoft.Excel", title: "excel-view", frame: [40,60,1200,800])
let otherProcess = WindowMetadata(id: 2, pid: 13, bundle: "com.microsoft.Excel", title: "excel-view", frame: [40,60,1200,800])
let otherApp = WindowMetadata(id: 3, pid: 12, bundle: "other", title: "excel-view", frame: [40,60,1200,800])
let otherTitle = WindowMetadata(id: 4, pid: 12, bundle: "com.microsoft.Excel", title: "different", frame: [40,60,1200,800])
let otherFrame = WindowMetadata(id: 5, pid: 12, bundle: "com.microsoft.Excel", title: "excel-view", frame: [41,60,1200,800])
precondition(r.valid)
precondition([good,otherProcess,otherApp,otherTitle,otherFrame].filter { matches($0,r) }.map { $0.id } == [1])
precondition([otherProcess,otherTitle].filter { matches($0,r) }.isEmpty)
precondition([good,good].filter { matches($0,r) }.count == 2)
print("PASS exact native selection: unique, zero, ambiguous and mismatched process/app/title/frame")
''')
  subprocess.run(['xcrun','swiftc','-D','CAPTURE_SELECTION_TEST',str(root/'office_capture.swift'),str(main),'-o',str(binary)],check=True)
  subprocess.run([str(binary)],check=True)
if __name__=='__main__':main()

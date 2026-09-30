#!/usr/bin/env python3
"""Run all isolated regression scripts with fake providers, never a live project."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import subprocess,sys
root=Path(__file__).resolve().parent
for name in ('autocycle','stage','sync','checkpoint'):
    subprocess.run(['/bin/bash','-n',str(root/name)],check=True)

def run(path):
    return subprocess.run([sys.executable,str(path)],cwd=root,capture_output=True,text=True)

failed=[]
with ThreadPoolExecutor(max_workers=4) as pool:
    jobs={pool.submit(run,path):path.name for path in sorted(root.glob('test_*.py'))}
    for job in as_completed(jobs):
        result=job.result();name=jobs[job]
        print(('FAIL ' if result.returncode else 'PASS ')+name,flush=True)
        print(result.stdout+result.stderr,flush=True)
        if result.returncode:failed.append(name)
subprocess.run(['node',str(root/'test_response_parser.js')],check=True)
if failed:sys.exit('Failed suites: '+', '.join(failed))
print('All AutoCycle tests passed.',flush=True)

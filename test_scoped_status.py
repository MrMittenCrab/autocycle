"""Append-only logical stage output, with real provider retry/control flow."""
import json, subprocess, tempfile
from pathlib import Path
from test_flow import Case, ok, fail, BASE
from test_network_resume import fixture, ERROR

def test_cycle_and_blocker():
 c=Case()
 try:
  ok(c.run('1'))
  r=c.run('--extend','1',REVIEW_STATUS='BLOCKED');fail(r)
  assert not any(x.startswith('            Step ') for x in r.stdout.splitlines()),r.stdout
  assert '    ✓ Found   Blocked:' in r.stdout and 'Action: provide required human decision' in r.stdout,r.stdout
  assert 'BLOCKED     ' not in r.stdout and 'confirmed by Review' not in r.stdout,r.stdout
  assert sum('✓ Found   ' in x for x in r.stdout.splitlines())==1,r.stdout
 finally:c.close()

def test_retry_output():
 c=Case()
 try:
  fixture(c);r=c.run('1',TRANSPORT_FAILURES='1',TRANSPORT_ERROR=ERROR);ok(r)
  lines=r.stdout.splitlines()
  assert sum(x.startswith('Implement ') for x in lines)==1,r.stdout
  assert r.stdout.count('✓ Task 0')==1,r.stdout
  assert any(x.startswith('Network     ✓ ') for x in lines),r.stdout
  assert lines.index('    ✓ Task 0') < next(i for i,x in enumerate(lines) if x.startswith('Network     ')),r.stdout
 finally:c.close()

def test_shared_display_timer_and_tasks():
 with tempfile.TemporaryDirectory() as d:
  script=r'''
const { StageDisplay } = require(process.argv[1]);
const file=process.argv[2];
let now=100000;let output='';
const emit=s=>output+=s;
for (const label of ['Review','Plan','Implement']) {
 const path=file+label;
 const first=new StageDisplay(path,label,()=>now,emit);
 first.tasks([{id:'a',content:'Inspect evidence',status:'completed'}]);
 now+=12000;first.event('↻ retry 1/2');
 const second=new StageDisplay(path,label,()=>now,emit);
 second.tasks([{id:'new-id',content:'Inspect evidence',status:'in_progress'}, {id:'b',content:'Verify copy',status:'completed'}]);
 now+=5000;second.finish('✓');
 if(second.elapsed()!=='00:17') throw Error('timer restarted');
}
process.stdout.write(output);
'''
  r=subprocess.run(['node','-e',script,str(BASE/'stage_display.js'),str(Path(d)/'display')],capture_output=True,text=True)
  assert r.returncode==0,r.stderr
  for label in ('Review','Plan','Implement'):assert r.stdout.count(label+' ')==1,r.stdout
  assert r.stdout.count('✓ Inspect evidence')==3,r.stdout
  assert '▶ Inspect evidence' not in r.stdout,r.stdout
  assert r.stdout.count('    ✓ 00:17')==2
  assert 'Implement   ✓ 00:17' in r.stdout,r.stdout

def test_controller_network_retries_share_review_and_plan_blocks():
 for kind in ('review','plan'):
  c=Case()
  try:
   codex=c.bin/'codex';source=codex.read_text()
   source=source.replace("event(kind,prompt)", """event(kind,prompt)
if kind==os.environ.get('FAIL_ONCE_KIND') and not (a/'failed-once').exists():
 (a/'failed-once').touch();(a/'network-down').touch();print('Error: ECONNRESET',file=sys.stderr);sys.exit(1)
""")
   codex.write_text(source)
   controller=c.bin/'autocycle';source=controller.read_text()
   source=source.replace('network_ready() { return 0; }', 'network_ready() { [[ ! -f "'+str(c.a/'network-down')+'" ]]; }')
   source=source.replace('network_recover() { return 0; }', 'network_recover() { rm -f "'+str(c.a/'network-down')+'"; return 0; }')
   controller.write_text(source)
   result=c.run('1',FAIL_ONCE_KIND=kind);ok(result)
   assert sum(line.startswith(kind.title()+'      ') for line in result.stdout.splitlines())==1,result.stdout
   assert '\n\nNetwork     … 00:00\n\n' in result.stdout,result.stdout
   assert 'Network     ✓ ' in result.stdout,result.stdout
   assert kind.title()+'      ✗' not in result.stdout,result.stdout
  finally:c.close()

def test_blocked_retry_reports_cumulative_time():
 import re
 c=Case()
 try:
  fixture(c)
  result=c.run('1',TRANSPORT_FAILURES='1',TRANSPORT_ERROR=ERROR,IMPLEMENT_BLOCKED='1');ok(result)
  assert len(re.findall(r'^Implement +',result.stdout,re.M))==1,result.stdout
  elapsed=re.search(r'^    ↪ Implement active (\d+):(\d+)$',result.stdout,re.M)
  assert elapsed,result.stdout
  assert 'Network     ✓ 00:02' in result.stdout,result.stdout
 finally:c.close()

def test_retry_exhaustion_reports_cumulative_time():
 import re
 c=Case()
 try:
  fixture(c)
  result=c.run('1',TRANSPORT_FAILURES='9',TRANSPORT_ERROR=ERROR);fail(result)
  assert len(re.findall(r'^Implement +',result.stdout,re.M))==1,result.stdout
  elapsed=re.search(r'^    – Implement active (\d+):(\d+)$',result.stdout,re.M)
  assert elapsed,result.stdout
  assert 'Network     ✓ 00:02' in result.stdout and 'Network     ✓ 00:04' in result.stdout,result.stdout
  assert 'Network     ✗ 00:00  retry exhausted' in result.stdout,result.stdout
 finally:c.close()

if __name__=='__main__':
 for name in [n for n in globals() if n.startswith('test_')]:globals()[name]();print('PASS '+name)

"""Real AutoCycle checkpoint/review flow with native Excel execution mocked."""
from test_flow import Case, ok, fail
import json

def test_excel_continuation_and_blocker():
 for failure in (False, True):
  c=Case()
  try:
   helper=c.p/'exercise_excel.py'
   helper.write_text('''from pathlib import Path
import subprocess,json,sys
from unittest.mock import patch
import excel_verification as excel
root=Path.cwd();evidence=root/'.git/autocycle';source=evidence/'source.xlsx'
source.write_bytes(b'original')
def run(args,**kwargs):
 if 'osascript' in args:
  if Path(args[-2]).name=='ensure_closed.applescript':return subprocess.CompletedProcess(args,0,'closed','')
  if sys.argv[1]=='failure':return subprocess.CompletedProcess(args,1,'','Excel save failed: Parameter error (-50)')
  Path(args[-1]).write_bytes(b'cached')
 else:
  assert Path(args[-1]).read_bytes()==b'cached'
  (evidence/'verifier-ran').write_text(args[-1])
 return subprocess.CompletedProcess(args,0,'verified','')
with patch.object(excel,'native_available'),patch.object(excel.subprocess,'run',side_effect=run):
 result=excel.verify(source,['existing-verifier','{workbook}'],evidence)
assert source.read_bytes()==b'original'
print(json.dumps(result))
''')
   agent=c.bin/'agent';s=agent.read_text()
   s=s.replace("  statuses=['completed','cancelled']",f'''  result=json.loads(subprocess.check_output([sys.executable,{str(helper)!r},{'failure' if failure else 'success'!r}],text=True))
  (root/'RESULT.md').write_text(json.dumps(result))
  if result['status']=='BLOCKED':os.environ['IMPLEMENT_BLOCKED']='1'
  statuses=['completed','cancelled']''')
   s=s.replace("message+='BLOCKER: deterministic implementation blocker\\n'", "message+='BLOCKER: '+result['action']+'\\n'")
   agent.write_text(s)
   r=c.run('1');ok(r)
   result=json.loads((c.repo/'RESULT.md').read_text())
   if failure:
    assert result['status']=='BLOCKED'
    assert not (c.a/'verifier-ran').exists()
    assert 'Excel save failed' in (c.a/'candidate.json').read_text()
    codex=c.bin/'codex';s=codex.read_text().replace("next_step='next bounded task'", "next_step='Diagnose native save error and retry verification'")
    codex.write_text(s)
    before=len(c.events())
    r=c.run('--extend','1',REVIEW_STATUS='PROBLEMS',REVIEW_BLOCKING='1',
            BLOCKER_KEY_OVERRIDE='native-save-error',PROGRESS_OUTCOME='NONE',FAIL_STAGE='plan');fail(r)
    assert [e['kind'] for e in c.events()[before:]]==['review','plan'],r.stdout
    cache=(c.a/'current-review').read_text()
    assert 'REVIEW_STATUS: PROBLEMS' in cache and 'Diagnose native save error' in cache
    assert 'HUMAN_ACTION: NONE' in cache

   else:
    assert result['status']=='VERIFIED'
    assert (c.a/'verifier-ran').exists()
    assert not (c.a/'candidate.json').exists()
    r=c.run('--extend','1',ENDPOINT_REACHED='1');ok(r)
    assert 'Blocked' not in r.stdout and 'BLOCKED' not in r.stdout,r.stdout
  finally:c.close()

if __name__=='__main__':test_excel_continuation_and_blocker();print('PASS Excel verification continuation and specific Review blocker')

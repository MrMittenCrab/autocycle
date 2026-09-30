"""Saved Excel blockers retry native verification without replaying completed work."""
import json
from pathlib import Path
from test_flow import Case, ok, fail


def exercise(failure='', pending=False, saved_stage='review_done', still_blocked=False):
 expected={'native':'Excel save failed','permission':'Excel open failed: User canceled. (-128)','verifier':'cached-value verification failed'}.get(failure,'')
 c=Case()
 try:
  ok(c.run('1'))
  source=c.a/'source.xlsx';source.write_bytes(b'original')
  codex=c.bin/'codex'
  s=codex.read_text().replace("kind='review' if prompt.startswith('Review HEAD') else 'plan'", '''if prompt.startswith('Resolve the saved Excel cached-value verification request only.'):
 event('excel-request',prompt)
 answer.write_text(json.dumps({'source':str(a/'source.xlsx'),'command':['existing-verifier','{workbook}']}))
 sys.exit(0)
kind='review' if prompt.startswith('Review HEAD') else 'plan'
if kind=='review' and os.environ.get('AUTOCYCLE_EXCEL_RECOVERY'):
 evidence=Path(os.environ['AUTOCYCLE_EXCEL_RECOVERY'])
 assert evidence.is_file(), 'Review ran before evidence was persisted'
 assert json.loads(evidence.read_text())['status']=='VERIFIED'
 assert str(evidence) in prompt, 'Review did not receive the new evidence'
''')
  s=s.replace("human_action='provide required human decision'", "human_action='STALE: manually recalculate Excel'")
  codex.write_text(s)
  r=c.run('--extend','1',REVIEW_STATUS='BLOCKED',BLOCKER_KEY_OVERRIDE='excel-cached-value-verification')
  fail(r);assert 'STALE' in r.stdout
  if saved_stage != 'review_done':
   path=c.a/'resume-state'
   path.write_text(path.read_text().replace('STAGE=review_done', 'STAGE='+saved_stage))
  if pending:c.enqueue('RECOVERY_INPUT: inspect fresh native evidence')
  saved=(c.a/'resume-state').read_text()
  rows=c.rows()
  cache=(c.a/'current-review').read_bytes()
  before=c.git('rev-parse','HEAD')
  branch_before=c.git('branch','--show-current')
  tree_before=c.git('status','--porcelain')
  events_before=len(c.events())
  state=json.loads((c.a/'work-state.json').read_text())
  helper=c.p/'excel_verification.py'
  s=helper.read_text().replace("if __name__=='__main__':sys.exit(main())", '''if __name__=='__main__':
 from unittest.mock import patch
 real_run=subprocess.run
 if sys.argv[1:2]==['--resume-blocker']:
  with (Path.cwd()/'.git/autocycle/events.jsonl').open('a') as f:
   f.write(json.dumps({'kind':'resume-blocker'})+'\\n')
 def mocked(args,**kwargs):
  if 'osascript' in args:
   if Path(args[-2]).name=='ensure_closed.applescript':
    return subprocess.CompletedProcess(args,0,'closed','')
   with (Path.cwd()/'.git/autocycle/excel-calls').open('a') as f:f.write('native\\n')
   if os.environ.get('EXCEL_FAILURE')=='permission':
    return subprocess.CompletedProcess(args,1,'','Excel open failed: User canceled. (-128)')
   if os.environ.get('EXCEL_FAILURE')=='native':
    return subprocess.CompletedProcess(args,1,'','Excel save failed: Parameter error (-50)')
   Path(args[-1]).write_bytes(b'cached')
   return subprocess.CompletedProcess(args,0,'saved','')
  if 'existing-verifier' in args:
   assert Path(args[-1]).read_bytes()==b'cached'
   assert Path(args[-1])!=Path.cwd()/'.git/autocycle/source.xlsx'
   with (Path.cwd()/'.git/autocycle/excel-calls').open('a') as f:f.write('verify\\n')
   return subprocess.CompletedProcess(args,1 if os.environ.get('EXCEL_FAILURE')=='verifier' else 0,'','Required cached value missing')
  return real_run(args,**kwargs)
 with patch(__name__+'.native_available'),patch.object(subprocess,'run',side_effect=mocked):
  sys.exit(main())
''')
  # Observe the durable evidence write in the same event stream as Review.
  s=s.replace("    if result['status'] == 'BLOCKED':", '''    with (Path.cwd()/'.git/autocycle/events.jsonl').open('a') as f:
        f.write(json.dumps({'kind':'fresh-evidence','path':str(folder/'result.json')})+'\\n')
    if result['status'] == 'BLOCKED':''')
  helper.write_text(s)
  resume_env = dict(REVIEW_STATUS='BLOCKED', BLOCKER_KEY_OVERRIDE='excel-cached-value-verification') if failure or still_blocked else dict(ENDPOINT_REACHED='1', INPUT_STATUS_OVERRIDE='COMPLETE' if pending else 'NONE')
  r=c.run('--resume',EXCEL_FAILURE=failure,**resume_env)
  assert c.git('rev-parse','HEAD')==before
  assert source.read_bytes()==b'original'
  assert c.git('branch','--show-current')==branch_before
  assert c.git('status','--porcelain')==tree_before
  assert not any(line.startswith(('Recovery ', 'Excel Recovery ')) for line in r.stdout.splitlines())
  new_events=c.events()[events_before:]
  assert [e['kind'] for e in new_events]==(['resume-blocker','excel-request','fresh-evidence'] if failure else ['resume-blocker','excel-request','fresh-evidence','review'])
  evidence=Path(new_events[2]['path'])
  recovery=json.loads(evidence.read_text())
  assert recovery['status']==('BLOCKED' if failure else 'VERIFIED')
  kinds=[e['kind'] for e in c.events()]
  assert kinds.count('implement')==kinds.count('plan')==1,kinds
  assert kinds.count('excel-request')==1,kinds
  after=json.loads((c.a/'work-state.json').read_text())
  branch='checkpoint/test'
  assert len(after['branches'][branch]['attempts'])==len(state['branches'][branch]['attempts'])
  if failure:
   fail(r)
   assert '    Blocked  ✗' in r.stdout and '    Action: Resolve ' in r.stdout,r.stdout
   assert expected in r.stdout,r.stdout
   assert 'STALE' not in r.stdout,r.stdout
   assert 'STAGE='+saved_stage in (c.a/'resume-state').read_text()
   assert after['branches'][branch]['work']==state['branches'][branch]['work']
   assert after['branches'][branch]['block']==state['branches'][branch]['block']
   assert [a['id'] for a in after['branches'][branch]['attempts']]==[a['id'] for a in state['branches'][branch]['attempts']]
   assert kinds.count('review')==2
   assert (c.a/'resume-state').read_text()==saved
   assert (c.a/'current-review').read_bytes()==cache
   assert c.rows()==rows
   assert expected in recovery['action']
   native_calls='native\nverify\n' if failure=='verifier' else 'native\n'
   assert (c.a/'excel-calls').read_text()==native_calls
   # Each explicit resume gets exactly one new attempt; no Review on failure.
   count=len(c.events())
   r=c.run('--resume',EXCEL_FAILURE=failure,**resume_env)
   fail(r)
   assert expected in r.stdout and 'STALE' not in r.stdout
   assert [e['kind'] for e in c.events()[count:]]==['resume-blocker','excel-request','fresh-evidence']
   assert c.events()[-1]['path']!=str(evidence)
   assert (c.a/'excel-calls').read_text()==native_calls*2
   assert (c.a/'resume-state').read_text()==saved
   assert c.rows()==rows
   assert (c.a/'current-review').read_bytes()==cache

  elif still_blocked:
   fail(r)
   assert recovery['evidence'] in new_events[-1]['prompt']
   assert (c.a/'resume-state').read_text()==saved
   assert c.rows()==rows
   assert (c.a/'excel-calls').read_text()=='native\nverify\n'
   # Recovery succeeded, but Review still owns the verdict. No loop in this
   # invocation; the next explicit resume alone authorizes another attempt.
   count=len(c.events())
   r=c.run('--resume',**resume_env)
   fail(r)
   later=c.events()[count:]
   assert [e['kind'] for e in later]==['resume-blocker','excel-request','fresh-evidence','review']
   next_evidence=Path(later[2]['path'])
   assert next_evidence!=evidence
   assert json.loads(next_evidence.read_text())['evidence'] in later[-1]['prompt']
   assert (c.a/'excel-calls').read_text()=='native\nverify\n'*2
   assert (c.a/'resume-state').read_text()==saved
  else:
   ok(r)
   assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
   assert kinds.count('review')==3
   assert 'Native Excel resume verification evidence: /' in c.events()[-1]['prompt']
   assert recovery['evidence'] in new_events[-1]['prompt']
   assert 'RUN_CYCLE=2' in (c.a/'resume-state').read_text()
   assert 'SESSION_NUMBER=1' in (c.a/'resume-state').read_text()
   if pending:assert 'RECOVERY_INPUT' in new_events[-1]['prompt']
   else:assert c.rows()==rows
   assert (c.a/'excel-calls').read_text()=='native\nverify\n'
   assert after['branches'][branch]['block'] is None
   events=c.events();ok(c.run('--resume'));assert c.events()==events
 finally:c.close()


if __name__=='__main__':
 for failure in ('','native','permission','verifier'):
  exercise(failure)
  print('PASS persisted Excel blocker resume: '+(failure or 'success'))

 for failure in ('','native'):
  exercise(failure,pending=True)
  print('PASS queued input retains recovery ordering: '+(failure or 'success'))
 for saved_stage in ('start','reviewing'):
  for failure in ('','native'):
   exercise(failure,saved_stage=saved_stage)
   print('PASS saved Review entry recovery: '+saved_stage+' '+(failure or 'success'))
 exercise(still_blocked=True)
 print('PASS successful recovery with fresh BLOCKED Review permits one attempt per explicit resume')

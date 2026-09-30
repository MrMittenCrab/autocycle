"""Session IDs, explicit lifecycle, and prose-plan handoff regressions."""
import json,os
from pathlib import Path
import progress
from test_flow import ok
from test_session import case,SESSION

def ledger(c):return json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']

def test_new_resume_extend_and_completion():
 c=case(False)
 try:
  target=(c.repo/'TARGET.md').read_bytes()
  r=c.run('1',NEW_SESSION=SESSION,PROGRESS_OUTCOME='VERIFIED');ok(r)
  assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='1.1'
  assert 'Session     ' not in r.stdout and 'Endpoint    ' not in r.stdout
  assert '✓ Planned Step 1.1' in r.stdout
  docs=[(c.repo/n).read_bytes() for n in ('TARGET.md','SESSION.md','IMPLEMENTATION.md')]
  before=c.events();ok(c.run('--resume'));assert c.events()==before
  rejected=c.run('50','--instruct','Do not queue this rejected direction');assert rejected.returncode!=0 and 'Session 1 is unfinished' in rejected.stdout
  assert c.rows()==[]
  r=c.run('--extend','1',PROGRESS_OUTCOME='VERIFIED');ok(r)
  assert 'Endpoint    ' not in r.stdout and 'Session     ' not in r.stdout
  assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='1.1'
  assert '✓ Planned Step 1.1' in r.stdout
  assert [(c.repo/n).read_bytes() for n in ('TARGET.md','SESSION.md')]==docs[:2]
  assert 'SESSION_NUMBER=1' in (c.a/'resume-state').read_text()
  assert 'RUN_MAX=2' in (c.a/'resume-state').read_text()
  r=c.run('--extend','4',ENDPOINT_REACHED='1');ok(r)
  assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
  assert len([e for e in c.events() if e['kind']=='implement'])==2
  assert r.stdout.count('    ✓ Found  ')==1
  assert 'Endpoint    ' not in r.stdout
  before=c.events();assert c.run('--extend','20').returncode!=0;assert c.events()==before
  fresh=SESSION.replace('working feature','next feature')
  r=c.run('50','--instruct','Endpoint: deliver the next feature',NEW_SESSION=fresh,ENDPOINT_REACHED='1');ok(r)
  assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='2.1'
  assert 'RUN_CYCLE=2' in (c.a/'resume-state').read_text()
  assert (c.repo/'TARGET.md').read_bytes()==target
  assert c.rows()[-1]['state']=='archived'
  assert 'SESSION_NUMBER=2' in (c.a/'resume-state').read_text()
  before=c.events();ok(c.run('--resume'));assert c.events()==before
 finally:c.close()

def test_plan_cursor_review_handoff():
 for explicit in (False,True):
  c=case()
  try:
   codex=c.bin/'codex';s=codex.read_text()
   # Omit the entire metadata row: controller must accept ordinary prose.
   s='\n'.join(line for line in s.split('\n') if not line.startswith("  'AUTOCYCLE_PLAN: '+json.dumps("))
   requirement='Verify 2 + 2 equals 4.' if explicit else 'Create the requested small feature.'
   s=s.replace('Record measured results in RESULT.md.',requirement)
   codex.write_text(s)
   agent=c.bin/'agent';s=agent.read_text().replace("event('implement',msg['params']['prompt'][0]['text'])", "event('implement',msg['params']['prompt'][0]['text'])\n  assert '"+requirement+"' in msg['params']['prompt'][0]['text']")
   if explicit:
    s=s.replace("if os.environ.get('NO_CHANGE')!='1':(root/'RESULT.md').write_text('Measured successful run '+str(count))", "measured=subprocess.check_output([sys.executable,'-c','print(2 + 2)'],text=True).strip()\n  assert measured=='4'\n  step=(root/'IMPLEMENTATION.md').read_text().splitlines()[0]\n  (root/'RESULT.md').write_text(step+'\\nVerification: python print(2 + 2) -> '+measured+'; explicit condition passed')")
   agent.write_text(s)
   r=c.run('2',ENDPOINT_REACHED='1');ok(r)
   events=c.events();imp=next(e['prompt'] for e in events if e['kind']=='implement')
   assert (c.repo/'IMPLEMENTATION.md').read_text() in imp
   if explicit:
    assert '1.1' in (c.repo/'RESULT.md').read_text()
    assert '-> 4; explicit condition passed' in (c.repo/'RESULT.md').read_text()
   assert requirement in imp
   assert 'explicit acceptance conditions' in imp and 'measured' in imp
   assert 'same IMPLEMENTATION.md' in events[-1]['prompt']
   assert 'RESULT.md' in events[-1]['prompt'] and 'plan omitted' in events[-1]['prompt']
  finally:c.close()

def test_major_and_optional_minor_transitions():
 c=case()
 try:
  p=c.bin/'codex';s=p.read_text()
  s=s.replace("'inputs':inputs}),", "'inputs':inputs,**({'minor':count-1} if count in (2,3) else {})}),")
  s=s.replace("report['direction']={}", "if work and work['step_id'] in ('1.1','1.1.1'):report['outcome']='VERIFIED'\n report['direction']={}")
  s=s.replace("os.environ.get('TEST_OBJECTIVE','test-objective')", "'bounded-approach-'+str(count)")
  p.write_text(s)
  ok(c.run('4'))
  state=ledger(c)
  assert list(state['allocated'])==['1.1','1.1.1','1.1.2','1.2'],state['allocated']
  assert state['work']['step_id']=='1.2'
  assert c.git('log','-1','--format=%s')=='Step 1.2'
 finally:c.close()

def test_interrupted_new_session_preserves_identity():
 c=case(False)
 try:
  r=c.run('3',NEW_SESSION=SESSION,FAIL_STAGE='implement')
  assert r.returncode!=0
  plan=(c.repo/'IMPLEMENTATION.md').read_bytes()
  assert progress.heading(plan.decode())=='1.1'
  r=c.run('--resume',ENDPOINT_REACHED='1');ok(r)
  assert (c.repo/'IMPLEMENTATION.md').read_bytes()==plan
  assert 'SESSION_NUMBER=1' in (c.a/'resume-state').read_text()
  assert len([e for e in c.events() if e['kind']=='plan'])==1
 finally:c.close()

def test_blocker_candidate_keeps_step_and_partial_work():
 c=case()
 try:
  r=c.run('2',IMPLEMENT_BLOCKED='1',PROGRESS_OUTCOME='VERIFIED');ok(r)
  assert r.stdout.count('    ◇ Blocker candidate:')==2
  assert '✓ Planned Step 1.1' in r.stdout
  assert list(ledger(c)['allocated'])==['1.1']
  assert (c.repo/'RESULT.md').read_text()=='Measured successful run 2'
  assert c.git('log','-1','--format=%s').startswith('Partial: Step 1.1 ')
  assert c.git('status','--porcelain')==''
 finally:c.close()

def test_review_completion_is_next_cycle_and_no_extra_checkpoint():
 c=case(False)
 try:
  r=c.run('1',NEW_SESSION=SESSION,ENDPOINT_REACHED='1');ok(r)
  assert [e['kind'] for e in c.events()]==['review','plan','implement']
  assert '    ✓ Found   verified' in r.stdout and 'Completed:' not in r.stdout
  assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
  head=c.git('rev-parse','HEAD');before=c.events()
  r=c.run('--extend','10',ENDPOINT_REACHED='1');ok(r)
  assert [e['kind'] for e in c.events()[len(before):]]==['review']
  assert 'Cycle       2/11' in r.stdout
  assert '    ✓ Found   ' in r.stdout and 'Endpoint    ' not in r.stdout
  lines=r.stdout.splitlines();i=next(i for i,l in enumerate(lines) if l.startswith('Review      ✓'))
  assert lines[i+1].startswith('    ✓ Found   ')
  assert 'Plan ' not in r.stdout and 'Checkpoint ' not in r.stdout
  assert 'Use --' not in r.stdout and 'Run:' not in r.stdout
  assert c.git('rev-parse','HEAD')==head
  assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
  r=c.run('--resume');ok(r);assert 'Completed:' not in r.stdout
  r=c.run('1',NEW_SESSION=SESSION.replace('working feature','next feature'));ok(r)
  assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='2.1'
 finally:c.close()

def test_restart_preserves_owned_partial_work_and_refuses_later_edits():
 from test_resume import adjudicate
 for unsafe in (False,True):
  c=case(False)
  try:
   p=c.start('2',NEW_SESSION=SESSION,BLOCK_STAGE='implement-edited');c.ready(p);c.kill(p)
   ok(adjudicate(c,'implementation-result'))
   state=c.a/'resume-state';state.write_text(state.read_text().replace('STAGE=implementing','STAGE=implement_done'))
   if unsafe:(c.repo/'private.txt').write_text('unowned edit')
   before=state.read_bytes();head=c.git('rev-parse','HEAD');index=c.git('ls-files','--stage')
   new=SESSION.replace('working feature','new product')
   r=c.run('1','--restart','--instruct','Endpoint: deliver the new product',NEW_SESSION=new)
   if unsafe:
    assert r.returncode!=0 and 'restart' in r.stdout.lower()
    assert c.git('rev-parse','HEAD')==head and state.read_bytes()==before
    assert c.git('ls-files','--stage')==index and (c.repo/'private.txt').read_text()=='unowned edit'
    assert c.rows()==[]
   else:
    ok(r);assert 'Completed:' not in r.stdout
    assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='2.1'
    assert (c.repo/'SESSION.md').read_text()==new
    assert (c.repo/'partial.txt').read_text()=='Preserve interrupted provider work\n'
    assert ledger(c)['last_session_end']['status']=='restarted'
    assert ledger(c)['deferred'][0]['work']['complete'] is False
    assert c.rows()[-1]['state']=='archived'
    assert 'Partial: Step 1.1' in c.git('log','--format=%s')
  finally:c.close()

def test_legacy_restart_and_no_obsolete_cli():
 c=case(False)
 try:
  legacy='# Step 9M.2.1.1.67 — legacy work\n'
  (c.repo/'IMPLEMENTATION.md').write_text(legacy)
  c.git('add','.');c.git('commit','-qm','Plan: Step 9M.2.1.1.67');c.git('push','-q')
  old=c.git('rev-parse','HEAD')
  r=c.run('1');assert r.returncode!=0 and 'Session 1 is unfinished' in r.stdout
  assert c.git('rev-parse','HEAD')==old
  r=c.run('1','--restart',NEW_SESSION=SESSION);ok(r)
  assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='2.1'
  assert c.git('show',old+':IMPLEMENTATION.md')==legacy.strip()
  assert 'Completed:' not in r.stdout
  state=(c.a/'resume-state').read_bytes()
  r=c.run('--new');assert r.returncode!=0
  assert (c.a/'resume-state').read_bytes()==state
 finally:c.close()

def test_restart_transition_is_resumable_without_double_increment():
 c=case(False)
 try:
  ok(c.run('1',NEW_SESSION=SESSION))
  helper=c.p/'progress.py';original=helper.read_text()
  helper.write_text(original.replace('    if output is not None: print(output)', "    if cmd=='new-session':raise RuntimeError('interrupted after ledger reset')\n    if output is not None: print(output)"))
  r=c.run('1','--restart');assert r.returncode!=0
  saved=(c.a/'resume-state').read_text()
  assert 'STAGE=session_restarted' in saved and 'SESSION_NUMBER=1' in saved
  assert ledger(c)['last_session_end']['status']=='restarted'
  helper.write_text(original)
  ok(c.run('--resume',NEW_SESSION=SESSION.replace('working feature','next feature')))
  assert 'SESSION_NUMBER=2' in (c.a/'resume-state').read_text()
  assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='2.1'
  assert len(ledger(c)['deferred'])==1
 finally:c.close()

def test_review_can_complete_existing_checkpoint_without_cursor():
 c=case()
 try:
  (c.repo/'RESULT.md').write_text('The required capability was independently verified.')
  c.git('add','.');c.git('commit','-qm','Owner checkpoint');c.git('push','-q')
  (c.a/'resume-state').write_text('STATE_VERSION=2\nRUN_MAX=50\nRUN_CYCLE=1\nSTAGE=start\nSTATE_BRANCH=checkpoint/test\n')
  p=c.bin/'codex';s=p.read_text().replace("'evidence':evidence if reached else []", "'evidence':([{'path':'RESULT.md','sha256':hashlib.sha256((root/'RESULT.md').read_bytes()).hexdigest(),'observation':'Inspected independently verified capability'}] if reached else [])")
  p.write_text(s)
  head=c.git('rev-parse','HEAD');r=c.run('--resume',ENDPOINT_REACHED='1');ok(r)
  assert [e['kind'] for e in c.events()]==['review']
  assert '    ✓ Found   Delivered the working feature.' in r.stdout
  assert 'Checkpoint ' not in r.stdout and 'Endpoint    ' not in r.stdout
  assert c.git('rev-parse','HEAD')==head
  assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
 finally:c.close()

if __name__=='__main__':
 for name,value in list(globals().items()):
  if name.startswith('test_') and callable(value):value();print('PASS '+name,flush=True)

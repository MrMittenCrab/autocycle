"""Advisory Review history and Completion-relative blocker decisions."""
import json,subprocess,sys
from pathlib import Path
import progress
from test_completion import Work,COMPLETION
from test_flow import Case,ok

def count(s):return progress.context(s,'')['stagnation']

def context(c):
 return json.loads(subprocess.check_output([sys.executable,str(c.p/'progress.py'),'context'],cwd=c.repo,env=c.env,text=True))

def test_stagnation_counts_reviewed_attempts_not_cycles_or_rechecks():
 with Work() as x:
  assert count(x.s)==0
  x.plan();assert count(x.s)==0
  report=x.review('NONE');assert count(x.s)==1
  progress.evaluate(x.s,report,'');assert count(x.s)==1
  report.write_text(report.read_text().replace('REVIEW_TOKEN: 1','REVIEW_TOKEN: recheck'))
  progress.evaluate(x.s,report,'');assert count(x.s)==1
  x.plan(minor=1);assert count(x.s)==1
  x.review('NONE');assert count(x.s)==2
  x.plan();assert count(x.s)==2
  progress.begin(x.s,x.c.git('rev-parse','HEAD'),'');assert count(x.s)==2
  x.review('VERIFIED');assert count(x.s)==0
  x.plan();x.review('NONE');assert count(x.s)==1
  x.plan();x.review('COMPLETE');assert count(x.s)==0

def test_unknown_evidence_is_not_a_no_progress_judgment():
 with Work() as x:
  x.plan();x.review('NONE');x.plan()
  a=progress.latest(x.s);a['outcome']='UNKNOWN';a['report']={'outcome':'UNKNOWN'}
  assert count(x.s)==1
  a['outcome']='RECOVERED';a['report']={'outcome':'NONE'};assert count(x.s)==2
  a['report']={'outcome':'UNKNOWN'};assert count(x.s)==1

def test_redirect_and_session_reset_stagnation():
 with Work() as x:
  x.plan();review=x.review('NONE',defer=True);assert count(x.s)==1
  x.draft(title='Different route',completion='The alternate capability works.')
  progress.prepare(x.s,x.path,review)
  Path('IMPLEMENTATION.md').write_text(x.path.read_text());x.c.git('add','.');x.c.git('commit','-qm','Plan: different major')
  progress.admit(x.s,x.c.git('rev-parse','HEAD'),'');assert count(x.s)==0
  assert x.s['work']['step_id']=='2.15'
  x.review('NONE');assert count(x.s)==1
  progress.new_session(x.s,'restarted',2);assert count(x.s)==0

def test_blocker_evidence_can_show_progress_and_a_wrong_route():
 with Work() as x:
  x.plan();x.review('NONE');x.plan()
  review=x.review('VERIFIED',defer=True)
  assert count(x.s)==0
  x.draft(title='Evidence-grounded different route',completion='The different route reaches the required capability.')
  progress.prepare(x.s,x.path,review)
  assert progress.heading(x.path.read_text())=='2.15'
  assert progress.record(x.path.read_text(),'AUTOCYCLE_PLAN')['defer_work']['work_id']==x.s['work']['id']
  Path('IMPLEMENTATION.md').write_text(x.path.read_text());x.c.git('add','.');x.c.git('commit','-qm','Plan: evidence-grounded route')
  progress.admit(x.s,x.c.git('rev-parse','HEAD'),'')
  assert x.s['work']['step_id']=='2.15' and count(x.s)==0

def test_large_stagnation_has_no_control_effect():
 with Work() as x:
  x.plan();x.review('NONE')
  original=x.s['work']['id'];a=x.s['attempts'][-1]
  x.s['attempts']=[dict(a,id=str(n)) for n in range(100)]
  assert count(x.s)==100 and x.s['block'] is None
  assert x.plan(minor=1)=='2.14.1'
  assert x.s['work']['id']==original and count(x.s)==100
  assert progress.completion(x.path.read_text())==COMPLETION

def test_candidate_decisions_follow_completion_without_new_phases():
 for decision in ('irrelevant','repair','redirect','external'):
  c=Case()
  try:
   ok(c.run('1',IMPLEMENT_BLOCKED='1'))
   original=(c.repo/'IMPLEMENTATION.md').read_text();before=context(c)
   assert before['stagnation']==0 and (c.a/'candidate.json').exists()
   candidate=(c.a/'candidate.json').read_bytes()
   p=c.bin/'codex';text=p.read_text()
   if decision=='repair':
    text=text.replace("'inputs':inputs}),", "'inputs':inputs,**({'minor':count-1} if count>1 else {})}),")
   if decision=='redirect':
    text=text.replace("report['direction']={}", "if work:report['defer_work']={'work_id':work['id'],'instruction_ids':[],'reason':'The blocker demonstrates that this intermediate goal is the wrong route to the Session Endpoint'}\n report['direction']={}")
   p.write_text(text)
   env={'PROGRESS_OUTCOME':'NONE'}
   if decision=='external':env.update(REVIEW_STATUS='BLOCKED',BLOCKER_KEY_OVERRIDE='external-source-access')
   elif decision=='repair':env.update(REVIEW_STATUS='PROBLEMS',REVIEW_BLOCKING='1',BLOCKER_KEY_OVERRIDE='completion-gap')
   elif decision=='redirect':env.update(TEST_COMPLETION='A different capability reaches the Endpoint.')
   else:env.update(REVIEW_STATUS='PASS',BLOCKER_KEY_OVERRIDE='NONE')
   r=c.run('--extend','1',**env)
   if decision=='external':
    assert r.returncode==2 and 'Completed:' not in r.stdout
    assert 'STAGE=session_complete' not in (c.a/'resume-state').read_text()
    assert len([e for e in c.events() if e['kind']=='plan'])==1
    assert context(c)['stagnation']==1
    events=c.events();assert c.run('--resume',**env).returncode==2
    assert c.events()[:-1]==events and c.events()[-1]['kind']=='review'
    assert c.events()[-1]['cycle']=='2' and context(c)['stagnation']==1
    assert c.run('--extend','2',**env).returncode==2
    assert context(c)['stagnation']==1
   else:
    ok(r);current=context(c)
    assert current['stagnation']==(0 if decision=='redirect' else 1)
    step=progress.heading((c.repo/'IMPLEMENTATION.md').read_text())
    assert step=={'irrelevant':'1.1','repair':'1.1.1','redirect':'1.2'}[decision]
    if decision!='redirect':assert progress.completion(original)==progress.completion((c.repo/'IMPLEMENTATION.md').read_text())
    if decision=='repair':
     ok(c.run('--extend','1',PROGRESS_OUTCOME='NONE'))
     assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='1.1.2'
     assert context(c)['stagnation']==2
   reviews=[e['prompt'] for e in c.events() if e['kind']=='review']
   prompt=reviews[1]
   for phrase in ('Does the candidate actually prevent the current major-Step Completion', 'wrong route', 'external', 'no threshold', 'advisory'):
    assert phrase in prompt,phrase
   line=next(l for l in prompt.splitlines() if l.startswith('AUTOCYCLE_WORK_CONTEXT: '))
   assert json.loads(line.split(': ',1)[1])['stagnation']==0
   assert any(q.read_bytes()==candidate for q in c.a.glob('candidate*.json'))
   assert 'Stagnation' not in r.stdout and 'Adjudication ' not in r.stdout
  finally:c.close()

def test_interruption_resume_and_extend_preserve_advisory_count():
 c=Case()
 try:
  ok(c.run('2',PROGRESS_OUTCOME='NONE'));assert context(c)['stagnation']==1
  events=c.events();ok(c.run('--resume'));assert events==c.events() and context(c)['stagnation']==1
  p=c.start('--extend','1',PROGRESS_OUTCOME='NONE',BLOCK_STAGE='review')
  c.ready(p);c.kill(p);assert context(c)['stagnation']==1
  ok(c.run('--resume',PROGRESS_OUTCOME='NONE'));assert context(c)['stagnation']==2
  ok(c.run('1','--restart'));assert context(c)['stagnation']==0
  assert progress.heading((c.repo/'IMPLEMENTATION.md').read_text())=='2.1'
 finally:c.close()

if __name__=='__main__':
 for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
  globals()[name]();print('PASS '+name,flush=True)

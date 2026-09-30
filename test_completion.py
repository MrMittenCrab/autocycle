"""Major Completion is prose in the admitted plan, judged only by opening Review."""
import json,os,hashlib
from pathlib import Path
import progress
from test_flow import Case,ok

COMPLETION='The normal build delivers the requested end-to-end capability.'

class Work:
 def __enter__(self):
  self.c=Case();self.cwd=Path.cwd();os.chdir(self.c.repo)
  self.session=os.environ.get('AUTOCYCLE_SESSION_NUMBER');os.environ['AUTOCYCLE_SESSION_NUMBER']='2'
  self.s={'allocated':{'2.13':{'status':'completed'}},'work':None,'attempts':[],'block':None}
  self.path=self.c.p/'draft.md';return self
 def __exit__(self,*args):
  os.chdir(self.cwd)
  if self.session is None:os.environ.pop('AUTOCYCLE_SESSION_NUMBER',None)
  else:os.environ['AUTOCYCLE_SESSION_NUMBER']=self.session
  self.c.close()
 def draft(self,title='Initial work',completion=COMPLETION,minor=None):
  self.path.write_text('# '+title+'\n'+('AUTOCYCLE_PLAN: '+json.dumps({'minor':minor})+'\n' if minor is not None else '')+'\n## Completion\n\n'+completion+'\n\nImplement the smallest direct change.\n')
 def plan(self,**kw):
  self.draft(**kw);progress.prepare(self.s,self.path)
  (self.c.repo/'IMPLEMENTATION.md').write_text(self.path.read_text())
  self.c.git('add','.');self.c.git('commit','-qm','Plan: '+self.path.read_text().splitlines()[0][2:]);self.c.git('push','-q')
  progress.admit(self.s,self.c.git('rev-parse','HEAD'),'');return progress.heading(self.path.read_text())
 def review(self,outcome='VERIFIED',defer=False,endpoint=False):
  (self.c.repo/'RESULT.md').write_text('Measured attempt '+str(len(self.s['attempts'])))
  self.c.git('add','.');self.c.git('commit','-qm','Checkpoint');self.c.git('push','-q')
  progress.checkpoint(self.s,'checkpoint','')
  w=self.s['work'];a=progress.latest(self.s)
  r={'work_id':w['id'],'attempt_id':a['id'],'finding_key':w['finding_key'],'blocking':False,'blocking_reason':'NONE','outcome':outcome,'reason':'Observed the checkpoint against parent Completion','evidence':[{'path':'RESULT.md','sha256':hashlib.sha256(Path('RESULT.md').read_bytes()).hexdigest(),'observation':'Measured capability'}] if outcome in ('VERIFIED','COMPLETE') else []}
  if defer:r['defer_work']={'work_id':w['id'],'reason':'This intermediate goal no longer advances the Session Endpoint','instruction_ids':[]}
  if endpoint:
   Path('SESSION.md').write_text('## Endpoint\nDeliver the capability\n## Priority\nEnd-to-end\n');self.c.git('add','.');self.c.git('commit','-qm','Session');self.c.git('push','-q')
   r['endpoint']={'status':'REACHED','session_sha256':hashlib.sha256(Path('SESSION.md').read_bytes()).hexdigest(),'evidence':r['evidence']}
  review=self.c.p/'review';review.write_text('REVIEW_STATUS: '+('DONE' if endpoint else 'PASS')+'\nREVIEW_TOKEN: '+str(len(self.s['attempts']))+'\nAUTOCYCLE_REVIEW: '+json.dumps(r)+'\n')
  code,_=progress.evaluate(self.s,review,'');assert code==0
  return review

def rejects(fn):
 try:fn()
 except ValueError:return
 raise AssertionError('Invalid transition was accepted')

def test_completion_required_without_acceptance_contract():
 with Work() as x:
  x.path.write_text('# New work\nOrdinary prose only\n');rejects(lambda:progress.prepare(x.s,x.path))
  assert x.plan()=='2.14'
  p=progress.record(x.path.read_text(),'AUTOCYCLE_PLAN')
  assert not {'baseline','success','verification','completion'}&p.keys()
  assert not {'baseline','success','verification','completion'}&x.s['work'].keys()
  assert 'Done when' not in x.path.read_text()

def test_optional_guidance_is_not_part_of_completion():
 with Work() as x:
  x.plan();x.review()
  x.draft(title='Use another approach')
  x.path.write_text(x.path.read_text().replace('Implement the smallest direct change.', 'Inspect and probe the actual behavior; no prescribed tests.'))
  progress.prepare(x.s,x.path)
  assert progress.completion(x.path.read_text())==COMPLETION
  assert progress.heading(x.path.read_text())=='2.14'

def test_continuations_keep_completion_and_review_closes_parent():
 with Work() as x:
  assert x.plan()=='2.14';parent=x.s['work']['id']
  assert not x.s['work']['complete'],'Cursor/checkpoint cannot close the Step'
  x.review();assert x.plan(title='Direct bounded repair',minor=1)=='2.14.1'
  assert x.s['work']['id']==parent
  x.review('NONE');assert x.plan(title='Revised bounded approach',minor=2)=='2.14.2'
  assert x.s['work']['id']==parent
  assert x.path.read_text().count(COMPLETION)==1
  x.review('COMPLETE');assert x.s['work']['complete']
  assert all(v['status']=='completed' for k,v in x.s['allocated'].items() if k.startswith('2.14'))
  assert x.plan(title='Distinct capability',completion='A distinct useful capability works.')=='2.15'

def test_resume_and_process_cycle_do_not_allocate_continuation():
 with Work() as x:
  x.plan();x.review();assert x.plan(minor=1,title='Bounded repair')=='2.14.1'
  sha=x.c.git('rev-parse','HEAD');n=len(x.s['attempts']);progress.begin(x.s,sha,'');assert len(x.s['attempts'])==n
  x.review();assert x.plan(title='Finish same attempt')=='2.14.1'
  assert len([k for k in x.s['allocated'] if k.startswith('2.14')])==2

def test_distinct_continuation_does_not_require_metadata_wording_changes():
 with Work() as x:
  x.plan();x.review('NONE')
  x.draft(minor=1)
  x.path.write_text(x.path.read_text().replace('Implement the smallest direct change.', 'Use the measured source defect to try a bounded alternate approach.'))
  progress.prepare(x.s,x.path)
  assert progress.heading(x.path.read_text())=='2.14.1'
  assert progress.completion(x.path.read_text())==COMPLETION
  Path('IMPLEMENTATION.md').write_text(x.path.read_text())
  x.c.git('add','.');x.c.git('commit','-qm','Plan: alternate approach')
  progress.admit(x.s,x.c.git('rev-parse','HEAD'),'')
  assert x.s['work']['step_id']=='2.14.1' and not x.s['work']['complete']

def test_no_weakening_nesting_or_unreviewed_continuation():
 with Work() as x:
  x.draft(minor=1);rejects(lambda:progress.prepare(x.s,x.path))
  x.plan();x.draft(minor=1);rejects(lambda:progress.prepare(x.s,x.path))
  x.review('NONE')
  x.draft(completion='Only a small fragment works.');rejects(lambda:progress.prepare(x.s,x.path))
  for minor in ('1.1',0,True,2):
   x.draft(minor=minor);rejects(lambda:progress.prepare(x.s,x.path))

def test_initial_completion_closes_major_and_endpoint():
 with Work() as x:
  x.plan();x.review('COMPLETE',endpoint=True);assert x.s['work']['complete']
  assert x.plan(title='Next capability',completion='The next capability works.')=='2.15'

def test_review_can_replace_inappropriate_goal_without_weakening_it():
 with Work() as x:
  x.plan();old=x.s['work']['id'];review=x.review('NONE',defer=True)
  x.draft(title='Genuinely different route',completion='A different end-to-end capability works.')
  progress.prepare(x.s,x.path,review)
  assert progress.heading(x.path.read_text())=='2.15'
  Path('IMPLEMENTATION.md').write_text(x.path.read_text());x.c.git('add','.');x.c.git('commit','-qm','Plan: replacement')
  progress.admit(x.s,x.c.git('rev-parse','HEAD'),'')
  assert x.s['deferred'][0]['work']['id']==old and not x.s['deferred'][0]['work']['complete']
  assert x.s['work']['id']!=old

def test_no_fixed_stagnation_redirect_or_block():
 with Work() as x:
  for n in range(4):
   assert x.plan(title='Approach '+str(n))=='2.14'
   x.review('NONE');assert x.s['block'] is None and not x.s['work']['complete']

def test_endpoint_cannot_claim_unfinished_completion():
 with Work() as x:
  x.plan();rejects(lambda:x.review('VERIFIED',endpoint=True))

def test_old_numerical_block_is_retired_without_losing_evidence():
 with Work() as x:
  x.plan()
  old={'reason':'REPEATED_NO_PROGRESS','instruction_ids':[],'stalled_attempts':2}
  x.s['block']=old
  x.review('NONE')
  assert x.s['block'] is None and not x.s['work']['complete']
  assert x.s['recoveries'][0]['block']==old

def test_interrupted_controller_continuation_resumes_same_substep():
 c=Case()
 try:
  ok(c.run('1',PROGRESS_OUTCOME='VERIFIED'))
  codex=c.bin/'codex';text=codex.read_text()
  text=text.replace("'inputs':inputs}),", "'inputs':inputs,**({'minor':1} if count==2 else {})}),")
  text=text.replace("os.environ.get('TEST_OBJECTIVE','test-objective')", "'bounded-approach-'+str(count)")
  codex.write_text(text)
  r=c.run('--extend','1',PROGRESS_OUTCOME='VERIFIED',FAIL_STAGE='implement')
  assert r.returncode!=0
  plan=(c.repo/'IMPLEMENTATION.md').read_bytes()
  assert progress.heading(plan.decode())=='1.1.1'
  before=len([e for e in c.events() if e['kind']=='plan'])
  ok(c.run('--resume',PROGRESS_OUTCOME='VERIFIED'))
  assert (c.repo/'IMPLEMENTATION.md').read_bytes()==plan
  assert len([e for e in c.events() if e['kind']=='plan'])==before
 finally:c.close()

if __name__=='__main__':
 for name,fn in list(globals().items()):
  if name.startswith('test_'):fn();print('PASS '+name,flush=True)

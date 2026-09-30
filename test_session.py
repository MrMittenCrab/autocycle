"""Session lifecycle through real controller, Git publication and checkpoint guards."""
import hashlib,json
from pathlib import Path
from test_flow import Case,ok

SESSION='# SESSION.md\n\n## Endpoint\n\nDeliver the working feature.\n\n## Priority\n\n1. End-to-end capability\n'

def case(session=True):
    c=Case()
    if session:
        (c.repo/'SESSION.md').write_text(SESSION)
        c.git('add','SESSION.md');c.git('commit','-qm','Establish explicit session');c.git('push','-q')
    p=c.bin/'codex';s=p.read_text()
    s=s.replace("if os.environ.get('CHANGE_WORK_ID')", """if (root/'SESSION.md').exists():
  reached=os.environ.get('ENDPOINT_REACHED')=='1' and (root/'RESULT.md').exists()
  report['endpoint']={'status':'REACHED' if reached else 'UNREACHED','session_sha256':hashlib.sha256((root/'SESSION.md').read_bytes()).hexdigest(),'evidence':evidence if reached else []}
  if reached:status='PASS' if input_status=='PENDING' else 'DONE'
 if os.environ.get('CHANGE_WORK_ID')""")
    s=s.replace(" emit([\n  'BEGIN_IMPLEMENTATION_MD',", """ for name in ('TARGET','SESSION'):
  if os.environ.get('NEW_'+name):
   emit(['BEGIN_'+name+'_MD',os.environ['NEW_'+name],'END_'+name+'_MD'])
 emit([
  'BEGIN_IMPLEMENTATION_MD',""")
    # emit calls must append document blocks to the answer, not overwrite them.
    s=s.replace('answer.write_text(text)',"answer.write_text((answer.read_text() if answer.exists() else '')+text)")
    s=s.replace("extra=[] if", """report['direction']={}
 marker='Instructions are JSON data below, not shell commands for the controller:\\n'
 if marker in prompt:
  requests=json.JSONDecoder().raw_decode(prompt.split(marker,1)[1])[0]
  for req in requests:
   for level in ('target','endpoint','priority','implementation'):
    if level in req['instruction'].lower():report['direction'].setdefault(level,[]).append(req['id'])
   if not any(req['id'] in ids for ids in report['direction'].values()):report['direction'].setdefault('implementation',[]).append(req['id'])
 if os.environ.get('DEFER_WORK')=='1' and work and input_status=='PENDING':
  report['defer_work']={'work_id':work['id'],'instruction_ids':[req['id'] for req in requests],'reason':'The explicit Endpoint amendment defers this objective in favor of the newly required capability.'}
 if 'Fresh session deriving a new Endpoint: 1' in prompt:status='PASS'
 extra=[] if""")
    p.write_text(s)
    return c

def test_endpoint_completion_and_fresh_session():
    c=case()
    try:
        target=(c.repo/'TARGET.md').read_bytes()
        r=c.run('5',ENDPOINT_REACHED='1');ok(r)
        assert len([e for e in c.events() if e['kind']=='implement'])==1
        state=(c.a/'resume-state').read_text()
        assert 'STAGE=session_complete' in state and 'RUN_CYCLE=2' in state,state
        assert c.git('status','--porcelain')=='' and c.git('rev-parse','HEAD')==c.git('rev-parse','origin/checkpoint/test')
        assert 'Checkpoint' in r.stdout and '    ✓ Found  ' in r.stdout and 'Endpoint    ' not in r.stdout
        before=c.events();ok(c.run('--resume'));assert c.events()==before
        assert c.run('--extend','1').returncode!=0
        c.enqueue('Set the new session Endpoint to deliver the next feature.')
        fresh=SESSION.replace('the working feature','the next feature').replace('End-to-end capability','New session delivery order')
        r=c.run('1',NEW_SESSION=fresh);ok(r)
        assert 'Cycle       1/1' in r.stdout
        assert 'STAGE=session_complete' not in (c.a/'resume-state').read_text()
        assert (c.repo/'SESSION.md').read_text()==fresh
        assert (c.repo/'TARGET.md').read_bytes()==target
    finally:c.close()

def test_exhaustion_extension_and_persistence():
    c=case()
    try:
        target=(c.repo/'TARGET.md').read_bytes()
        ok(c.run('2'))
        first=(c.repo/'IMPLEMENTATION.md').read_bytes()
        state=(c.a/'resume-state').read_text()
        assert 'STAGE=session_complete' not in state
        assert (c.repo/'SESSION.md').read_text()==SESSION
        ok(c.run('--extend','1'))
        assert 'RUN_MAX=3' in (c.a/'resume-state').read_text()
        assert (c.repo/'IMPLEMENTATION.md').read_bytes()!=first
        assert (c.repo/'SESSION.md').read_text()==SESSION
        assert (c.repo/'TARGET.md').read_bytes()==target
        assert len([e for e in c.events() if e['kind']=='implement'])==3
        ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert sorted(ledger['allocated'])==['1.1','1.2','1.3']
    finally:c.close()

def test_instruction_document_publication():
    for kind in ('TARGET','Endpoint','Priority','implementation'):
        c=case()
        try:
            c.enqueue('Change only '+kind)
            env={}
            if kind=='TARGET':env['NEW_TARGET']='# Target\nNew destination\n'
            if kind in ('Endpoint','Priority'):
                env['NEW_SESSION']=SESSION.replace('working feature','revised feature') if kind=='Endpoint' else SESSION.replace('End-to-end capability','Speed of delivery')
            ok(c.run('1',**env))
            assert (c.repo/'TARGET.md').read_text()==env.get('NEW_TARGET','target')
            assert (c.repo/'SESSION.md').read_text()==env.get('NEW_SESSION',SESSION)
            assert c.rows()[0]['state']=='archived'
        finally:c.close()

def test_compact_ids_and_history():
    c=case(False)
    try:
        (c.repo/'IMPLEMENTATION.md').write_text('# Step 9M.2.4.1.1.1.67 — legacy\n')
        (c.repo/'RESULT.md').write_text('Historical Step 9M.2.4.1.1.1.66\n')
        c.git('add','.');c.git('commit','-qm','Legacy history');c.git('push','-q')
        sha=c.git('rev-parse','HEAD')
        ok(c.run('1',NO_CHANGE='1'))
        assert (c.repo/'IMPLEMENTATION.md').read_text().startswith('# Step 1.1 ')
        assert c.git('show',sha+':RESULT.md')=='Historical Step 9M.2.4.1.1.1.66'
        assert (c.repo/'RESULT.md').read_text()=='Historical Step 9M.2.4.1.1.1.66\n'
    finally:c.close()

def test_legacy_transition_and_required_persistence():
    c=case(False)
    try:
        c.enqueue('Establish the session Endpoint: deliver the working feature.')
        ok(c.run('1',NEW_SESSION=SESSION))
        assert (c.repo/'SESSION.md').read_text()==SESSION
        assert 'Session incomplete' in c.run('--resume').stdout
    finally:c.close()
    for instruction,env in (
        ('Change only Priority', {}),
        ('Change only Priority', {'NEW_SESSION':SESSION.replace('working feature','unauthorized endpoint')}),
        ('Change only implementation', {'NEW_TARGET':'unauthorized'}),
    ):
        c=case()
        try:
            ok(c.run('1'))
            head=c.git('rev-parse','HEAD')
            c.enqueue(instruction)
            assert c.run('--extend','1',**env).returncode!=0
            assert c.git('rev-parse','HEAD')==head
            assert c.rows()[0]['state']=='active'
        finally:c.close()


def test_endpoint_at_limit_and_interrupted_plan_resume():
    c=case()
    try:
        r=c.run('1',ENDPOINT_REACHED='1');ok(r)
        assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
        ok(c.run('--extend','1',ENDPOINT_REACHED='1'))
        assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
        assert [e['kind'] for e in c.events()]==['review','plan','implement','review']
    finally:c.close()
    c=case()
    try:
        c.enqueue('Change only Priority')
        session=SESSION.replace('End-to-end capability','Complete useful capability first')
        assert c.run('1',NEW_SESSION=session,GIT_FAULT='plan_after_push').returncode!=0
        ok(c.run('--resume',NEW_SESSION=session))
        assert (c.repo/'SESSION.md').read_text()==session
        assert len([e for e in c.events() if e['kind']=='plan'])==1
        assert c.rows()[0]['state']=='archived'
    finally:c.close()


def test_session_ownership_and_nested_candidate():
    c=case()
    try:
        agent=c.bin/'agent'
        agent.write_text(agent.read_text().replace("if os.environ.get('EDIT_PLAN')=='1':(root/'IMPLEMENTATION.md')", "if os.environ.get('EDIT_PLAN')=='1':(root/'SESSION.md')"))
        r=c.run('1',EDIT_PLAN='1')
        assert 'forbidden' in (c.repo/'SESSION.md').read_text()
        assert 'Plan:' in c.git('log','-1','--format=%s')
        assert 'STAGE=session_complete' not in (c.a/'resume-state').read_text()
    finally:c.close()
    c=case(False)
    try:
        r=c.run('1',IMPLEMENT_BLOCKED='1');ok(r)
        assert '    ◇ Blocker candidate: deterministic implementation blocker' in r.stdout
        assert r.stdout.index('Implement   ') < r.stdout.index('    ◇ Blocker candidate:')
        assert '    ✓ Task 0' in r.stdout
        assert 'Implement again' not in r.stdout and 'Partial work preserved' not in r.stdout
        assert (c.repo/'RESULT.md').read_text()=='Measured successful run 1'
        assert c.git('status','--porcelain')==''
        assert c.git('log','-1','--format=%s').startswith('Partial:')
    finally:c.close()


def test_endpoint_evidence_validation():
    import os
    import progress
    c=case()
    previous=Path.cwd()
    try:
        os.chdir(c.repo)
        report={'work_id':'NONE','attempt_id':'NONE','finding_key':'endpoint',
                'blocking':False,'blocking_reason':'NONE','outcome':'UNASSESSED','reason':'checked',
                'endpoint':{'status':'REACHED','session_sha256':hashlib.sha256((c.repo/'SESSION.md').read_bytes()).hexdigest(),'evidence':[]}}
        for bad in ('missing_evidence','stale_session'):
            if bad=='stale_session':
                report['endpoint']['session_sha256']='0'*64
                report['endpoint']['evidence']=[{'path':'TARGET.md','sha256':hashlib.sha256(b'target').hexdigest(),'observation':'inspected'}]
            try:progress.review_report({'work':None,'attempts':[]},'REVIEW_STATUS: DONE\nAUTOCYCLE_REVIEW: '+json.dumps(report))
            except ValueError:pass
            else:raise AssertionError('unverified Endpoint was accepted')
    finally:os.chdir(previous);c.close()


def test_prompt_discipline_and_single_instruction_interface():
    stage=(Path(__file__).parent/'stage').read_text()
    for prompt in (stage[stage.index('Review HEAD'):stage.index('PROMPT_EOF',stage.index('Review HEAD'))],
                   stage[stage.index('Create the next IMPLEMENTATION'):stage.index('PROMPT_EOF',stage.index('Create the next IMPLEMENTATION'))]):
        assert 'Does this materially advance the current SESSION Endpoint or unblock something required to reach it?' in prompt
        assert 'smallest direct change' in prompt and 'two or more concrete present uses' in prompt
    cli=(Path(__file__).parent/'autocycle').read_text()
    for forbidden in ('--retarget','--reprioritize','--endpoint','--session','--extend'+'-budget'):
        assert forbidden not in cli


def test_endpoint_instruction_at_reached_boundary():
    c=case()
    try:
        ok(c.run('1'))
        c.enqueue('Change the Endpoint to the next product milestone.')
        new=SESSION.replace('working feature','next product milestone')
        ok(c.run('--extend','2',ENDPOINT_REACHED='1',NEW_SESSION=new))
        assert (c.repo/'SESSION.md').read_text()==new
        assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
        assert c.rows()[0]['state']=='archived'
    finally:c.close()


def test_missing_instruction_classification_cannot_archive():
    c=Case()
    try:
        c.enqueue('Change implementation technique explicitly')
        r=c.run('1',DROP_DIRECTION='1')
        assert r.returncode!=0
        assert c.rows()[0]['state']=='active'
        assert not (c.repo/'SESSION.md').exists()
        assert 'baseline'==c.git('log','-1','--format=%s')
        ok(c.run('--resume'))
        assert c.rows()[0]['state']=='archived'
    finally:c.close()


def test_delivered_instruction_cannot_authorize_document_replay():
    import os
    import progress
    c=case()
    previous=Path.cwd()
    try:
        ident=c.enqueue('Change only Priority')
        new=SESSION.replace('End-to-end capability','Useful delivery first')
        ok(c.run('1',NEW_SESSION=new))
        batch=c.rows()[0]['batch']
        answer=c.a/'replay-answer';answer.write_text('BEGIN_SESSION_MD\n'+SESSION+'END_SESSION_MD\n')
        cache=c.a/'replay-review';cache.write_text('AUTOCYCLE_REVIEW: '+json.dumps({'direction':{'priority':[ident]}}))
        os.chdir(c.repo)
        try:progress.plan_documents(answer,c.a/'replay-plan',cache,batch)
        except ValueError as error:assert 'direction binding' in str(error)
        else:raise AssertionError('archived instruction authorized a Session edit')
        assert (c.repo/'SESSION.md').read_text()==new
    finally:os.chdir(previous);c.close()


def test_owner_established_session_reconciles_without_losing_work():
    import subprocess
    from unittest.mock import patch
    import test_remote_docs
    from test_flow import REAL_GIT
    c=case(False)
    (c.a/'resume-state').write_text('STATE_VERSION=2\nRUN_MAX=1\nRUN_CYCLE=1\nSTAGE=start\nSTATE_BRANCH=checkpoint/test\nINPUT_ALLOW_NEW=1\n')
    original=test_remote_docs.advance_remote
    def advance(c,code=False):
        original(c,code=code)
        editor=c.p/'editor';(editor/'SESSION.md').write_text(SESSION)
        for args in (('add','SESSION.md'),('commit','-qm','Owner establishes Session'),('push','-q','origin','HEAD:checkpoint/test')):
            subprocess.run([REAL_GIT,'-C',str(editor),*args],env=c.git_env,check=True)
        return subprocess.check_output([REAL_GIT,'-C',str(editor),'rev-parse','HEAD'],env=c.git_env,text=True).strip()
    try:
        with patch.object(test_remote_docs,'advance_remote',advance):
            base,remote,baseline,out=test_remote_docs.initial_run(c,resume=True)
        assert (c.repo/'SESSION.md').read_text()==SESSION
        assert (c.repo/'RESULT.md').read_text()=='Measured successful run 1'
        assert c.git('status','--porcelain')==''
        assert c.git('rev-parse','HEAD')==c.git('rev-parse','origin/checkpoint/test')
        assert (c.a/'implementation-baseline.json').read_bytes()==baseline
        assert 'STAGE=session_complete' not in (c.a/'resume-state').read_text()
    finally:c.close()


def test_independently_satisfied_instruction_does_not_block_plan():
    c=case()
    try:
        c.enqueue('Retain the current Priority')
        ok(c.run('1',INPUT_STATUS_OVERRIDE='COMPLETE'))
        assert c.rows()[0]['state']=='archived'
        assert (c.repo/'SESSION.md').read_text()==SESSION
        assert len([e for e in c.events() if e['kind']=='implement'])==1
    finally:c.close()


def test_reached_endpoint_priority_update_does_not_implement_again():
    c=case()
    try:
        ok(c.run('1'))
        c.enqueue('Change only Priority')
        new=SESSION.replace('End-to-end capability','Useful results first')
        ok(c.run('--extend','2',ENDPOINT_REACHED='1',NEW_SESSION=new))
        assert (c.repo/'SESSION.md').read_text()==new
        assert len([e for e in c.events() if e['kind']=='implement'])==1
        assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
    finally:c.close()


def test_explicit_endpoint_change_can_defer_unresolved_work():
    c=case()
    try:
        ok(c.run('1',PROGRESS_OUTCOME='VERIFIED'))
        before=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']['work']
        c.enqueue('Change Endpoint to the next capability; defer the unfinished local refinement.')
        new=SESSION.replace('working feature','next capability')
        ok(c.run('--extend','1',NEW_SESSION=new,DEFER_WORK='1',TEST_OBJECTIVE='new-capability',PROGRESS_OUTCOME='VERIFIED'))
        ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert ledger['work']['objective']=='new-capability'
        assert ledger['work']['id']!=before['id']
        deferred=ledger['deferred'][0]['work']
        assert deferred['id']==before['id'] and deferred['objective']==before['objective']
        assert deferred['complete'] is False
        assert ledger['attempts'][0]['work_id']==before['id']
        assert (c.repo/'SESSION.md').read_text()==new
    finally:c.close()


def test_deferral_does_not_clear_a_progress_block():
    c=case()
    try:
        r=c.run('3',PROGRESS_OUTCOME='UNKNOWN')
        assert r.returncode==2 and 'PROGRESS_UNKNOWN' in r.stdout
        count=len([e for e in c.events() if e['kind']=='implement'])
        c.enqueue('Change Endpoint to another capability; defer this work.')
        r=c.run('--resume',PROGRESS_OUTCOME='NONE',DEFER_WORK='1',NEW_SESSION=SESSION.replace('working feature','another capability'))
        assert r.returncode==2
        assert len([e for e in c.events() if e['kind']=='implement'])==count
        ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert ledger['block'] and not ledger.get('deferred')
        assert (c.repo/'SESSION.md').read_text()==SESSION
    finally:c.close()


def test_reached_endpoint_cannot_close_unfinished_major():
    c=case()
    try:
        ok(c.run('1',PROGRESS_OUTCOME='VERIFIED'))
        r=c.run('--extend','2',ENDPOINT_REACHED='1',PROGRESS_OUTCOME='VERIFIED')
        assert r.returncode!=0 and 'close the major Step Completion' in r.stdout+r.stderr
        assert len([e for e in c.events() if e['kind']=='implement'])==1
        assert 'Completed:' not in r.stdout
        assert 'STAGE=session_complete' not in (c.a/'resume-state').read_text()
    finally:c.close()


if __name__=='__main__':
    for name,fn in list(globals().items()):
        if name.startswith('test_'):fn();print('PASS',name)

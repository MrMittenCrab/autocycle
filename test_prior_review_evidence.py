"""Historical facts through the real Review, cache and progress guard; local providers only."""
import hashlib
import json
import subprocess
import sys
from test_flow import Case, ok


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# The deterministic reviewer reads a factual receipt and its tested source. It
# does not infer correctness from the prior report's COMPLETE decision.
REVIEWER = r'''
 if os.environ.get('HISTORICAL_SETUP')=='1' and evidence:
  receipt=root/'earlier-check.json'
  report['evidence']=[{'path':receipt.name,'sha256':hashlib.sha256(receipt.read_bytes()).hexdigest(),'observation':'Recorded check of feature A'}]
 if os.environ.get('CLOSE_WITH_HISTORY')=='1':
  prior=context['prior_reviews'][0]
  old=prior['evidence'][0]
  historical=dict(old,prior_attempt_id=prior['attempt_id'])
  receipt_path=root/old['path']
  receipt=json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
  historical['applies_to']=[{'path':'feature-a.txt','sha256':receipt.get('source_sha256','0'*64),'observation':'The source tested by the retained receipt'}]
  historical['observation']='The earlier check covers unchanged feature A under the current Endpoint; feature B is checked separately.'
  mode=os.environ.get('HISTORICAL_MODE','valid')
  if mode=='no_basis':historical.pop('applies_to')
  if mode=='unknown_attempt':historical['prior_attempt_id']='invented'
  if mode=='rebound_receipt':historical['sha256']=hashlib.sha256(receipt_path.read_bytes()).hexdigest()
  if mode=='summary_basis':
   historical['applies_to']=[{'path':'.git/autocycle/work-state.json','sha256':hashlib.sha256((a/'work-state.json').read_bytes()).hexdigest(),'observation':'Previously accepted'}]
  if mode=='summary':
   historical={'path':'.git/autocycle/work-state.json','sha256':hashlib.sha256((a/'work-state.json').read_bytes()).hexdigest(),'observation':'Previously COMPLETE'}
  report['endpoint']['evidence']=evidence+[historical]
  if receipt.get('result')=='FAIL' or 'feature C' in (root/'SESSION.md').read_text():
   status='PROBLEMS'
   report['endpoint']['status']='UNREACHED'
   report['endpoint']['evidence']=[]
   report['blocking']=True
   report['blocking_reason']='Historical facts do not satisfy the current Endpoint'
   blocker_key='historical-fact-insufficient'
   human_action='NONE';next_step='Produce the missing factual verification'
'''


def fixture():
    c=Case()
    (c.repo/'feature-a.txt').write_text('feature A verified input\n')
    (c.repo/'earlier-check.json').write_text(json.dumps({'source_sha256':sha(c.repo/'feature-a.txt'),'result':'PASS'}))
    (c.repo/'SESSION.md').write_text('# Session\n## Endpoint\nDeliver feature A and feature B.\n## Priority\nFinish B.\n')
    c.git('add','.');c.git('commit','-qm','Historical factual fixture');c.git('push','-q')
    provider=c.bin/'codex'
    provider.write_text(provider.read_text().replace(" extra=[] if",REVIEWER+"\n extra=[] if"))
    ok(c.run('2',HISTORICAL_SETUP='1'))
    ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
    assert ledger['attempts'][0]['outcome']=='COMPLETE'
    assert len(ledger['attempts'])==2
    return c


def test_closing_review_reuses_facts_and_cache_rechecks_them():
    c=fixture()
    try:
        receipt=(c.repo/'earlier-check.json').read_bytes()
        before=len(c.events())
        r=c.run('--extend','1',REVIEW_STATUS='DONE',CLOSE_WITH_HISTORY='1');ok(r)
        assert [e['kind'] for e in c.events()[before:]]==['review']
        assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
        report=json.loads(next(x.split(': ',1)[1] for x in (c.a/'current-review').read_text().splitlines() if x.startswith('AUTOCYCLE_REVIEW: ')))
        assert report['outcome']=='COMPLETE' and report['endpoint']['status']=='REACHED'
        historical=report['endpoint']['evidence'][-1]
        assert historical['path']=='earlier-check.json'
        assert historical['applies_to'][0]['path']=='feature-a.txt'
        assert (c.repo/'earlier-check.json').read_bytes()==receipt
        # Re-review overwrites the active attempt's report. That report must
        # not become its own historical provenance after guard stores it.
        historical['prior_attempt_id']=report['attempt_id']
        answer=c.p/'self-reference.answer'
        answer.write_text('REVIEW_STATUS: DONE\nAUTOCYCLE_REVIEW: '+json.dumps(report)+'\n')
        r=subprocess.run([sys.executable,str(c.p/'progress.py'),'validate-review',str(answer)],cwd=c.repo,env=c.env,capture_output=True,text=True)
        assert r.returncode!=0 and 'unknown prior evidence attempt' in r.stderr,r.stderr
        # Cache validation and guard must also check the historical source, not
        # just the bytes of the immutable successful receipt.
        (c.repo/'feature-a.txt').write_text('changed after Review')
        c.git('add','.');c.git('commit','-qm','Change source after acceptance');c.git('push','-q')
        r=subprocess.run([sys.executable,str(c.p/'progress.py'),'validate-review',str(c.a/'current-review')],cwd=c.repo,env=c.env,capture_output=True,text=True)
        assert r.returncode!=0
        assert 'hash mismatch' in r.stderr,r.stderr
    finally:c.close()


def test_invalid_historical_bindings_cannot_inherit_acceptance():
    for mode in ('changed_source','missing_source','changed_receipt','missing_receipt','no_basis','unknown_attempt','summary','summary_basis','rebound_receipt'):
        c=fixture()
        try:
            if mode=='changed_source':(c.repo/'feature-a.txt').write_text('changed without verification')
            if mode=='missing_source':(c.repo/'feature-a.txt').unlink()
            if mode=='changed_receipt':(c.repo/'earlier-check.json').write_text('{}')
            if mode=='rebound_receipt':
                receipt=c.repo/'earlier-check.json'
                receipt.write_text(receipt.read_text()+'\n')
            if mode=='missing_receipt':(c.repo/'earlier-check.json').unlink()
            if mode.startswith(('changed_','missing_')) or mode=='rebound_receipt':
                c.git('add','-A');c.git('commit','-qm','Change applicability');c.git('push','-q')
            before=len(c.events())
            r=c.run('--extend','1',REVIEW_STATUS='DONE',CLOSE_WITH_HISTORY='1',HISTORICAL_MODE=mode)
            assert r.returncode!=0,(mode,r.stdout,r.stderr)
            assert 'invalid progress evidence' in r.stdout,(mode,r.stdout,r.stderr)
            assert [e['kind'] for e in c.events()[before:]]==['review']*3
            assert 'STAGE=session_complete' not in (c.a/'resume-state').read_text()
        finally:c.close()


def test_prior_acceptance_does_not_decide_semantic_sufficiency():
    for mode in ('failed_fact','changed_requirement'):
        c=fixture()
        try:
            if mode=='failed_fact':
                receipt=c.repo/'earlier-check.json'
                data=json.loads(receipt.read_text());data['result']='FAIL';receipt.write_text(json.dumps(data))
                # Simulate a mistaken old acceptance, even with matching hashes.
                ledger_path=c.a/'work-state.json'
                ledger=json.loads(ledger_path.read_text())
                ledger['branches']['checkpoint/test']['attempts'][0]['report']['evidence'][0]['sha256']=sha(receipt)
                ledger_path.write_text(json.dumps(ledger))
            else:
                (c.repo/'SESSION.md').write_text('# Session\n## Endpoint\nDeliver feature A, feature B and feature C.\n## Priority\nVerify C.\n')
            c.git('add','-A');c.git('commit','-qm','Current factual requirements');c.git('push','-q')
            r=c.run('--extend','1',REVIEW_STATUS='DONE',CLOSE_WITH_HISTORY='1',FAIL_STAGE='plan')
            assert r.returncode!=0,(r.stdout,r.stderr)
            assert 'STAGE=session_complete' not in (c.a/'resume-state').read_text()
            assert 'REVIEW_STATUS: PROBLEMS' in (c.a/'current-review').read_text()
            if mode=='failed_fact':assert c.events()[-1]['kind']=='plan',(r.stdout,r.stderr)
            else:
                # Manual protected-document edits still require owner reconciliation.
                assert c.events()[-1]['kind']=='review',(r.stdout,r.stderr)
                assert 'ownership' in r.stdout.lower() or 'protected' in r.stdout.lower(),(r.stdout,r.stderr)
        finally:c.close()


def test_derived_review_summary_is_not_factual_evidence():
    c=Case()
    try:
        summary=c.a/'current-review'
        summary.write_text('REVIEW_STATUS: DONE\nREVIEW: accepted earlier\n')
        report={'work_id':'NONE','attempt_id':'NONE','finding_key':'endpoint',
                'blocking':False,'blocking_reason':'NONE','outcome':'UNASSESSED',
                'reason':'No current attempt', 'evidence':[{'path':str(summary),
                'sha256':sha(summary),'observation':'An earlier Review accepted it'}]}
        answer=c.p/'summary.answer'
        answer.write_text('REVIEW_STATUS: DONE\nAUTOCYCLE_REVIEW: '+json.dumps(report)+'\n')
        r=subprocess.run([sys.executable,str(c.p/'progress.py'),'validate-review',str(answer)],
                         cwd=c.repo,env=c.env,capture_output=True,text=True)
        assert r.returncode!=0,'Derived Review summary was accepted as factual evidence'
        assert 'self-referential' in r.stderr,r.stderr
    finally:c.close()


def test_rejected_review_recovers_at_existing_review_boundary():
    c=fixture()
    try:
        before=len(c.events())
        r=c.run('--extend','1',REVIEW_STATUS='DONE',CLOSE_WITH_HISTORY='1',HISTORICAL_MODE='summary')
        assert r.returncode!=0 and 'self-referential' in r.stdout
        assert 'STAGE=reviewing' in (c.a/'resume-state').read_text()
        ok(c.run('--resume',REVIEW_STATUS='DONE',CLOSE_WITH_HISTORY='1'))
        assert [e['kind'] for e in c.events()[before:]]==['review']*4
        assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
    finally:c.close()


if __name__=='__main__':
    for name,fn in list(globals().items()):
        if name.startswith('test_'):fn();print('PASS',name,flush=True)

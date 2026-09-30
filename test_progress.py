"""Real controller regressions: provider claims cannot bypass durable attempt accounting."""
from test_flow import Case, ok


def test_no_progress_is_judged_without_fixed_attempt_limit():
    c=Case()
    try:
        c.enqueue('Repair the actual objective')
        ok(c.run('4',PROGRESS_OUTCOME='NONE'))
        assert len([e for e in c.events() if e['kind']=='implement'])==4
        assert c.rows()[0]['state']=='archived'
        before=c.events();ok(c.run('--resume'));assert c.events()==before
    finally:c.close()


def test_incremental_and_documentation_progress():
    for objective in ('fix-reconciliation', 'write-required-documentation'):
        c=Case()
        try:
            ok(c.run('3',PROGRESS_OUTCOME='VERIFIED',TEST_OBJECTIVE=objective))
            assert len([e for e in c.events() if e['kind']=='implement'])==3
            import json
            s=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
            assert s['block'] is None
            assert len({a['work_id'] for a in s['attempts']})==1
            assert len(s['allocated'])==1,'Retries must reuse the same project step ID'
        finally:c.close()


def test_unknown_is_bounded():
    c=Case()
    try:
        r=c.run('3',PROGRESS_OUTCOME='UNKNOWN')
        assert r.returncode==2,(r.stdout,r.stderr)
        assert 'PROGRESS_UNKNOWN' in r.stdout
        assert len([e for e in c.events() if e['kind']=='implement'])==1
        assert len([e for e in c.events() if e['kind']=='review'])==3
        before=c.events();assert c.run('--resume',PROGRESS_OUTCOME='UNKNOWN').returncode==2
        assert c.events()[:-1]==before and c.events()[-1]['kind']=='review'
    finally:c.close()


def test_advisory_findings_do_not_force_admin_work():
    c=Case()
    try:
        ok(c.run('2',REVIEW_STATUS='PROBLEMS',PROGRESS_OUTCOME='VERIFIED'))
        assert len([e for e in c.events() if e['kind']=='implement'])==2
        assert 'BLOCKED' not in c.run('--resume').stdout
    finally:c.close()


def test_nochange_extension_preserves_completion_without_stall_counter():
    import json
    c=Case()
    try:
        ok(c.run('1',NO_CHANGE='1',PROGRESS_OUTCOME='NONE'))
        before=c.events();ok(c.run('--resume'));assert c.events()==before
        ok(c.run('--extend','2',NO_CHANGE='1',PROGRESS_OUTCOME='NONE'))
        ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert ledger['block'] is None and len(ledger['allocated'])==1
        assert len([e for e in c.events() if e['kind']=='implement'])==3
    finally:c.close()


def test_wrong_identity_or_unbound_evidence_cannot_advance():
    for change in ({'CHANGE_WORK_ID':'1'}, {'BAD_EVIDENCE':'1'}, {'DROP_PROGRESS_REPORT':'1'}):
        c=Case()
        try:
            ok(c.run('1'))
            r=c.run('--extend','1',**change)
            assert r.returncode!=0,(r.stdout,r.stderr)
            assert len([e for e in c.events() if e['kind']=='implement'])==1
        finally:c.close()


def test_numbering_registry_ignores_reservations():
    import json,subprocess,sys
    c=Case()
    try:
        parent='9M.2.4.1.1.1'
        (c.repo/'IMPLEMENTATION.md').write_text('# Step '+parent+'.12 — actual work\nReserve '+parent+'.13; mention '+parent+'.14\n')
        c.git('add','.');c.git('commit','-qm','Plan: actual work')
        (c.repo/'RESULT.md').write_text('Only .12 was executed; prospective '+parent+'.13 remains unallocated')
        c.git('add','.');c.git('commit','-qm','Step '+parent+'.12')
        def helper(*args):
            r=subprocess.run([sys.executable,str(c.p/'progress.py'),*args],cwd=c.repo,env=c.env,capture_output=True,text=True)
            ok(r);return r.stdout
        before=json.loads(helper('context'))
        assert before['allocated_ids']==[parent+'.12'],before
        plan=c.p/'draft.md'
        data={'objective':'actual-benchmark','finding_key':'classification','kind':'work','baseline':'gap is 8','success':'gap becomes 0','verification':'inspect measured gap'}
        for n in range(2):
            plan.write_text('# Step '+parent+'.99 — proposed repair\nAUTOCYCLE_PLAN: '+json.dumps(data)+'\n## Completion\nThe measured gap is zero.\n')
            helper('prepare',str(plan))
            assert plan.read_text().startswith('# Step 1.1 ')
            assert json.loads(helper('context'))['allocated_ids']==[parent+'.12']
        (c.repo/'IMPLEMENTATION.md').write_text(plan.read_text());c.git('add','.');c.git('commit','-qm','Plan: actual next work')
        sha=c.git('rev-parse','HEAD');helper('begin',sha)
        after=json.loads(helper('context'))
        assert after['allocated_ids']==sorted(['1.1',parent+'.12'])
        ident=after['work']['id'];helper('begin',sha)
        assert json.loads(helper('context'))['work']['id']==ident
    finally:c.close()


def test_target_done_ends_budget():
    c=Case()
    try:
        ok(c.run('1'))
        ok(c.run('--extend','20',REVIEW_STATUS='DONE'))
        assert [e['kind'] for e in c.events()]==['review','plan','implement','review']
        assert 'STAGE=session_complete' in (c.a/'resume-state').read_text()
    finally:c.close()



def test_replayed_verified_review_is_not_a_new_attempt():
    import json,subprocess,sys,hashlib
    c=Case()
    try:
        ok(c.run('1'))
        helper=[sys.executable,str(c.p/'progress.py')]
        r=subprocess.run(helper+['context'],cwd=c.repo,env=c.env,capture_output=True,text=True);ok(r)
        context=json.loads(r.stdout)
        report={'work_id':context['work']['id'],'attempt_id':context['attempt']['id'],'finding_key':'wording one','blocking':False,'blocking_reason':'NONE','outcome':'VERIFIED','reason':'Observed result improves the original criterion','evidence':[{'path':'RESULT.md','sha256':hashlib.sha256((c.repo/'RESULT.md').read_bytes()).hexdigest(),'observation':'Measured result'}]}
        report['endpoint']={'status':'UNREACHED','session_sha256':hashlib.sha256((c.repo/'SESSION.md').read_bytes()).hexdigest(),'evidence':[]}
        answer=c.p/'review.answer'
        for token in ('first-review','replayed-review'):
            answer.write_text('REVIEW_STATUS: PASS\nREVIEW_TOKEN: '+token+'\nAUTOCYCLE_REVIEW: '+json.dumps(report)+'\n')
            r=subprocess.run(helper+['guard',str(answer)],cwd=c.repo,env=c.env,capture_output=True,text=True);ok(r)
        s=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert len(s['attempts'])==1 and s['attempts'][0]['outcome']=='VERIFIED',s
    finally:c.close()


def test_unknown_recovery_survives_both_guards():
    c=Case()
    try:
        r=c.run('2',PROGRESS_OUTCOME='UNKNOWN');assert r.returncode==2,(r.stdout,r.stderr)
        c.enqueue('Change approach: reproduce the defect using the original source data')
        r=c.run('--resume',PROGRESS_OUTCOME='UNKNOWN',REVIEW_RECOVERY='REVISED_APPROACH');ok(r)
        assert len([e for e in c.events() if e['kind']=='implement'])==2
    finally:c.close()


def test_old_evidence_cannot_alternate_as_progress():
    c=Case()
    try:
        r=c.run('6',PROGRESS_OUTCOME='VERIFIED',ALTERNATE_RESULT='1')
        ok(r)
        import json
        state=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert state['attempts'][-2]['outcome']=='NONE'
        assert state['block'] is None
        assert len([e for e in c.events() if e['kind']=='implement'])==6
    finally:c.close()


def test_planning_candidates_are_bounded():
    c=Case()
    try:
        r=c.run('4',PLAN_BLOCKED='1')
        ok(r)
        assert len([e for e in c.events() if e['kind']=='plan'])==4
        assert not [e for e in c.events() if e['kind']=='implement']
    finally:c.close()



def test_upgrade_at_legacy_review_boundary_keeps_pending_attempt():
    import json
    c=Case()
    try:
        ok(c.run('1'))
        (c.a/'work-state.json').unlink()
        state=(c.a/'resume-state').read_text().replace('RUN_MAX=1','RUN_MAX=2').replace('RUN_CYCLE=1','RUN_CYCLE=2').replace('STAGE=checkpoint_done','STAGE=review_done')
        (c.a/'resume-state').write_text(state)
        ok(c.run('--resume',PROGRESS_OUTCOME='NONE'))
        ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert len(ledger['attempts'])==2,ledger
        assert ledger['attempts'][0]['outcome']=='NONE'
        assert len(ledger['allocated'])==1
    finally:c.close()


if __name__=='__main__':
    import sys
    selected=sys.argv[1:] or [name for name in globals() if name.startswith('test_')]
    for name in selected:
        globals()[name]()
        print('PASS '+name,flush=True)

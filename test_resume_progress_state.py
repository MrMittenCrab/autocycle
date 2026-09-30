"""Resume must distinguish saved planning candidates from implemented work."""
import json
import sys
from test_flow import Case, ok


def ledger(c):
    return json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']


def instrument(c):
    # Observe durable controller state at each real provider boundary.
    for name in ('codex', 'agent'):
        p=c.bin/name
        p.write_text(p.read_text().replace("'kind':kind,", "'state':(a/'resume-state').read_text(),'kind':kind,"))


def test_progress_block_resume_rechecks_current_evidence():
    for outcome in ('UNKNOWN', 'VERIFIED', 'NONE'):
        resolved=outcome!='UNKNOWN'
        c=Case()
        try:
            instrument(c)
            assert c.run('2', PROGRESS_OUTCOME='UNKNOWN').returncode==2
            before=ledger(c); old_cache=(c.a/'current-review').read_text()
            assert before['block'] and before['attempts'][-1]['kind']=='implementation'
            assert 'REVIEW_STATUS: PASS' in old_cache
            count=len(c.events())
            r=c.run('--resume', PROGRESS_OUTCOME=outcome)
            events=c.events()[count:]
            assert [e['kind'] for e in events]==(['review','plan','implement'] if resolved else ['review']), (r.stdout, events)
            assert 'STAGE=reviewing' in events[0]['state']
            after=ledger(c)
            assert after['attempts'][0]['review_token']!=before['attempts'][0]['review_token']
            assert after['attempts'][0]['id']==before['attempts'][0]['id']
            assert 'RUN_CYCLE=2' in (c.a/'resume-state').read_text()
            if resolved:
                ok(r);assert after['block'] is None
                assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
            else:
                assert (c.a/'current-review').read_text()!=old_cache
                assert r.returncode==2
                assert after['block']['review_token']!=before['block']['review_token']
                assert after['attempts'][0]['rechecks']==before['attempts'][0]['rechecks']+1
                assert 'STAGE=review_done' in (c.a/'resume-state').read_text()
        finally:c.close()


def test_restarted_planning_candidate_is_not_implementation_progress():
    c=Case()
    try:
        c.enqueue('historical instruction');ok(c.run('1'))
        old=ledger(c)
        c.enqueue('current Session instruction')
        r=c.run('2','--restart',PLAN_BLOCKED='1',PROGRESS_OUTCOME='UNASSESSED')
        ok(r)
        s=ledger(c)
        assert s['block'] is None
        assert all(a['kind']=='planning' for a in s['attempts'][s['session_start']:])
        assert len(s['attempts'][:s['session_start']])==len(old['attempts'])
        count=len(c.events());ok(c.run('--extend','1',PROGRESS_OUTCOME='UNKNOWN'))
        assert [e['kind'] for e in c.events()[count:]]==['review','plan','implement']
        assert ledger(c)['attempts'][-1]['kind']=='implementation'
    finally:c.close()


def test_saved_planning_unknown_block_is_reassessed():
    c=Case()
    try:
        instrument(c)
        c.enqueue('historical');ok(c.run('1'))
        c.enqueue('current');ok(c.run('2','--restart',PLAN_BLOCKED='1',PROGRESS_OUTCOME='NONE'))
        # Reconstruct the old controller's stopped state, without a new implementation.
        path=c.a/'work-state.json';data=json.loads(path.read_text());s=data['branches']['checkpoint/test']
        a=s['attempts'][-1]
        assert a['kind']=='planning'
        a.update(outcome='UNKNOWN',rechecks=2,review_token='old-terminal-token')
        s['block']={'reason':'PROGRESS_UNKNOWN','attempt_id':a['id'],'work_id':a['work_id'],
                    'instruction_ids':[row['id'] for row in c.rows() if row['state']=='active'],
                    'review_token':'old-terminal-token','evidence_key':''}
        path.write_text(json.dumps(data))
        state=c.a/'resume-state'
        state.write_text(state.read_text().replace('STAGE=checkpoint_done','STAGE=review_done')
                         .replace('BOUNDARY_KIND=planning','BOUNDARY_KIND=checkpoint'))
        count=len(c.events());r=c.run('--resume',PROGRESS_OUTCOME='UNKNOWN');ok(r)
        events=c.events()[count:]
        assert [e['kind'] for e in events]==['review','plan','implement']
        assert 'STAGE=reviewing' in events[0]['state']
        context=json.loads(next(line.split(': ',1)[1] for line in events[0]['prompt'].splitlines()
                                if line.startswith('AUTOCYCLE_WORK_CONTEXT: ')))
        assert context['attempt']['kind']=='planning' and context['work']['provisional']
        assert context['verification_only'] is False
        assert ledger(c)['block'] is None
        assert ledger(c)['recoveries'][-1]['block']['review_token']=='old-terminal-token'
        assert 'SESSION_NUMBER=2' in state.read_text()
    finally:c.close()


def test_healthy_plan_resume_preserves_instruction_ownership():
    c=Case()
    try:
        instrument(c)
        c.enqueue('historical');ok(c.run('1'))
        c.enqueue('owned by restarted Session')
        p=c.start('1','--restart',BLOCK_STAGE='plan');c.ready(p);c.kill(p)
        rows=c.rows();batch=rows[1]['batch']
        c.enqueue('queued later')
        count=len(c.events());ok(c.run('--resume'))
        assert [e['kind'] for e in c.events()[count:]]==['plan','implement']
        after=c.rows()
        assert after[0]==rows[0]
        assert after[1]['batch']==batch and after[1]['state']=='archived'
        assert after[2]['state']=='pending'
        assert 'STAGE=planning' in c.events()[count]['state']
    finally:c.close()


def test_planning_candidate_preserves_current_concrete_blocker():
    c=Case()
    try:
        ok(c.run('1',PLAN_BLOCKED='1'))
        r=c.run('--extend','1',REVIEW_STATUS='BLOCKED',PROGRESS_OUTCOME='UNKNOWN',
                BLOCKER_KEY_OVERRIDE='current-access-failure')
        assert r.returncode==2 and 'PROGRESS_UNKNOWN' not in r.stdout
        count=len(c.events())
        r=c.run('--resume',REVIEW_STATUS='BLOCKED',PROGRESS_OUTCOME='UNKNOWN',
                BLOCKER_KEY_OVERRIDE='new-access-failure')
        assert r.returncode==2 and 'PROGRESS_UNKNOWN' not in r.stdout
        assert [e['kind'] for e in c.events()[count:]]==['review']
        assert 'BLOCKER_KEY: new-access-failure' in (c.a/'current-review').read_text()
        assert ledger(c)['block'] is None
    finally:c.close()


if __name__=='__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS',name,flush=True)

"""Retained candidates are evidence; current Review decides whether they block."""
import json
import subprocess
import sys
from test_flow import Case, ok


def preserved_checkpoint():
    c = Case()
    c.enqueue('Fix the objective and preserve all evidence')
    ok(c.run('1', GIT_FAULT='checkpoint_before_push'))
    assert (c.a/'candidate.json').exists()
    c.git('push', 'origin', 'checkpoint/test')
    return c


def test_resolved_candidate_allows_advisory_review_and_plan():
    c = preserved_checkpoint()
    try:
        candidate = (c.a/'candidate.json').read_bytes()
        ok(c.run('--extend','1', REVIEW_STATUS='PROBLEMS',
                 BLOCKER_KEY_OVERRIDE='NONE', PROGRESS_OUTCOME='COMPLETE'))
        assert len([e for e in c.events() if e['kind']=='implement']) == 2
        assert len([e for e in c.events() if e['kind']=='review']) == 2
        assert any(p.read_bytes()==candidate for p in c.a.glob('candidate-*.json'))
        assert c.rows()[0]['state'] == 'archived'
    finally:
        c.close()


def test_resolved_candidate_allows_pass_and_done():
    for status in ('PASS', 'DONE'):
        c = preserved_checkpoint()
        try:
            candidate = (c.a/'candidate.json').read_bytes()
            ok(c.run('--extend','1', REVIEW_STATUS=status,
                     BLOCKER_KEY_OVERRIDE='NONE', PROGRESS_OUTCOME='COMPLETE'))
            assert any(p.read_bytes()==candidate for p in c.a.glob('candidate-*.json'))
            assert len([e for e in c.events() if e['kind']=='implement']) == (1 if status=='DONE' else 2)
            assert c.rows()[0]['state'] == 'archived'
        finally:
            c.close()


def test_missing_key_on_current_blocker_rejected_with_or_without_candidate():
    for candidate in (False, True):
        for status in ('PROBLEMS', 'BLOCKED'):
            c = preserved_checkpoint() if candidate else Case()
            try:
                args = ('--extend','1') if candidate else ('1',)
                before = len([e for e in c.events() if e['kind']=='plan']) if candidate else 0
                r = c.run(*args, REVIEW_STATUS=status, REVIEW_BLOCKING='1',
                          BLOCKER_KEY_OVERRIDE='NONE', PROGRESS_OUTCOME='NONE')
                assert r.returncode != 0, (r.stdout, r.stderr)
                assert 'blocking finding lacks stable blocker key' in r.stdout+r.stderr, (r.stdout,r.stderr)
                assert len([e for e in c.events() if e['kind']=='plan']) == before
            finally:
                c.close()


def test_human_blocker_with_key_still_stops():
    c = preserved_checkpoint()
    try:
        r = c.run('--extend','1', REVIEW_STATUS='BLOCKED',
                  BLOCKER_KEY_OVERRIDE='source-access', PROGRESS_OUTCOME='NONE')
        assert r.returncode == 2, (r.stdout,r.stderr)
        assert len([e for e in c.events() if e['kind']=='implement']) == 1
        assert c.rows()[0]['state'] == 'archived'
    finally:
        c.close()


def test_candidate_resolution_does_not_bypass_progress_or_publication():
    for fault in ('evidence', 'unknown', 'unpublished'):
        c = Case()
        try:
            c.enqueue('Complete the original objective')
            ok(c.run('1', GIT_FAULT='checkpoint_before_push'))
            if fault != 'unpublished': c.git('push', 'origin', 'checkpoint/test')
            extra = {'BAD_EVIDENCE':'1'} if fault=='evidence' else {}
            r = c.run('--extend','1', REVIEW_STATUS='PROBLEMS', BLOCKER_KEY_OVERRIDE='NONE',
                      PROGRESS_OUTCOME='UNKNOWN' if fault=='unknown' else 'COMPLETE', **extra)
            if fault=='unknown':
                ok(r)
                assert len([e for e in c.events() if e['kind']=='implement']) == 2
                import json
                state=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
                assert state['attempts'][0]['outcome']=='UNKNOWN' and not state['work']['complete']
                continue
            assert r.returncode != 0, (r.stdout,r.stderr)
            assert len([e for e in c.events() if e['kind']=='implement']) == 1
            assert c.rows()[0]['state'] == 'archived'
            expected = {'evidence':'progress evidence hash mismatch', 'unknown':'PROGRESS_UNKNOWN',
                        'unpublished':'local work is not safely checkpointed/published'}[fault]
            assert expected in r.stdout+r.stderr, (r.stdout,r.stderr)
        finally:
            c.close()


def test_progress_unknown_recovery_uses_new_evidence_with_retained_candidate():
    c = preserved_checkpoint()
    try:
        # Preserve coverage of the legacy indeterminate PASS guard. A directed
        # PROBLEMS Review now continues autonomously (covered above).
        r = c.run('--extend','1', REVIEW_STATUS='PASS', BLOCKER_KEY_OVERRIDE='ownership', PROGRESS_OUTCOME='UNKNOWN')
        assert r.returncode == 2 and 'PROGRESS_UNKNOWN' in r.stdout, (r.stdout,r.stderr)
        old = json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        c.enqueue('Read the original implementation log; the exact preserved edits are now checkpointed')
        ok(c.run('--resume', REVIEW_STATUS='PROBLEMS', BLOCKER_KEY_OVERRIDE='NONE',
                 PROGRESS_OUTCOME='VERIFIED', REVIEW_RECOVERY='NEW_EVIDENCE'))
        new = json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert new['block'] is None and len(new['recoveries']) == 1
        assert new['work']['id'] == old['work']['id']
        assert len(new['attempts']) == 2
        assert all(row['state']=='archived' for row in c.rows())
    finally:
        c.close()


def test_cached_blocking_pass_is_rejected_by_canonical_validator():
    c = Case()
    try:
        ok(c.run('1'))
        context = subprocess.check_output([sys.executable,str(c.p/'progress.py'),'context'], cwd=c.repo, env=c.env, text=True)
        context = json.loads(context)
        report = {'work_id':context['work']['id'], 'attempt_id':context['attempt']['id'],
                  'finding_key':'original', 'blocking':True, 'blocking_reason':'verification is unavailable',
                  'outcome':'UNKNOWN', 'reason':'unverified', 'evidence':[], 'recovery':'NONE'}
        path = c.a/'invalid-answer'
        path.write_text('REVIEW_STATUS: PASS\nBLOCKER_KEY: actual-blocker\nAUTOCYCLE_REVIEW: '+json.dumps(report)+'\n')
        r = subprocess.run([sys.executable,str(c.p/'progress.py'),'validate-review',str(path)],cwd=c.repo,env=c.env,capture_output=True,text=True)
        assert r.returncode != 0 and 'blocking finding requires PROBLEMS or BLOCKED' in r.stderr, (r.stdout,r.stderr)
    finally:
        c.close()


if __name__=='__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]()
        print('PASS '+name,flush=True)

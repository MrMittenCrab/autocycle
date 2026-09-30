"""A saved BLOCKED Review is re-evaluated once, within its existing cycle."""
import json
from test_flow import Case, ok, fail


def test_blocked_review_retries_once_and_preserves_identity():
    c = Case()
    try:
        ok(c.run('1'))
        fail(c.run('--extend', '1', REVIEW_STATUS='BLOCKED'))
        before = (c.a/'resume-state').read_text()
        work = json.loads((c.a/'work-state.json').read_text())
        head = c.git('rev-parse', 'HEAD')
        cache = (c.a/'current-review').read_text()
        result = c.run('--resume', REVIEW_STATUS='BLOCKED', BLOCKER_KEY_OVERRIDE='new-external-blocker')
        fail(result)
        assert [e['kind'] for e in c.events()] == ['review','plan','implement','review','review']
        assert c.events()[-1]['cycle'] == '2'
        assert 'Cycle       2/2' in result.stdout
        assert 'Review      ✓ resumed' not in result.stdout
        assert (c.a/'resume-state').read_text() == before
        assert c.git('rev-parse', 'HEAD') == head
        after = json.loads((c.a/'work-state.json').read_text())
        old_branch = work['branches']['checkpoint/test']
        new_branch = after['branches']['checkpoint/test']
        assert new_branch['work'] == old_branch['work']
        assert new_branch['allocated'] == old_branch['allocated']
        assert [a['id'] for a in new_branch['attempts']] == [a['id'] for a in old_branch['attempts']]
        for old, new in zip(old_branch['attempts'], new_branch['attempts']):
            assert {k:v for k,v in old.items() if k != 'review_token'} == {k:v for k,v in new.items() if k != 'review_token'}
        assert (c.a/'current-review').read_text() != cache
        assert 'BLOCKER_KEY: new-external-blocker' in (c.a/'current-review').read_text()
        result = c.run('--resume')
        ok(result)
        assert [e['kind'] for e in c.events()][-3:] == ['review','plan','implement']
        assert c.events()[-3]['cycle'] == '2'
        assert 'RUN_CYCLE=2' in (c.a/'resume-state').read_text()
        assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
    finally:
        c.close()


def test_fresh_blocked_review_does_not_trigger_progress_recheck():
    c = Case()
    try:
        ok(c.run('1'))
        fail(c.run('--extend','1',REVIEW_STATUS='BLOCKED'))
        count = len(c.events())
        result = c.run('--resume',REVIEW_STATUS='BLOCKED',PROGRESS_OUTCOME='UNKNOWN')
        fail(result)
        assert len(c.events()) == count+1
        assert '    ✓ Found   Blocked:' in result.stdout
        assert 'STAGE=review_done' in (c.a/'resume-state').read_text()
    finally:
        c.close()


if __name__ == '__main__':
    test_blocked_review_retries_once_and_preserves_identity()
    test_fresh_blocked_review_does_not_trigger_progress_recheck()
    print('PASS BLOCKED Review retries once, preserves identity, and continues after resolution')

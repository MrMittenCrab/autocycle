"""Opening Review admission with a real controller, Git and durable state."""
import json
import subprocess
import sys
from test_flow import ok
from test_session import case


def ledger(c):
    return json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']


def progress(c, *args):
    return subprocess.run([sys.executable, str(c.p/'progress.py'), *args],
                          cwd=c.repo, env=c.env, capture_output=True, text=True)


def opening_boundary():
    c = case()
    # Admit a real published Plan, but stop before implementation/checkpoint.
    r = c.run('1', FAIL_STAGE='implement')
    assert r.returncode != 0
    a = ledger(c)['attempts'][-1]
    assert a['kind'] == 'implementation' and a['phase'] == 'running'
    assert a['checkpoint_sha'] is None and a['outcome'] is None
    # Reconstruct the saved opening-Review boundary in this isolated fixture.
    # Keep the real Plan, attempt, ownership baseline and frozen input batch.
    state = c.a/'resume-state'
    assert 'STAGE=implementing' in state.read_text()
    state.write_text(state.read_text().replace('STAGE=implementing', 'STAGE=start'))
    return c


def review(c, **changes):
    s = ledger(c)
    import hashlib
    report = dict(work_id=s['work']['id'], attempt_id=s['attempts'][-1]['id'],
                  finding_key=s['work']['finding_key'], blocking=False,
                  blocking_reason='NONE', outcome='UNASSESSED',
                  reason='no checkpointed attempt exists to assess.', evidence=[],
                  endpoint=dict(status='UNREACHED', evidence=[], session_sha256=
                      hashlib.sha256((c.repo/'SESSION.md').read_bytes()).hexdigest()))
    report.update(changes)
    path = c.p/'opening-review'
    path.write_text('REVIEW_STATUS: PASS\nREVIEW_TOKEN: opening-decision\n'
                    'AUTOCYCLE_REVIEW: '+json.dumps(report)+'\n')
    return path


def continuation(c):
    path = c.p/'continuation.md'
    path.write_bytes((c.repo/'IMPLEMENTATION.md').read_bytes())
    ok(progress(c, 'prepare', str(path)))
    (c.repo/'IMPLEMENTATION.md').write_bytes(path.read_bytes())
    c.git('add', 'IMPLEMENTATION.md'); c.git('commit', '-qm', 'Plan: continuation')
    c.git('push', '-q')
    return c.git('rev-parse', 'HEAD')


def test_opening_unassessed_is_durable_before_plan_and_next_begin():
    c = opening_boundary()
    try:
        old = ledger(c)['attempts'][-1]
        # Observe real on-disk state before the Plan provider runs.
        provider = c.bin/'codex'
        provider.write_text(provider.read_text().replace("'kind':kind,",
            "'cache':(a/'current-review').read_text() if (a/'current-review').exists() else '','ledger':json.loads((a/'work-state.json').read_text()),'kind':kind,"))
        count = len(c.events())
        r = c.run('--resume'); ok(r)
        events = c.events()[count:]
        assert [e['kind'] for e in events] == ['review', 'plan', 'implement']
        at_plan = events[1]['ledger']['branches']['checkpoint/test']['attempts'][-1]
        assert at_plan['id'] == old['id'] and at_plan['work_id'] == old['work_id']
        assert at_plan['outcome'] == 'UNASSESSED' and at_plan['checkpoint_sha'] is None
        assert at_plan['report']['attempt_id'] == old['id']
        assert at_plan['report']['outcome'] == 'UNASSESSED'
        token = next(x[14:] for x in events[1]['cache'].splitlines()
                     if x.startswith('REVIEW_TOKEN: '))
        assert at_plan['review_token'] == token
        s = ledger(c)
        assert len(s['attempts']) == 2
        assert s['attempts'][0] == at_plan
        assert s['attempts'][1]['id'] != old['id']
        assert s['attempts'][1]['work_id'] == old['work_id']
        assert s['attempts'][1]['phase'] == 'checkpointed'
        assert s['work']['step_id'] == '1.1'
        assert c.git('rev-parse', 'HEAD') == c.git('rev-parse', 'origin/checkpoint/test')
    finally:
        c.close()


def test_validation_alone_or_no_review_cannot_admit():
    c = opening_boundary()
    try:
        sha = continuation(c)
        for validate in (False, True):
            if validate: ok(progress(c, 'validate-review', str(review(c))))
            before = (c.a/'work-state.json').read_bytes()
            r = progress(c, 'begin', sha)
            assert r.returncode != 0 and 'preceding attempt still needs opening Review' in r.stderr
            assert (c.a/'work-state.json').read_bytes() == before
    finally:
        c.close()


def test_invalid_identity_checkpoint_or_missing_token_fails_closed():
    for invalid in ('attempt', 'work', 'checkpoint', 'token'):
        c = opening_boundary()
        try:
            if invalid == 'checkpoint': ok(progress(c, 'checkpoint', 'checkpoint', ''))
            path = review(c, **({'attempt_id':'wrong-attempt'} if invalid == 'attempt' else
                               {'work_id':'wrong-work'} if invalid == 'work' else {}))
            if invalid == 'token':
                path.write_text(path.read_text().replace('REVIEW_TOKEN: opening-decision\n', ''))
            before = (c.a/'work-state.json').read_bytes()
            r = progress(c, 'guard', str(path))
            expected = {'attempt':'wrong attempt', 'work':'work ID',
                        'checkpoint':'checkpointed attempt requires a progress assessment',
                        'token':'unique decision token'}[invalid]
            assert r.returncode != 0 and expected in r.stderr, (r.stdout, r.stderr)
            assert (c.a/'work-state.json').read_bytes() == before
        finally:
            c.close()


def test_accepted_review_is_idempotent_and_cannot_review_next_attempt():
    c = opening_boundary()
    try:
        path = review(c)
        ok(progress(c, 'guard', str(path)))
        accepted = ledger(c)['attempts'][-1]
        assert accepted['outcome'] == 'UNASSESSED'
        ok(progress(c, 'guard', str(path)))
        assert ledger(c)['attempts'][-1] == accepted
        sha = continuation(c)
        ok(progress(c, 'begin', sha))
        ok(progress(c, 'begin', sha))  # Same admitted implementation is adopted.
        before = (c.a/'work-state.json').read_bytes()
        r = progress(c, 'guard', str(path))
        assert r.returncode != 0 and 'wrong attempt' in r.stderr
        assert (c.a/'work-state.json').read_bytes() == before
        assert ledger(c)['attempts'][-1]['outcome'] is None
        r = progress(c, 'begin', continuation(c))
        assert r.returncode != 0 and 'preceding attempt still needs opening Review' in r.stderr
    finally:
        c.close()


def test_unassessed_review_preserves_external_block_and_resume():
    c = opening_boundary()
    try:
        count = len(c.events())
        r = c.run('--resume', REVIEW_STATUS='BLOCKED')
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert [e['kind'] for e in c.events()[count:]] == ['review']
        assert len(ledger(c)['attempts']) == 1
        assert 'REVIEW_STATUS: BLOCKED' in (c.a/'current-review').read_text()
        count = len(c.events())
        ok(c.run('--resume'))
        assert [e['kind'] for e in c.events()[count:]] == ['review', 'plan', 'implement']
        assert len(ledger(c)['attempts']) == 2
    finally:
        c.close()



def test_uncheckpointed_progress_block_requires_reviewed_recovery():
    c = opening_boundary()
    try:
        # Reconstruct a retained progress block in the isolated saved ledger.
        path = c.a/'work-state.json'
        data = json.loads(path.read_text())
        s = data['branches']['checkpoint/test']; a = s['attempts'][-1]
        block = dict(reason='PROGRESS_UNKNOWN', work_id=a['work_id'], attempt_id=a['id'],
                     instruction_ids=[], evidence_key='', review_token='blocked-decision')
        s['block'] = block
        path.write_text(json.dumps(data))
        r = progress(c, 'guard', str(review(c)))
        assert r.returncode == 2 and 'PROGRESS_UNKNOWN' in r.stdout
        assert ledger(c)['block'] == block
        assert ledger(c)['attempts'][-1]['outcome'] is None
        ident = c.enqueue('Use a newly specified concrete repair approach')
        batch = 'f'*32
        ok(subprocess.run([sys.executable, str(c.p/'instructions.py'), 'rollover', a['batch'], batch,
                           'checkpoint/test', c.git('rev-parse', 'HEAD')],
                          cwd=c.repo, env=c.env, capture_output=True, text=True))
        answer = review(c, recovery='REVISED_APPROACH', recovery_instruction_ids=[ident],
                        recovery_reason='Use the newly specified concrete repair approach',
                        direction={'implementation':[ident]})
        answer.write_text(answer.read_text()+'INPUT_STATUS: PENDING\n')
        for _ in range(2):  # Controller guard, then Plan guard.
            ok(progress(c, 'guard', str(answer), batch))
            assert ledger(c)['block'] is None
            assert ledger(c)['attempts'][-1]['outcome'] == 'RECOVERED'
        assert len(ledger(c)['recoveries']) == 1
        ok(progress(c, 'begin', continuation(c), batch))
        assert len(ledger(c)['attempts']) == 2
    finally:
        c.close()


if __name__ == '__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name](); print('PASS', name, flush=True)

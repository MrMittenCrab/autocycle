"""Implementation message chunks retain semantic boundaries, including on failure."""
from test_flow import Case,ok


def test_fragmented_final_message_is_accepted_after_commentary():
    c=Case()
    try:
        c.enqueue('Complete the actual work')
        r=c.run('1',FRAGMENTED_RESPONSE='1');ok(r)
        assert c.git('status','--porcelain')==''
        assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
        assert c.rows()[0]['state']=='archived'
        assert len([e for e in c.events() if e['kind']=='implement'])==1
        ok(c.run('--extend','1',REVIEW_STATUS='DONE'))
        assert c.rows()[0]['state']=='archived'
    finally:c.close()


def test_fragmented_blocker_is_preserved_for_review():
    c=Case()
    try:
        ok(c.run('1',FRAGMENTED_RESPONSE='1',IMPLEMENT_BLOCKED='1'))
        assert (c.a/'candidate.json').exists()
        assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
    finally:c.close()


def test_abnormal_stop_still_rejects_and_flushes_log():
    c=Case()
    try:
        r=c.run('1',FRAGMENTED_RESPONSE='1',IMPLEMENT_STOP_REASON='cancelled')
        assert r.returncode!=0,(r.stdout,r.stderr)
        log=next(c.a.glob('cursor-*.log')).read_text()
        assert '[result] stopReason=cancelled' in log,log[-1000:]
        assert '[error]' in log,log[-1000:]
        assert c.git('status','--porcelain')
        assert 'STAGE=implementing' in (c.a/'resume-state').read_text()
    finally:c.close()


if __name__=='__main__':
    import sys
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS '+name,flush=True)

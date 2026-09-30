"""Interrupted dirty implementation must not rewrite its original ownership baseline."""
import json
import subprocess
import sys
from test_flow import Case, ok, fail


def adjudicate(c, *args):
    return subprocess.run([sys.executable, str(c.p/'adjudication.py'), *args],
                          cwd=c.repo, env=c.env, capture_output=True, text=True)


def test_dirty_interruption_stops_before_reexecution():
    for paused_edit in ('none','working-tree','index-only'):
        c=Case()
        try:
            c.enqueue('Finish the original objective')
            p=c.start('2', BLOCK_STAGE='implement-edited')
            c.ready(p)
            baseline=(c.a/'implementation-baseline.json').read_bytes()
            assert json.loads(baseline)['clean'] is True
            assert c.git('status','--porcelain')
            c.kill(p)
            if paused_edit=='working-tree':
                (c.repo/'private-manual.txt').write_text('Unrelated private draft')
            elif paused_edit=='index-only':
                saved=(c.repo/'RESULT.md').read_bytes()
                (c.repo/'RESULT.md').write_text('Staged manual changes')
                c.git('add','RESULT.md')
                (c.repo/'RESULT.md').write_bytes(saved)
            head=c.git('rev-parse','HEAD')
            files={p.name:p.read_bytes() for p in c.repo.iterdir() if p.is_file()}
            index=c.git('ls-files','--stage')
            events=c.events()
            batch=c.rows()[0]['batch']
            for _ in range(2):
                r=c.run('--resume')
                fail(r)
                assert c.events()==events, 'Uncertain resume must stop before rerunning providers'
                assert c.git('rev-parse','HEAD')==head==c.git('rev-parse','origin/checkpoint/test')
                assert {p.name:p.read_bytes() for p in c.repo.iterdir() if p.is_file()}==files
                assert c.git('ls-files','--stage')==index
                assert (c.a/'implementation-baseline.json').read_bytes()==baseline
                assert c.rows()[0]['state']=='archived' and c.rows()[0]['batch']==batch
        finally:c.close()


def test_baseline_is_immutable_when_dirty_work_resumes():
    c=Case()
    try:
        ok(adjudicate(c,'baseline'))
        before=(c.a/'implementation-baseline.json').read_bytes()
        (c.repo/'RESULT.md').write_text('Interrupted work')
        fail(adjudicate(c,'baseline'))
        assert (c.a/'implementation-baseline.json').read_bytes()==before
        # No provider completed, so no result authorizes an automatic checkpoint.
        fail(adjudicate(c,'checkpoint-safe'))
    finally:c.close()


def test_preexisting_dirty_baseline_is_never_promoted():
    for kind in ('working-tree','index-only','legacy-uncertain'):
        c=Case()
        try:
            if kind=='working-tree':
                (c.repo/'manual.txt').write_text('Pre-existing manual work')
            elif kind=='index-only':
                original=(c.repo/'TARGET.md').read_bytes()
                (c.repo/'TARGET.md').write_text('Staged manual work')
                c.git('add','TARGET.md')
                (c.repo/'TARGET.md').write_bytes(original)
            initial=adjudicate(c,'baseline')
            if kind=='legacy-uncertain':ok(initial)
            else:fail(initial)
            if kind=='legacy-uncertain':
                # Old releases could destroy clean=true on resume. A matching
                # file map cannot prove that the original index was clean.
                b=json.loads((c.a/'implementation-baseline.json').read_text())
                b['clean']=False
                (c.a/'implementation-baseline.json').write_text(json.dumps(b))
            before=(c.a/'implementation-baseline.json').read_bytes()
            assert json.loads(before)['clean'] is False
            (c.repo/'RESULT.md').write_text('Finished implementation')
            fail(adjudicate(c,'baseline'))
            assert (c.a/'implementation-baseline.json').read_bytes()==before
            ok(adjudicate(c,'implementation-result'))
            fail(adjudicate(c,'checkpoint-safe'))
        finally:c.close()


def test_edits_after_completed_provider_still_block_checkpoint():
    c=Case()
    try:
        ok(adjudicate(c,'baseline'))
        (c.repo/'RESULT.md').write_text('Completed provider work')
        ok(adjudicate(c,'implementation-result'))
        ok(adjudicate(c,'checkpoint-safe'))
        (c.repo/'private-manual.txt').write_text('Later external work')
        fail(adjudicate(c,'checkpoint-safe'))
    finally:c.close()


def test_existing_manual_checkpoint_is_not_recommitted():
    c=Case()
    try:
        ok(c.run('1'))
        state=c.a/'resume-state'
        state.write_text(state.read_text().replace('STAGE=checkpoint_done','STAGE=implement_done'))
        head=c.git('rev-parse','HEAD');events=c.events()
        r=c.run('--resume');ok(r)
        assert c.git('rev-parse','HEAD')==head
        assert c.events()==events
        assert 'existing commit preserved' in r.stdout
        assert 'STAGE=checkpoint_done' in state.read_text()
    finally:c.close()


if __name__=='__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]()
        print('PASS '+name,flush=True)

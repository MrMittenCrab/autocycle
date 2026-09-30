"""Opening Review and restart isolation through the real controller."""
import json,re
from test_session import case,SESSION
from test_flow import ok


def context(event):
    return json.loads(next(line.split(': ',1)[1] for line in event['prompt'].splitlines()
                          if line.startswith('AUTOCYCLE_WORK_CONTEXT: ')))


def test_fresh_and_restarted_opening_review():
    for restart in (False,True):
        c=case(False)
        try:
            archived={}
            if restart:
                r=c.run('3',PROGRESS_OUTCOME='UNKNOWN');assert r.returncode==2
                old=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
                assert old['block']
                (c.a/'candidate.json').write_text('{"reason":"old Session blocker"}')
                for name in ('candidate.json','implementation-baseline.json','implementation-result.json','latest-implementation'):
                    if (c.a/name).exists():archived[name]=(c.a/name).read_bytes()
            before=len(c.events()) if restart else 0
            new=SESSION.replace('working feature','new capability').replace('End-to-end capability','New delivery priority')
            args=('1','--restart') if restart else ('1',)
            r=c.run(*args,NEW_SESSION=new);ok(r)
            events=c.events()[before:]
            assert [e['kind'] for e in events]==['review','plan','implement']
            opening=context(events[0]);planning=context(events[1])
            for data in (opening,planning):
                assert data['work'] is None and data['attempt'] is None
                assert data['block'] is None and data['stagnation']==0
            number=2 if restart else 1
            assert opening['session_number']==number
            assert re.search(r'Review\s+✓ [^\n]+\n    ✓ Found   verified\n',r.stdout),r.stdout
            assert (c.repo/'IMPLEMENTATION.md').read_text().startswith(f'# Step {number}.1 ')
            assert (c.repo/'SESSION.md').read_text()==new
            if restart:
                ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
                assert ledger['last_session_end']['status']=='restarted'
                assert ledger['deferred'][-1]['work']['complete'] is False
                assert ledger['deferred'][-1]['block']==old['block']
                for name,data in archived.items():assert (c.a/'session-history/1'/name).read_bytes()==data
                assert not (c.a/'candidate.json').exists()
                assert 'Completed:' not in r.stdout
        finally:c.close()


def test_invalid_opening_review_stays_failed_with_child_reason():
    for malformed in (False,True):
        c=case(False)
        try:
            if not malformed:
                provider=c.bin/'codex'
                provider.write_text(provider.read_text().replace("'outcome':outcome,", "'outcome':'VERIFIED',"))
            r=c.run('1',**({'DROP_PROGRESS_REPORT':'1'} if malformed else {}))
            assert r.returncode!=0
            assert [e['kind'] for e in c.events()]==['review']*3
            assert re.search(r'Review\s+✗ [^\n]*invalid progress evidence; [^\n]+',r.stdout),r.stdout
            assert '✓' not in r.stdout.split('Review',1)[1].split('\n',1)[0]
            ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
            assert ledger['block'] is None and ledger['attempts']==[]
        finally:c.close()


if __name__=='__main__':
    for name,fn in list(globals().items()):
        if name.startswith('test_'):fn();print('PASS',name)

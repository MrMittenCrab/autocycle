"""Cycle 112: Cursor edits files, then ends without status after [unavailable].

Exercise the real controller, ACP parser, ownership receipts and Git checkpoint.
Only providers are fake; Work seeds admitted 2.14/2.14.1 with reviewed history.
"""
import json
import shlex
import sys
from pathlib import Path

import progress
from test_completion import Work, COMPLETION
from test_flow import ok, fail
from test_network_resume import fixture, ERROR
from test_session import SESSION


def ledger(c):
    return json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']


def setup(x, minor=False):
    c=x.c
    (c.repo/'feature.py').write_text('value = 0\n')
    x.plan()
    x.review('NONE')
    (c.a/'prior-implementation.log').write_text('IMPLEMENT_STATUS: COMPLETE\n')
    (c.a/'latest-implementation').write_text('HEAD: '+x.s['attempts'][-1]['plan_sha']+'\nBRANCH: checkpoint/test\nLOG: '+str(c.a/'prior-implementation.log')+'\n')
    (c.repo/'SESSION.md').write_text(SESSION)
    x.plan(title='Finish the bounded repair', **({'minor':1} if minor else {}))
    (c.a/'work-state.json').write_text(json.dumps({'version':progress.VERSION,'branches':{'checkpoint/test':x.s}}))
    sha=c.git('rev-parse','HEAD')
    state=dict(STATE_VERSION=2, SESSION_NUMBER=2, SESSION_NEW=0,
               BOUNDARY_KIND='checkpoint', NEXT_INPUT_ID='', CHECKPOINT_UNSAFE=0,
               NETWORK_RETRIES=0, CANDIDATE_RESOLVED=0, RUN_MAX=1, RUN_CYCLE=1,
               STAGE='implementing', PLAN_SHA=sha, IMPLEMENT_BASE_SHA=sha,
               STATE_BRANCH='checkpoint/test', INPUT_CYCLE_ID='', INPUT_ALLOW_NEW=0)
    (c.a/'resume-state').write_text(''.join(f'{k}={shlex.quote(str(v))}\n' for k,v in state.items()))
    fixture(c)
    agent=c.bin/'agent'
    text=agent.read_text()
    marker="  if os.environ.get('NO_CHANGE')!='1':"
    # Observe provider entry before any writes, including on retries and resume.
    insertion="""  before={'state':json.loads((a/'work-state.json').read_text()),
          'plan':(root/'IMPLEMENTATION.md').read_text(),
          'session':(root/'SESSION.md').read_text(),
          'feature':(root/'feature.py').read_text(),
          'partial':(root/'partial.txt').read_text() if (root/'partial.txt').exists() else None}
  with (a/'provider-entry.jsonl').open('a') as f:f.write(json.dumps(before)+'\\n')
  if not (root/'partial.txt').exists():
   (root/'feature.py').write_text('value = 41\\n')
   (root/'partial.txt').write_text('Retain valid partial work\\n')
  else:
   assert (root/'feature.py').read_text()=='value = 41\\n'
   assert (root/'partial.txt').read_text()=='Retain valid partial work\\n'
"""
    assert marker in text
    agent.write_text(text.replace(marker,insertion+marker))
    # Record and reject destructive Git operations even if their damage is hidden
    # by a later provider write. Reads and normal add/commit/push remain real.
    git=c.bin/'git'
    git.write_text('#!/usr/bin/env python3\n'+"""import os,sys,subprocess
from pathlib import Path
args=sys.argv[1:]
if args and (args[0] in ('reset','clean','restore') or (args[0]=='checkout' and '--' in args)):
 Path('.git/autocycle/unsafe-git').write_text(repr(args));sys.exit(99)
sys.exit(subprocess.run([os.environ['REAL_GIT'],*args]).returncode)
""")
    return sha


def run_recovery(minor=False, blocked=False, error=ERROR):
    with Work() as x:
        c=x.c
        sha=setup(x,minor)
        original=ledger(c)
        prior_evidence=(c.a/'latest-implementation').read_bytes()
        expected_step='2.14.1' if minor else '2.14'
        assert original['work']['step_id']==expected_step
        assert progress.stagnation(original)==1
        docs={n:(c.repo/n).read_bytes() for n in ('TARGET.md','SESSION.md','IMPLEMENTATION.md')}
        index=c.git('ls-files','--stage')
        r=c.run('--resume',TRANSPORT_FAILURES='99',TRANSPORT_ERROR=error)
        fail(r)
        assert r.returncode==76,(r.stdout,r.stderr)
        assert 'Completed:' not in r.stdout
        assert ledger(c)==original
        state=(c.a/'resume-state').read_text()
        for field in ('STAGE=implementing','SESSION_NUMBER=2','RUN_CYCLE=1','RUN_MAX=1',f'PLAN_SHA={sha}',f'IMPLEMENT_BASE_SHA={sha}'):
            assert field in state
        assert c.git('rev-parse','HEAD')==sha==c.git('rev-parse','origin/checkpoint/test')
        assert c.git('ls-files','--stage')==index
        assert c.git('status','--porcelain')
        assert not (c.a/'unsafe-git').exists()
        assert not (c.a/'candidate.json').exists()
        assert not (c.a/'implementation-result.json').exists()
        assert (c.a/'latest-implementation').read_bytes()==prior_evidence
        receipt=json.loads((c.a/'implementation-interruption.json').read_text())
        assert receipt['snapshot']['head']==sha
        assert 'Provider RetriableError' in Path(receipt['log']).read_text()
        baseline=(c.a/'implementation-baseline.json').read_bytes()
        assert json.loads(baseline)['clean'] is True
        assert {n:(c.repo/n).read_bytes() for n in docs}==docs
        entries=[json.loads(line) for line in (c.a/'provider-entry.jsonl').read_text().splitlines()]
        assert len(entries)==3  # Existing transport retries; no new policy here.
        assert all(e['state']['branches']['checkpoint/test']==original for e in entries)
        assert all(e['plan']==docs['IMPLEMENTATION.md'].decode() and e['session']==SESSION for e in entries)
        assert [e['kind'] for e in c.events()]==['implement']*3

        r=c.run('--resume', **({'IMPLEMENT_BLOCKED':'1'} if blocked else {}));ok(r)
        assert 'Completed:' not in r.stdout
        assert (c.a/'implementation-baseline.json').read_bytes()==baseline
        assert not (c.a/'implementation-interruption.json').exists()
        assert not (c.a/'unsafe-git').exists()
        assert (c.repo/'partial.txt').read_text()=='Retain valid partial work\n'
        assert (c.repo/'feature.py').read_text()=='value = 41\n'
        assert c.git('status','--porcelain')==''
        checkpoint=c.git('rev-parse','HEAD')
        assert checkpoint==c.git('rev-parse','origin/checkpoint/test')
        assert c.git('rev-list','--count',sha+'..HEAD')=='1'
        assert c.git('show','HEAD:partial.txt')=='Retain valid partial work'
        assert {n:(c.repo/n).read_bytes() for n in docs}==docs
        after=ledger(c)
        assert after['work']==original['work'] and after['allocated']==original['allocated']
        assert after['block']==original['block'] is None
        assert len(after['attempts'])==len(original['attempts'])
        attempt=after['attempts'][-1]
        assert attempt['id']==original['attempts'][-1]['id']
        assert attempt['phase']=='checkpointed' and attempt['outcome'] is None
        assert progress.stagnation(after)==1 and not after['work']['complete']
        assert (c.a/'candidate.json').exists()==blocked
        assert (c.a/'implementation-result.json').exists()!=blocked
        assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
        assert 'SESSION_NUMBER=2' in (c.a/'resume-state').read_text()
        entries=[json.loads(line) for line in (c.a/'provider-entry.jsonl').read_text().splitlines()]
        assert entries[-1]['state']['branches']['checkpoint/test']==original
        assert entries[-1]['partial']=='Retain valid partial work\n'
        events=c.events();ok(c.run('--resume'))
        assert c.events()==events and c.git('rev-parse','HEAD')==checkpoint

        # Budget extension admits the next opening Review. Stop its fake provider
        # after it records the real prompt, before it can judge or launch Plan.
        p=c.start('--extend','1',BLOCK_STAGE='review')
        try:
            c.ready(p)
            prompt=c.events()[-1]['prompt']
            context=json.loads(next(line.split(': ',1)[1] for line in prompt.splitlines() if line.startswith('AUTOCYCLE_WORK_CONTEXT: ')))
            assert context['session_number']==2
            assert context['work']['step_id']==expected_step
            assert context['work']['id']==original['work']['id']
            assert context['attempt']['id']==attempt['id']
            assert context['attempt']['checkpoint_sha']==checkpoint
            assert context['stagnation']==1 and context['block'] is None and not context['complete']
            assert 'Read the same IMPLEMENTATION.md' in prompt
            assert progress.completion((c.repo/'IMPLEMENTATION.md').read_text())==COMPLETION
            assert 'latest-implementation' in prompt and 'RESULT.md' in prompt
            assert ('Adjudicate the preserved candidate' in prompt)==blocked
            latest=(c.a/'latest-implementation').read_text()
            assert 'HEAD: '+sha in latest
            log=Path(next(line[5:] for line in latest.splitlines() if line.startswith('LOG: ')))
            assert 'IMPLEMENT_STATUS: '+('BLOCKED' if blocked else 'COMPLETE') in log.read_text()
            assert c.git('rev-parse','HEAD')==checkpoint
            assert ledger(c)==after
        finally:
            c.kill(p)
        # Let Review assess measured recovery as progress without declaring the
        # parent or Endpoint complete. Stop Plan before it publishes new work.
        p=c.start('--resume',BLOCK_STAGE='plan',PROGRESS_OUTCOME='VERIFIED')
        try:
            c.ready(p)
            reviewed=ledger(c)
            assert reviewed['work']['step_id']==expected_step
            assert not reviewed['work']['complete'] and reviewed['block'] is None
            assert reviewed['attempts'][-1]['id']==attempt['id']
            assert reviewed['attempts'][-1]['outcome']=='VERIFIED'
            assert progress.stagnation(reviewed)==0
            assert c.git('rev-parse','HEAD')==checkpoint
            assert {n:(c.repo/n).read_bytes() for n in docs}==docs
        finally:
            c.kill(p)


def test_historical_unavailable_recovers_major():
    run_recovery()


def test_historical_unavailable_recovers_substep():
    run_recovery(minor=True)


def test_prior_premature_close_recovers_partial_source_work():
    run_recovery(error='Error: RetriableError: [unknown] Premature close')


def test_real_blocker_after_recovery_reaches_review_as_candidate():
    run_recovery(minor=True,blocked=True)


if __name__=='__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS '+name,flush=True)

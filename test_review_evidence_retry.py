"""Review-only correction uses the real controller and checkpoint ledger."""
import json
import sys
from test_flow import ok
from test_session import case
from test_opening_review import context
from test_resume_progress_state import ledger, instrument

GAP = {'fact': 'Required external measurement is absent', 'material_reason': 'Completion requires this measurement'}
REASON = 'missing required facts require PROBLEMS or evidenced BLOCKED without claiming Completion'


def setup_response(c, mode, always=False):
    c.env['REVIEW_STATUS']='BLOCKED'  # Explicit simulated external measurement dependency.
    provider = c.bin/'codex'
    marker = " extra=[] if os.environ.get('DROP_PROGRESS_REPORT')=='1'"
    addition = '''
 report['unresolved_facts']=[GAP]
 report['blocking']=True;report['blocking_reason']='Required external measurement is absent'
 status='BLOCKED';blocker_key='required-measurement'
 human_action='Provide required measurement';next_step=human_action
 report['outcome']='UNKNOWN'
 correcting='Review validation failure:' in prompt
 if ALWAYS or not correcting:
  if MODE=='semantic':report['outcome']='VERIFIED'
  elif MODE=='complete':report['outcome']='COMPLETE'
  elif MODE=='schema':report['blocking']='true'
  elif MODE=='envelope':status='INVALID'
'''.replace('GAP', repr(GAP)).replace('ALWAYS', repr(always)).replace('MODE', repr(mode))
    assert marker in provider.read_text()
    source=provider.read_text().replace(marker, addition+marker)
    if mode=='json':
        source=source.replace("['AUTOCYCLE_REVIEW: '+json.dumps(report)]",
                              "['AUTOCYCLE_REVIEW: '+(json.dumps(report) if correcting else '{invalid')]")
    provider.write_text(source)


def test_invalid_review_retries_without_replaying_implementation():
    for mode, reason in [('semantic', REASON), ('complete', REASON), ('schema', 'blocking must be boolean'),
                         ('json', 'invalid progress evidence;'), ('envelope', 'invalid Codex review')]:
        c=case()
        try:
            instrument(c);ok(c.run('1'))
            before=ledger(c);head=c.git('rev-parse','HEAD')
            files={p:(c.repo/p).read_bytes() for p in ('IMPLEMENTATION.md','SESSION.md','RESULT.md')}
            setup_response(c,mode);count=len(c.events())
            r=c.run('--extend','1',PROGRESS_OUTCOME='VERIFIED')
            assert r.returncode==2,(r.stdout,r.stderr)
            events=c.events()[count:]
            assert [e['kind'] for e in events]==['review','review'],r.stdout
            assert reason in events[1]['prompt']
            assert context(events[0])==context(events[1])
            assert events[0]['state']==events[1]['state']
            after=ledger(c)
            assert after['work']['id']==before['work']['id'] and not after['work']['complete']
            assert len(after['attempts'])==1
            assert after['attempts'][0]['checkpoint_sha']==before['attempts'][0]['checkpoint_sha']
            assert after['attempts'][0]['outcome']=='UNKNOWN'
            assert after['unresolved_facts'][0]==GAP
            assert c.git('rev-parse','HEAD')==head
            assert all((c.repo/p).read_bytes()==data for p,data in files.items())
        finally:c.close()


def test_exhaustion_keeps_checkpoint_resumable():
    c=case()
    try:
        instrument(c);ok(c.run('1'))
        before=ledger(c);cache=c.a/'current-review'
        assert not cache.exists()
        provider=(c.bin/'codex').read_text()
        setup_response(c,'semantic',always=True);count=len(c.events())
        r=c.run('--extend','1',PROGRESS_OUTCOME='VERIFIED')
        assert r.returncode!=0
        events=c.events()[count:]
        assert [e['kind'] for e in events]==['review']*3,r.stdout
        assert REASON in events[-1]['prompt'] and REASON in r.stdout
        failures=list(c.a.glob('review-*.validation'))
        assert len(failures)==3
        assert all(REASON in p.read_text() and p.with_suffix('.answer').is_file() for p in failures)
        assert ledger(c)==before
        assert not cache.exists()
        assert 'STAGE=reviewing' in (c.a/'resume-state').read_text()
        assert all(context(e)==context(events[0]) and e['state']==events[0]['state'] for e in events)
        (c.bin/'codex').write_text(provider)
        setup_response(c,'schema');count=len(c.events())
        r=c.run('--resume',PROGRESS_OUTCOME='VERIFIED')
        assert r.returncode==2,(r.stdout,r.stderr)
        assert [e['kind'] for e in c.events()[count:]]==['review','review']
        assert ledger(c)['attempts'][0]['id']==before['attempts'][0]['id']
    finally:c.close()


def test_verified_evidence_without_gaps_is_accepted():
    c=case()
    try:
        ok(c.run('1'));count=len(c.events())
        ok(c.run('--extend','1',PROGRESS_OUTCOME='VERIFIED'))
        assert [e['kind'] for e in c.events()[count:]]==['review','plan','implement']
        assert ledger(c)['attempts'][0]['outcome']=='VERIFIED'
    finally:c.close()


if __name__=='__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS',name,flush=True)

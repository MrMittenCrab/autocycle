"""Prospective authority: isolated repositories, fake providers and mocked Office."""
import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch
import adjudication as a
import native_office as office
from test_flow import Case, ok, fail
from test_native_office_flow import prepare, native_handoff_provider, observer_provider


def parser(text, tasks):
    script="const p=require(process.argv[1]); console.log(JSON.stringify(p.nativeHandoff(JSON.parse(process.argv[2]),JSON.parse(process.argv[3]))));"
    return json.loads(subprocess.check_output(['node','-e',script,str(Path(__file__).with_name('implementation_response.js')),json.dumps(text),json.dumps(tasks)],text=True))


def test_declaration_protocol():
    tasks=[dict(content='Implemented source',status='completed'),dict(content='Controller capture delegated',status='cancelled')]
    ident='a'*32
    for text in ('', 'controller capture pending', 'NATIVE_HANDOFF: {}', 'NATIVE_HANDOFF: nope',
                 'NATIVE_HANDOFF: {"requests":[]}', 'NATIVE_HANDOFF: {"requests":["../path"]}',
                 'NATIVE_HANDOFF: '+json.dumps({'requests':[ident,ident]}),
                 ('NATIVE_HANDOFF: '+json.dumps({'requests':[ident]})+'\n')*2):
        assert not parser(text,tasks)['valid'],text
    assert parser('NATIVE_HANDOFF: '+json.dumps({'requests':[ident]}),tasks)['valid']
    tasks[1]['content']='Ordinary cancelled task'
    assert parser('',tasks)['valid']
    assert not parser('NATIVE_HANDOFF: broken',tasks)['valid']


def test_generic_todos_through_implement():
    for mode in ('ordinary-cancelled','pending','all-cancelled','no-todos'):
        c=Case()
        try:
            agent=c.bin/'agent';script=agent.read_text()
            if mode=='all-cancelled':script=script.replace("statuses=['completed','cancelled']", "statuses=['cancelled','cancelled']")
            if mode=='no-todos':script=script.replace("statuses=['completed','cancelled']", "statuses=[]")
            agent.write_text(script)
            result=c.run('1',TODO_PENDING='1' if mode=='pending' else '0')
            if mode=='ordinary-cancelled':
                ok(result)
                assert not (c.a/'native-handoff.json').exists()
            else:
                fail(result)
                assert not (c.a/'implementation-result.json').exists()
        finally:c.close()


def test_controller_request_validation():
    from test_native_office import FakeOffice
    c=Case();previous=Path.cwd()
    try:
        prepare(c);fail(c.run('1',FAIL_STAGE='implement'))
        os.chdir(c.repo)
        with patch.dict(os.environ,c.git_env):
            workspace=office.Workspace(c.a,FakeOffice())
            source=c.a/'source.docx';source.write_bytes(b'source')
            base={'app':'word','source':str(source),'source_sha256':office.digest(source)}
            def submit(value,caller='Implement'):
                a.observation_request(caller,'OBSERVER_REQUEST: '+json.dumps({'requests':[value]}))
            for mutation in ({'app':'powerpoint'}, {'source_sha256':'0'*64}, {'source':'/missing.docx'},
                             {'zoom':0}, {'operation':'invented'}, {'attempt_id':'arbitrary'}, {'requested_head':'0'*40}):
                try:submit(dict(base,**mutation))
                except (ValueError,RuntimeError,KeyError,OSError):pass
                else:raise AssertionError('accepted '+repr(mutation))
                assert not (c.a/'observer-call.json').exists()
            try:submit(base,'Plan')
            except ValueError:pass
            else:raise AssertionError('Planner called Observer')
            old=office.enqueue(workspace,{'app':'word','source':str(source)},c.git('rev-parse','HEAD'))
            old_bytes=old.read_bytes();submit(base)
            record=a.observation_call();ident=record['requests'][0]['id']
            assert ident!=old.stem and old.read_bytes()==old_bytes
            for key in ('head','index'):
                changed=json.loads(json.dumps(record));changed['snapshot'][key]='changed'
                a.atomic(c.a/'observer-call.json',changed)
                try:a.observation_call()
                except ValueError:pass
                else:raise AssertionError('changed '+key+' accepted')
            a.atomic(c.a/'observer-call.json',record)
            request=workspace.root/'requests'/(ident+'.json');raw=request.read_bytes()
            request.chmod(0o600);request.write_bytes(raw+b' ')
            try:a.observation_call()
            except ValueError:pass
            else:raise AssertionError('mutated request accepted')
            request.write_bytes(raw)
            source.write_bytes(b'changed')
            try:a.observation_call()
            except ValueError:pass
            else:raise AssertionError('changed source accepted')
            source.write_bytes(b'source')
            office.process_requests(workspace,('word',),c.git('rev-parse','HEAD'),[ident])
            a.observation_result();returned=a.observation_call(returned=True)
            assert returned['snapshot']['identity']['binding']['attempt']['id']
            receipt=workspace.root/'receipts'/(ident+'.json');raw=receipt.read_bytes()
            receipt.chmod(0o600);receipt.write_bytes(raw+b' ')
            try:a.observation_consume('Implement')
            except ValueError:pass
            else:raise AssertionError('mutated receipt accepted')
            receipt.write_bytes(raw);a.observation_consume('Implement')
            assert not (c.a/'observer-call.json').exists()
            assert not (c.a/'native-handoff.json').exists()
    finally:os.chdir(previous);c.close()


def test_authenticated_legacy_handoff_reader():
    c=Case();previous=Path.cwd()
    try:
        prepare(c);fail(c.run('1',FAIL_STAGE='implement'))
        os.chdir(c.repo)
        with patch.dict(os.environ,c.git_env):
            source=c.a/'legacy.docx';source.write_bytes(b'old admitted capture')
            snapshot=a.native_snapshot()
            queued=office.enqueue(office.Workspace(c.a),{'app':'word','source':str(source)},snapshot['head'])
            record={'identity':snapshot['identity'],'head':snapshot['head'],
                    'requests':[{'id':queued.stem,'request_sha256':a.digest(queued.read_bytes()),'source_sha256':office.digest(source)}],
                    'state':'capture pending','evidence':'unverified'}
            # Synthetic saved-state fixture; production no longer writes this form.
            a.atomic(c.a/'native-handoff.json',record)
            a.atomic(c.a/'implementation-result.json',{'native_handoff_sha256':a.digest((c.a/'native-handoff.json').read_bytes())})
            assert a.native_handoff_ids()==[queued.stem]
            saved=(c.a/'native-handoff.json').read_bytes();(c.a/'native-handoff.json').unlink()
            try:a.native_handoff_ids()
            except ValueError:pass
            else:raise AssertionError('missing saved handoff swept queue')
            (c.a/'native-handoff.json').write_bytes(saved)
            queued.chmod(0o600);queued.write_bytes(queued.read_bytes()+b' ')
            try:a.native_handoff_ids()
            except ValueError:pass
            else:raise AssertionError('mutated legacy request accepted')
    finally:os.chdir(previous);c.close()


def seed_old(c):
    fail(c.run('1',FAIL_STAGE='implement'))
    source=c.a/'visual-source.docx';source.write_bytes(b'provider-owned Word work')
    path=office.enqueue(office.Workspace(c.a),{'app':'word','source':str(source)},c.git('rev-parse','HEAD'))
    return path,path.read_bytes()


def test_historical_isolation_through_review():
    c=Case()
    try:
        prepare(c);old,raw=seed_old(c);observer_provider(c)
        ok(c.run('--resume'))
        record=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        fresh=record['observations'][0]['request_id']
        assert not (c.a/'native-handoff.json').exists()
        assert fresh!=old.stem and old.read_bytes()==raw
        fresh_request=json.loads((c.a/'office/requests'/(fresh+'.json')).read_text())
        assert fresh_request['source_sha256']==json.loads(raw)['source_sha256']
        ownership=(c.a/'implementation-result.json').read_bytes()
        assert [p.stem for p in (c.a/'office/receipts').glob('*.json')]==[fresh]
        ok(c.run('--extend','1',REVIEW_STATUS='DONE'))
        assert [p.stem for p in (c.a/'office/receipts').glob('*.json')]==[fresh]
        assert old.read_bytes()==raw
        assert (c.a/'implementation-result.json').read_bytes()==ownership
        review=[e for e in c.events() if e['kind']=='review'][-1]['prompt']
        index=json.loads((c.a/'office/review-index.json').read_text())
        assert [entry['request_id'] for entry in index['entries']]==[fresh]
        assert index['entries'][0]['status']=='CAPTURED'
        assert 'Native Office evidence index:' in review
        assert '"status": "CAPTURED"' not in review
        # A completed Session's record stays historical during the next opening Review.
        fail(c.run('1',FAIL_STAGE='plan'))
        assert old.read_bytes()==raw
        assert [p.stem for p in (c.a/'office/receipts').glob('*.json')]==[fresh]
        assert c.events()[-1]['kind']=='plan'
    finally:c.close()



def test_candidate_review_cannot_sweep_legacy_queue():
    c=Case()
    try:
        prepare(c);old,raw=seed_old(c)
        ok(c.run('--resume',IMPLEMENT_BLOCKED='1'))
        assert json.loads((c.a/'candidate.json').read_text())['kind']=='implementation'
        assert not (c.a/'implementation-result.json').exists()
        fail(c.run('--extend','1',REVIEW_STATUS='PROBLEMS',FAIL_STAGE='plan'))
        assert old.read_bytes()==raw
        assert not list((c.a/'office/receipts').glob('*.json'))
        assert c.events()[-1]['kind']=='plan'
    finally:c.close()


def test_false_delegation_through_implement():
    for mode in ('no-declaration','prose','unknown','empty','malformed','old','blocked-malformed','blocked-delegated'):
        c=Case()
        try:
            prepare(c)
            if mode=='old':old,raw=seed_old(c)
            native_handoff_provider(c)
            agent=c.bin/'agent';script=agent.read_text()
            original="if queued:report+='NATIVE_HANDOFF: '+json.dumps({'requests':[Path(queued).stem]})+'\\n'"
            replacement={
                'no-declaration':"if queued:report+='Request submitted\\n'",
                'prose':"if queued:report+='controller capture pending\\n'",
                'unknown':"if queued:report+='NATIVE_HANDOFF: '+json.dumps({'requests':['0'*32]})+'\\n'",
                'empty':"if queued:report+='NATIVE_HANDOFF: {\"requests\":[]}\\n'",
                'malformed':"if queued:report+='NATIVE_HANDOFF: invalid\\n'",
                'old':"if queued:report+='NATIVE_HANDOFF: '+json.dumps({'requests':[%r]})+'\\n'" % (old.stem if mode=='old' else ''),
            }.get(mode, "if queued:report+='NATIVE_HANDOFF: invalid\\n'" if mode=='blocked-malformed' else original)
            assert original in script
            agent.write_text(script.replace(original,replacement))
            fail(c.run('--resume' if mode=='old' else '1', IMPLEMENT_BLOCKED='1' if mode.startswith('blocked-') else '0'))
            assert not (c.a/'implementation-result.json').exists()
            assert not list((c.a/'office/receipts').glob('*.json'))
            if mode=='old':assert old.read_bytes()==raw
        finally:c.close()


if __name__=='__main__':
    import sys
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS '+name,flush=True)

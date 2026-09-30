"""Real controller dispatch and provider handoff, with only macOS mocked."""
import json
import subprocess
from pathlib import Path
from test_flow import BASE, Case, ok, fail


def prepare(c, deny=False, black=False):
    config=c.repo/'.autocycle.toml'
    config.write_text('[capabilities]\nnative_office=["excel","word"]\n')
    c.git('add','.autocycle.toml');c.git('commit','-qm','Enable native Office');c.git('push','-q')
    (c.p/'test_native_office.py').write_bytes((BASE/'test_native_office.py').read_bytes())
    path=c.p/'native_office.py';text=path.read_text()
    injection='''if __name__=='__main__':
    from test_native_office import FakeOffice
    class FixtureOffice(FakeOffice):
        verify_word_rendered = staticmethod(MacOffice.verify_word_rendered)
        def __init__(self):
            super().__init__()
            self.denied = DENY
            self.black = BLACK
        def open(self,app,path):
            with (Path.cwd()/'.git/autocycle/native-events').open('a') as f:f.write('open '+str(path)+'\\n')
            super().open(app,path)
        def capture(self,path,bounds):
            with (Path.cwd()/'.git/autocycle/native-events').open('a') as f:f.write('capture parent='+str(os.getppid())+'\\n')
            return super().capture(path,bounds)
    MacOffice=FixtureOffice
    original_init=Workspace.__init__
    def fixture_init(self,*args,**kwargs):
        original_init(self,*args,**kwargs)
        self.wait_seconds=.01
        self.poll_seconds=.001
    Workspace.__init__=fixture_init
    def check_power(pid,attempts=10):
        # The real check waits for this process's pmset assertion. Preserve
        # that asynchronous boundary without calling macOS in the fixture.
        ready=Path.cwd()/'.git/autocycle/power-ready.json'
        deadline=time.monotonic()+5
        while not ready.exists():
            os.kill(pid,0)
            if time.monotonic()>=deadline:
                raise RuntimeError('Mock caffeinate did not become ready')
            time.sleep(.01)
        state=json.loads(ready.read_text())
        if state['pid']!=pid or state['controller_pid']!=os.getppid():
            raise RuntimeError('Mock assertion belongs to a different process')
        with (Path.cwd()/'.git/autocycle/native-events').open('a') as f:f.write('power '+str(pid)+'\\n')
'''.replace('DENY',str(deny)).replace('BLACK',str(black))
    text=text.replace("if __name__=='__main__':",injection)
    path.write_text(text)
    (c.bin/'uname').write_text('#!/bin/sh\necho Darwin\n');(c.bin/'uname').chmod(0o755)
    caffeinate=c.bin/'caffeinate'
    caffeinate.write_text('''#!/usr/bin/env python3
import json,os,signal,sys,time
from pathlib import Path
p=Path.cwd()/'.git/autocycle/power-args'
def stop(*args):
 (p.parent/'power-stopped').write_text('stopped')
 raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
args=sys.argv[1:]
if args[:3]!=['-d','-i','-w'] or len(args)!=4 or int(args[3])!=os.getppid():
 raise SystemExit('Unexpected caffeinate lifetime arguments: '+repr(args))
p.write_text(' '.join(args))
ready=p.parent/'power-ready.json'
temporary=ready.with_suffix('.tmp')
temporary.write_text(json.dumps({'pid':os.getpid(),'controller_pid':int(args[3])}))
temporary.replace(ready)
while True:signal.pause()
''');caffeinate.chmod(0o755)


def test_controller_modes():
    for mode in (('1',),('--resume',),('1','--restart'),('--extend','1')):
        c=Case()
        try:
            ok(c.run('1'))
            before=(c.a/'resume-state').read_bytes()
            prepare(c,deny=True)
            r=c.run(*mode);fail(r)
            assert 'Access      ✗ ' in r.stdout and 'Excel fixed-slot access failed' in r.stdout,(mode,r.stdout,r.stderr)
            assert 'Cycle       ' not in r.stdout and 'Review      ' not in r.stdout
            assert (c.a/'resume-state').read_bytes()==before
            assert (c.a/'native-events').read_text().count('power ')==1
            assert (c.a/'power-args').read_text().startswith('-d -i -w ')
            assert (c.a/'power-stopped').exists()
        finally:c.close()
    c=Case()
    try:
        prepare(c)
        ok(c.run('--instruct','native opt-in instruction'))
        ok(c.run('--instructions'));ok(c.run('--stop'))
        assert not (c.a/'native-events').exists()
    finally:c.close()


def test_provider_request_captured_before_review():
    c=Case()
    try:
        prepare(c)
        observer_provider(c, app='excel')
        # Exercise the real Review prompt with >2 MiB of rendered capture data.
        helper=c.p/'native_office.py'
        helper.write_text(helper.read_text().replace('return super().capture(path,bounds)',
            "result=super().capture(path,bounds); result['word_rendered']['lines'].append({'text':'OCR_SENTINEL'+'x'*2200000,'confidence':0.1}); return result"))
        provider=c.bin/'codex'
        provider.write_text(provider.read_text().replace("event(kind,prompt)", r"""event(kind,prompt)
if kind=='review':
 import re
 match=re.search(r'Native Office evidence index:\npath: ([^\n]+)\nsha256: ([a-f0-9]+)\nentries: ([0-9]+)', prompt)
 assert match, 'Missing index locator'
 index_path=root/match[1]
 assert hashlib.sha256(index_path.read_bytes()).hexdigest()==match[2]
 entries=json.loads(index_path.read_text())['entries']
 assert len(entries)==int(match[3])
 for entry in entries:
  receipt=json.loads((root/entry['receipt']).read_text())
  result=json.loads((root/entry['result']).read_text())
  rendered=result['native_window']['word_rendered']
  raw=(root/rendered['path']).read_bytes()
  assert hashlib.sha256(raw).hexdigest()==rendered['sha256']
  assert 'OCR_SENTINEL' in raw.decode()
  assert (root/entry['screenshot']).read_bytes().startswith(b'\x89PNG')
  (a/'provider-followed-index').write_text(str(len(raw)))
"""))
        r=c.run('2',REVIEW_STATUS='PASS');ok(r)
        assert r.stdout.count('Access      ✓')==1,r.stdout
        events=(c.a/'native-events').read_text()
        assert 'capture parent=' in events
        reviews=[e for e in c.events() if e['kind']=='review']
        assert len(reviews)==2
        assert 'entries: 1' in reviews[1]['prompt']
        assert 'entries: 0' in reviews[0]['prompt']
        assert 'OCR_SENTINEL' not in reviews[1]['prompt']
        assert '"status": "CAPTURED"' not in reviews[1]['prompt']
        assert len(reviews[1]['prompt']) < 60000
        assert int((c.a/'provider-followed-index').read_text()) > 2*1024*1024
        print('full synthetic Review prompt chars:', len(reviews[1]['prompt']))
        assert (c.a/'visual-source.xlsx').read_bytes()==b'provider-owned Word work'
    finally:c.close()


def native_handoff_provider(c, mode='delegated'):
    """Script the existing todo protocol; enqueue uses the real request CLI.

    Cancellation requires both the structured declaration and controller authority.
    Pending provider todos still fail with otherwise valid requests.
    """
    agent=c.bin/'agent';text=agent.read_text()
    marker="  statuses=['completed','cancelled']"
    script=f'''  mode={mode!r}
  prompt=msg['params']['prompt'][0]['text']
  source=a/'visual-source.docx';source.write_bytes(b'provider-owned Word work')
  request={{'app':'word','source':str(source)}}
  if mode=='invalid':request['source']=str(a/'missing.docx')
  queued=''
  if mode not in ('missing','prose'):
   request_input=a/'view-input.json';request_input.write_text(json.dumps(request))
   submitted=subprocess.run([sys.executable,{str(c.p/'native_office.py')!r},'request',str(request_input)],capture_output=True,text=True)
   if submitted.returncode==0:
    queued=submitted.stdout.strip()
    record=json.loads(Path(queued).read_text())
    assert record['source_sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
   else:assert mode=='invalid',submitted.stderr
  assert not list((a/'office/receipts').glob('*.json')), 'Provider must not capture'
  statuses=['completed','pending']
  if mode in ('delegated','provider-pending'):
   assert queued
   statuses=['completed','cancelled']
   if mode=='provider-pending':statuses.append('pending')
  handoff='controller capture pending; visual evidence unverified'
  report='Provider implementation complete. '+handoff+'\\nRequest: '+(queued or 'NONE')+'\\n'
  if queued:report+='NATIVE_HANDOFF: '+json.dumps({{'requests':[Path(queued).stem]}})+'\\n'
  (root/'RESULT.md').write_text(report)
'''
    assert marker in text
    text=text.replace(marker,script)
    text=text.replace("'Task '+str(i)", "['Implement Word changes','Controller capture delegated' if mode in ('delegated','provider-pending') else 'Inspect every Word page','Finish provider work'][i]")
    text=text.replace("message='IMPLEMENT_STATUS: '+impl_status+'\\n'", "message=report+'IMPLEMENT_STATUS: '+impl_status+'\\n'")
    agent.write_text(text)


def inspect_implementation(c):
    log=next(c.a.glob('cursor-*.log'))
    return json.loads(subprocess.check_output(
        ['node',str(c.p/'implementation_response.js'),str(log)],text=True))


def observer_provider(c, app='word', pending=False):
    agent=c.bin/'agent';text=agent.read_text()
    marker="  statuses=['completed','cancelled']"
    script="""  source=a/('visual-source.'+('xlsx' if APP=='excel' else 'docx'))
  if not (a/'fixture-observed').exists():
   (a/'fixture-observed').touch()
   source.write_bytes(b'provider-owned Word work')
   request={'app':APP,'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
   if APP=='excel':request['worksheet']='Sheet1'
   print(json.dumps({'jsonrpc':'2.0','method':'cursor/update_todos','params':{'todos':[{'id':'observe','content':'Inspect native view','status':'pending'}],'merge':False}}),flush=True)
   report='OBSERVER_REQUEST: '+json.dumps({'requests':[request]})
   print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'text':report}}}}),flush=True)
   print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'stopReason':'end_turn'}}),flush=True)
   continue
  records=[json.loads(p.read_text()) for p in (a/'office/receipts').glob('*.json')]
  assert records, 'Observer must return before Implement continues'
  (root/'RESULT.md').write_text('Observed native result: '+records[-1]['status']+'; semantic acceptance belongs to Review')
  statuses=['completed','pending' if PENDING else 'completed']
""".replace('APP',repr(app)).replace('PENDING',repr(pending))
    assert marker in text;agent.write_text(text.replace(marker,script))


def test_legacy_delegation_is_rejected_for_new_work():
    c=Case()
    try:
        prepare(c);native_handoff_provider(c)
        fail(c.run('1'))
        assert not (c.a/'native-handoff.json').exists()
        assert not (c.a/'implementation-result.json').exists()
        assert not list((c.a/'office/receipts').glob('*.json'))
        assert 'STAGE=implementing' in (c.a/'resume-state').read_text()
    finally:c.close()


def test_observation_does_not_excuse_pending_provider_work():
    c=Case()
    try:
        prepare(c);observer_provider(c,pending=True)
        fail(c.run('1'))
        assert [e['kind'] for e in c.events()]==['review','plan','implement','implement']
        assert list((c.a/'office/receipts').glob('*.json'))
        assert not (c.a/'implementation-result.json').exists()
        assert 'STAGE=implementing' in (c.a/'resume-state').read_text()
    finally:c.close()


def test_stage_local_capture_remains_review_owned():
    for black in (False,True):
        c=Case()
        try:
            prepare(c,black=black);observer_provider(c)
            result=c.run('1');ok(result)
            assert [e['kind'] for e in c.events()]==['review','plan','implement','implement']
            assert not (c.a/'native-handoff.json').exists()
            records=[json.loads(p.read_text()) for p in (c.a/'office/receipts').glob('*.json')]
            assert len(records)==1,records
            assert records[0]['status']==('BLOCKED' if black else 'CAPTURED'),records
            assert records[0]['status'] in (c.repo/'RESULT.md').read_text()
            if black:
                assert records[0]['failure_kind']=='native_capture'
                assert 'screenshot' not in records[0]
            assert c.git('status','--porcelain')==''
            ownership=(c.a/'implementation-result.json').read_bytes()
            captures=(c.a/'native-events').read_text().count('capture parent=')
            if black:
                fail(c.run('--extend','1',REVIEW_STATUS='PROBLEMS',PROGRESS_OUTCOME='NONE',REVIEW_BLOCKING='1',BLOCKER_KEY_OVERRIDE='native-capture-unverified',FAIL_STAGE='plan'))
                assert c.events()[-1]['kind']=='plan'
            else:ok(c.run('--extend','1',REVIEW_STATUS='DONE'))
            assert (c.a/'implementation-result.json').read_bytes()==ownership
            assert (c.a/'native-events').read_text().count('capture parent=')==captures
            assert len([e for e in c.events() if e['kind']=='implement'])==2
            assert len([e for e in c.events() if e['kind']=='review'])==2
            assert (c.a/'visual-source.docx').read_bytes()==b'provider-owned Word work'
        finally:c.close()


if __name__=='__main__':
    import sys
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS '+name,flush=True)

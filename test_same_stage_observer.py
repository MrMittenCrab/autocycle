"""Native observation returns to its caller, with fake Office and real Controller."""
import json
from test_flow import Case, ok
from test_native_office_flow import prepare


def test_review_observes_checkpoint_and_continues():
    c=Case()
    try:
        prepare(c)
        source=c.repo/'report.docx';source.write_bytes(b'checkpointed Word document')
        c.git('add','report.docx');c.git('commit','-qm','Report');c.git('push','-q')
        provider=c.bin/'codex';text=provider.read_text()
        marker="answer=Path(args[i+1])"
        # The response is emitted only once; the next Review sees native evidence.
        text=text.replace("def emit(lines):", "def emit(lines):")
        needle="if kind=='review':\n status="
        replacement="""if kind=='review' and not list((a/'office/receipts').glob('*.json')):
 request={'app':'word','source':'report.docx','source_sha256':hashlib.sha256((root/'report.docx').read_bytes()).hexdigest()}
 emit(['OBSERVER_REQUEST: '+json.dumps({'requests':[request]})]);sys.exit(0)
if kind=='review':
 status="""
        assert needle in text;provider.write_text(text.replace(needle,replacement))
        result=c.run('1');ok(result)
        events=c.events()
        assert [e['kind'] for e in events]==['review','review','plan','implement'],events
        assert events[0]['cycle']==events[1]['cycle']=='1'
        assert events[0]['head']==events[1]['head']
        assert result.stdout.count('Review      ')==1,result.stdout
        assert '    ✓ Observe Word' in result.stdout,result.stdout
        assert 'entries: 1' in events[1]['prompt']
        assert not (c.a/'native-handoff.json').exists()
        assert source.read_bytes()==b'checkpointed Word document'
    finally:c.close()


def test_implement_observes_and_resumes_same_attempt():
    c=Case()
    try:
        prepare(c)
        agent=c.bin/'agent';text=agent.read_text()
        marker="  statuses=['completed','cancelled']"
        replacement="""  source=a/'visual-source.docx'
  if count==1:
   source.write_bytes(b'provider-owned intermediate Word work')
   request={'app':'word','source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
   print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'text':'OBSERVER_REQUEST: '+json.dumps({'requests':[request]})}}}}),flush=True)
   print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'stopReason':'end_turn'}}),flush=True)
   continue
  assert list((a/'office/receipts').glob('*.json'))
"""+marker
        assert marker in text;agent.write_text(text.replace(marker,replacement))
        result=c.run('1');ok(result)
        events=c.events()
        assert [e['kind'] for e in events]==['review','plan','implement','implement'],events
        assert events[-2]['cycle']==events[-1]['cycle']=='1'
        assert events[-2]['head']==events[-1]['head']
        state=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert len(state['attempts'])==1,state
        assert result.stdout.count('Implement   ')==1,result.stdout
        assert '    ✓ Observe Word' in result.stdout,result.stdout
        assert not (c.a/'native-handoff.json').exists()
        assert (c.repo/'RESULT.md').read_text()=='Measured successful run 2'
        captures=(c.a/'native-events').read_text().count('capture parent=')
        ok(c.run('--extend','1',REVIEW_STATUS='DONE'))
        assert (c.a/'native-events').read_text().count('capture parent=')==captures
    finally:c.close()

def test_interrupted_observer_returns_to_same_implementation():
    from test_native_office_flow import observer_provider
    from test_flow import fail
    for returned in (False,True):
        c=Case()
        try:
            prepare(c);observer_provider(c)
            controller=c.bin/'autocycle';original=controller.read_text()
            marker='    adjudication observe-result || return 1' if returned else '    CALL=$(adjudication observe-call) || return 1'
            assert marker in original
            controller.write_text(original.replace(marker,marker+'\n    exit 91',1))
            result=c.run('1');assert result.returncode==91,(result.stdout,result.stderr)
            saved=json.loads((c.a/'observer-call.json').read_text())
            assert saved['state']==('returned' if returned else 'pending')
            baseline=(c.a/'implementation-baseline.json').read_bytes()
            controller.write_text(original)
            result=c.run('--resume');ok(result)
            assert (c.a/'implementation-baseline.json').read_bytes()==baseline
            assert [e['kind'] for e in c.events()]==['review','plan','implement','implement']
            assert len(list((c.a/'office/receipts').glob('*.json')))==1
            assert (c.a/'native-events').read_text().count('capture parent=')==1
            assert not (c.a/'observer-call.json').exists()
        finally:c.close()


if __name__=='__main__':
    test_review_observes_checkpoint_and_continues();print('PASS same Review observation',flush=True)
    test_implement_observes_and_resumes_same_attempt();print('PASS same Implement observation and Review reuse',flush=True)
    test_interrupted_observer_returns_to_same_implementation();print('PASS interrupted same-stage Observer recovery',flush=True)

"""Delayed synthetic Bridge work must fall within its owner's displayed duration."""
import json
import unittest
from test_flow import Case, ok
from test_native_office_flow import prepare, observer_provider

class TimingTests(unittest.TestCase):
    def test_bridge_delays_are_owned_and_review_includes_pruning(self):
        c=Case();self.addCleanup(c.close);prepare(c);observer_provider(c)
        source=c.repo/'review.docx';source.write_bytes(b'checkpointed native view')
        c.git('add','review.docx');c.git('commit','-qm','Review artifact');c.git('push','-q')
        provider=c.bin/'codex';text=provider.read_text()
        marker="if kind=='review':\n status="
        replacement="""if kind=='review' and not list((a/'office/receipts').glob('*.json')):
 emit(['OBSERVER_REQUEST: '+json.dumps({'requests':[{'app':'word','source':'review.docx','source_sha256':hashlib.sha256((root/'review.docx').read_bytes()).hexdigest()}]})]);sys.exit(0)
if kind=='review':
 status="""
        assert marker in text;provider.write_text(text.replace(marker,replacement))
        helper=c.p/'native_office.py';text=helper.read_text()
        marker="    elif args.operation=='process':"
        injected='''    elif args.operation=='process':
        displays=[]
        for path in (Path.cwd()/'.git/autocycle').glob('stage-display.*'):
            if path.stat().st_size:
                state=json.loads(path.read_text())
                if state.get('label') in ('Review','Implement') and not state.get('finished'):
                    displays.append((state['started'],path,state))
        assert displays, 'Bridge operation has no active owning timer'
        _,path,state=max(displays)
        before=time.time_ns()//1000000
        time.sleep(1.15)
        with (Path.cwd()/'.git/autocycle/timed-bridge.jsonl').open('a') as out:
            out.write(json.dumps({'display':str(path),'label':state['label'],'before':before,'after':time.time_ns()//1000000})+'\\n')
'''
        assert marker in text;helper.write_text(text.replace(marker,injected))
        result=c.run('1');ok(result)
        measurements=[json.loads(line) for line in (c.a/'timed-bridge.jsonl').read_text().splitlines()]
        self.assertEqual([m['label'] for m in measurements],['Review','Implement'])
        from pathlib import Path
        for m in measurements:
            state=json.loads(Path(m['display']).read_text())
            self.assertLessEqual(state['started'],m['before'])
            duration=state['finished'].split()[1]
            minutes,seconds=map(int,duration.split(':'))
            self.assertGreaterEqual((minutes*60+seconds+1)*1000,m['after']-state['started'])
        self.assertEqual(result.stdout.count('Review      '),1,result.stdout)
        self.assertEqual(result.stdout.count('Implement   '),1,result.stdout)
        for phase in ('Verifier','Proof','Evidence','Capability'):
            self.assertNotIn(phase+'     ',result.stdout)

if __name__=='__main__':unittest.main()

"""Real controller admission/resume regression; fake providers and Office only."""
import json
import unittest
from test_flow import Case, ok
from test_native_office_flow import prepare

REVIEW = '''
 if os.environ.get('SEMANTIC_GAP')=='1':
  report['unresolved_facts']=[{'fact':'All document pages readable','material_reason':'Parent Completion requires readable content'}]
  report['blocking']=True;report['blocking_reason']='Content coverage remains uncertain'
  report['outcome']='UNKNOWN' if attempt else 'UNASSESSED'
  status='PROBLEMS';blocker_key='readability';human_action='NONE'
'''

class FlowTests(unittest.TestCase):
    def fixture(self):
        c=Case();self.addCleanup(c.close);prepare(c)
        provider=c.bin/'codex';text=provider.read_text()
        text=text.replace(" extra=[] if",REVIEW+"\n extra=[] if")
        text=text.replace("'inputs':inputs}),", "'inputs':inputs,**({'evidence_routes':[{'fact':'All document pages readable',**({'commands':[['python3','existing_check.py']]} if os.environ.get('SUPPORTED_ROUTE')=='1' else {'missing_capability':'No supported finite observation route'})}]} if os.environ.get('SEMANTIC_GAP')=='1' else {})}),")
        provider.write_text(text)
        (c.repo/'existing_check.py').write_text('print("observed")\n')
        c.git('add','existing_check.py');c.git('commit','-qm','Fixture check');c.git('push','-q')
        return c

    def test_capability_gap_stops_before_implement_and_does_not_loop(self):
        c=self.fixture()
        controller=c.bin/'autocycle'
        controller.write_text(controller.read_text().replace('network_ready() { return 0; }','network_ready() { return 1; }'))
        result=c.run('1',SEMANTIC_GAP='1')
        self.assertEqual(result.returncode,78,result.stdout+result.stderr)
        self.assertEqual([e['kind'] for e in c.events()],['review','plan'])
        self.assertIn('Verification-capability gap',result.stdout)
        state=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        self.assertFalse(state.get('missing_evidence'))
        self.assertEqual(state['verification_capability_gap']['semantic_fact'],'All document pages readable')
        before=len(c.events())
        result=c.run('--resume',SEMANTIC_GAP='1')
        self.assertEqual(result.returncode,78,result.stdout+result.stderr)
        self.assertEqual([e['kind'] for e in c.events()[before:]],['review','plan'])
        self.assertFalse(list((c.a/'office/requests').glob('*.json')))

    def test_plan_inability_is_terminal_not_a_review_cycle(self):
        c=self.fixture()
        provider=c.bin/'codex';text=provider.read_text()
        text=text.replace("event(kind,prompt)", "event(kind,prompt)\nif kind=='plan':\n answer.write_text('PLAN_STATUS: BLOCKED\\nBLOCKER: No finite supported route can be constructed\\n');sys.exit(0)")
        provider.write_text(text)
        result=c.run('1',SEMANTIC_GAP='1')
        self.assertEqual(result.returncode,78,result.stdout+result.stderr)
        self.assertEqual([e['kind'] for e in c.events()],['review','plan'])

    def test_supported_route_reaches_implement(self):
        c=self.fixture();result=c.run('1',SEMANTIC_GAP='1',SUPPORTED_ROUTE='1');ok(result)
        self.assertIn('implement',[e['kind'] for e in c.events()])
        self.assertIn('existing_check.py',(c.repo/'IMPLEMENTATION.md').read_text())

    def test_legacy_resume_reviews_before_any_execution(self):
        c=self.fixture();ok(c.run('1'))
        state_path=c.a/'work-state.json';data=json.loads(state_path.read_text())
        state=data['branches']['checkpoint/test']
        state['missing_evidence']={'requirements':[{'requirement':{'fact':'unclassified','description':'All pages readable'},'attempt':{'outcome':'UNAVAILABLE'}}]}
        state_path.write_text(json.dumps(data))
        before=len(c.events());ok(c.run('--resume'))
        self.assertEqual(c.events()[before]['kind'],'review')
        state=json.loads(state_path.read_text())['branches']['checkpoint/test']
        self.assertFalse(state.get('missing_evidence'))
        self.assertFalse(list((c.a/'office/requests').glob('*.json')))
        self.assertNotIn('"fact": "unclassified"',json.dumps(state))

    def test_extend_legacy_implement_boundary_also_returns_to_review(self):
        c=self.fixture();ok(c.run('1'))
        path=c.a/'work-state.json';data=json.loads(path.read_text())
        data['branches']['checkpoint/test']['missing_evidence']={'requirements':[{'requirement':{'fact':'unclassified'}}]}
        path.write_text(json.dumps(data))
        resume=c.a/'resume-state';resume.write_text(resume.read_text().replace('STAGE=checkpoint_done','STAGE=implementing'))
        before=len(c.events());ok(c.run('--extend','1'))
        self.assertEqual(c.events()[before]['kind'],'review')
        self.assertFalse(json.loads(path.read_text())['branches']['checkpoint/test'].get('missing_evidence'))

if __name__=='__main__':unittest.main()

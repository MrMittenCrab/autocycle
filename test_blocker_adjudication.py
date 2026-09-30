"""External-dependency semantics; synthetic receipts do not prove native access."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import progress
from test_missing_evidence import MissingTests as _MissingTests
from test_flow import ok
from test_session import case
from test_resume_progress_state import ledger, instrument
from test_opening_review import context


class ContractTests(unittest.TestCase):
    setUp = _MissingTests.setUp
    # Use the small isolated evidence store, without inheriting its test cases.
    def answer(self, status='PROBLEMS', fact='Required rendered DOCX readability'):
        r={'work_id':'NONE','attempt_id':'NONE','finding_key':'verification',
           'blocking':True,'blocking_reason':fact,'outcome':'UNASSESSED',
           'reason':'No checkpointed attempt','evidence':[],
           'unresolved_facts':[{'fact':fact,'material_reason':'Required by Completion'}]}
        text=f'REVIEW_STATUS: {status}\nBLOCKER_KEY: verification\nHUMAN_ACTION: NONE\nNEXT_STEP: Bounded verification or diagnosis\n'
        return r,text

    def validate(self,r,text):
        with patch.object(progress,'instruction_direction',return_value={}):
            return progress.review_report(self.s,text+'AUTOCYCLE_REVIEW: '+json.dumps(r))

    def test_actionable_states_are_problems(self):
        for fact in ('Remaining implementation defect','Render inspection not attempted',
                     'Capture helper exists, no attempt','Ambiguous capture failure: timeout',
                     'Repairable capture implementation error','No demonstrated route; diagnosis pending'):
            with self.subTest(fact=fact):
                r,text=self.answer(fact=fact)
                self.validate(r,text)
                with self.assertRaisesRegex(ValueError,'external dependency'):
                    self.validate(r,text.replace('PROBLEMS','BLOCKED'))

    def blocked(self,kind='permission'):
        r,text=self.answer('BLOCKED')
        action='Grant Screen Recording permission in System Settings' if kind=='permission' else 'Provide the required supplier-source.csv from the source owner'
        dependency={'fact':r['blocking_reason'],'route':'owned capture attempt' if kind=='permission' else 'source inventory check',
                    'kind':kind,'check':'deterministic','action':action,
                    'inputs':[{'path':str(self.source),'sha256':progress.digest(self.source.read_bytes()),'observation':'Checked input scope'}]}
        path=self.ac/'dependency.json'
        path.write_text(json.dumps({'reviewed_head':'head','external_dependency':dependency}))
        r['external_blocker']={k:dependency[k] for k in ('fact','route','action')}
        r['external_blocker']['evidence']=[{'path':str(path),'sha256':progress.digest(path.read_bytes()),'observation':'External dependency established by check'}]
        text=text.replace('HUMAN_ACTION: NONE','HUMAN_ACTION: '+action).replace('NEXT_STEP: Bounded verification or diagnosis','NEXT_STEP: '+action)
        return r,text,path

    def test_legitimate_external_dependencies(self):
        for kind in ('permission','external_artifact'):
            r,text,_=self.blocked(kind);self.validate(r,text)

    def test_speculation_tampering_staleness_and_wrong_action_rejected(self):
        for change in ('missing','ambiguous','repairable','stale','inputs','action','hash'):
            with self.subTest(change=change):
                r,text,path=self.blocked();receipt=json.loads(path.read_text())
                if change=='missing':receipt.pop('external_dependency')
                elif change in ('ambiguous','repairable'):receipt['external_dependency']['kind']=change
                elif change=='stale':receipt['reviewed_head']='old'
                elif change=='inputs':receipt['external_dependency']['inputs']=[]
                elif change=='action':text=text.replace('HUMAN_ACTION: Grant','HUMAN_ACTION: Guess')
                path.write_text(json.dumps(receipt))
                if change!='hash':r['external_blocker']['evidence'][0]['sha256']=progress.digest(path.read_bytes())
                else:path.write_text('{}')
                with self.assertRaises(ValueError):self.validate(r,text)


    def test_generic_receipt_cannot_supply_a_missing_route(self):
        r,_=self.answer()
        route={'fact':r['unresolved_facts'][0]['fact'],'producer':{'status':'PRODUCED'}}
        with self.assertRaises(progress.VerificationCapabilityGap):
            progress.validate_evidence_routes({'evidence_routes':[route]},r)


del _MissingTests

def provider(c, always=False, missing=True):
    p=c.bin/'codex';marker=" extra=[] if os.environ.get('DROP_PROGRESS_REPORT')=='1'"
    addition='''
 report['blocking']=True;report['blocking_reason']='Required render inspection remains unattempted'
 report['outcome']='UNKNOWN' if attempt else 'UNASSESSED'
 report['unresolved_facts']=GAPS
 status='BLOCKED' if ALWAYS or 'Review validation failure:' not in prompt else 'PROBLEMS'
 blocker_key='render-inspection';human_action='Provide rendered pages' if status=='BLOCKED' else 'NONE'
 next_step='Attempt capture, or perform bounded capability diagnosis'
'''.replace('ALWAYS',repr(always)).replace('GAPS',repr([{'fact':'Required render readability','material_reason':'Completion requires readable content'}] if missing else []))
    text=p.read_text().replace(marker,addition+marker,1)
    # Existing project inspection route; admission does not assert its truth.
    text=text.replace("'inputs':inputs}),", "'inputs':inputs,'evidence_routes':[{'fact':'Required render readability','commands':[['git','diff','HEAD']]}]}),")
    p.write_text(text)


class ControllerTests(unittest.TestCase):
    def setup_case(self):
        c=case();self.addCleanup(c.close);instrument(c);ok(c.run('1',PROGRESS_OUTCOME='VERIFIED'));return c

    def test_invalid_blocked_retries_only_review_then_plans(self):
        c=self.setup_case();c.enqueue('Preserve Completion and verify the rendered output')
        before=ledger(c);head=c.git('rev-parse','HEAD');provider(c)
        n=len(c.events());result=c.run('--extend','1',WITHOUT_EXTERNAL_EVIDENCE='1',FAIL_STAGE='plan')
        self.assertNotEqual(result.returncode,0)
        events=c.events()[n:];self.assertEqual([e['kind'] for e in events],['review','review','plan'])
        self.assertIn('artifact-bound external dependency',events[1]['prompt'])
        self.assertEqual(context(events[0]),context(events[1]))
        self.assertEqual(events[0]['state'],events[1]['state'])
        after=ledger(c);self.assertEqual(after['work']['id'],before['work']['id'])
        self.assertEqual(after['attempts'][0]['id'],before['attempts'][0]['id'])
        self.assertEqual(c.git('rev-parse','HEAD'),head)
        self.assertEqual(after['attempts'][0]['outcome'],'UNKNOWN')
        self.assertFalse(after['work']['complete']);self.assertTrue(after['unresolved_facts'])
        self.assertIn('REVIEW_STATUS: PROBLEMS',(c.a/'current-review').read_text())

    def test_exhaustion_preserves_state_and_can_resume(self):
        c=self.setup_case();before=ledger(c);provider(c,always=True);n=len(c.events())
        result=c.run('--extend','1',WITHOUT_EXTERNAL_EVIDENCE='1')
        self.assertNotEqual(result.returncode,0)
        events=c.events()[n:];self.assertEqual([e['kind'] for e in events],['review']*3)
        self.assertEqual(ledger(c),before)
        self.assertIn('STAGE=reviewing',(c.a/'resume-state').read_text())
        self.assertTrue(all(e['state']==events[0]['state'] for e in events))
        self.assertEqual(len(list(c.a.glob('*.validation'))),3)

    def test_old_cached_blocked_revalidates_same_checkpoint(self):
        c=self.setup_case();result=c.run('--extend','1',REVIEW_STATUS='BLOCKED',PROGRESS_OUTCOME='UNKNOWN')
        self.assertEqual(result.returncode,2)
        self.assertEqual(c.run('--resume',REVIEW_STATUS='BLOCKED',PROGRESS_OUTCOME='UNKNOWN').returncode,2)
        self.assertTrue(ledger(c)['block'])
        cache=c.a/'current-review';rows=cache.read_text().splitlines()
        frozen=next(line for line in rows if line.startswith('INPUT_BATCH: ')).split(': ',1)[1]
        c.enqueue('Later instruction must wait outside the frozen recovery batch')
        for i,line in enumerate(rows):
            if line.startswith('REVIEW_POLICY:'):rows[i]='REVIEW_POLICY: five-stage-session-v11'
            if line.startswith('AUTOCYCLE_REVIEW: '):
                r=json.loads(line.split(': ',1)[1]);r.pop('external_blocker',None)
                rows[i]='AUTOCYCLE_REVIEW: '+json.dumps(r)
        cache.write_text('\n'.join(rows)+'\n');before=ledger(c);head=c.git('rev-parse','HEAD');provider(c)
        n=len(c.events());result=c.run('--resume',WITHOUT_EXTERNAL_EVIDENCE='1',FAIL_STAGE='plan')
        events=c.events()[n:];self.assertEqual([e['kind'] for e in events],['review','review','plan'],result.stdout)
        self.assertEqual(c.git('rev-parse','HEAD'),head)
        self.assertEqual(ledger(c)['work']['id'],before['work']['id'])
        self.assertEqual(len(ledger(c)['attempts']),len(before['attempts']))
        self.assertEqual(events[0]['state'],events[1]['state'])
        self.assertIn('INPUT_BATCH: '+frozen,cache.read_text())
        self.assertNotIn('Later instruction must wait',events[0]['prompt'])
        self.assertEqual(c.rows()[-1]['state'],'pending')

    def test_opening_gap_can_start_first_attempt(self):
        c=case();self.addCleanup(c.close);provider(c)
        ok(c.run('1',WITHOUT_EXTERNAL_EVIDENCE='1'))
        state=ledger(c)
        self.assertEqual(len(state['attempts']),1)
        self.assertTrue(state['unresolved_facts'])
        self.assertFalse(state.get('missing_evidence'))
        self.assertFalse(state['work']['complete'])

    def test_actionable_gap_continues_without_claiming_completion(self):
        c=self.setup_case();provider(c);before=ledger(c)
        ok(c.run('--extend','2',WITHOUT_EXTERNAL_EVIDENCE='1'))
        after=ledger(c)
        self.assertEqual(len(after['attempts']),3)
        self.assertEqual(after['work']['id'],before['work']['id'])
        self.assertFalse(after['work']['complete'])
        self.assertTrue(after['unresolved_facts'])
        self.assertFalse(after.get('missing_evidence'))


if __name__=='__main__':unittest.main()

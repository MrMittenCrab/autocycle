"""Semantic control regression fixtures; no live Office or project state."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import test_missing_evidence as fixtures
import progress
import native_office as office


class ControlTests(unittest.TestCase):
    setUp = fixtures.MissingTests.setUp
    report = fixtures.MissingTests.report
    prepare = fixtures.MissingTests.prepare
    def test_new_unclassified_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unclassified|Controller'):
            progress.missing_requirements({'blocking':True,'missing_evidence':[{'fact':'unclassified'}]})

    def test_legacy_resume_goes_to_review_without_request(self):
        self.report()
        self.s['missing_evidence']={'requirements':[{'requirement':{'fact':'unclassified'},'attempt':{'outcome':'UNAVAILABLE'}}]}
        self.assertEqual(self.prepare()[0],0)
        self.assertFalse(self.s.get('missing_evidence'))
        self.assertFalse(progress.missing_requests(self.s))
        self.assertFalse((self.ac/'office/requests').exists())
        before=copy.deepcopy(self.s)
        self.prepare()
        self.assertEqual(before,self.s)

    def test_cache_only_legacy_item_also_routes_to_review(self):
        self.report([{'fact':'unclassified','description':'Underlying readability'}])
        self.assertEqual(self.prepare()[0],0)
        self.assertTrue(self.s['legacy_evidence_review'])
        self.assertFalse(progress.missing_requests(self.s))
        before=copy.deepcopy(self.s);self.prepare();self.assertEqual(before,self.s)

    def test_resume_does_not_synthesize_from_prose(self):
        self.report();text=self.cache.read_text();r=progress.record(text,'AUTOCYCLE_REVIEW');r.pop('missing_evidence')
        self.cache.write_text('REVIEW_STATUS: BLOCKED\nAUTOCYCLE_REVIEW: '+json.dumps(r))
        self.prepare();self.prepare()
        self.assertNotIn('unclassified',json.dumps(self.s))
        self.assertFalse(progress.missing_requests(self.s))

    def test_aggregation_needs_no_matching_receipt_or_new_timestamp(self):
        self.s['missing_evidence']={'requirements':[{'requirement':{'fact':'unclassified'}}], 'after_ns':10**30}
        refs=[]
        for page in range(1,18):
            path=self.root/f'page-{page}.json';path.write_text(json.dumps({'page':page,'readable':True}))
            refs.append({'path':str(path),'sha256':office.digest(path),'observation':f'Page {page} readable',
                         'applies_to':[{'path':str(self.source),'sha256':office.digest(self.source),'observation':'unchanged source'}]})
        checked=progress.evidence_valid(refs)
        r=self.report([],refs,blocking=False)
        progress.validate_missing_evidence(self.s,r,checked)
        progress.update_missing_evidence(self.s,self.cache,r,checked)
        self.assertFalse(self.s.get('missing_evidence'))
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):progress.evidence_valid(refs)

    def test_authenticated_page_observations_aggregate_without_receipt_protocol(self):
        from test_native_office import FakeOffice
        class Pages(FakeOffice):
            def word_content(self,path):return ''.join(f'VIEWPORT TEST PAGE {i} unique content marker paragraph' for i in range(1,18))
            def word_page_map(self,path,request):
                self.page=request['page']
                return {'page':self.page,'pages':[f'VIEWPORT TEST PAGE {i} unique content marker paragraph' for i in range(1,18)]}
            def capture(self,path,bounds):
                result=super().capture(path,bounds)
                result['word_rendered']['lines']=[{'text':f'VIEWPORT TEST PAGE {self.page} unique content marker paragraph','confidence':1.0}]
                return result
        source=self.root/'document.docx';source.write_bytes(b'unchanged 17-page fixture')
        ws=office.Workspace(self.ac,Pages())
        refs=[]
        for page in range(1,18):
            request=office.enqueue(ws,{'app':'word','source':str(source),'page':page},'head')
            office.process_requests(ws,('word',),'head',[request.stem])
            _,result,path=office.request_record(ws,request.stem)
            self.assertEqual(result['status'],'CAPTURED',result)
            refs.append({'path':str(path),'sha256':office.digest(path),'observation':f'Page {page} readable',
                         'applies_to':[{'path':str(source),'sha256':office.digest(source),'observation':'Applicable unchanged DOCX'}]})
        checked=progress.review_evidence(self.s,refs)
        self.assertEqual(len(checked),17)
        self.s['missing_evidence']={'requirements':[{'requirement':{'fact':'unclassified'}}]}
        r=self.report([],refs,blocking=False)
        r.update(finding_key='document-readability', outcome='UNASSESSED',
                 reason='Applicable native observations collectively cover all 17 readable pages',
                 blocking_reason='NONE', direction={})
        with patch.object(progress,'instruction_direction',return_value={}):
            admitted, observations=progress.review_report(self.s,'REVIEW_STATUS: PASS\nAUTOCYCLE_REVIEW: '+json.dumps(r))
        self.assertEqual(len(observations),17)
        self.assertFalse(admitted['blocking'])
        progress.update_missing_evidence(self.s,self.cache,r,checked)
        self.assertFalse(self.s.get('missing_evidence'))
        source.write_bytes(b'changed document')
        with self.assertRaisesRegex(ValueError,'source changed|hash mismatch'):
            progress.review_evidence(self.s,refs)

    def test_plan_routes_admission_and_absence(self):
        fact={'fact':'All pages readable','material_reason':'Required by parent Completion'}
        r={'unresolved_facts':[fact],'blocking':True}
        request={'app':'word','source':str(self.source.with_suffix('.docx')),'page':1}
        Path(request['source']).write_bytes(b'document')
        p={'evidence_routes':[{'fact':fact['fact'],'requests':[request]}]}
        with patch.object(office,'capabilities',return_value=('word',)):
            progress.validate_evidence_routes(p,r)
        p['evidence_routes'][0]['requests'][0]['operation']='whole_document_verifier'
        with self.assertRaisesRegex(progress.VerificationCapabilityGap,'whole_document_verifier'):
            progress.validate_evidence_routes(p,r)
        with self.assertRaisesRegex(progress.VerificationCapabilityGap,'Required by parent Completion'):
            progress.validate_evidence_routes({},r)

    def test_existing_command_route_and_no_invented_command(self):
        r={'unresolved_facts':[{'fact':'Check output','material_reason':'Completion'}],'blocking':True}
        script=self.root/'check.py';script.write_text('raise SystemExit(1)')
        p={'evidence_routes':[{'fact':'Check output','commands':[[sys.executable,str(script)]]}]}
        progress.validate_evidence_routes(p,r)  # Runtime failure is not capability absence.
        for command in (['nonexistent-autocycle-verifier'], ['env','python3','invented.py'], ['osascript','-e','tell application Word']):
            p['evidence_routes'][0]['commands']=[command]
            with self.assertRaises(progress.VerificationCapabilityGap):progress.validate_evidence_routes(p,r)

    def test_office_source_binding_required_even_without_optional_applies_to(self):
        request=office.enqueue(self.ws,office.navigation_request(self.req),'head')
        office.process_requests(self.ws,('excel',),'head',[request.stem])
        _,result,path=office.request_record(self.ws,request.stem)
        refs=[{'path':str(path),'sha256':office.digest(path),'observation':'Native navigation'}]
        progress.review_evidence(self.s,refs)
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'source changed'):
            progress.review_evidence(self.s,refs)

    def test_legacy_running_attempt_cannot_bypass_begin(self):
        self.s['attempts']=[{'id':'a','plan_sha':'head','kind':'implementation','phase':'running','outcome':None}]
        self.s['missing_evidence']={'requirements':[{'requirement':{'fact':'unclassified'}}]}
        with patch.object(progress,'plan_text',return_value='# Step 1.1 — Legacy'):
            with self.assertRaisesRegex(ValueError,'legacy evidence'):
                progress.begin(self.s,'head','')

    def test_review_cannot_create_retry_metadata(self):
        with self.assertRaisesRegex(ValueError,'Controller'):
            progress.update_missing_evidence(self.s,self.cache,self.report(),[])

    def test_generic_receipt_is_not_an_admission_route(self):
        r={'unresolved_facts':[{'fact':'Arbitrary unsupported truth','material_reason':'Endpoint'}],'blocking':True}
        p={'evidence_routes':[{'fact':'Arbitrary unsupported truth','producer':{'status':'PRODUCED','requirement':'Arbitrary unsupported truth'}}]}
        with self.assertRaises(progress.VerificationCapabilityGap):progress.validate_evidence_routes(p,r)


if __name__=='__main__':unittest.main()

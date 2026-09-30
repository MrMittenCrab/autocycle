"""Navigation semantics and native action receipts; no live Office operations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import native_office as office
from test_native_office import FakeOffice


def workbook(path, target="'Detail'!A1"):
    ns='http://schemas.openxmlformats.org/'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}spreadsheetml/2006/main" xmlns:r="{ns}officeDocument/2006/relationships"><sheets><sheet name="Start" sheetId="1" r:id="s1"/><sheet name="Detail" sheetId="2" r:id="s2"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="s1" Target="worksheets/sheet1.xml"/><Relationship Id="s2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{ns}spreadsheetml/2006/main" xmlns:r="{ns}officeDocument/2006/relationships"><hyperlinks><hyperlink ref="A2" r:id="h1"/></hyperlinks></worksheet>')
        z.writestr('xl/worksheets/_rels/sheet1.xml.rels',f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="h1" Target="#{target}" TargetMode="External"/></Relationships>')
        z.writestr('xl/worksheets/sheet2.xml',f'<worksheet xmlns="{ns}spreadsheetml/2006/main"/>')


def requirement(path):
    return {'fact':'workbook_navigation','source':str(path),'source_sha256':office.digest(path),
            'worksheet':'Start','links':[{'cell':'A2','sheet':'Detail','range':'A1'}], 'return_sheet':'Start'}


class NavigationOffice(FakeOffice):
    wrong=False
    def navigation_action(self,path,operation,worksheet,cell=None):
        before={'workbook':str(path),'worksheet':'Start' if operation=='follow_hyperlink' else ('Wrong' if self.wrong else 'Detail'),'selection':'$A$2','active_cell':'$A$2'}
        after={'workbook':str(path),'worksheet':('Wrong' if self.wrong else 'Detail') if operation=='follow_hyperlink' else worksheet,'selection':'$A$1','active_cell':'$A$1'}
        return {'operation':operation,'control':{'worksheet':worksheet,'cell':cell},'before':before,'after':after}


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.source=self.root/'book.xlsx';workbook(self.source)
        self.backend=NavigationOffice();self.ws=office.Workspace(self.root/'ac',self.backend)
        self.req=requirement(self.source)

    def produce(self):
        req=office.navigation_request(self.req)
        path=office.enqueue(self.ws,req,'head')
        office.process_requests(self.ws,('excel',),'head',[path.stem])
        return office.request_record(self.ws,path.stem)

    def test_receipts_use_ordinary_pipeline_without_capture(self):
        self.assertTrue(hasattr(office,'navigation_request'),'semantic navigation verifier is missing')
        request,result,path=self.produce()
        self.assertEqual(result['status'],'PRODUCED')
        self.assertEqual(result['actions'][0]['operation'],'follow_hyperlink')
        self.assertEqual(result['actions'][1]['operation'],'activate_worksheet')
        self.assertEqual(result['actions'][1]['after']['worksheet'],'Start')
        self.assertNotIn('screenshot',result)
        self.assertFalse(any(e[0]=='capture' for e in self.backend.events))
        self.assertEqual(office.review_evidence(self.ws)[0]['receipt'],str(path))
        self.assertNotIn('recovery_id',request)

    def test_wrong_destination_is_failed_with_actual_receipt(self):
        self.assertTrue(hasattr(office,'navigation_request'),'semantic navigation verifier is missing')
        self.backend.wrong=True
        _,result,_=self.produce()
        self.assertEqual(result['status'],'FAILED')
        self.assertEqual(result['actions'][0]['after']['worksheet'],'Wrong')
        self.assertEqual(result['actions'][-1]['after']['worksheet'],'Start')

    def test_merged_destination_is_checked_by_active_cell(self):
        action=self.backend.navigation_action
        def merged(*args):
            receipt=action(*args)
            if args[1]=='follow_hyperlink':receipt['after']['selection']='$A$1:$H$1'
            return receipt
        with patch.object(self.backend,'navigation_action',side_effect=merged):
            _,result,_=self.produce()
        self.assertEqual(result['status'],'PRODUCED')
        self.assertEqual(result['actions'][0]['after']['selection'],'$A$1:$H$1')

    def test_structural_destination_mismatch_does_not_open_office(self):
        self.assertTrue(hasattr(office,'navigation_request'),'semantic navigation verifier is missing')
        workbook(self.source,"'Wrong'!A1");self.req['source_sha256']=office.digest(self.source)
        _,result,_=self.produce()
        self.assertEqual(result['status'],'FAILED')
        self.assertFalse(any(e[0]=='open' for e in self.backend.events))

    def test_native_action_reads_actual_states_and_follows_authored_link(self):
        self.assertTrue(hasattr(office.MacOffice,'navigation_action'),'native action receipts are missing')
        backend=office.MacOffice();path=self.ws.slot('excel','view')
        sep=backend.IDENTITY_REPLY_SEP
        reply=sep.join([str(path),'Start','$A$2','$A$2',str(path),'Detail','$A$1','$A$1'])
        with patch.object(backend,'find',side_effect=lambda app,p,body:body),patch.object(backend,'script',return_value=reply) as script:
            result=backend.navigation_action(path,'follow_hyperlink','Start','A2')
        body=script.call_args.args[1]
        self.assertIn('follow',body)
        self.assertIn('hyperlink 1',body)
        self.assertNotIn('activate object worksheet',body)
        self.assertIn('active sheet',body)
        self.assertEqual(result['before']['selection'],'$A$2')
        self.assertEqual(result['after']['worksheet'],'Detail')

    def test_interrupted_action_is_retained_without_blind_replay(self):
        p=office.enqueue(self.ws,office.navigation_request(self.req),'head')
        with patch.object(self.backend,'navigation_action',side_effect=KeyboardInterrupt),self.assertRaises(KeyboardInterrupt):
            office.process_requests(self.ws,('excel',),'head',[p.stem])
        opens=len([e for e in self.backend.events if e[0]=='open'])
        office.process_requests(self.ws,('excel',),'head',[p.stem])
        self.assertEqual(len([e for e in self.backend.events if e[0]=='open']),opens)
        result=office.request_record(self.ws,p.stem)[1]
        self.assertEqual(result['status'],'UNAVAILABLE')
        self.assertIn('interrupted',result['action'].lower())

    def test_native_receipt_rejects_wrong_workbook_and_malformed_state(self):
        backend=office.MacOffice();path=self.ws.slot('excel','view');sep=backend.IDENTITY_REPLY_SEP
        for reply in ('not structured',sep.join([str(path),'Start','$A$2','$A$2','/another/book.xlsx','Detail','$A$1','$A$1'])):
            with patch.object(backend,'find',side_effect=lambda app,p,body:body),patch.object(backend,'script',return_value=reply),self.assertRaises(office.NativeError):
                backend.navigation_action(path,'follow_hyperlink','Start','A2')

    def test_interruption_after_result_reuses_it_before_receipt(self):
        self.assertTrue(hasattr(office,'navigation_request'),'semantic navigation verifier is missing')
        p=office.enqueue(self.ws,office.navigation_request(self.req),'head')
        original=office.publish_json
        def interrupted(path,value):
            if Path(path).parent.name=='receipts':raise KeyboardInterrupt()
            original(path,value)
        with patch.object(office,'publish_json',side_effect=interrupted),self.assertRaises(KeyboardInterrupt):
            office.process_requests(self.ws,('excel',),'head',[p.stem])
        opens=len([e for e in self.backend.events if e[0]=='open'])
        office.process_requests(self.ws,('excel',),'head',[p.stem])
        self.assertEqual(len([e for e in self.backend.events if e[0]=='open']),opens)
        self.assertEqual(office.request_record(self.ws,p.stem)[1]['status'],'PRODUCED')


if __name__=='__main__':unittest.main()

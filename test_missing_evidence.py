"""Durable semantic recovery, freshness, and bounded attempts, isolated state only."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import progress
import native_office as office
from test_navigation_evidence import NavigationOffice, workbook, requirement


class MissingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.ac=self.root/'ac';self.ac.mkdir()
        self.source=self.root/'book.xlsx';workbook(self.source)
        self.req=requirement(self.source)
        self.s={'work':None,'attempts':[],'block':None,'allocated':{}}
        self.cache=self.ac/'current-review'
        self.boundary={'SESSION_NUMBER':'2','RUN_CYCLE':'4','INPUT_CYCLE_ID':''}
        self.addCleanup(patch.stopall)
        patch.object(progress,'acdir',return_value=self.ac).start()
        patch.object(progress,'observation_boundary',return_value=self.boundary).start()
        patch.object(progress,'git',side_effect=lambda *a:'head' if a==('rev-parse','HEAD') else str(self.root)).start()
        patch.object(office,'capabilities',return_value=('excel',)).start()
        self.ws=office.Workspace(self.ac,NavigationOffice())

    def report(self,missing=None,evidence=None,token='first',blocking=True):
        r={'work_id':'NONE','attempt_id':'NONE','blocking':blocking,'blocking_reason':'Required navigation remains undemonstrated',
           'missing_evidence':missing if missing is not None else [self.req],'evidence':evidence or []}
        text='REVIEW_STATUS: '+('BLOCKED' if blocking else 'PASS')+'\nREVIEWED_SHA: head\nREVIEW_TOKEN: '+token+'\nAUTOCYCLE_REVIEW: '+json.dumps(r)+'\n'
        self.cache.write_text(text)
        return r

    def register(self,missing=None):
        self.assertTrue(hasattr(progress,'update_missing_evidence'),'semantic missing requirements are not retained')
        r=self.report(missing)
        progress.update_missing_evidence(self.s,self.cache,r,[])

    def prepare(self):return progress.resume_missing_evidence(self.s,self.cache)

    def dispatch(self):
        ids=progress.missing_requests(self.s)
        office.process_requests(self.ws,('excel',),'head',ids)
        return ids

    def checked(self,ident):
        _,result,path=office.request_record(self.ws,ident)
        return result,[{'path':str(path),'sha256':office.digest(path),'observation':'Inspected native transition'}]

    def test_legacy_supported_and_unavailable_state_only_routes_to_review(self):
        for fact in (self.req, {'fact':'unclassified'}, {'fact':'future_fact'}):
            self.s['missing_evidence']={'requirements':[{'requirement':fact,'attempt':{'outcome':'UNAVAILABLE'}}]}
            self.assertEqual(self.prepare()[0],0)
            self.assertFalse(self.s.get('missing_evidence'))
            self.assertEqual(progress.missing_requests(self.s),[])
            self.assertFalse((self.ac/'office/requests').exists())

    def test_fresh_review_clears_transition_idempotently(self):
        self.s['missing_evidence']={'requirements':[{'requirement':{'fact':'unclassified'}}]}
        self.prepare()
        r=self.report([],[],blocking=False)
        progress.update_missing_evidence(self.s,self.cache,r,[])
        before=copy.deepcopy(self.s)
        self.assertEqual(self.prepare()[0],3)
        self.assertEqual(before,self.s)


if __name__=='__main__':unittest.main()

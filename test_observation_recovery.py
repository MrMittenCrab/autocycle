"""Read-only legacy migration and same-stage Observer interruption safety."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import native_office as office
import progress
from test_native_office import FakeOffice
from test_flow import Case, fail


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.c=c=Case();self.addCleanup(c.close)
        previous=Path.cwd();os.chdir(c.repo);self.addCleanup(os.chdir,previous)
        self.env=patch.dict(os.environ,c.git_env);self.env.start();self.addCleanup(self.env.stop)
        source=c.repo/'source.xlsx';source.write_bytes(b'checkpointed source')
        c.git('add','source.xlsx');c.git('commit','-qm','Source');c.git('push','-q')
        self.source=source;self.head=c.git('rev-parse','HEAD')
        self.ws=office.Workspace(c.a,FakeOffice())
        queued=office.enqueue(self.ws,{'app':'excel','source':str(source),'worksheet':'Overview'},self.head)
        office.process_requests(self.ws,('excel',),self.head,[queued.stem])
        self.ident=queued.stem
        self.receipt=self.ws.root/'receipts'/queued.name
        self.boundary={'SESSION_NUMBER':'1','RUN_CYCLE':'1','INPUT_CYCLE_ID':''}
        self.state={'allocated':{},'work':None,'attempts':[],'block':None}
        self.cache=c.a/'current-review'
        report={'work_id':'NONE','attempt_id':'NONE','blocking':True,'blocking_reason':'Inspect actual page clarity','retry_observations':[self.ident]}
        self.cache.write_text('REVIEW_STATUS: BLOCKED\nREVIEWED_SHA: '+self.head+'\nREVIEW_TOKEN: old\nAUTOCYCLE_REVIEW: '+json.dumps(report)+'\n')
        (c.a/'resume-state').write_text('STATE_VERSION=2\nSESSION_NUMBER=1\nSESSION_NEW=1\nRUN_MAX=1\nRUN_CYCLE=1\nSTAGE=review_done\nSTATE_BRANCH=checkpoint/test\nINPUT_CYCLE_ID=\n')
        self.originals={p:p.read_bytes() for p in (self.ws.root/'requests').glob('*.json')}
        self.originals[self.receipt]=self.receipt.read_bytes()

    def legacy_block(self):
        previous={'reason':'prior unresolved work','instruction_ids':[]}
        self.state['block']={'reason':'OBSERVATION_EVIDENCE','work_id':'NONE','attempt_id':'NONE','review_token':'old','previous_block':previous,
            'observations':{'id':'a'*32,'head':self.head,'boundary':self.boundary,'requests':[{
                'request_id':self.ident,'replacement_request_id':'b'*32,
                'request_sha256':office.digest(self.ws.root/'requests'/(self.ident+'.json')),
                'receipt_sha256':office.digest(self.receipt)}]}}
        return previous

    def preserved(self):
        for path,raw in self.originals.items():self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self.c.git('rev-parse','HEAD'),self.head)
        self.assertFalse((self.c.a/'observer-call.json').exists())

    def test_cache_only_migration_is_idempotent_without_dispatch(self):
        self.assertEqual(progress.migrate_observations(self.state,self.cache)[0],0)
        self.assertIsNone(self.state['block'])
        self.assertTrue(self.state['legacy_observation_review'])
        saved=copy.deepcopy(self.state)
        self.assertEqual(progress.migrate_observations(self.state,self.cache)[0],0)
        self.assertEqual(self.state,saved)
        self.assertEqual(len(list((self.ws.root/'requests').glob('*.json'))),1)
        self.preserved()

    def test_pending_and_completed_replacements_are_read_not_replayed(self):
        for completed in (False,True):
            previous=self.legacy_block()
            request=json.loads(next(iter(self.originals)).read_text())
            request.update(recovery_id='a'*32,replaces_request_id=self.ident)
            office.enqueue(self.ws,request,self.head,request_id='b'*32)
            if completed:office.process_requests(self.ws,('excel',),self.head,['b'*32])
            self.state.pop('legacy_observation_review',None)
            captures=len(self.ws.backend.events)
            self.assertEqual(progress.migrate_observations(self.state,self.cache)[0],0)
            self.assertEqual(self.state['block'],previous)
            self.assertEqual(len(self.ws.backend.events),captures)
            self.assertEqual(len(list((self.ws.root/'requests').glob('*.json'))),2)
            self.preserved()

    def test_changed_bindings_fail_closed(self):
        for change in ('receipt','source','boundary','work','replacement'):
            self.legacy_block();saved=copy.deepcopy(self.state)
            raw=self.receipt.read_bytes()
            if change=='receipt':
                self.receipt.chmod(0o600);self.receipt.write_bytes(raw+b' ')
            elif change=='source':self.source.write_bytes(b'changed')
            elif change=='boundary':self.state['block']['observations']['boundary']=dict(self.boundary,RUN_CYCLE='2');saved=copy.deepcopy(self.state)
            elif change=='work':
                text=self.cache.read_text();self.cache.write_text(text.replace('"work_id": "NONE"','"work_id": "other"'))
            else:
                request=json.loads((self.ws.root/'requests'/(self.ident+'.json')).read_text())
                request.update(recovery_id='c'*32,replaces_request_id=self.ident)
                office.enqueue(self.ws,request,self.head,request_id='b'*32)
            with self.assertRaises(ValueError):progress.migrate_observations(self.state,self.cache)
            self.assertEqual(self.state,saved)
            self.receipt.write_bytes(raw);self.source.write_bytes(b'checkpointed source')
            if change=='work':self.cache.write_text(text)

    def test_new_review_cannot_create_delegated_recovery(self):
        for report in ({'retry_observations':[self.ident]},{'observation_recovery_id':'a'*32}):
            with self.assertRaisesRegex(ValueError,'same-stage'):
                progress.validate_observations(self.state,report,'',[])

    def test_controller_resumes_legacy_state_with_ordinary_review(self):
        self.legacy_block()
        self.state['block']['previous_block']=None
        (self.c.a/'work-state.json').write_text(json.dumps({'version':progress.VERSION,'branches':{'checkpoint/test':self.state}}))
        result=self.c.run('--resume',FAIL_STAGE='plan');fail(result)
        self.assertEqual([e['kind'] for e in self.c.events()],['review','plan'])
        self.assertEqual(len(list((self.ws.root/'requests').glob('*.json'))),1)
        self.preserved()


class CaptureInterruptionTests(unittest.TestCase):
    def test_finalized_capture_reused_after_receipt_publication_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            backend=FakeOffice();workspace=office.Workspace(directory,backend)
            source=Path(directory)/'source.xlsx';source.write_bytes(b'source')
            request=office.enqueue(workspace,{'app':'excel','source':str(source),'worksheet':'Overview'},'head',request_id='c'*32)
            publish=office.publish_json
            def interrupted(path,value):
                if path.parent.name=='receipts':raise KeyboardInterrupt()
                return publish(path,value)
            with patch.object(office,'publish_json',side_effect=interrupted),self.assertRaises(KeyboardInterrupt):
                office.process_requests(workspace,('excel',),'head',[request.stem])
            self.assertEqual(len([e for e in backend.events if e[0]=='capture']),1)
            office.process_requests(workspace,('excel',),'head',[request.stem])
            self.assertEqual(len([e for e in backend.events if e[0]=='capture']),1)
            self.assertEqual(office.request_record(workspace,request.stem)[1]['status'],'CAPTURED')

    def test_exception_receipt_retains_request_for_another_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            backend=FakeOffice();workspace=office.Workspace(directory,backend)
            source=Path(directory)/'source.xlsx';source.write_bytes(b'source')
            request=office.enqueue(workspace,{'app':'excel','source':str(source),'worksheet':'Overview',
                    'recovery_id':'a'*32},'head',request_id='c'*32)
            with patch.object(workspace,'view',side_effect=RuntimeError('infrastructure unavailable')):
                office.process_requests(workspace,('excel',),'head',[request.stem])
            self.assertEqual(office.request_record(workspace,request.stem)[1]['status'],'BLOCKED')


if __name__=='__main__': unittest.main()

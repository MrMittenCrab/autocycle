"""Bounded tiered retention; all artifacts are disposable temporary fixtures."""
import fcntl
import json
import time
from pathlib import Path
import unittest
from unittest.mock import patch
import native_office as office
import progress
import test_office_boundary as boundary_tests


class RetentionTests(unittest.TestCase):
    def setUp(self):
        boundary_tests.BoundaryTests.setUp(self)
        (self.ac/'resume-state').unlink()
    capture = boundary_tests.BoundaryTests.capture
    state = boundary_tests.BoundaryTests.state

    def boundary(self, stage='review_done', roots=None):
        (self.ac/'resume-state').write_text('STATE_BRANCH=main\nSESSION_NUMBER=1\nSTAGE='+stage+'\n')
        review = self.ac/'current-review'
        if not review.exists(): review.write_text('admitted fixture Review')
        self.state(dict({'attempts':[], 'office_retention':{
            'session':{'branch':'main','number':'1'}, 'review_sha256':office.digest(review),
            'accepted':[], 'admitted_ns':time.time_ns()}}, **(roots or {})))
        (self.ac/'resume-state').touch()

    def test_requires_admission_and_saved_boundary_keeps_legacy(self):
        _, _, legacy = self.capture()
        self.boundary('reviewing')
        _, _, owned = self.capture()
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],0)
        self.boundary()
        report=office.garbage_collect(self.ws,self.repo)
        self.assertEqual(report['deleted_files'],2)
        self.assertTrue(Path(legacy['screenshot']).exists())
        self.assertFalse(Path(owned['screenshot']).exists())

    def test_active_roots_completion_and_immutable_history(self):
        self.boundary()
        captures=[self.capture() for _ in range(7)]
        roots={'attempts':[{'native_requests':[captures[0][0]]}],
               'missing_evidence':{'request_id':captures[1][0]},
               'block':{'previous_block':{'request_id':captures[2][0]},
                        'observations':{'request_id':captures[3][0]}}}
        self.boundary(roots=roots)
        state=json.loads((self.ac/'work-state.json').read_text())
        state['branches']['main']['office_retention']['accepted']=[{'path':str(captures[4][1])}]
        (self.ac/'work-state.json').write_text(json.dumps(state))
        (self.ac/'candidate.json').write_text(json.dumps({'request_id':captures[5][0]}))
        immutable={p:p.read_bytes() for p in self.ws.root.rglob('*.json') if p.name!='review-index.json'}
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],2)
        for _,_,r in captures[:-1]:self.assertTrue(Path(r['screenshot']).exists())
        (self.ac/'resume-state').write_text('STATE_BRANCH=main\nSESSION_NUMBER=1\nSTAGE=session_complete\n')
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],12)
        for p,raw in immutable.items():
            if p.name!='rendered.json':self.assertEqual(p.read_bytes(),raw)
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],0)
        with self.assertRaisesRegex((ValueError,RuntimeError),'prun|retain|fresh'):
            office.validate_word_receipt(captures[4][2])
        with patch.object(progress,'git',return_value=str(self.repo)),patch.object(progress,'acdir',return_value=self.ac):
            with self.assertRaisesRegex(ValueError,'prun|retain|fresh'):
                progress.evidence_valid([{'path':str(captures[4][1]),'sha256':office.digest(captures[4][1]),'observation':'reuse visual'}])

    def test_stale_admission_does_not_prune(self):
        self.boundary(); _,_,r=self.capture()
        (self.ac/'current-review').write_text('unadmitted replacement')
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],0)
        self.assertTrue(Path(r['screenshot']).exists())

    def test_controller_uses_inherited_lock_and_still_serializes_office(self):
        self.boundary();self.capture();self.boundary()
        with (self.ac/'controller.lock').open('rb') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.ws.lock():
                with self.assertRaises(RuntimeError):
                    office.garbage_collect(self.ws,self.repo,controller_fd=lock.fileno())
            self.assertEqual(office.garbage_collect(self.ws,self.repo,controller_fd=lock.fileno())['deleted_files'],2)

    def test_recoveries_and_exact_attempt_binding(self):
        self.boundary()
        used,_,a=self.capture('head'); recovered,_,b=self.capture('old'); _,_,unused=self.capture('head')
        self.boundary(roots={'attempts':[{'id':'active','checkpoint_sha':'head','native_requests':[used]}],
                             'recoveries':[{'block':{'request_id':recovered}}]})
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],2)
        self.assertTrue(Path(a['screenshot']).exists());self.assertTrue(Path(b['screenshot']).exists())
        self.assertFalse(Path(unused['screenshot']).exists())

    def test_completion_releases_newer_capture_and_survives_session_rollover(self):
        self.boundary(); _,_,result=self.capture()
        # Completion releases everything owned, even newer than admission.
        (self.ac/'resume-state').write_text('STATE_BRANCH=main\nSESSION_NUMBER=1\nSTAGE=session_complete\n')
        self.assertEqual(office.garbage_collect(self.ws,self.repo,dry_run=True)['prunable']['files'],2)
        # Existing Session transition keeps the durable completion designation.
        state=json.loads((self.ac/'work-state.json').read_text())['branches']['main']
        state.update(work=None,block=None,allocated={})
        with patch.object(progress,'git',return_value='head'):
            progress.new_session(state,'completed','1')
        state['office_retention']={'session':{'branch':'main','number':'2'},'accepted':[],
                                  'review_sha256':office.digest(self.ac/'current-review'),'admitted_ns':time.time_ns()}
        self.state(state)
        (self.ac/'resume-state').write_text('STATE_BRANCH=main\nSESSION_NUMBER=2\nSTAGE=review_done\n')
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],2)
        self.assertFalse(Path(result['screenshot']).exists())

    def test_completed_historical_family_remains_live_when_reused_by_active_session(self):
        self.boundary();_,receipt,result=self.capture()
        state={'attempts':[], 'office_completed_sessions':['1'], 'office_retention':{
            'session':{'branch':'main','number':'2'}, 'accepted':[{'path':str(receipt)}],
            'review_sha256':office.digest(self.ac/'current-review'),'admitted_ns':time.time_ns()}}
        self.state(state)
        (self.ac/'resume-state').write_text('STATE_BRANCH=main\nSESSION_NUMBER=2\nSTAGE=review_done\n')
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],0)
        self.assertTrue(Path(result['screenshot']).exists())

    def test_crash_between_deletions_retries_without_rewriting_receipts(self):
        self.boundary();_,receipt,result=self.capture();self.boundary()
        before=receipt.read_bytes();unlink=Path.unlink;count=0
        def interrupt(path,*args,**kwargs):
            nonlocal count
            if path.name in ('view.png','rendered.json'):
                count+=1
                if count==2:raise OSError('injected deletion interruption')
            return unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',interrupt):
            with self.assertRaisesRegex(OSError,'interruption'):office.garbage_collect(self.ws,self.repo)
        self.assertEqual(office.artifact_state(result['artifact_retention']),'pruning')
        with self.assertRaisesRegex(ValueError,'pruned'):office.require_live_artifacts(result)
        self.assertEqual(office.garbage_collect(self.ws,self.repo)['deleted_files'],1)
        self.assertEqual(receipt.read_bytes(),before)
        self.assertEqual(office.artifact_state(result['artifact_retention']),'pruned')

class ControllerRetentionTests(unittest.TestCase):
    def test_real_controller_completion_prunes_only_after_saved_state(self):
        self.check_completion(['--extend','1'])

    def test_manual_review_completion_uses_the_same_boundary(self):
        self.check_completion(['--review-only'])

    def check_completion(self, mode):
        from test_session import case
        from test_flow import ok
        from test_native_office import FakeOffice
        c=case();self.addCleanup(c.close)
        ok(c.run('1'))
        ws=office.Workspace(c.a,FakeOffice())
        source=c.a/'fixture.docx';source.write_bytes(b'fixture source')
        head=c.git('rev-parse','HEAD')
        request=office.enqueue(ws,{'app':'word','source':str(source)},head)
        receipt=Path(office.process_requests(ws,('word',),head,[request.stem])[0])
        raw=receipt.read_bytes();result=json.loads(raw)
        state=json.loads((c.a/'work-state.json').read_text())
        state['branches']['checkpoint/test']['attempts'][-1]['native_requests']=[request.stem]
        (c.a/'work-state.json').write_text(json.dumps(state))
        if mode == ['--review-only']:
            # Manual stages require absent controller state; keep factual progress.
            (c.a/'resume-state').unlink()
        completed=c.run(*mode,ENDPOINT_REACHED='1');ok(completed)
        self.assertIn('STAGE=session_complete',(c.a/'resume-state').read_text())
        self.assertFalse(Path(result['screenshot']).exists())
        self.assertEqual(receipt.read_bytes(),raw)
        self.assertEqual((ws.root/'gc-last-error.log').read_text(),'')
        final_report=json.loads((ws.root/'gc-last-report.json').read_text())
        self.assertEqual(final_report['deleted_files'],2)
        self.assertIn('    ✓ Pruned  2 files · '+str(final_report['deleted_bytes'])+' B',completed.stdout)
        ok(c.run('--resume'))
        self.assertEqual(json.loads((ws.root/'gc-last-report.json').read_text())['deleted_files'],0)

class SaveBoundaryTests(unittest.TestCase):
    def test_failed_state_save_never_runs_gc(self):
        import os, subprocess, tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'office/evidence').mkdir(parents=True)
            shim=root/'python3'
            shim.write_text('#!/bin/sh\nif [ "$1" = "-" ]; then exit 1; fi\ntouch "'+str(root/'gc-ran')+'"\n')
            shim.chmod(0o755)
            source=Path('autocycle').read_text()
            function=source[source.index('save_state() {'):source.index('\nload_state() {')]
            script='AC_DIR="$1"; STATE="$1/resume-state"; STAGE=session_complete; OFFICE_HELPER=fixture\n'+function+'\nsave_state\n'
            run=subprocess.run(['/bin/bash','-c',script,'fixture',str(root)],env=dict(os.environ,PATH=str(root)+':'+os.environ['PATH']))
            self.assertNotEqual(run.returncode,0)
            self.assertFalse((root/'gc-ran').exists())

if __name__=='__main__':unittest.main()

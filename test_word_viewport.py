"""Word permission and rendered-page acceptance; isolated from native applications."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import native_office as o
from test_native_office import FakeOffice, png
from test_office_references import OfficeEvents


class WordViewportTests(unittest.TestCase):
    def test_human_resolution_continues_one_owned_open(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'word-view.docx'; path.write_bytes(b'owned')
            backend=o.MacOffice(); backend.permission_poll_seconds=.005
            events=OfficeEvents(); release=threading.Event(); calls=[]
            def script(app,body,*args,**kwargs):
                if 'set targetDoc to open' in body:
                    calls.append(kwargs); self.assertTrue(release.wait(1))
                return o.MacOffice.script(backend,app,body,*args,**kwargs)
            def observe():release.set(); return True
            with patch.object(backend,'script',side_effect=script), patch.object(backend,'word_permission_pending',side_effect=observe), patch.object(o.subprocess,'run',side_effect=events.run), patch('sys.stderr',new=io.StringIO()) as output:
                backend.open('word',path)
                self.assertIn('waiting for human',output.getvalue())
                self.assertIn(('word',str(path)),backend.opened)
                backend.close('word',path,False)
            self.assertEqual(calls,[{'timeout':300,'reference':True}])
            self.assertEqual(events.opened,{'user.xlsx','user.docx'})

    def test_permission_timeout_is_external_action_and_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            backend=o.MacOffice(); backend.permission_poll_seconds=.005
            barrier=threading.Event(); calls=[]
            def fail(*a,**kw):
                calls.append(kw); self.assertTrue(barrier.wait(1))
                raise subprocess.TimeoutExpired('osascript',302)
            def pending():barrier.set(); return True
            with patch.object(backend,'script',side_effect=fail), patch.object(backend,'word_permission_pending',side_effect=pending), patch('sys.stderr',new=io.StringIO()):
                with self.assertRaises(o.NativeError) as caught:backend.word_open('open',Path(d)/'word-view.docx')
            self.assertIn('External action required',caught.exception.permission_action)
            fake=FakeOffice(); fake.open=lambda *a: (_ for _ in ()).throw(caught.exception)
            w=o.Workspace(d,fake,wait_seconds=.001); source=Path(d)/'source.docx';source.write_bytes(b'source')
            for verify in (False,True):
                result=w.verify('word',source,['check','{document}']) if verify else w.view({'app':'word','source':str(source),'source_sha256':o.digest(source),'page':1})
                self.assertEqual(result['status'],'BLOCKED')
                self.assertEqual(result['external_dependency']['kind'],'permission')
                self.assertEqual(result['failure_kind'],'native_access')
                self.assertIn('External action required',result['action'])
                self.assertNotIn('screenshot',result)
            self.assertEqual(len(calls),1)
            self.assertFalse(backend.opened)

    def test_open_resolution_at_poll_deadline_keeps_returned_reference(self):
        with patch.object(o.concurrent.futures,'ThreadPoolExecutor') as pool:
            future=pool.return_value.__enter__.return_value.submit.return_value
            future.result.side_effect=[o.concurrent.futures.TimeoutError(), 'owned-reference']
            future.done.return_value=True;future.exception.return_value=None
            self.assertEqual(o.MacOffice().word_open('open',Path('/tmp/word-view.docx')),'owned-reference')

    def test_unknown_failure_is_not_permission(self):
        backend=o.MacOffice()
        with patch.object(backend,'script',side_effect=o.NativeError('Word error')):
            with self.assertRaises(o.NativeError) as caught:backend.word_open('open',Path('/tmp/word-view.docx'))
        self.assertFalse(hasattr(caught.exception,'permission_action'))

    def test_page_navigation_selects_range_without_scroll_heuristic(self):
        b=o.MacOffice(); scripts=[]
        with patch.object(b,'find',side_effect=lambda a,p,body:body), patch.object(b,'script',side_effect=lambda a,body,*args,**kw:scripts.append(body)):
            for page in (1,3,6):b.position('word',Path('/owned/word-view.docx'),{'page':page})
        for page,body in zip((1,3,6),scripts):
            self.assertIn(f'position absolute count {page}',body)
            self.assertIn('select pageRange',body)
            self.assertNotIn('page scroll',body)
            self.assertNotIn('vertical percent',body)

    def test_independent_page_marker_acceptance_and_wrong_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'word-view.docx'; p.write_bytes(b'copy'); image=Path(d)/'view.png';png(image)
            pages=[f'VIEWPORT TEST PAGE {i}' for i in range(1,7)]
            for page in (1,3,6):
                page_map={'page':page,'pages':pages}
                for visible in (1,3,6):
                    native={'word_rendered':{'method':'vision-document-canvas','lines':[{'text':pages[visible-1],'confidence':1}]}}
                    if visible == page:
                        proof=o.MacOffice.verify_word_rendered(p,image,page_map,native)
                        self.assertEqual(proof['page'],page)
                        self.assertEqual(proof['screenshot_sha256'],o.digest(image))
                        self.assertEqual(proof['copy_sha256'],o.digest(p))
                    else:
                        with self.assertRaises(o.NativeError):o.MacOffice.verify_word_rendered(p,image,page_map,native)
            for native in (None,{}, {'word_rendered':{'method':'selection-page','page':3}}, {'word_rendered':{'method':'vision-document-canvas','lines':[{'text':pages[2],'confidence':.4}]}}):
                with self.assertRaises(o.NativeError):o.MacOffice.verify_word_rendered(p,image,{'page':3,'pages':pages},native)
            repeated={'page':1,'pages':[pages[0],pages[0]]}
            native={'word_rendered':{'method':'vision-document-canvas','lines':[{'text':pages[0],'confidence':1}]}}
            with self.assertRaises(o.NativeError):o.MacOffice.verify_word_rendered(p,image,repeated,native)

    def test_stuck_viewport_transaction_fails_closed_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            b=FakeOffice(); w=o.Workspace(d,b); s=Path(d)/'source.docx';s.write_bytes(b'original')
            unrelated=Path(d)/'user.docx';b.opened.add(unrelated)
            pages=['Fixture rendered page one','Fixture rendered page two','Fixture rendered page three']
            b.word_page_map=lambda p,r:{'page':3,'pages':pages}
            b.word_content=lambda p:''.join(pages)
            result=w.view({'app':'word','source':str(s),'source_sha256':o.digest(s),'page':3})
            self.assertEqual(result['status'],'BLOCKED')
            self.assertIn('not independently visible',result['action'])
            self.assertEqual(len([e for e in b.events if e[0]=='capture']),2)
            self.assertEqual(b.opened,{unrelated})
            self.assertEqual(s.read_bytes(),b'original')
            self.assertFalse(list(w.root.glob('evidence/**/*.png')))

    def test_changed_owned_copy_cannot_be_bound_to_original_source(self):
        with tempfile.TemporaryDirectory() as d:
            b=FakeOffice(); w=o.Workspace(d,b); p=w.populate('word','view')
            original=o.digest(p);w.open('word',p);p.write_bytes(b'changed copy')
            with self.assertRaisesRegex(o.NativeError,'no longer matches'):
                w.capture_owned('word',p,{'page':1,'source_sha256':original},Path(d)/'out.png')
            self.assertFalse(any(e[0]=='capture' for e in b.events))

    def test_word_wrong_owned_identity_stops_before_navigation_or_capture(self):
        with tempfile.TemporaryDirectory() as d:
            b=FakeOffice(); w=o.Workspace(d,b); p=w.populate('word','view')
            w.open('word',p)
            def wrong(*args):o.MacOffice.confirm_owned_identity(p,str(p),'/other/word-view.docx')
            b.verify_document=wrong
            with self.assertRaises(o.NativeError):w.capture_owned('word',p,{'page':3},Path(d)/'out.png')
            self.assertFalse(any(e[0] in ('position','capture') for e in b.events))

    def test_pending_permission_blocker_survives_unowned_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            b=FakeOffice(); w=o.Workspace(d,b); source=Path(d)/'source.docx';source.write_bytes(b'original')
            pending=[False]
            def query(*args):
                if pending[0]:raise o.NativeError('Unowned pending lock')
                return False
            def opening(*args):
                pending[0]=True
                error=o.NativeError('External action required: Grant File Access')
                error.permission_action=str(error);raise error
            b.open=opening;b.is_open=query
            result=w.view({'app':'word','source':str(source),'source_sha256':o.digest(source)})
            self.assertEqual(result['status'],'BLOCKED')
            self.assertIn('External action required',result['action'])
            self.assertEqual(result['cleanup_error'],'Unowned pending lock')
            self.assertEqual(result['external_dependency']['kind'],'permission')
            pending[0]=False; output=io.StringIO()
            self.assertFalse(o.preflight(w,('word',),output))
            self.assertIn('External action required',output.getvalue())
            self.assertFalse(any(e[0]=='close' for e in b.events))

    def test_cached_word_observation_requires_independent_proof(self):
        with tempfile.TemporaryDirectory() as d:
            w=o.Workspace(d,FakeOffice()); source=Path(d)/'source.docx';source.write_bytes(b'source')
            request=o.enqueue(w,{'app':'word','source':str(source),'page':3},'head')
            receipt=w.root/'receipts'/request.name;receipt.parent.mkdir()
            image=Path(d)/'view.png';png(image)
            record={'request_id':request.stem,'request':json.loads(request.read_text()),'status':'CAPTURED',
                    'source':str(source),'source_sha256':o.digest(source),'screenshot':str(image),
                    'screenshot_sha256':o.digest(image)}
            receipt.write_text(json.dumps(record))
            with self.assertRaises(o.NativeError):o.request_record(w,request.stem)
            self.assertEqual(o.review_evidence(w)[0]['status'],'BLOCKED')

    def test_legacy_or_misbound_word_receipt_rejected(self):
        result={'request':{'app':'word','page':3},'source_sha256':'source','screenshot_sha256':'image'}
        with self.assertRaises(o.NativeError):o.validate_word_receipt(result)
        result['word_visible_page']={'method':'unique-page-text-in-captured-canvas','page':3,'copy_sha256':'source','screenshot_sha256':'image'}
        o.validate_word_receipt(result)
        for key,value in [('page',1),('copy_sha256','wrong'),('screenshot_sha256','wrong')]:
            wrong=dict(result,word_visible_page=dict(result['word_visible_page'],**{key:value}))
            with self.assertRaises(o.NativeError):o.validate_word_receipt(wrong)

if __name__=='__main__':unittest.main()

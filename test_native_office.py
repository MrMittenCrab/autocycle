"""Deterministic Office/macOS tests; never drive the user's GUI."""
import contextlib
import hashlib
import io
import json
import platform
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
import native_office as office


def png(path, uniform=False):
    def chunk(kind, data):
        return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data))
    rows=b''.join(b'\0'+bytes([0 if uniform else (x+y)%256 for x in range(320*3)]) for y in range(240))
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',320,240,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))


class FakeOffice:
    validate_request = staticmethod(office.MacOffice.validate_request)
    def __init__(self):
        self.opened=set();self.events=[];self.pending=0;self.denied=False;self.black=False
    def is_open(self,app,path):
        self.events.append(('query',app,path))
        if self.pending:
            self.pending-=1
            return False
        return path in self.opened
    def open(self,app,path):
        self.events.append(('open',app,path))
        if not self.denied:self.opened.add(path)
    def close(self,app,path,save):
        self.events.append(('close',app,path,save));self.opened.discard(path)
    def calculate_save(self,app,path):
        self.events.append(('save',app,path));path.write_bytes(path.read_bytes()+b'native state')
    def verify_document(self,app,path):self.events.append(('identity',app,path))
    def position(self,app,path,request):self.events.append(('position',app,path));return [0,0,800,600]
    def confirm_view(self,app,path,request):self.events.append(('confirm',app,path));return [0,0,800,600]
    def word_content(self,path):return 'Fixture rendered page one'
    def word_page_map(self,path,request):return {'page':1,'pages':['Fixture rendered page one']}
    def capture(self,path,bounds):
        self.events.append(('capture',path));png(path,self.black)
        return {'width':320,'height':240,'word_rendered':{'method':'vision-document-canvas','lines':[{'text':'Fixture rendered page one','confidence':1.0}]}}
    def frontmost(self):return 'test.frontmost'
    def restore(self,bundle):self.events.append(('restore',bundle))


class OfficeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ac=self.root/'.git/autocycle';self.backend=FakeOffice()
        self.ws=office.Workspace(self.ac,self.backend,wait_seconds=.05,poll_seconds=.001)
    def source(self,app='excel'):
        p=self.root/('source'+office.EXTENSIONS[app]);p.write_bytes(b'authoritative');return p
    def test_capabilities_absent_empty_and_valid(self):
        self.assertEqual(office.capabilities(self.root),())
        p=self.root/'.autocycle.toml';p.write_text('[other]\nx=1\n');self.assertEqual(office.capabilities(self.root),())
        p.write_text('[capabilities]\nnative_office=["excel","word"]\n')
        self.assertEqual(office.capabilities(self.root),('excel','word'))
        for value in ('["powerpoint"]','"excel"','[1]'):
            p.write_text('[capabilities]\nnative_office='+value+'\n')
            with self.assertRaises(ValueError):office.capabilities(self.root)
    def test_fixed_slots_and_close_semantics(self):
        user=self.root/'user.xlsx';historical=self.ac/'excel-verification-old/saved-copy.xlsx'
        self.backend.opened.update((user,historical))
        for app in office.EXTENSIONS:
            for role in ('verify','view'):
                slot=self.ws.slot(app,role);self.backend.opened.add(slot)
                with self.ws.lock():self.ws.populate(app,role,self.source(app))
                close=[e for e in self.backend.events if e[0]=='close'][-1]
                self.assertEqual(close,('close',app,slot,role=='verify'))
                self.assertEqual(slot.resolve(),(self.ac/'office'/f'{app}-{role}{office.EXTENSIONS[app]}').resolve())
                self.assertEqual(slot.read_bytes(),b'authoritative')
        self.assertTrue({user,historical} <= self.backend.opened)
    @unittest.skipUnless(platform.system()=='Darwin','macOS filesystem aliases')
    def test_macos_temporary_alias_resolves_to_same_slot(self):
        # Derive the alias from the OS temporary directory, not a hard-coded
        # macOS prefix. Some environments already supply a canonical path.
        if self.root == self.root.resolve():
            self.skipTest('OS temporary directory is already canonical')
        source=self.source()
        with self.ws.lock():self.ws.populate('excel','view',source)
        alias=self.ac/'office'/'excel-view.xlsx'
        canonical=self.ws.slot('excel','view')
        self.assertNotEqual(alias,canonical)
        self.assertTrue(alias.samefile(canonical))
        self.assertEqual(alias.resolve(),canonical.resolve())

    def test_identity_preserved_and_alias_rejected(self):
        source=self.source();slot=self.ws.slot('excel','view')
        with self.ws.lock():self.ws.populate('excel','view',source)
        inode=slot.stat().st_ino
        with self.ws.lock():self.ws.populate('excel','view',source)
        self.assertEqual(inode,slot.stat().st_ino)
        slot.unlink();slot.hardlink_to(source)
        with self.ws.lock(),self.assertRaises(office.NativeError):self.ws.populate('excel','view',source)
        self.assertEqual(source.read_bytes(),b'authoritative')
        with self.assertRaises(ValueError):self.ws.close('excel',source)
    def test_access_success_pending_denied_and_no_capture(self):
        self.backend.pending=2
        out=io.StringIO()
        self.assertTrue(office.preflight(self.ws,('excel','word'),out))
        self.assertIn('Access      ✓',out.getvalue());self.assertFalse(self.backend.opened)
        opened=[e[2] for e in self.backend.events if e[0]=='open']
        self.assertEqual(len(opened),4);self.assertTrue(all(p.parent==self.ws.root for p in opened))
        self.assertFalse(any(e[0]=='capture' for e in self.backend.events))
        self.backend.denied=True;out=io.StringIO()
        self.assertFalse(office.preflight(self.ws,('excel',),out))
        self.assertRegex(out.getvalue(),r'Access      ✗ \d+:\d+  Excel fixed-slot access failed')
    def test_verify_freezes_native_bytes_and_view_preserves_them(self):
        for app in office.EXTENSIONS:
            source=self.source(app)
            runner=lambda args,**kw:subprocess.CompletedProcess(args,0,'verified','')
            with patch.object(office.subprocess,'run',side_effect=runner):
                result=self.ws.verify(app,source,['check','{document}'])
            self.assertEqual(result['status'],'VERIFIED',result)
            snapshot=Path(result['snapshot']);before=snapshot.read_bytes()
            request={'app':app,'source':str(snapshot),'source_sha256':office.digest(snapshot),'worksheet':'Sheet1'}
            result=self.ws.view(request)
            self.assertEqual(result['status'],'CAPTURED',result)
            self.assertEqual(snapshot.read_bytes(),before);self.assertEqual(source.read_bytes(),b'authoritative')
            self.assertFalse(any(e[0]=='open' and e[2]==snapshot for e in self.backend.events))
            self.assertIn(('close',app,self.ws.slot(app,'view'),False),self.backend.events)
            self.assertTrue(Path(result['screenshot']).is_file())
    def test_capture_failure_preserves_source_and_is_not_presentation_failure(self):
        self.backend.black=True;source=self.source()
        result=self.ws.view({'app':'excel','source':str(source),'source_sha256':office.digest(source),'worksheet':'Sheet1'})
        self.assertEqual(result['failure_kind'],'native_capture');self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(source.read_bytes(),b'authoritative');self.assertFalse(self.backend.opened)
        self.assertNotIn('screenshot',result)
    def test_access_failure_distinct_from_capture_failure(self):
        self.backend.denied=True;source=self.source()
        result=self.ws.view({'app':'excel','source':str(source),'source_sha256':office.digest(source),'worksheet':'Sheet1'})
        self.assertEqual(result['failure_kind'],'native_access')
    def test_png_validation(self):
        path=self.root/'image.png'
        for data in (None,b'',b'not a PNG'):
            if data is not None:path.write_bytes(data)
            with self.assertRaises(office.NativeError):office.validate_png(path)
        png(path,True)
        with self.assertRaises(office.NativeError):office.validate_png(path)
        png(path);self.assertEqual(office.validate_png(path)['width'],320)
    def test_global_lock_serializes_apps(self):
        other=office.Workspace(self.ac,self.backend)
        with self.ws.lock(),self.assertRaises(office.NativeError):
            with other.lock():pass
    def test_provider_handoff_idempotent_and_retained_capture_revalidated(self):
        source=self.source();request={'app':'excel','source':str(source),'worksheet':'Sheet1'}
        req=office.enqueue(self.ws,request,head='head')
        self.assertFalse(any(e[0]=='capture' for e in self.backend.events))
        results=office.process_requests(self.ws,('excel',),head='head')
        self.assertEqual(len(results),1)
        result=json.loads(Path(results[0]).read_text());self.assertEqual(result['status'],'CAPTURED')
        self.assertEqual(result['request_id'],req.stem)
        count=len([e for e in self.backend.events if e[0]=='capture'])
        office.process_requests(self.ws,('excel',),head='head')
        self.assertEqual(count,len([e for e in self.backend.events if e[0]=='capture']))
        image=Path(result['screenshot'])
        original_bytes=image.read_bytes();original_mode=image.stat().st_mode
        self.assertEqual(original_mode & 0o222,0)
        self.assertEqual(office.review_evidence(self.ws)[0]['status'],'CAPTURED')
        # Corrupt only a writable fixture, never retained evidence or its mode.
        fixture=office.Workspace(self.root/'corruption-fixture',self.backend)
        writable=self.root/'corrupt-copy.png';writable.write_bytes(original_bytes)
        png(writable,True)
        with self.assertRaises(office.NativeError):office.validate_png(writable)
        receipts=fixture.root/'receipts';receipts.mkdir(parents=True)
        (receipts/'fixture.json').write_text(json.dumps(dict(result,screenshot=str(writable))))
        records=office.review_evidence(fixture)
        self.assertEqual(image.read_bytes(),original_bytes)
        self.assertEqual(image.stat().st_mode,original_mode)
        self.assertIn('native_capture',json.dumps(records));self.assertNotIn('"status": "CAPTURED"',json.dumps(records))
    def test_native_scripts_scope_paths_and_do_not_automate_security(self):
        from test_office_references import OfficeEvents
        events=OfficeEvents();scripts=[]
        def run(args,**kwargs):
            scripts.append(args)
            return events.run(args,**kwargs)
        backend=office.MacOffice()
        with patch.object(office.subprocess,'run',side_effect=run):
            for app in office.EXTENSIONS:
                for role in ('view','verify'):
                    path=self.ws.slot(app,role)
                    path.parent.mkdir(parents=True,exist_ok=True)
                    self.assertFalse(backend.is_open(app,path))
                    backend.open(app,path)
                    backend.calculate_save(app,path)
                    backend.position(app,path,{'worksheet':'A "quoted" sheet','start':5,'end':6})
                    backend.close(app,path,role=='verify')
        code='\n'.join(args[args.index('-e')+1] for args in scripts)
        self.assertIn('close targetDoc saving no',code)
        self.assertIn('close targetDoc saving yes',code)
        self.assertNotIn('calculate full',code)
        self.assertNotIn('System Events',code)
        self.assertNotIn('securebookmarks',code.lower())
        self.assertNotIn('A "quoted" sheet',code)  # Arguments stay data.

    def test_changed_request_source_is_not_opened_or_captured(self):
        source=self.source()
        path=office.enqueue(self.ws,{'app':'excel','source':str(source),'worksheet':'Sheet1'},head='head')
        source.write_bytes(b'new product bytes')
        records=office.process_requests(self.ws,('excel',),head='new-head')
        result=json.loads(Path(records[0]).read_text())
        self.assertEqual(result['status'],'BLOCKED')
        self.assertFalse(any(e[0] in ('open','capture') for e in self.backend.events))
        self.assertEqual(source.read_bytes(),b'new product bytes')

    def test_power_checks_own_pid_and_flags(self):
        text='pid 99(caffeinate): PreventUserIdleDisplaySleep\npid 99(caffeinate): PreventUserIdleSystemSleep'
        with patch.object(office.subprocess,'run',return_value=subprocess.CompletedProcess([],0,text,'')) as run:
            office.check_power(99,attempts=1)
        self.assertEqual(run.call_args.args[0],['/usr/bin/pmset','-g','assertions'])
        with patch.object(office.subprocess,'run',return_value=subprocess.CompletedProcess([],0,text.replace('99','98'),'')):
            with self.assertRaises(office.NativeError):office.check_power(99,attempts=1)

if __name__=='__main__':unittest.main()

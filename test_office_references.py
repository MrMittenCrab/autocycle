"""Exercise the real backend/preflight with Office's Apple-event boundary simulated."""
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import native_office as office


LOCK_NAMES = {'excel-verify.xlsx':'~$excel-verify.xlsx', 'excel-view.xlsx':'~$excel-view.xlsx',
              'word-verify.docx':'~$rd-verify.docx', 'word-view.docx':'~$rd-view.docx'}


class OfficeEvents:
    def __init__(self):
        self.opened={'user.xlsx', 'user.docx'}
        self.closed=[]
        self.denied=False
        self.metadata_errors=0
        self.wrong_reference=False
        self.partial_failure=False

    def run(self, command, **kwargs):
        code=command[command.index('-e')+1]
        argv=command[command.index('--')+1:] if '--' in command else []
        if not argv:return subprocess.CompletedProcess(command,0,'','')
        path=Path(argv[0]);name=path.name
        def result(text='',rc=0):return subprocess.CompletedProcess(command,rc,text,'Microsoft Excel error -50' if rc else '')
        if 'update links 0' in code:return result('',1)
        if 'POSIX file slotPath' in code.split('tell application',1)[-1]:return result('',1)
        if 'full name' in code or 'name of targetDoc' in code:
            self.metadata_errors+=1
            return result('',1)
        lock=path.with_name(LOCK_NAMES[name])
        if 'set targetDoc to open' in code or ('open workbook workbook' in code and 'set targetDoc' not in code):
            if self.denied:return result('',1)
            self.opened.add(name);lock.write_bytes(b'Office owner')
            if self.partial_failure:return result('',1)
            if self.wrong_reference:name='user.xlsx'
            kind='workbook' if name.endswith('xlsx') else 'document'
            app='Microsoft Excel' if kind=='workbook' else 'Microsoft Word'
            return result(f'{kind} "{name}" of application "{app}"')
        if 'close targetDoc saving' in code:
            self.opened.remove(name);self.closed.append(name);lock.unlink()
            return result('CLOSED')
        if 'return "OPEN"' in code:
            return result('OPEN' if name in self.opened else 'CLOSED')
        return result('SAVED')


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.events=OfficeEvents()
        self.patch=patch.object(office.subprocess,'run',side_effect=self.events.run)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.ws=office.Workspace(Path(self.tmp.name),wait_seconds=.01,poll_seconds=.001)

    def test_access_metadata_minus_50_does_not_block_or_close_user_documents(self):
        out=io.StringIO()
        self.assertTrue(office.preflight(self.ws,('excel','word'),out),out.getvalue())
        self.assertEqual(self.events.opened,{'user.xlsx','user.docx'})
        self.assertEqual(self.events.closed,['excel-verify.xlsx','excel-view.xlsx','word-verify.docx','word-view.docx'])
        self.assertEqual(self.events.metadata_errors,0)
        self.assertIn('Access      ✓',out.getvalue())

    def test_failed_open_is_access_failure(self):
        self.events.denied=True
        out=io.StringIO()
        self.assertFalse(office.preflight(self.ws,('excel',),out))
        self.assertIn('Access      ✗',out.getvalue())
        self.assertEqual(self.events.opened,{'user.xlsx','user.docx'})
        self.assertEqual(self.events.closed,[])

    def test_stale_lock_is_not_open_evidence_or_permission_to_overwrite(self):
        for app in ('excel','word'):
            path=self.ws.slot(app,'verify');path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'preserve')
            path.with_name(LOCK_NAMES[path.name]).write_bytes(b'stale')
            out=io.StringIO()
            self.assertFalse(office.preflight(self.ws,(app,),out))
            self.assertEqual(path.read_bytes(),b'preserve')
        self.assertEqual(self.events.closed,[])

    def test_lost_or_replaced_lock_cannot_close_replacement_document(self):
        for app in ('excel','word'):
            for replacement in (False,True):
                ws=office.Workspace(Path(self.tmp.name)/f'{app}-{replacement}',wait_seconds=.01,poll_seconds=.001)
                path=ws.populate(app,'verify');ws.open(app,path)
                lock=path.with_name(LOCK_NAMES[path.name]);lock.unlink()
                if replacement:lock.write_bytes(b'replacement lock')
                with self.assertRaises(office.NativeError):ws.close(app,path)
                self.events.opened.discard(path.name)
        self.assertEqual(self.events.closed,[])

    def test_wrong_reference_or_partial_open_never_authorizes_close(self):
        for flag in ('wrong_reference','partial_failure'):
            with self.subTest(flag=flag):
                setattr(self.events,flag,True)
                ws=office.Workspace(Path(self.tmp.name)/flag,wait_seconds=.01,poll_seconds=.001)
                self.assertFalse(office.preflight(ws,('excel',),io.StringIO()))
                self.assertEqual(self.events.closed,[])
                self.assertIn('user.xlsx',self.events.opened)
                self.events.opened.discard('excel-verify.xlsx')
                setattr(self.events,flag,False)

    def test_preexisting_same_name_is_not_owned(self):
        self.events.opened.add('excel-verify.xlsx')
        out=io.StringIO()
        self.assertFalse(office.preflight(self.ws,('excel',),out))
        self.assertIn('excel-verify.xlsx',self.events.opened)
        self.assertEqual(self.events.closed,[])


if __name__=='__main__':unittest.main()

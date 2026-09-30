"""Excel is always mocked here; real native check is opt-in."""
import tempfile, unittest, subprocess, sys
from pathlib import Path
from unittest.mock import patch
import excel_verification as excel

class ExcelTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.source=self.root/'original.xlsx';self.source.write_bytes(b'original workbook')
  self.evidence=self.root/'evidence';self.evidence.mkdir()
 def run_check(self, runner):
  with patch.object(excel,'native_available',return_value=None),patch.object(excel.subprocess,'run',side_effect=runner):
   return excel.verify(self.source,[sys.executable,'check.py','{workbook}'],self.evidence)
 def test_success_verifies_copy_and_preserves_source(self):
  calls=[]
  def run(args,**kwargs):
   calls.append(args)
   if 'osascript' in args and Path(args[-2]).name=='ensure_closed.applescript':
    return subprocess.CompletedProcess(args,0,'closed','')
   if 'osascript' in args:
    target=Path(args[-1]);self.assertNotEqual(target,self.source);target.write_bytes(b'Excel cached values')
   else:
    self.assertEqual(Path(args[-1]).read_bytes(),b'Excel cached values')
   return subprocess.CompletedProcess(args,0,'verified','')
  result=self.run_check(run)
  self.assertEqual(result['status'],'VERIFIED');self.assertEqual(result['action'],'NONE')
  self.assertEqual(len(calls),3);self.assertEqual(self.source.read_bytes(),b'original workbook')
  self.assertTrue(Path(result['copy']).exists())
 def test_automation_failure_specific_action_no_verifier(self):
  calls=[]
  def run(args,**kwargs):
   calls.append(args);return subprocess.CompletedProcess(args,1,'','Not authorized to send Apple events. (-1743)')
  result=self.run_check(run)
  self.assertEqual(result['status'],'BLOCKED');self.assertIn('Automation',result['action'])
  self.assertIn('-1743',result['action']);self.assertEqual(len(calls),1)
  self.assertEqual(self.source.read_bytes(),b'original workbook')
 def test_cached_values_still_missing_blocks(self):
  def run(args,**kwargs):
   return subprocess.CompletedProcess(args,0 if 'osascript' in args else 1,'','Missing cached Sheet1!A1' if 'osascript' not in args else '')
  result=self.run_check(run)
  self.assertEqual(result['status'],'BLOCKED');self.assertIn('Missing cached Sheet1!A1',result['action'])
 def test_timeout_is_specific_and_never_runs_verification(self):
  def run(args,**kwargs):return subprocess.CompletedProcess(args,124,'','')
  result=self.run_check(run)
  self.assertEqual(result['status'],'BLOCKED');self.assertIn('timed out',result['action'])
  self.assertIn('dialog',result['action'])
 def test_missing_excel(self):
  with patch.object(excel,'native_available',side_effect=RuntimeError('Microsoft Excel is not installed')):
   result=excel.verify(self.source,['verify','{workbook}'],self.evidence)
  self.assertEqual(result['status'],'BLOCKED');self.assertIn('not installed',result['action'])
 def test_verifier_must_receive_copy(self):
  with self.assertRaises(ValueError):excel.verify(self.source,['verify',str(self.source)],self.evidence)
 def test_script_is_scoped(self):
  script=excel.APPLESCRIPT
  self.assertNotIn('calculate full',script);self.assertNotIn('quit',script)
  self.assertNotIn('active workbook',script);self.assertNotIn('display alerts',script)
  self.assertIn('close verificationBook',script);self.assertIn('save verificationBook',script)
  self.assertIn('calculate (used range of targetSheet)',script)

if __name__=='__main__':unittest.main()

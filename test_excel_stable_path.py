"""Native calls are mocked: granting one file must cover subsequent retries."""
import json
import fcntl
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch
import excel_verification as excel


class StablePathTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.source=self.root/'source.xlsx'
  self.source.write_bytes(b'original one');self.evidence=self.root/'evidence'
  self.authorized=set();self.open_paths=[];self.verifier_paths=[]
 def run_native(self,args,**kwargs):
  path=Path(args[-1])
  if 'osascript' in args:
   if Path(args[-2]).name=='ensure_closed.applescript':
    return subprocess.CompletedProcess(args,0,'closed','')
   self.open_paths.append(path)
   if path not in self.authorized:
    self.authorized.add(path) # Simulate one human grant for this path.
    if len(self.authorized)>1:
     return subprocess.CompletedProcess(args,1,'','Excel open failed: Grant Access required')
   path.write_bytes(b'cached '+path.read_bytes())
  else:
   self.verifier_paths.append(path)
   assert path.read_bytes().startswith(b'cached ')
  return subprocess.CompletedProcess(args,0,'verified','')
 def verify(self):
  with patch.object(excel,'native_available'),patch.object(excel.subprocess,'run',side_effect=self.run_native):
   return excel.verify(self.source,['check','{workbook}'],self.evidence)
 def test_authorized_path_reused_after_source_content_changes(self):
  first=self.verify();self.assertEqual(first['status'],'VERIFIED')
  first_evidence=Path(first['evidence']).read_bytes()
  first_copy=Path(first.get('snapshot',first['copy'])).read_bytes()
  self.source.write_bytes(b'original two')
  second=self.verify()
  self.assertEqual(second['status'],'VERIFIED',second['action'])
  self.assertEqual(self.open_paths[0],self.open_paths[1])
  self.assertEqual(self.verifier_paths,self.open_paths)
  self.assertEqual(self.source.read_bytes(),b'original two')
  self.assertEqual(Path(first['evidence']).read_bytes(),first_evidence)
  self.assertEqual(Path(first['snapshot']).read_bytes(),first_copy)
  self.assertNotEqual(first['evidence'],second['evidence'])
 def test_update_preserves_authorized_inode_until_excel_saves(self):
  first=self.verify();copy=Path(first['copy']);inode=copy.stat().st_ino
  def native(args,**kwargs):
   if 'osascript' in args and Path(args[-2]).name!='ensure_closed.applescript':
    self.assertEqual(Path(args[-1]),copy)
    self.assertEqual(copy.stat().st_ino,inode)
   return self.run_native(args,**kwargs)
  with patch.object(excel,'native_available'),patch.object(excel.subprocess,'run',side_effect=native):
   result=excel.verify(self.source,['check','{workbook}'],self.evidence)
  self.assertEqual(result['status'],'VERIFIED')
 def test_open_copy_is_not_overwritten(self):
  first=self.verify();copy=Path(first['copy']);copy.write_bytes(b'open unsaved work')
  def native(args,**kwargs):
   if 'osascript' in args and Path(args[-2]).name=='ensure_closed.applescript':
    return subprocess.CompletedProcess(args,1,'','Verification workbook is already open; close that copy before retrying')
   return self.run_native(args,**kwargs)
  with patch.object(excel,'native_available'),patch.object(excel.subprocess,'run',side_effect=native):
   result=excel.verify(self.source,['check','{workbook}'],self.evidence)
  self.assertEqual(result['status'],'BLOCKED')
  self.assertIn('already open',result['action'])
  self.assertEqual(copy.read_bytes(),b'open unsaved work')
 def test_concurrent_retry_cannot_overwrite_verification_copy(self):
  first=self.verify();copy=Path(first['copy']);before=copy.read_bytes()
  with (copy.parent/'verification.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   result=self.verify()
  self.assertEqual(result['status'],'BLOCKED')
  self.assertIn('Another verification',result['action'])
  self.assertEqual(copy.read_bytes(),before)
 def test_stable_path_cannot_alias_original(self):
  first=self.verify();copy=Path(first['copy']);copy.unlink()
  copy.hardlink_to(self.source)
  before=self.source.read_bytes();result=self.verify()
  self.assertEqual(result['status'],'BLOCKED')
  self.assertIn('aliases',result['action'])
  self.assertEqual(self.source.read_bytes(),before)
 def test_same_filename_in_different_directories_reuses_fixed_slot(self):
  first=self.verify()
  self.source=self.root/'other'/'source.xlsx';self.source.parent.mkdir()
  self.source.write_bytes(b'other source');self.authorized.clear()
  second=self.verify()
  self.assertEqual(second['status'],'VERIFIED')
  self.assertEqual(first['copy'],second['copy'])
  self.assertEqual(Path(second['copy']).resolve(),(self.evidence/'office'/'excel-verify.xlsx').resolve())
 def test_permission_failure_preserves_open_boundary(self):
  def denied(args,**kwargs):
   return subprocess.CompletedProcess(args,1,'','Excel open failed: User canceled. (-128)')
  with patch.object(excel,'native_available'),patch.object(excel.subprocess,'run',side_effect=denied):
   result=excel.verify(self.source,['check','{workbook}'],self.evidence)
  self.assertEqual(result['status'],'BLOCKED')
  self.assertIn('Excel open failed: User canceled. (-128)',result['action'])
  self.assertNotIn('interactive failures',result['action'])
  self.assertEqual(self.source.read_bytes(),b'original one')

if __name__=='__main__':unittest.main()

"""Review's specific failure evidence must survive both retries and interrupts."""
import json
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import excel_verification as excel

class EvidenceTests(unittest.TestCase):
 def test_failed_saved_copy_survives_retry(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);source=root/'source.xlsx';source.write_bytes(b'original')
   def run(args,**kwargs):
    if 'osascript' in args:
     if Path(args[-2]).name!='ensure_closed.applescript':Path(args[-1]).write_bytes(b'saved bad cache')
     return subprocess.CompletedProcess(args,0,'','')
    return subprocess.CompletedProcess(args,1,'','Required cache wrong')
   with patch.object(excel,'native_available'),patch.object(excel.subprocess,'run',side_effect=run):
    result=excel.verify(source,['verify','{workbook}'],root/'evidence')
   self.assertEqual(result['status'],'BLOCKED')
   self.assertIn('snapshot',result)
   Path(result['copy']).write_bytes(b'next attempt')
   self.assertEqual(Path(result['snapshot']).read_bytes(),b'saved bad cache')
   self.assertIn('Required cache wrong',result['action'])
 def test_interrupted_record_does_not_hide_current_failure(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);good=root/'excel-resume-good';good.mkdir()
   record=good/'result.json';record.write_text(json.dumps({'reviewed_sha':'HEAD','blocker_key':'excel-cached-value-verification','status':'BLOCKED','action':'Excel open failed: User canceled. (-128)'}))
   broken=root/'excel-resume-interrupted';broken.mkdir();(broken/'result.json').write_text('{"status":')
   errors=io.StringIO()
   with patch('adjudication.location',return_value=root),contextlib.redirect_stderr(errors):
    self.assertEqual(excel.review_evidence('HEAD'),str(record))
    self.assertEqual(excel.review_evidence('OTHER'),'NONE')
   self.assertIn('Unreadable Excel recovery evidence',errors.getvalue())
   self.assertEqual(excel.failure_action(str(record),'excel-cached-value-verification'),'Excel open failed: User canceled. (-128)')
   self.assertEqual(excel.failure_action(str(record),'unrelated'),'')

 def test_partial_new_record_cannot_resurrect_old_success(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);old=root/'excel-resume-old';old.mkdir()
   (old/'result.json').write_text(json.dumps({'reviewed_sha':'HEAD','status':'VERIFIED'}))
   newer=root/'excel-resume-new';newer.mkdir();(newer/'result.json').write_text('{')
   with patch('adjudication.location',return_value=root),contextlib.redirect_stderr(io.StringIO()):
    self.assertEqual(excel.review_evidence('HEAD'),'NONE')

if __name__=='__main__':unittest.main()

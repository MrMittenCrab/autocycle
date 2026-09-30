"""Native boundary and Office transaction regressions; no GUI required."""
import json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import native_office as o
from test_native_office import FakeOffice,png

class CaptureTests(unittest.TestCase):
 def test_only_typed_permission_failure_establishes_external_dependency(self):
  for status in ('permission_failure','ambiguous','invalid_request','capture_failure'):
   with tempfile.TemporaryDirectory() as d:
    b=FakeOffice();w=o.Workspace(d,b);source=Path(d)/'source.docx';source.write_bytes(b'source')
    def fail(*args):
     error=o.NativeError('capture failed','native_capture');error.capture_status=status;raise error
    b.capture=fail
    result=w.view({'app':'word','source':str(source),'source_sha256':o.digest(source)})
    self.assertEqual('external_dependency' in result,status=='permission_failure')
    if status=='permission_failure':
     self.assertEqual(result['external_dependency']['kind'],'permission')
     self.assertEqual(result['external_dependency']['inputs'][0]['sha256'],o.digest(source))
 def test_helper_protocol(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'view.png';route={'pid':123,'bundle_id':'com.microsoft.Excel','title':'excel-view','frame':[40,60,1200,800]}
   ok={'status':'success','window_id':42,**route,'width':2400,'height':1600,'output':str(p)}
   for status in ('success','no_window','ambiguous','capture_failure','permission_failure','invalid_request'):
    reply=ok if status=='success' else {'status':status,'message':'test'}
    with patch.object(o.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(reply),'')) as run:
     if status=='success':self.assertEqual(o.MacOffice().capture(p,route)['window_id'],42)
     else:
      with self.assertRaises(o.NativeError):o.MacOffice().capture(p,route)
     self.assertNotIn('screencapture',str(run.call_args))
   for reply in ('','{}','[]','not json',json.dumps(dict(ok,pid=999))):
    with patch.object(o.subprocess,'run',return_value=subprocess.CompletedProcess([],0,reply,'')):
     with self.assertRaises(o.NativeError):o.MacOffice().capture(p,route)
 def test_postcheck_discards(self):
  with tempfile.TemporaryDirectory() as d:
   b=FakeOffice();w=o.Workspace(d,b);s=Path(d)/'source.xlsx';s.write_bytes(b'source')
   req={'app':'excel','source':str(s),'source_sha256':o.digest(s),'worksheet':'Access'}
   def check(*args):
    if any(e[0]=='capture' for e in b.events):raise o.NativeError('Wrong document')
    return [0,0,800,600]
   b.confirm_view=check
   result=w.view(req)
   self.assertEqual(result['status'],'BLOCKED');self.assertNotIn('screenshot',result)
   self.assertFalse(list(w.root.glob('evidence/**/view.png')))
 def test_owned_capture_reuses_session(self):
  with tempfile.TemporaryDirectory() as d:
   b=FakeOffice();w=o.Workspace(d,b);p=w.populate('excel','view');w.open('excel',p)
   for i in range(2):
    result=w.capture_owned('excel',p,{'worksheet':'Access'},Path(d)/f'{i}.png')
    self.assertEqual(result['image']['width'],320)
   self.assertEqual(len([e for e in b.events if e[0]=='open']),1)
   self.assertEqual(len([e for e in b.events if e[0]=='confirm']),6)
 def test_zero_recovery_is_bounded_and_ambiguity_is_not_retried(self):
  for status, expected in [('no_window',2),('ambiguous',1),('permission_failure',1)]:
   with tempfile.TemporaryDirectory() as d:
    b=FakeOffice();w=o.Workspace(d,b);p=w.populate('excel','view');w.open('excel',p)
    calls=[]
    def fail(image, route):
     calls.append(route);error=o.NativeError(status);error.capture_status=status;raise error
    b.capture=fail
    with self.assertRaises(o.NativeError):w.capture_owned('excel',p,{'worksheet':'Access'},Path(d)/'out.png')
    self.assertEqual(len(calls),expected)
    self.assertEqual(len([e for e in b.events if e[0]=='position']),1 if status=='no_window' else 0)
    self.assertEqual(len([e for e in b.events if e[0]=='identity']),expected)
    self.assertFalse((Path(d)/'out.png').exists())
 def test_wrong_full_path_never_captured(self):
  with tempfile.TemporaryDirectory() as d:
   b=FakeOffice();w=o.Workspace(d,b);p=w.populate('excel','view');w.open('excel',p)
   def wrong(*args):o.MacOffice.confirm_owned_identity(p,str(p),'/other/excel-view.xlsx')
   b.verify_document=wrong
   with self.assertRaises(o.NativeError):w.capture_owned('excel',p,{'worksheet':'Access'},Path(d)/'out.png')
   self.assertFalse(any(e[0]=='capture' for e in b.events))
 def test_helper_abnormal_exit_reports_diagnostic_and_rejects(self):
  with patch.object(o.subprocess,'run',return_value=subprocess.CompletedProcess([],-6,'','native assertion')):
   with self.assertRaisesRegex(o.NativeError,'helper exited -6'):o.MacOffice().capture(Path('/tmp/out.png'),{})
 def test_helper_crash_and_unavailable(self):
  for error in (FileNotFoundError('helper'),subprocess.TimeoutExpired('helper',20)):
   with patch.object(o.subprocess,'run',side_effect=error):
    with self.assertRaises(o.NativeError):o.MacOffice().capture(Path('/tmp/out.png'),{})
 def test_invalid_request_rejected_before_office_actions(self):
  for request in ({'worksheet':'Access','zoom':True},{'worksheet':'Access','scroll_row':0},{'worksheet':'Access','bounds':[0,0,1,1]}):
   with tempfile.TemporaryDirectory() as d:
    b=FakeOffice();w=o.Workspace(d,b);p=w.slot('excel','view')
    with self.assertRaises(o.NativeError):w.capture_owned('excel',p,request,Path(d)/'out.png')
    self.assertFalse(b.events)
 def test_wrong_slot_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   w=o.Workspace(d,FakeOffice())
   with self.assertRaises((ValueError,o.NativeError)):w.capture_owned('excel',Path(d)/'user.xlsx',{},Path(d)/'out.png')

if __name__=='__main__':unittest.main()

"""Transport failures retry partial work; paused edits still require reconciliation."""
import json
from test_flow import Case, ok, fail

ERROR='Error: RetriableError: [unavailable] Error'

def fixture(c):
 p=c.bin/'agent'
 s=p.read_text()
 marker="  if os.environ.get('FRAGMENTED_RESPONSE')=='1':"
 s=s.replace(marker,"""  if count <= int(os.environ.get('TRANSPORT_FAILURES','0')):
   chunk('agent_message_chunk',os.environ['TRANSPORT_ERROR'])
   print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'stopReason':'end_turn'}}),flush=True)
   continue
"""+marker)
 p.write_text(s)

def test_retries_partial_work_without_review_or_plan():
 for error in (ERROR,'Error: RetriableError: [unknown] Premature close','Error: RetriableError: [unavailable] PING timed out'):
  c=Case()
  try:
   fixture(c);ok(c.run('1',TRANSPORT_FAILURES='1',TRANSPORT_ERROR=error))
   kinds=[e['kind'] for e in c.events()]
   assert kinds==['review','plan','implement','implement'],kinds
   assert c.git('status','--porcelain')==''
   assert not (c.a/'implementation-interruption.json').exists()
  finally:c.close()

def test_exhaustion_preserves_receipt_and_resumes_same_step():
 c=Case()
 try:
  fixture(c);fail(c.run('1',TRANSPORT_FAILURES='9',TRANSPORT_ERROR=ERROR))
  assert len([e for e in c.events() if e['kind']=='implement'])==3
  baseline=(c.a/'implementation-baseline.json').read_bytes()
  assert (c.a/'implementation-interruption.json').exists()
  assert not (c.a/'implementation-result.json').exists()
  ok(c.run('--resume'))
  assert (c.a/'implementation-baseline.json').read_bytes()==baseline
  assert len([e for e in c.events() if e['kind']=='plan'])==1
 finally:c.close()

def test_paused_worktree_and_index_edits_block_resume():
 for kind in ('file','index','protected'):
  c=Case()
  try:
   fixture(c);fail(c.run('1',TRANSPORT_FAILURES='9',TRANSPORT_ERROR=ERROR))
   assert (c.a/'implementation-interruption.json').exists()
   if kind=='file':(c.repo/'external.txt').write_text('manual')
   elif kind=='protected':(c.repo/'TARGET.md').write_text('manual')
   else:
    original=(c.repo/'RESULT.md').read_bytes()
    (c.repo/'RESULT.md').write_text('staged');c.git('add','RESULT.md')
    (c.repo/'RESULT.md').write_bytes(original)
   events=c.events();fail(c.run('--resume'));assert c.events()==events
  finally:c.close()

def test_protected_document_edit_during_failure_is_not_adopted():
 c=Case()
 try:
  fixture(c);fail(c.run('1',TRANSPORT_FAILURES='9',TRANSPORT_ERROR=ERROR,EDIT_PLAN='1'))
  assert len([e for e in c.events() if e['kind']=='implement'])==1
  assert not (c.a/'implementation-interruption.json').exists()
 finally:c.close()

def test_resume_requires_saved_state():
 c=Case()
 try:
  r=c.run('--resume');fail(r)
  assert 'no saved session' in r.stdout.lower(),(r.stdout,r.stderr)
  assert not (c.a/'resume-state').exists()
  assert not (c.a/'events.jsonl').exists()
 finally:c.close()

def test_resume_keeps_saved_budget_and_checks_branch():
 c=Case()
 try:
  fail(c.run('7',FAIL_STAGE='review'))
  state=(c.a/'resume-state').read_bytes()
  c.git('switch','-c','checkpoint/wrong')
  r=c.run('--resume');fail(r)
  assert 'resume state belongs to' in r.stdout,(r.stdout,r.stderr)
  assert (c.a/'resume-state').read_bytes()==state
  c.git('switch','checkpoint/test')
  ok(c.run('--resume',ENDPOINT_REACHED='1'))
 finally:c.close()

def test_unknown_provider_error_is_not_adopted():
 c=Case()
 try:
  fixture(c)
  fail(c.run('1',TRANSPORT_FAILURES='9',TRANSPORT_ERROR='Error: RetriableError: invalid tool result'))
  assert len([e for e in c.events() if e['kind']=='implement'])==1
  assert not (c.a/'implementation-interruption.json').exists()
  events=c.events();fail(c.run('--resume'));assert c.events()==events
 finally:c.close()

def test_unrelated_errors_are_not_retried():
 for error in ('Error: RetriableError: [permission_denied] Error',
               'Error: RetriableError: [unknown] Error',
               'Error: [unavailable] Error',
               'The code example says RetriableError: [unavailable] Error'):
  c=Case()
  try:
   fixture(c);fail(c.run('1',TRANSPORT_FAILURES='9',TRANSPORT_ERROR=error))
   assert len([e for e in c.events() if e['kind']=='implement'])==1
   assert not (c.a/'implementation-interruption.json').exists()
   assert not (c.a/'implementation-result.json').exists()
  finally:c.close()

if __name__=='__main__':
 import sys
 for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
  globals()[name]();print('PASS '+name,flush=True)

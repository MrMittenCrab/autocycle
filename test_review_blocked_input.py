import json
from test_flow import Case, ok, fail

def test_review_block_consumes_recovery_input():
 c=Case()
 try:
  fail(c.run('1',REVIEW_STATUS='BLOCKED'))
  assert json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']['block'] is None
  old=(c.a/'resume-state').read_text()
  fail(c.run('--resume',REVIEW_STATUS='BLOCKED'))
  assert len(c.events())==2, 'without new input, retry blocked Review once'
  c.enqueue('REPAIR_MACHINE_GENERATED_CONTRADICTION')
  ok(c.run('--resume',REVIEW_STATUS='PASS',INPUT_STATUS_OVERRIDE='COMPLETE'))
  reviews=[e for e in c.events() if e['kind']=='review']
  assert len(reviews)==3, 'queued recovery must trigger fresh Review'
  assert 'REPAIR_MACHINE_GENERATED_CONTRADICTION' in reviews[-1]['prompt']
  assert 'RUN_MAX=1' in old
 finally:c.close()

def test_new_instruction_does_not_override_fresh_block():
 c=Case()
 try:
  fail(c.run('1',REVIEW_STATUS='BLOCKED'))
  c.enqueue('Inspect new evidence but retain unresolved constraints')
  fail(c.run('--resume',REVIEW_STATUS='BLOCKED'))
  assert [e['kind'] for e in c.events()]==['review','review']
  assert 'STAGE=review_done' in (c.a/'resume-state').read_text()
 finally:c.close()

if __name__=='__main__':
 test_review_block_consumes_recovery_input()
 test_new_instruction_does_not_override_fresh_block()
 print('PASS Review BLOCKED recovery input')

from test_flow import Case,ok
from pathlib import Path
import importlib.util,subprocess,os
c=Case()
try:
 (c.repo/'IMPLEMENTATION.md').write_text('# Step 9M.12 — prior detailed step')
 c.git('add','IMPLEMENTATION.md');c.git('commit','-qm','Plan: Step 9M.12 — prior detailed step');c.git('push','-q')
 (c.repo/'RESULT.md').write_text('Executed detailed step');c.git('add','RESULT.md');c.git('commit','-qm','Step 9M.12');c.git('push','-q')
 (c.repo/'IMPLEMENTATION.md').write_text('# Step 9 — heading lost detail')
 c.git('add','IMPLEMENTATION.md');c.git('commit','-qm','Plan: Step 9 — heading lost detail');c.git('push','-q')
 # A non-Plan checkpoint avoids intentionally inferring an existing plan.
 (c.repo/'RESULT.md').write_text('done');c.git('add','RESULT.md');c.git('commit','-qm','Checkpoint');c.git('push','-q')
 c.enqueue('Persist Step 9 workbooks')
 r=c.run('1');ok(r)
 lines=r.stdout.splitlines()
 sync=next(i for i,x in enumerate(lines) if x.startswith('    ✓ Baseline '))
 assert lines[sync+1].startswith('Implement '),r.stdout
 checkpoint=next(i for i,x in enumerate(lines) if x.startswith('    ✓ Checkpoint '))
 assert lines[checkpoint-1].startswith('    '),r.stdout
 assert lines[lines.index('Cycle       1/1')+1]=='',r.stdout
 for e in c.events():
  if e['kind'] in ('review','plan'):
   assert '9M.12' in e['prompt'] and 'Step indexing:' in e['prompt']
   assert 'Step <session>.<step>[.<sub-step>]' in e['prompt'] and 'Persist Step 9 workbooks' in e['prompt']
 print('PASS actual cycle spacing and historical indexing context in both prompts')
finally:c.close()

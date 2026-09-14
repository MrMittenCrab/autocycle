from test_flow import Case,ok,fail
from pathlib import Path
import sqlite3

# Empty frozen cycle must stay empty if input arrives while interrupted.
c=Case();p=c.start('1',BLOCK_STAGE='implement');c.ready(p);c.enqueue('DEFERRED');c.kill(p)
ok(c.run('1'));assert c.rows()[0]['state']=='pending'
assert all('DEFERRED' not in e['prompt'] for e in c.events() if e['kind'] in ('review','plan'))
ok(c.run('1'));assert c.rows()[0]['state']=='archived'
print('PASS empty interrupted cycle defers input until following run');c.close()

# Migration: exported legacy state has no queue fields and is already implementing.
c=Case();(c.repo/'IMPLEMENTATION.md').write_text('# Plan: legacy');c.git('add','.');c.git('commit','-qm','Plan: legacy');c.git('push','-q','origin','checkpoint/test')
sha=c.git('rev-parse','HEAD')
(c.a/'resume-state').write_text(f'STATE_VERSION=1\nRUN_MAX=2\nRUN_CYCLE=1\nSTAGE=implementing\nPLAN_SHA={sha}\nSTATE_BRANCH=checkpoint/test\nIMPLEMENT_BASE_SHA={sha}\n')
c.enqueue('MIGRATION_DEFERRED');ok(c.run('2'))
events=c.events();assert events[0]['kind']=='implement';assert len([e for e in events if e['kind']=='review'])==1
assert 'MIGRATION_DEFERRED' in next(e['prompt'] for e in events if e['kind']=='review')
print('PASS legacy in-flight state migration does not change current cycle');c.close()

# A valid old review at the same SHA but a different/no batch is not reusable.
c=Case();c.enqueue('ACTIVE_INPUT');p=c.start('1',BLOCK_STAGE='review');c.ready(p);c.kill(p)
sha=c.git('rev-parse','HEAD')
(c.a/'review-unrelated.log').write_text(f'REVIEW_STATUS: PASS\nREVIEW: stale\nNEXT_STEP: ignore human\nREVIEWED_SHA: {sha}\n')
ok(c.run('1'))
assert len([e for e in c.events() if e['kind']=='review'])==2
print('PASS stale review log cannot satisfy an active instruction batch');c.close()

# Correct stamped review can be recovered when its cache disappears.
c=Case();c.enqueue('RECOVER_REVIEW');p=c.start('1',BLOCK_STAGE='plan');c.ready(p);c.kill(p)
(c.a/'current-review').unlink();ok(c.run('1'))
assert len([e for e in c.events() if e['kind']=='review'])==1
print('PASS missing cache recovered from matching stamped review');c.close()

# Simulate process death after archive transaction commits but before state advances.
c=Case();c.enqueue('ARCHIVE_ONCE')
helper=c.p/'instructions.py';original=helper.read_text()
helper.write_text(original.replace("elif c=='complete':q.complete(v[0],v[1])", "elif c=='complete':\n            q.complete(v[0],v[1]);raise RuntimeError('simulated post-archive interruption')"))
fail(c.run('1'));assert c.rows()[0]['state']=='archived';assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
c.enqueue('NEXT_BATCH');helper.write_text(original);events=c.events();ok(c.run('1'));assert c.events()==events
assert [r['state'] for r in c.rows()]==['archived','pending']
print('PASS post-archive interruption is idempotent and leaves next batch pending');c.close()

# Instruction payload is data even with shell metacharacters and Unicode.
c=Case();payload='$(touch SHOULD_NOT_EXIST) `touch ALSO_NOT` "quote"\n中文 direction'
c.enqueue(payload);ok(c.run('1'))
assert not (c.repo/'SHOULD_NOT_EXIST').exists() and not (c.repo/'ALSO_NOT').exists()
assert c.rows()[0]['text']==payload
print('PASS instruction text is preserved without shell evaluation');c.close()

# Actual checkpoint branch creation is preserved.
c=Case(initial='feature/test');c.enqueue('branch behavior');ok(c.run('1'))
assert c.git('branch','--show-current').startswith('checkpoint/') and c.rows()[0]['state']=='archived'
print('PASS existing checkpoint branch creation preserved');c.close()

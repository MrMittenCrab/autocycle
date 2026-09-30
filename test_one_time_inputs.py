"""A published Plan consumes steering; implementation acceptance stays in Review."""
import json
import sqlite3
import sys
import importlib.util
import os
import uuid
from pathlib import Path
from test_flow import Case, ok, fail

spec=importlib.util.spec_from_file_location('queue_one_time',Path(__file__).with_name('instructions.py'))
queue_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue_module)


def test_published_input_is_not_reinjected_next_cycle():
    c=Case()
    try:
        ident=c.enqueue('ONE_SHOT_SENTINEL improve the requested area')
        ok(c.run('1'))
        assert c.rows()[0]['state']=='archived', 'A published incorporating Plan must consume steering'
        assert 'checkpoint_done' in (c.a/'resume-state').read_text()
        ledger=json.loads((c.a/'work-state.json').read_text())['branches']['checkpoint/test']
        assert ledger['attempts'][-1]['outcome'] is None, 'Consuming steering cannot accept implementation'
        ok(c.run('--extend','1'))
        for kind in ('review','plan'):
            events=[e for e in c.events() if e['kind']==kind]
            assert 'ONE_SHOT_SENTINEL' in events[0]['prompt']
            assert 'ONE_SHOT_SENTINEL' not in events[1]['prompt'], 'Previously incorporated steering was reinjected'
        assert c.rows()[0]['id']==ident and c.rows()[0]['state']=='archived'
    finally:c.close()


def test_missing_commitment_prevents_publication_and_consumption():
    c=Case()
    try:
        c.enqueue('An instruction the planner must account for')
        initial=c.git('rev-parse','HEAD')
        fail(c.run('1',DROP_INPUT_RECEIPT='1'))
        assert c.git('rev-parse','origin/checkpoint/test')==initial
        assert c.rows()[0]['state']=='active'
        assert not any(e['kind']=='implement' for e in c.events())
    finally:c.close()


def test_late_arrivals_still_get_their_own_next_plan():
    c=Case()
    try:
        c.enqueue('FIRST_STEERING')
        p=c.start('2',BLOCK_STAGE='implement');c.ready(p)
        c.enqueue('LATER_STEERING')
        (c.a/'release').touch();out,err=p.communicate(timeout=180)
        assert p.returncode==0,(out,err)
        plans=[e['prompt'] for e in c.events() if e['kind']=='plan']
        assert 'FIRST_STEERING' in plans[0] and 'LATER_STEERING' not in plans[0]
        assert 'FIRST_STEERING' not in plans[1] and 'LATER_STEERING' in plans[1]
        assert all(r['state']=='archived' for r in c.rows())
    finally:c.close()


def test_failed_planning_preserves_input_and_published_resume_consumes_once():
    for fault in ('before-plan','after-push','sync'):
        c=Case()
        try:
            c.enqueue('RESUMABLE_STEERING')
            control={'FAIL_STAGE':'plan'} if fault=='before-plan' else {'GIT_FAULT':'plan_after_push' if fault=='after-push' else 'sync_after_merge'}
            fail(c.run('1',**control))
            if fault=='before-plan':assert c.rows()[0]['state']=='active'
            ok(c.run('--resume'))
            assert c.rows()[0]['state']=='archived'
            db=sqlite3.connect(c.a/'instructions.sqlite3')
            try:assert db.execute('SELECT count(*) FROM deliveries').fetchone()[0]==1
            finally:db.close()
        finally:c.close()


def test_legacy_import_uses_published_binding_and_preserves_unplanned_input():
    c=Case();original=Path.cwd()
    try:
        os.chdir(c.repo)
        q=queue_module.Queue(c.a)
        old=q.enqueue('OLD_ALREADY_PLANNED');bid=uuid.uuid4().hex
        base=c.git('rev-parse','HEAD');q.freeze(bid,'checkpoint/test',base,True)
        (c.repo/'IMPLEMENTATION.md').write_text('# Step Legacy — original accepted plan\nKeep the original acceptance and constraints.\n')
        c.git('add','IMPLEMENTATION.md');c.git('commit','-qm','Plan: legacy\n\nAutocycle-Input-Batch: '+bid)
        sha=c.git('rev-parse','HEAD');c.git('push','-q','origin','checkpoint/test')
        q.db.execute('UPDATE batches SET plan_sha=? WHERE id=?',(sha,bid))
        later=q.enqueue('UNPLANNED_REQUEST');new=uuid.uuid4().hex
        q.rollover(bid,new,'checkpoint/test',sha)
        for table in ('deliveries','input_notices','legacy_batches'):q.db.execute('DROP TABLE '+table)
        q.db.execute('PRAGMA user_version=2');q.db.close()
        q=queue_module.Queue(c.a)
        assert (c.a/'instructions.v2.backup.sqlite3').exists()
        assert q.import_legacy()==1
        assert q.import_legacy()==0
        assert [r['id'] for r in q.listing()]==[later]
        assert 'OLD_ALREADY_PLANNED' not in q.prompt(new)
        assert 'UNPLANNED_REQUEST' in q.prompt(new)
        receipt=q.db.execute('SELECT plan_sha,legacy FROM deliveries').fetchone()
        assert tuple(receipt)==(sha,1)
        assert next(i for i in q.items(bid) if i['id']==old)['text']=='OLD_ALREADY_PLANNED'
        q.db.close()
    finally:os.chdir(original);c.close()


def test_legacy_import_does_not_trust_unbound_or_unpublished_plan():
    for fault in ('unbound','unpublished','wrong-trailer'):
        c=Case();original=Path.cwd()
        try:
            os.chdir(c.repo);q=queue_module.Queue(c.a)
            q.enqueue('MUST_REMAIN');bid=uuid.uuid4().hex
            q.freeze(bid,'checkpoint/test',c.git('rev-parse','HEAD'),True)
            (c.repo/'IMPLEMENTATION.md').write_text('# Step Legacy — retained work\n')
            c.git('add','IMPLEMENTATION.md')
            trailer=bid if fault!='wrong-trailer' else '0'*32
            c.git('commit','-qm','Plan: legacy\n\nAutocycle-Input-Batch: '+trailer)
            sha=c.git('rev-parse','HEAD')
            if fault!='unpublished':c.git('push','-q','origin','checkpoint/test')
            if fault!='unbound':q.db.execute('UPDATE batches SET plan_sha=? WHERE id=?',(sha,bid))
            q.db.execute('INSERT INTO legacy_batches VALUES (?)',(bid,))
            try:q.import_legacy()
            except (RuntimeError,__import__('subprocess').CalledProcessError):pass
            assert q.listing()[0]['state']=='active'
            assert q.db.execute('SELECT count(*) FROM deliveries').fetchone()[0]==0
            q.db.close()
        finally:os.chdir(original);c.close()


def test_notice_is_not_repeated_after_restart_or_rollover():
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        q=queue_module.Queue(directory);q.enqueue('pending input')
        bid=uuid.uuid4().hex;q.freeze(bid,'test','a'*40,True)
        assert q.notice(bid)==1
        q.db.close();q=queue_module.Queue(directory)
        assert q.notice(bid)==0
        new=uuid.uuid4().hex;q.rollover(bid,new,'test','a'*40)
        assert q.notice(new)==0 and q.listing()[0]['state']=='active'
        q.db.close()


def test_upgrade_rechecks_old_policy_review_before_planning():
    c=Case()
    try:
        c.enqueue('ONE_TIME_AFTER_UPGRADE')
        p=c.start('1',BLOCK_STAGE='plan');c.ready(p);c.kill(p)
        cache=c.a/'current-review'
        cache.write_text(cache.read_text().replace('REVIEW_POLICY: '+__import__('adjudication').POLICY,
                                                   'REVIEW_POLICY: five-stage-session-v8'))
        ok(c.run('--resume'))
        assert len([e for e in c.events() if e['kind']=='review'])==2
        assert len([e for e in c.events() if e['kind']=='implement'])==1
        assert c.rows()[0]['state']=='archived'
    finally:c.close()


if __name__=='__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS '+name,flush=True)

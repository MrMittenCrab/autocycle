"""Git-backed fixtures exercise controller plumbing; prompt contracts cover Review policy.

Providers are deterministic fakes, so these tests do not claim to prove an LLM's
semantic judgment. Historical bytes, cache validity and ownership are real.
"""
import hashlib
import json
import sys
from pathlib import Path
from test_flow import Case, ok, fail
from test_resume import adjudicate


def test_tracked_move_source_deletion_and_replacement_use_git():
    c = Case()
    try:
        originals = {'legacy/source/report.pdf': b'%PDF-original-source',
                     'legacy/generated.bin': b'old generated',
                     'legacy/redundant.bin': b'obsolete',
                     'legacy/moved.bin': b'preserved generated'}
        for name, data in originals.items():
            p = c.repo/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
        (c.repo/'provenance.json').write_text(json.dumps({'source': 'legacy/source/report.pdf', 'issuer': 'company'}))
        c.git('add','.'); c.git('commit','-qm','Artifacts'); c.git('push','-q')
        agent = c.bin/'agent'
        marker = "  if os.environ.get('NO_CHANGE')!='1':"
        mutation = '''  baseline=json.loads((a/'implementation-baseline.json').read_text())['head']
  assert not list(a.glob('migration-*.json'))
  for old,new in [('legacy/source/report.pdf','build/input/company/source/report.pdf'),('legacy/moved.bin','build/output/moved.bin')]:
   prior=subprocess.check_output([os.environ['REAL_GIT'],'show',baseline+':'+old])
   target=root/new;target.parent.mkdir(parents=True,exist_ok=True)
   (root/old).rename(target)
   assert prior==target.read_bytes()
   assert hashlib.sha256(prior).hexdigest()==hashlib.sha256(target.read_bytes()).hexdigest()
   assert len(prior)==target.stat().st_size
  (root/'provenance.json').write_text(json.dumps({'source':'build/input/company/source/report.pdf','issuer':'company'}))
  (root/'legacy/redundant.bin').unlink()
  (root/'legacy/generated.bin').write_bytes(b'new generated; semantic value=42')
'''
        agent.write_text(agent.read_text().replace(marker,mutation+marker))
        ok(c.run('1'))
        b = json.loads((c.a/'implementation-baseline.json').read_text())['head']
        for name,data in originals.items():
            assert c.git('show',b+':'+name).encode()==data
        assert not (c.repo/'legacy/source/report.pdf').exists()
        assert not (c.repo/'legacy/moved.bin').exists()
        assert not (c.repo/'legacy/redundant.bin').exists()
        provenance=json.loads((c.repo/'provenance.json').read_text())
        assert (c.repo/provenance['source']).read_bytes()==originals['legacy/source/report.pdf']
        assert provenance['issuer']=='company'
        assert b'42' in (c.repo/'legacy/generated.bin').read_bytes()
        assert not list(c.a.glob('migration-*.json'))
        assert not list(c.a.rglob('*.pdf')) and not list(c.a.rglob('*.bin'))
        prompts={e['kind']:e['prompt'] for e in c.events() if e['kind'] in ('review','plan','implement')}
        assert set(prompts)=={'review','plan','implement'}
        for prompt in prompts.values():
            for phrase in ('Git-first project history', 'IMPLEMENT_BASE_SHA', 'fail closed',
                           'git show B:path', 'preserved relocation',
                           'Historical existence needs no receipt',
                           'Legitimately changed generated output does not require byte identity',
                           'no duplicate legacy copy, receipt, backup or human approval'):
                assert phrase in prompt, phrase
            assert 'artifact-receipt' not in prompt and 'Grandfather' not in prompt
        ok(c.run('--extend','1',REVIEW_STATUS='DONE',PROGRESS_OUTCOME='COMPLETE'))
        assert 'REVIEW_STATUS: DONE' in (c.a/'current-review').read_text()
    finally:c.close()


def test_stale_review_rechecked_without_exception_instruction():
    c=Case()
    try:
        fail(c.run('1',REVIEW_STATUS='BLOCKED'))
        # A previous observer saw transient bytes never represented by Git.
        transient=c.repo/'transient-output.bin'
        transient.write_bytes(b'temporary generation, not the implementation baseline')
        observed=hashlib.sha256(transient.read_bytes()).hexdigest()
        transient.unlink()
        old=(c.a/'current-review').read_text()
        old=old.replace('REVIEW: ', 'REVIEW: Require missing transient-output.bin SHA-256 '+observed+'; ', 1)
        policy=adjudicate(c,'policy').stdout.strip()
        (c.a/'current-review').write_text(old.replace(policy,'five-stage-session-v10'))
        fail(adjudicate(c,'valid',str(c.a/'current-review'),''))
        ok(c.run('--resume',REVIEW_STATUS='PASS'))
        assert c.rows()==[]
        prompt=[e['prompt'] for e in c.events() if e['kind']=='review'][-1]
        for phrase in ('re-evaluate an old migration blocker from B',
                       'not represented by B, is not reconstructible from Git',
                       'not explicitly required by active user instructions',
                       'stale requirement based on the wrong historical reference point',
                       'delivered instructions do not create permanent acceptance requirements'):
            assert phrase in prompt
    finally:c.close()


def test_exact_non_git_requirement_and_current_defects_still_block():
    for defect in ('exact-non-git-state','missing-canonical-data','relocation-mismatch',
                   'broken-provenance','build-failed','check-failed','regression-failed',
                   'non-git-source-loss'):
        c=Case()
        try:
            (c.repo/'.gitignore').write_text('/local-evidence/\n')
            local=c.repo/'local-evidence';local.mkdir()
            source=local/'source.pdf';destination=local/'canonical.pdf'
            source.write_bytes(b'%PDF-irreplaceable');destination.write_bytes(source.read_bytes())
            expected=hashlib.sha256(source.read_bytes()).hexdigest()
            (c.repo/'source-reference.json').write_text(json.dumps({'path':'local-evidence/canonical.pdf','sha256':expected}))
            check=c.repo/'check.py';check.write_text('raise SystemExit(0)\n')
            if defect=='exact-non-git-state':
                (c.repo/'SESSION.md').write_text('Explicitly preserve and verify exact non-Git artifact SHA-256 '+expected)
                destination.unlink();source.unlink()
            elif defect in ('missing-canonical-data','non-git-source-loss'):
                destination.unlink();source.unlink()
            elif defect=='relocation-mismatch':
                destination.write_bytes(b'wrong source')
            elif defect=='broken-provenance':
                (c.repo/'source-reference.json').write_text(json.dumps({'path':'local-evidence/missing.pdf','sha256':expected}))
            else:
                check.write_text('raise SystemExit(1)\n')
            c.git('add','.');c.git('commit','-qm','Current evidence fixture');c.git('push','-q')
            provider=c.bin/'codex'
            marker=" active='Allowed INPUT_STATUS: COMPLETE | PENDING' in prompt"
            verification=""" # Deterministic Review provider measures fixture defects, not a forced status.
 ref=json.loads((root/'source-reference.json').read_text())
 destination=root/ref['path']
 intact=destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest()==ref['sha256']
 checks=subprocess.run([sys.executable,str(root/'check.py')]).returncode==0
 status='PASS' if intact and checks else 'PROBLEMS'
"""
            provider.write_text(provider.read_text().replace(marker,verification+marker))
            fail(c.run('1',BLOCKER_KEY_OVERRIDE=defect,REVIEW_BLOCKING='1',FAIL_STAGE='plan'))
            assert [e['kind'] for e in c.events()]==['review','plan']
            assert 'REVIEW_STATUS: PROBLEMS' in (c.a/'current-review').read_text()
            assert 'BLOCKER_KEY: '+defect in (c.a/'current-review').read_text()
            prompt=c.events()[0]['prompt']
            for phrase in ('Explicit exact-state requirements', 'remain binding',
                           'while both copies are available', 'Without a surviving destination, block',
                           'generated, temporary, cache or redundant state has no historical preservation requirement',
                           'Missing canonical source/data, relocation content mismatch, broken provenance',
                           'failed build/check/regression or native verification'):
                assert phrase in prompt
        finally:c.close()


def test_receipt_commands_removed_and_baseline_still_immutable():
    c=Case()
    try:
        ok(adjudicate(c,'baseline'))
        before=(c.a/'implementation-baseline.json').read_bytes()
        for command in ('artifact-receipt','migration-policy'):
            fail(adjudicate(c,command))
        (c.repo/'private.txt').write_text('unrelated dirty work')
        fail(adjudicate(c,'baseline'))
        assert (c.a/'implementation-baseline.json').read_bytes()==before
        assert not list(c.a.glob('migration-*.json'))
    finally:c.close()


def test_normal_cache_tracks_baseline_not_obsolete_archive():
    c=Case()
    try:
        ok(adjudicate(c,'baseline'))
        original=adjudicate(c,'identity').stdout
        obsolete=c.a/'migration-receipts-obsolete.json'
        obsolete.write_text('[]')
        assert adjudicate(c,'identity').stdout==original
        obsolete.unlink()
        b=c.a/'implementation-baseline.json'
        b.write_text(b.read_text()+'\n')
        assert adjudicate(c,'identity').stdout!=original
        state=c.a/'resume-state'
        first=c.git('rev-parse','HEAD')
        state.write_text('IMPLEMENT_BASE_SHA='+first+'\nPLAN_SHA=unused\nSTAGE=reviewing\n')
        baseline_identity=adjudicate(c,'identity').stdout
        state.write_text('IMPLEMENT_BASE_SHA='+first+'\nPLAN_SHA=other\nSTAGE=review_done\n')
        assert adjudicate(c,'identity').stdout==baseline_identity
        state.write_text('IMPLEMENT_BASE_SHA=changed\nPLAN_SHA=other\n')
        assert adjudicate(c,'identity').stdout!=baseline_identity
        state.write_text("IMPLEMENT_BASE_SHA=''\nPLAN_SHA="+first+'\n')
        assert adjudicate(c,'identity').stdout==baseline_identity
        state.write_text("IMPLEMENT_BASE_SHA=''\nPLAN_SHA=''\nSTAGE=planning\n")
        assert adjudicate(c,'identity').stdout==baseline_identity
    finally:c.close()



def test_non_git_source_continuity_before_removal():
    c=Case()
    try:
        (c.repo/'.gitignore').write_text('/local-source/\n')
        c.git('add','.');c.git('commit','-qm','Ignore local source');c.git('push','-q')
        local=c.repo/'local-source';local.mkdir()
        old=local/'original.pdf';old.write_bytes(b'%PDF-non-git-source')
        agent=c.bin/'agent';marker="  if os.environ.get('NO_CHANGE')!='1':"
        mutation="""  old=root/'local-source/original.pdf';target=root/'local-source/canonical.pdf'
  target.write_bytes(old.read_bytes())
  assert old.read_bytes()==target.read_bytes()  # continuity while both exist
  source_sha=hashlib.sha256(old.read_bytes()).hexdigest()
  (root/'provenance.json').write_text(json.dumps({'path':'local-source/canonical.pdf','sha256':source_sha}))
  old.unlink()
"""
        agent.write_text(agent.read_text().replace(marker,mutation+marker))
        ok(c.run('1'))
        assert not old.exists()
        reference=json.loads((c.repo/'provenance.json').read_text())
        assert hashlib.sha256((c.repo/reference['path']).read_bytes()).hexdigest()==reference['sha256']
        assert not c.git('ls-files','local-source')
        assert not list(c.a.glob('migration-*.json')) and not list(c.a.rglob('*.pdf'))
    finally:c.close()


if __name__=='__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS '+name,flush=True)

"""Migration resume needs a current shutdown receipt, never plan text or old logs.

The legacy engine mutation reproduces the September 21 missing-receipt failure
without reading, copying, or running any BAV project files.
"""
import json
from pathlib import Path
import sys

from test_completion import Work
from test_flow import Case, ok, fail
from test_historical_recovery import setup, ledger
from test_provider_retry import ERROR
from test_resume import adjudicate


def migration_setup(x, legacy=False):
    c = x.c
    for tree in ('benchmark', 'release'):
        for company in ('alpha', 'beta'):
            for i in range(16):
                p = c.repo/tree/company/f'{i}.dat'
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(bytes([i])*128)
    c.git('add', '.')
    c.git('commit', '-qm', 'Legacy company artifacts')
    c.git('push', '-q')
    ok(adjudicate(c, 'baseline'))
    log = c.a/'cursor-old.log'
    log.write_text('Earlier interrupted implementation\n')
    ok(adjudicate(c, 'interruption', str(log)))
    old = {p.name: p.read_bytes() for p in c.a.glob('interruption-*.json')}
    # The previous attempt consumed its receipt; a subsequent Plan starts a new
    # baseline. Immutable diagnostic archives intentionally remain in place.
    ok(adjudicate(c, 'baseline'))
    (c.a/'implementation-baseline.json').unlink()
    sha = setup(x)
    agent = c.bin/'agent'
    text = agent.read_text()
    marker = "  if os.environ.get('NO_CHANGE')!='1':"
    migration = """  for tree, destination in (('benchmark','input'),('release','output')):
   for company in ('alpha','beta'):
    for i in range(16):
     source=root/tree/company/(str(i)+'.dat')
     target=root/'build'/destination/company/(str(i)+'.dat')
     if source.exists():
      target.parent.mkdir(parents=True,exist_ok=True)
      target.write_bytes(source.read_bytes())
      source.unlink()
     assert target.read_bytes()==bytes([i])*128
"""
    assert marker in text
    agent.write_text(text.replace(marker, migration+marker))
    if legacy:
        # Reproduce the old classification path in the isolated engine only:
        # resource_exhausted falls through to completion-marker rejection.
        engine = c.bin/'stage'
        text = engine.read_text()
        assert r'\[resource_exhausted\]' in text
        engine.write_text(text.replace(r'\[resource_exhausted\]', r'\[legacy_disabled_capacity\]'))
    return sha, old


def snapshot(c):
    return {
        'head': c.git('rev-parse', 'HEAD'),
        'index': c.git('ls-files', '--stage'),
        'files': {str(p.relative_to(c.repo)): p.read_bytes()
                  for p in c.repo.rglob('*') if p.is_file() and '.git' not in p.relative_to(c.repo).parts},
        'baseline': (c.a/'implementation-baseline.json').read_bytes(),
        'ledger': ledger(c),
        'archives': {p.name: p.read_bytes() for p in c.a.glob('interruption-*.json')},
    }


def check_migration(c):
    for tree, destination in (('benchmark', 'input'), ('release', 'output')):
        for company in ('alpha', 'beta'):
            for i in range(16):
                assert not (c.repo/tree/company/f'{i}.dat').exists()
                assert (c.repo/'build'/destination/company/f'{i}.dat').read_bytes() == bytes([i])*128


def test_staging_migration_deletions_preserves_checkpoint_ownership():
    c = Case()
    try:
        (c.repo/'legacy.dat').write_bytes(b'original artifact')
        (c.repo/'unrelated.txt').write_text('Unrelated baseline file')
        c.git('add', '.')
        c.git('commit', '-qm', 'Artifact baseline')
        ok(adjudicate(c, 'baseline'))
        baseline = (c.a/'implementation-baseline.json').read_bytes()
        (c.repo/'legacy.dat').rename(c.repo/'canonical.dat')
        ok(adjudicate(c, 'implementation-result'))
        result = (c.a/'implementation-result.json').read_bytes()
        ok(adjudicate(c, 'checkpoint-safe'))
        c.git('add', '-A')
        ok(adjudicate(c, 'checkpoint-safe'))
        assert (c.a/'implementation-baseline.json').read_bytes() == baseline
        assert (c.a/'implementation-result.json').read_bytes() == result
        # A later unrelated deletion must still fail after staging it.
        (c.repo/'unrelated.txt').unlink()
        c.git('add', '-A')
        fail(adjudicate(c, 'checkpoint-safe'))
    finally:
        c.close()


def test_legacy_resource_failure_cannot_be_repaired_by_installing_retry_fix():
    with Work() as x:
        c = x.c
        sha, old = migration_setup(x, legacy=True)
        r = c.run('--resume', TRANSPORT_FAILURES='99', TRANSPORT_ERROR=ERROR)
        fail(r)
        assert 'Implementation response rejected' in r.stderr, (r.stdout, r.stderr)
        assert not (c.a/'implementation-interruption.json').exists()
        check_migration(c)
        before = snapshot(c)
        assert before['head'] == sha and before['archives'] == old
        # Install only into the disposable harness, retaining the exact paused
        # work and state. The real repository and user installation are unused.
        engine = c.bin/'stage'
        text = engine.read_text()
        engine.write_text(text.replace(r'\[legacy_disabled_capacity\]', r'\[resource_exhausted\]'))
        events = c.events()
        r = c.run('--resume')
        fail(r)
        assert 'uncheckpointed changes with unverified ownership' in r.stdout+r.stderr
        assert 'cannot preserve implementation baseline' in r.stdout+r.stderr
        assert 'implementation stage; local work preserved' in r.stdout+r.stderr
        assert c.events() == events
        assert snapshot(c) == before


def test_current_receipt_resumes_migration_despite_older_snapshots():
    with Work() as x:
        c = x.c
        sha, old = migration_setup(x)
        r = c.run('--resume', TRANSPORT_FAILURES='99', TRANSPORT_ERROR=ERROR)
        assert r.returncode == 76, (r.stdout, r.stderr)
        before = snapshot(c)
        receipt = json.loads((c.a/'implementation-interruption.json').read_text())
        assert receipt['snapshot']['head'] == sha
        assert all((c.a/n).read_bytes() == data for n, data in old.items())
        check_migration(c)
        ok(c.run('--resume'))
        check_migration(c)
        assert c.git('status', '--porcelain') == ''
        assert (c.a/'implementation-baseline.json').read_bytes() == before['baseline']
        assert ledger(c)['attempts'][-1]['id'] == before['ledger']['attempts'][-1]['id']
        assert all((c.a/n).read_bytes() == data for n, data in before['archives'].items())
        assert not (c.a/'implementation-interruption.json').exists()
        assert not (c.a/'unsafe-git').exists()


def test_migration_receipt_rejects_paused_edits_and_stale_or_missing_authority():
    for kind in ('unrelated', 'owned-path', 'index', 'protected', 'log', 'missing', 'stale'):
        with Work() as x:
            c = x.c
            _, old = migration_setup(x)
            r = c.run('--resume', TRANSPORT_FAILURES='99', TRANSPORT_ERROR=ERROR)
            assert r.returncode == 76, (r.stdout, r.stderr)
            receipt = c.a/'implementation-interruption.json'
            if kind == 'unrelated':
                (c.repo/'manual.txt').write_text('Unrelated work')
            elif kind == 'owned-path':
                (c.repo/'build/input/alpha/0.dat').write_text('Later manual change')
            elif kind == 'index':
                c.git('add', 'benchmark')
            elif kind == 'protected':
                (c.repo/'SESSION.md').write_text('Different session')
            elif kind == 'log':
                record = json.loads(receipt.read_text())
                with Path(record['log']).open('a') as f:
                    f.write('Changed after shutdown\n')
            elif kind == 'missing':
                receipt.unlink()
            else:
                receipt.write_bytes(next(iter(old.values())))
            before = snapshot(c)
            receipt_before = receipt.read_bytes() if receipt.exists() else None
            events = c.events()
            fail(c.run('--resume'))
            assert c.events() == events
            assert snapshot(c) == before
            assert (receipt.read_bytes() if receipt.exists() else None) == receipt_before
            assert not (c.a/'unsafe-git').exists()


if __name__ == '__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name](); print('PASS '+name, flush=True)

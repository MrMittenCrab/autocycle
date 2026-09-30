"""Explicit human recovery owns one exact interrupted state, never inferred work."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

import adjudication
from test_completion import Work
from test_flow import ok, fail
from test_migration_resume import migration_setup, snapshot, check_migration
from test_resume import adjudicate

AUTH = ('--authorize-current-worktree',
        'I attest that the entire current working tree and index are intentional interrupted implementation work.')


def interrupted(x):
    c = x.c
    migration_setup(x)
    ok(adjudicate(c, 'baseline'))
    for tree, destination in (('benchmark', 'input'), ('release', 'output')):
        for source in (c.repo/tree).rglob('*.dat'):
            target = c.repo/'build'/destination/source.relative_to(c.repo/tree)
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
    c.git('add', 'benchmark')  # Include staged deletions and unstaged moves.
    (c.repo/'manual-authorized.txt').write_text('Explicitly attested current work')
    (c.repo/'.gitignore').write_text('ignored-cache/\n')
    (c.repo/'ignored-cache').mkdir()
    (c.repo/'ignored-cache/data').write_bytes(b'Included in the complete fingerprint')
    (c.repo/'empty-directory').mkdir()
    (c.repo/'authorized-link').symlink_to('manual-authorized.txt')
    # No provider has run; seed its empty event log for the no-entry assertions.
    (c.a/'events.jsonl').touch()
    # Ordinary resume initializes its input batch, then still fails ownership
    # before provider entry. The human action must not initialize Session state.
    fail(c.run('--resume'))
    return c


def unchanged(c):
    return (snapshot(c), (c.repo/'.git/index').read_bytes(),
            (c.a/'resume-state').read_bytes(), (c.a/'work-state.json').read_bytes())


def test_explicit_adoption_preserves_files_index_and_identity_then_resumes_once():
    with Work() as x:
        c = interrupted(x)
        fail(c.run('--resume'))
        before = unchanged(c)
        events = c.events()
        fail(adjudicate(c, 'adopt'))
        assert not (c.a/'human-adoption.json').exists()
        ok(adjudicate(c, 'adopt', *AUTH))
        receipt = json.loads((c.a/'human-adoption.json').read_text())
        assert receipt['authority']['source'] == 'explicit-human-authorization'
        assert receipt['authority']['attestation'] == AUTH[1]
        assert receipt['snapshot']['work_binding']['work']['id'] == x.s['work']['id']
        assert receipt['snapshot']['session']['SESSION_NUMBER'] == '2'
        assert receipt['snapshot']['worktree']['benchmark/alpha/0.dat'] == 'missing'
        assert 'ignored-cache/data' in receipt['snapshot']['worktree']
        assert receipt['snapshot']['worktree']['authorized-link'] == 'link:manual-authorized.txt'
        assert receipt['snapshot']['worktree']['empty-directory'].startswith('directory:')
        ok(adjudicate(c, 'verify-adoption'))
        assert unchanged(c) == before and c.events() == events
        fail(adjudicate(c, 'adopt', *AUTH))  # Cannot overwrite live authority.
        ok(c.run('--resume'))  # Disposable fake-provider repo only.
        check_migration(c)
        assert not (c.a/'human-adoption.json').exists()
        assert len(list(c.a.glob('human-adoption-*.json'))) == 1
        fail(adjudicate(c, 'verify-adoption'))


def test_adoption_rejects_later_changes_and_preserves_receipt():
    for kind in ('file', 'deletion', 'index', 'index-format', 'ignored', 'directory-mode', 'symlink', 'empty-directory', 'session', 'work',
                 'baseline', 'branch', 'head', 'archive', 'repository'):
        with Work() as x:
            c = interrupted(x)
            ok(adjudicate(c, 'adopt', *AUTH))
            receipt = (c.a/'human-adoption.json').read_bytes()
            if kind == 'file': (c.repo/'unrelated.txt').write_text('Later edit')
            elif kind == 'deletion': (c.repo/'manual-authorized.txt').unlink()
            elif kind == 'index': c.git('add', 'release')
            elif kind == 'index-format': c.git('update-index', '--index-version', '4')
            elif kind == 'ignored': (c.repo/'ignored-cache/data').write_text('Changed')
            elif kind == 'directory-mode': (c.repo/'ignored-cache').chmod(0o700)
            elif kind == 'symlink':
                (c.repo/'authorized-link').unlink()
                (c.repo/'authorized-link').symlink_to('unrelated.txt')
            elif kind == 'empty-directory': (c.repo/'empty-directory').rmdir()
            elif kind == 'session':
                p = c.a/'resume-state';p.write_text(p.read_text().replace('SESSION_NUMBER=2', 'SESSION_NUMBER=3'))
            elif kind == 'work':
                p = c.a/'work-state.json';d = json.loads(p.read_text())
                d['branches']['checkpoint/test']['work']['id'] = 'f'*32;p.write_text(json.dumps(d))
            elif kind == 'baseline':
                p = c.a/'implementation-baseline.json';p.write_text(p.read_text()+'\n')
            elif kind == 'branch': c.git('symbolic-ref', 'HEAD', 'refs/heads/wrong')
            elif kind == 'head': c.git('update-ref', 'HEAD', 'HEAD^')
            elif kind == 'archive': next(c.a.glob('human-adoption-*.json')).write_text('{}')
            else:
                copied = c.p/'copied-repository';shutil.copytree(c.repo, copied)
                os.chdir(copied)
                try:
                    adjudication.verify_adoption()
                except ValueError: pass
                else: raise AssertionError('Copied receipt authorized a different repository')
                finally: os.chdir(c.repo)
                continue
            fail(adjudicate(c, 'verify-adoption'))
            events = c.events()
            fail(c.run('--resume'))
            assert c.events() == events
            assert (c.a/'human-adoption.json').read_bytes() == receipt


def test_adoption_requires_valid_original_boundaries_and_no_controller():
    for kind in ('controller', 'progress-lock', 'protected', 'dirty-baseline', 'missing-state', 'finished-attempt'):
        with Work() as x:
            c = interrupted(x)
            lock = None
            if kind in ('controller', 'progress-lock'):
                lock = (c.a/('controller.lock' if kind == 'controller' else 'work-state.lock')).open('a+b')
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif kind == 'protected': (c.repo/'IMPLEMENTATION.md').write_text('Changed plan')
            elif kind == 'dirty-baseline':
                p = c.a/'implementation-baseline.json';d = json.loads(p.read_text());d['clean'] = False;p.write_text(json.dumps(d))
            elif kind == 'missing-state': (c.a/'resume-state').unlink()
            else:
                p = c.a/'work-state.json';d = json.loads(p.read_text())
                d['branches']['checkpoint/test']['attempts'][-1]['phase'] = 'checkpointed';p.write_text(json.dumps(d))
            try:
                fail(adjudicate(c, 'adopt', *AUTH))
                assert not (c.a/'human-adoption.json').exists()
            finally:
                if lock: lock.close()


def test_changes_during_adoption_never_leave_live_authority():
    for when in ('before-publish', 'after-publish'):
        for kind in ('worktree', 'index', 'session', 'implementation', 'baseline', 'repository', 'branch', 'head'):
            with Work() as x:
                c = interrupted(x)
                original = adjudication.atomic
                def changing_write(path, data):
                    original(path, data)
                    if ((when == 'before-publish' and path.name.startswith('human-adoption-'))
                            or (when == 'after-publish' and path.name == 'human-adoption.json')):
                        if kind == 'worktree': (c.repo/'racing.txt').write_text('Concurrent edit')
                        elif kind == 'index': c.git('add', 'release')
                        elif kind == 'repository': os.chdir(c.p)
                        elif kind == 'branch': c.git('symbolic-ref', 'HEAD', 'refs/heads/wrong')
                        elif kind == 'head': c.git('update-ref', 'HEAD', 'HEAD^')
                        else:
                            name = {'session':'resume-state', 'implementation':'work-state.json',
                                    'baseline':'implementation-baseline.json'}[kind]
                            p = c.a/name;p.write_text(p.read_text()+'\n')
                try:
                    with patch.object(adjudication, 'atomic', changing_write):
                        try: adjudication.adopt_current_worktree(AUTH[1])
                        except (ValueError, OSError, adjudication.subprocess.SubprocessError): pass
                        else: raise AssertionError('Adoption accepted a concurrent '+kind+' change')
                finally: os.chdir(c.repo)
                assert not (c.a/'human-adoption.json').exists()


def test_archive_alone_never_authorizes_resume():
    with Work() as x:
        c = interrupted(x)
        ok(adjudicate(c, 'adopt', *AUTH))
        (c.a/'human-adoption.json').unlink()
        assert len(list(c.a.glob('human-adoption-*.json'))) == 1
        before = unchanged(c)
        events = c.events()
        fail(adjudicate(c, 'verify-adoption'))
        result = c.run('--resume')
        fail(result)
        assert 'unverified ownership' in result.stdout + result.stderr
        assert c.events() == events
        assert unchanged(c) == before


if __name__ == '__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS '+name, flush=True)

#!/usr/bin/env python3
"""Merge concurrent origin planning-document edits at a verified checkpoint.

The executed plan, original ownership baseline, progress counters and queue
remain intact. A separate receipt authorizes only the imported document bytes.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

DOCS = {'TARGET.md', 'SESSION.md', 'IMPLEMENTATION.md'}


def require(ok, message):
    if not ok: raise RuntimeError(message)


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE)


def gs(*args):
    return git(*args).decode().strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def acdir():
    return Path(os.fsdecode(git('rev-parse', '--git-path', 'autocycle')).strip()).resolve()


def atomic(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    temporary = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    with temporary.open('wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def changed(base, tip):
    return {os.fsdecode(x) for x in git('diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', base, tip, '--').split(b'\0') if x}


def regular_docs(revision):
    values = {}
    for name in DOCS:
        row = git('ls-tree', '-z', revision, '--', name)
        if not row and name=='SESSION.md':
            values[name] = None
            continue
        require(row, 'Planning document is missing at ' + revision + ': ' + name)
        mode, kind, oid = row.split(b'\t', 1)[0].split()
        require(kind == b'blob' and mode in (b'100644', b'100755'), 'Planning documents must be regular files.')
        values[name] = {'mode': mode.decode(), 'oid': oid.decode()}
    return values


def ancestor(base, tip):
    return subprocess.run(['git', 'merge-base', '--is-ancestor', base, tip], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE).returncode == 0


def baseline():
    path = acdir() / 'implementation-baseline.json'
    require(path.is_file(), 'The original implementation baseline is missing.')
    value = json.loads(path.read_bytes())
    require(value.get('clean') is True and value.get('branch') == gs('branch', '--show-current'),
            'The original implementation baseline is not clean or belongs to another branch.')
    return value


def verify_merge(receipt, head):
    require(receipt.get('version') == 1 and receipt['branch'] == gs('branch', '--show-current'), 'Document receipt belongs to another branch or version.')
    require(gs('rev-list', '--parents', '-n', '1', head).split()[1:] == [receipt['local'], receipt['remote']],
            'Recovered HEAD is not the recorded two-parent merge.')
    require(changed(receipt['base'], receipt['remote']) <= DOCS and ancestor(receipt['base'], receipt['remote']),
            'Remote history no longer proves a document-only update.')
    require(regular_docs(receipt['local']) == regular_docs(receipt['base']), 'Local implementation changed protected planning documents.')
    require(changed(receipt['local'], head) <= DOCS and regular_docs(head) == regular_docs(receipt['remote']),
            'Merge altered implementation work or did not preserve the remote document bytes.')
    require(gs('rev-list', '--parents', '-n', '1', receipt['local']).split()[1:] == [receipt['base']],
            'Local checkpoint is not the direct child of its executed plan.')
    require(digest((acdir() / 'implementation-baseline.json').read_bytes()) == receipt['baseline_sha256'],
            'The original ownership baseline changed after reconciliation.')


def docs_baseline(original):
    path = acdir() / 'remote-docs-current.json'
    if not path.exists(): return original
    receipt = json.loads(path.read_bytes())
    if receipt.get('base') != original or receipt.get('merged') != gs('rev-parse', 'HEAD'):
        return original
    verify_merge(receipt, receipt['merged'])
    return receipt['remote']


def known_receipt(head, remote):
    path = acdir() / 'remote-docs-current.json'
    if not path.exists(): return None
    receipt = json.loads(path.read_bytes())
    if receipt.get('base') != baseline()['head']: return None
    if head not in (receipt.get('local'), receipt.get('merged')):
        # A process may have exited after Git committed but before the receipt
        # was completed. Only the exact verified merge can finish that journal.
        verify_merge(receipt, head)
        receipt['merged'] = head
        atomic(path, receipt)
        atomic(Path(receipt['history']) / 'receipt.json', receipt)
    if receipt.get('merged') == head:
        verify_merge(receipt, head)
        require(remote in (receipt['remote'], head), 'Origin moved again during recovery. Both histories are preserved; inspect the additional edits before another merge.')
    else:
        require(remote == receipt['remote'], 'Origin changed after reconciliation was prepared.')
    return receipt


def prepare_receipt(head, remote):
    ac = acdir()
    b = baseline(); base = b['head']
    require(gs('rev-list', '--parents', '-n', '1', head).split()[1:] == [base],
            'Local history is not one checkpoint after the saved plan. Preserve it for reviewed reconciliation.')
    require(ancestor(base, remote) and changed(base, remote) and changed(base, remote) <= DOCS,
            'Remote changes are not limited to TARGET.md, SESSION.md and IMPLEMENTATION.md after the executed plan. Automatic reconciliation stopped.')
    require(regular_docs(head) == regular_docs(base), 'The local checkpoint changed protected planning documents.')
    regular_docs(remote)
    import adjudication
    evidence_path = ac / 'implementation-result.json'
    if not evidence_path.exists(): evidence_path = ac / 'candidate.json'
    require(evidence_path.exists(), 'No frozen implementation snapshot proves ownership of the local checkpoint.')
    evidence = json.loads(evidence_path.read_bytes())
    require(evidence.get('head') == base and evidence.get('branch') == b['branch'] and evidence.get('files') == adjudication.files(),
            'Local files do not match the frozen implementation result. No additional edits were adopted.')
    state = json.loads((ac / 'work-state.json').read_bytes())['branches'][b['branch']]
    attempt = state['attempts'][-1]
    require(attempt['plan_sha'] == base and attempt['work_id'] == state['work']['id'] and
            attempt['phase'] in ('running', 'checkpointed'), 'The current work/attempt does not own this checkpoint.')
    folder = ac / 'remote-docs-history' / (head + '-' + remote)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('resume-state', 'implementation-baseline.json', 'implementation-result.json', 'candidate.json',
                 'current-review', 'latest-implementation', 'work-state.json', 'blocker-checkpoint-failure.log'):
        source = ac / name
        if source.is_file():
            destination = folder / name
            if not destination.exists(): destination.write_bytes(source.read_bytes())
    for label, revision in (('executed', base), ('remote', remote)):
        for name in DOCS:
            destination = folder / (label + '-' + name)
            if not destination.exists() and subprocess.run(['git','cat-file','-e',revision+':'+name],capture_output=True).returncode==0:
                destination.write_bytes(git('show', revision + ':' + name))
    for label, revision in (('local', head), ('remote', remote)):
        ref = 'refs/autocycle/remote-docs/' + label + '/' + head + '-' + remote
        git('update-ref', ref, revision)
    receipt = dict(version=1, branch=b['branch'], base=base, local=head, remote=remote, merged=None,
                   history=str(folder), work_id=state['work']['id'], attempt_id=attempt['id'],
                   baseline_sha256=digest((ac / 'implementation-baseline.json').read_bytes()),
                   implementation_snapshot_sha256=digest(evidence_path.read_bytes()),
                   ownership='Frozen implementation result matches the clean local checkpoint; remote changes affect only planning documents.',
                   acceptance='Unresolved. A fresh opening Review against updated TARGET, SESSION and IMPLEMENTATION is required; the executed plan and original work criteria remain recorded separately.')
    atomic(folder / 'receipt.json', receipt)
    atomic(ac / 'remote-docs-current.json', receipt)
    return receipt


def reconcile(publish=False):
    branch = gs('branch', '--show-current')
    require(branch.startswith('checkpoint/'), 'Reconciliation requires the existing checkpoint branch.')
    head = gs('rev-parse', 'HEAD')
    ref = 'refs/remotes/origin/' + branch
    result = subprocess.run(['git', 'rev-parse', '--verify', ref], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode: return False
    remote = result.stdout.decode().strip()
    if head == remote: return False
    ac = acdir()
    if not (ac / 'implementation-baseline.json').exists(): return False
    require(not git('status', '--porcelain'), 'Working tree or index is not clean; preserve the local edits before reconciling remote documents.')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer'):
        require(not Path(gs('rev-parse', '--git-path', name)).exists(), 'An existing Git operation needs separate reconciliation.')
    # An ordinary unpublished checkpoint requires no merge. Its publication is
    # still performed by checkpoint, not by a general automatic push command.
    if ancestor(remote, head) and not (ac / 'remote-docs-current.json').exists(): return False
    receipt = known_receipt(head, remote)
    if receipt is None:
        if ancestor(remote, head): return False
        receipt = prepare_receipt(head, remote)
    if receipt.get('merged') is None:
        require(head == receipt['local'] and gs('rev-parse', ref) == receipt['remote'], 'Reconciliation inputs changed.')
        # The local side changed no planning documents and the remote side
        # changed nothing else; still verify Git's exact resulting tree.
        run = subprocess.run(['git', 'merge', '--no-ff', '--no-edit', '-m',
                              'Reconcile remote planning edits (acceptance pending Review)', receipt['remote']],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        require(run.returncode == 0, 'Git merge stopped; saved refs and evidence preserve both histories. ' + run.stderr.decode(errors='replace'))
        head = gs('rev-parse', 'HEAD')
        verify_merge(receipt, head)
        receipt['merged'] = head
        atomic(Path(receipt['history']) / 'receipt.json', receipt)
        atomic(ac / 'remote-docs-current.json', receipt)
    require(not git('status', '--porcelain'), 'Files changed during the document merge; nothing was force-pushed.')
    if publish:
        run = subprocess.run(['git', 'push', '-u', 'origin', 'HEAD:refs/heads/' + branch], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        require(run.returncode == 0, 'Merged work is preserved locally; ordinary push failed. ' + run.stderr.decode(errors='replace'))
        require(gs('rev-parse', ref) == head, 'Published revision could not be confirmed; preserve the merge for verification.')
    print('Reconcile   ✓ remote planning edits preserved; fresh Review required', flush=True)
    return True


@contextlib.contextmanager
def controller_lock():
    path = acdir() / 'controller.lock'
    # Calls from the shell controller inherit its already-held open description.
    try:
        inherited = os.fstat(9)
        same = (inherited.st_dev, inherited.st_ino) == (path.stat().st_dev, path.stat().st_ino)
    except OSError: same = False
    if same:
        yield
    else:
        with path.open('a+b') as stream:
            try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Another AutoCycle controller is running.') from None
            yield


def main():
    require(len(sys.argv) == 2 and sys.argv[1] in ('merge', 'settle'), 'Usage: remote_docs.py merge|settle')
    with controller_lock():
        changed_state = reconcile(publish=sys.argv[1] == 'settle')
    sys.exit(10 if changed_state and sys.argv[1] == 'settle' else 0)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('Remote document reconciliation: ' + str(error), file=sys.stderr)
        sys.exit(1)

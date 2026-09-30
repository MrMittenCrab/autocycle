"""Concurrent remote planning edits retain implementation and force fresh Review."""
import json
from pathlib import Path
import subprocess
import sys
from test_flow import Case, ok, fail, REAL_GIT

LEGACY_CHECKPOINT = '#!/bin/bash\nset -euo pipefail\n\nROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {\n    echo "Not inside a Git repository."\n    exit 1\n}\n\ncd "$ROOT"\n\nif [[ -z "$(git status --porcelain)" ]]; then\n    echo "Nothing to checkpoint."\n    exit 0\nfi\n\nBRANCH=$(git branch --show-current)\n\nif [[ -z "$BRANCH" ]]; then\n    echo "Detached HEAD. Refusing."\n    exit 1\nfi\n\ngit fetch --prune origin >/dev/null\n\n# Never commit implementation work directly to main or a planning branch.\n# If we are not already on a checkpoint branch, create one.\nif [[ "$BRANCH" != checkpoint/* ]]; then\n    BRANCH="checkpoint/$(date +%Y%m%d-%H%M%S)"\n\n    while git show-ref --verify --quiet "refs/heads/$BRANCH" \\\n       || git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; do\n        sleep 1\n        BRANCH="checkpoint/$(date +%Y%m%d-%H%M%S)"\n    done\n\n    git switch -c "$BRANCH" >/dev/null\nfi\n\n# Custom message:\n#   checkpoint "my message"\n#\n# Default message:\n#   derive "Step 9K.1" from IMPLEMENTATION.md\nif [[ $# -gt 0 ]]; then\n    MESSAGE="$*"\nelse\n    MESSAGE=""\n\n    if [[ -f IMPLEMENTATION.md ]]; then\n        MESSAGE=$(\n            sed -nE \'s/^# (Step [^ ]+).*/\\1/p\' IMPLEMENTATION.md \\\n            | head -n 1\n        )\n    fi\n\n    if [[ -z "$MESSAGE" ]]; then\n        MESSAGE="Checkpoint $(date \'+%Y-%m-%d %H:%M\')"\n    fi\nfi\n\n# AutoCycle supplies a frozen provider result and a clean ownership baseline.\n# Recheck immediately around staging; on failure preserve both work and index.\nowned_checkpoint_guard() {\n    if [[ "${AUTOCYCLE_CHECKPOINT_GUARD:-0}" == 1 ]]; then\n        python3 "$HOME/.autocycle/adjudication.py" checkpoint-safe || {\n            echo "Checkpoint ownership/evidence changed; work and index preserved."\n            return 1\n        }\n    fi\n}\nowned_checkpoint_guard\ngit add -A\nowned_checkpoint_guard\n\nif ! OUTPUT=$(git commit -m "$MESSAGE" 2>&1); then\n    echo "$OUTPUT"\n    exit 1\nfi\n\nif ! OUTPUT=$(git push -u origin "$BRANCH" 2>&1); then\n    echo "$OUTPUT"\n    exit 1\nfi\n\necho "Branch: $BRANCH"\necho "Commit: $(git log -1 --oneline)"\n'


def advance_remote(c, code=False):
    remote = c.p / 'editor'
    subprocess.run([REAL_GIT, 'clone', '-q', '-b', 'checkpoint/test', str(c.p / 'origin'), str(remote)], env=c.git_env, check=True)
    def git(*args):
        return subprocess.check_output([REAL_GIT, '-C', str(remote), *args], env=c.git_env, text=True, stderr=subprocess.PIPE).strip()
    git('checkout', '-q', '-B', 'checkpoint/test', 'origin/checkpoint/test')
    git('config', 'user.name', 'Owner'); git('config', 'user.email', 'owner@example.com')
    (remote / 'TARGET.md').write_text('Updated target: no yellow Answer Key; preserve in-flight work.\n')
    if code: (remote / 'remote-code.py').write_text('remote implementation\n')
    else:
        p = remote / 'IMPLEMENTATION.md'
        p.write_text(p.read_text() + '\nOwner revision: preserve work and use new TARGET.\n')
    git('add', '.'); git('commit', '-qm', 'Owner updates project direction')
    git('push', '-q', 'origin', 'HEAD:checkpoint/test')
    return git('rev-parse', 'HEAD')


def initial_run(c, *, resume=False, legacy=False, code=False, push_fault=False, merge_fault=False):
    current = (c.bin / 'checkpoint').read_bytes()
    if legacy:
        (c.bin / 'checkpoint').write_text(LEGACY_CHECKPOINT.replace('python3 "$HOME/.autocycle/adjudication.py"', 'python3 "' + str(c.p / 'adjudication.py') + '"'))
    c.enqueue('Keep the original objective')
    fault = 'checkpoint_before_push' if push_fault else ('merge_after_commit' if merge_fault else '')
    p = c.start('--resume' if resume else '1', BLOCK_STAGE='implement', **({'GIT_FAULT': fault} if fault else {}))
    c.ready(p)
    baseline = (c.a / 'implementation-baseline.json').read_bytes()
    base = c.git('rev-parse', 'HEAD')
    try:
        remote = advance_remote(c, code=code)
    except BaseException:
        c.kill(p)
        raise
    (c.a / 'release').touch()
    out, err = p.communicate(timeout=120)
    assert p.returncode == 0, (out, err)
    (c.bin / 'checkpoint').write_bytes(current)
    (c.a / 'ready').unlink(missing_ok=True)
    (c.a / 'release').unlink(missing_ok=True)
    return base, remote, baseline, out


def test_docs_edit_during_implementation_merges_then_requires_review():
    c = Case()
    try:
        base, remote, baseline, out = initial_run(c)
        assert c.git('rev-parse', 'HEAD') == c.git('rev-parse', 'origin/checkpoint/test'), out
        assert c.git('status', '--porcelain') == ''
        assert c.git('merge-base', '--is-ancestor', remote, 'HEAD') == ''
        assert (c.a / 'implementation-baseline.json').read_bytes() == baseline
        assert (c.repo / 'TARGET.md').read_text().startswith('Updated target:')
        assert 'Owner revision:' in (c.repo / 'IMPLEMENTATION.md').read_text()
        receipt = json.loads((c.a / 'remote-docs-current.json').read_text())
        assert receipt['base'] == base and receipt['remote'] == remote
        state = json.loads((c.a / 'work-state.json').read_text())['branches']['checkpoint/test']
        assert state['attempts'][-1]['outcome'] is None
        p = c.start('--extend','1', BLOCK_STAGE='review')
        c.ready(p)
        try:
            assert len([e for e in c.events() if e['kind'] == 'implement']) == 1
            assert 'remote-docs-current.json' in c.events()[-1]['prompt']
        finally: c.kill(p)
    finally: c.close()


def test_existing_failed_push_recovers_without_replaying_implementation():
    c = Case()
    try:
        base, remote, baseline, out = initial_run(c, legacy=True)
        assert c.git('rev-parse', 'HEAD') != c.git('rev-parse', 'origin/checkpoint/test')
        original = json.loads((c.a / 'work-state.json').read_text())['branches']['checkpoint/test']['work']['id']
        ok(c.run('--extend','1', PROGRESS_OUTCOME='VERIFIED', NO_CHANGE='1'))
        assert c.git('rev-parse', 'HEAD') == c.git('rev-parse', 'origin/checkpoint/test')
        assert c.git('status', '--porcelain') == ''
        assert c.git('merge-base', '--is-ancestor', remote, 'HEAD') == ''
        state = json.loads((c.a / 'work-state.json').read_text())['branches']['checkpoint/test']
        assert state['work']['id'] == original
        assert state['attempts'][0]['outcome'] == 'VERIFIED'
        reviews = [e for e in c.events() if e['kind'] == 'review']
        assert len(reviews) == 3, 'Initial Review, stale local Review, and mandatory post-merge Review'
        assert (c.repo / 'TARGET.md').read_text().startswith('Updated target:')
        assert len([e for e in c.events() if e['kind'] == 'implement']) == 2, 'Only the bounded new Plan may implement'
    finally: c.close()


def test_remote_code_and_unrelated_local_edits_are_preserved_not_merged():
    for kind in ('remote-code', 'local-edit', 'wrong-result', 'protected-edit'):
        c = Case()
        try:
            base, remote, baseline, out = initial_run(c, legacy=True, code=kind == 'remote-code')
            if kind == 'local-edit': (c.repo / 'private.txt').write_text('Unrelated manual edit')
            if kind == 'protected-edit': (c.repo / 'TARGET.md').write_text('Local manual target')
            if kind == 'wrong-result':
                p = c.a / 'implementation-result.json'; data = json.loads(p.read_text()); data['head'] = remote; p.write_text(json.dumps(data))
            head = c.git('rev-parse', 'HEAD')
            status = c.git('status', '--porcelain')
            fail(c.run('--extend','1', PROGRESS_OUTCOME='VERIFIED'))
            assert c.git('rev-parse', 'HEAD') == head
            assert c.git('status', '--porcelain') == status
            assert (c.a / 'implementation-baseline.json').read_bytes() == baseline
            assert not (c.a / 'remote-docs-current.json').exists()
        finally: c.close()


def test_push_failure_after_merge_resumes_without_duplicate_merge():
    c = Case()
    try:
        base, remote, baseline, out = initial_run(c, push_fault=True)
        merged = c.git('rev-parse', 'HEAD')
        assert merged != c.git('rev-parse', 'origin/checkpoint/test')
        assert (c.a / 'remote-docs-current.json').is_file()
        ok(c.run('--extend','1', PROGRESS_OUTCOME='VERIFIED', NO_CHANGE='1'))
        merges = c.git('rev-list', '--merges', base + '..HEAD').splitlines()
        assert merges == [merged]
        assert c.git('rev-parse', 'HEAD') == c.git('rev-parse', 'origin/checkpoint/test')
    finally: c.close()


def test_merge_committed_before_receipt_finalized_recovers_once():
    c = Case()
    try:
        wrapper = c.bin / 'git-fault'
        text = wrapper.read_text()
        text = text.replace("sys.exit(r.returncode)",
                            "if r.returncode==0 and fail=='merge_after_commit' and 'merge' in args and '--no-ff' in args:sys.exit(1)\n"
                            "sys.exit(r.returncode)")
        wrapper.write_text(text)
        base, remote, baseline, out = initial_run(c, merge_fault=True)
        merged = c.git('rev-parse', 'HEAD')
        receipt = json.loads((c.a / 'remote-docs-current.json').read_text())
        assert receipt['merged'] is None
        assert c.git('rev-list', '--merges', base + '..HEAD').splitlines() == [merged]
        assert (c.a / 'implementation-baseline.json').read_bytes() == baseline
        ok(c.run('--extend','1', PROGRESS_OUTCOME='VERIFIED', NO_CHANGE='1'))
        receipt = json.loads((c.a / 'remote-docs-current.json').read_text())
        assert receipt['merged'] == merged
        assert c.git('rev-list', '--merges', base + '..HEAD').splitlines() == [merged]
        assert c.git('rev-parse', 'HEAD') == c.git('rev-parse', 'origin/checkpoint/test')
        assert c.rows()[0]['state'] == 'archived'
    finally: c.close()


if __name__ == '__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name](); print('PASS ' + name, flush=True)

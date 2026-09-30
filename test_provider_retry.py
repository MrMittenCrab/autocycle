"""Explicit Cursor errors take precedence over markerless end_turn validation."""
import json
import sys
from test_completion import Work
from test_historical_recovery import setup, ledger
from test_flow import ok

ERROR = 'Error: RetriableError: [resource_exhausted] Error'


def run_case(failures, error=ERROR):
    with Work() as x:
        c = x.c
        sha = setup(x)
        original = ledger(c)
        docs = {n: (c.repo/n).read_bytes() for n in ('TARGET.md', 'SESSION.md', 'IMPLEMENTATION.md')}
        # Seed the existing cycle input identity; legacy empty IDs are filled on resume.
        state_path = c.a/'resume-state'
        state_path.write_text(state_path.read_text().replace("INPUT_CYCLE_ID=''", 'INPUT_CYCLE_ID=0123456789abcdef0123456789abcdef'))
        state = state_path.read_bytes()
        agent = c.bin/'agent'
        text = agent.read_text()
        marker = "   chunk('agent_message_chunk',os.environ['TRANSPORT_ERROR'])"
        text = text.replace(marker, """   chunk('agent_message_chunk','Inspecting implementation files.')
   chunk('tool_call','Read feature.py')
   chunk('tool_call_update','Read complete')
""" + marker)
        agent.write_text(text)
        r = c.run('--resume', TRANSPORT_FAILURES=str(failures), TRANSPORT_ERROR=error)
        assert (c.a/'events.jsonl').exists(), (r.stdout, r.stderr)
        retryable = error == ERROR
        attempts = min(failures + 1, 3) if retryable else 1
        assert [e['kind'] for e in c.events()] == ['implement'] * attempts, (r.stdout, r.stderr)
        entries = [json.loads(line) for line in (c.a/'provider-entry.jsonl').read_text().splitlines()]
        assert all(e['state']['branches']['checkpoint/test'] == original for e in entries)
        assert all(e['partial'] == 'Retain valid partial work\n' and e['feature'] == 'value = 41\n' for e in entries[1:])
        assert {n: (c.repo/n).read_bytes() for n in docs} == docs
        assert c.rows() == []
        assert not (c.a/'unsafe-git').exists()
        logs = [p.read_text() for p in sorted(c.a.glob('cursor-*.log'))]
        failed_logs = [s for s in logs if error in s]
        assert len(failed_logs) == min(failures, attempts)
        for log in failed_logs:
            assert log.index('Read complete') < log.index(error) < log.index('[result] stopReason=end_turn')
            assert 'IMPLEMENT_STATUS:' not in log
            if retryable:
                assert 'Provider RetriableError' in log
                assert 'Implementation response rejected' not in log
        if retryable:
            assert 'Network ' not in r.stdout, r.stdout
            assert 'retry 1/2' in r.stdout
            assert ERROR in r.stdout + r.stderr
        if failures < 3 and retryable:
            ok(r)
            assert c.git('status', '--porcelain') == ''
            assert 'STAGE=checkpoint_done' in (c.a/'resume-state').read_text()
        else:
            assert r.returncode == (76 if retryable else 1), (r.stdout, r.stderr)
            assert (c.a/'resume-state').read_bytes() == state, ((c.a/'resume-state').read_text(), state.decode())
            assert ledger(c) == original
            assert c.git('rev-parse', 'HEAD') == sha
            assert c.git('status', '--porcelain')
            assert not (c.a/'implementation-result.json').exists()
            assert not (c.a/'candidate.json').exists()
            assert (c.a/'implementation-interruption.json').exists() == retryable
            if retryable:
                assert 'retry 2/2' in r.stdout
                assert 'after 3 attempts' in r.stdout
                assert 'Provider RetriableError' in r.stdout + r.stderr
                ok(c.run('--resume'))
            else:
                assert 'Implementation response rejected' in r.stderr
        assert (c.repo/'partial.txt').read_text() == 'Retain valid partial work\n'
        assert (c.repo/'feature.py').read_text() == 'value = 41\n'


def test_resource_exhausted_retry_succeeds():
    run_case(1)


def test_resource_exhausted_retry_exhaustion_is_resumable():
    run_case(9)


def test_ordinary_markerless_end_turn_is_rejected():
    run_case(9, 'Implementation still in progress.')


def test_resource_exhausted_without_explicit_retriable_error_is_rejected():
    run_case(9, 'Error: [resource_exhausted] Error')


if __name__ == '__main__':
    for name in sys.argv[1:] or [n for n in globals() if n.startswith('test_')]:
        globals()[name](); print('PASS ' + name, flush=True)

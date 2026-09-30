#!/usr/bin/env python3
"""Run selected regression scripts; no selection runs full deterministic certification.

AGENTS.md governs test scope. Tests use isolated fixtures, never a live project.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent


def select_tests(names, parser):
    available = sorted(root.glob('test_*.py')) + [root/'test_response_parser.js']
    if not names:
        return available
    selected = []
    for name in names:
        path = Path(name)
        if not path.is_absolute():
            path = root/path
        # Only repository-root test scripts are allowed, never symlinks or
        # traversal outside the root. Validate every target before running any.
        if ('..' in path.parts or path.is_symlink() or not path.is_file()
                or path.resolve().parent != root or path.resolve() not in available):
            parser.error('unknown or unsafe test target: '+name)
        path = path.resolve()
        if path not in selected:
            selected.append(path)
    return selected


def run(path):
    interpreter = 'node' if path.suffix == '.js' else sys.executable
    return subprocess.run([interpreter, str(path)], cwd=root, capture_output=True, text=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tests', nargs='*', help='repository-root test_*.py names/paths or test_response_parser.js; omitted = full certification')
    args = parser.parse_args(argv)
    paths = select_tests(args.tests, parser)
    if not args.tests:
        for name in ('autocycle', 'stage', 'sync', 'checkpoint'):
            subprocess.run(['/bin/bash', '-n', str(root/name)], check=True)
    failed = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(run, path): path.name for path in paths if path.suffix == '.py'}
        for job in as_completed(jobs):
            result = job.result()
            name = jobs[job]
            print(('FAIL ' if result.returncode else 'PASS ')+name, flush=True)
            print(result.stdout+result.stderr, flush=True)
            if result.returncode:
                failed.append(name)
    for path in paths:
        if path.suffix == '.js':
            result = run(path)
            print(('FAIL ' if result.returncode else 'PASS ')+path.name, flush=True)
            print(result.stdout+result.stderr, flush=True)
            if result.returncode:
                failed.append(path.name)
    if failed:
        print('Failed suites: '+', '.join(failed), file=sys.stderr, flush=True)
        return 1
    print('Selected AutoCycle tests passed.' if args.tests else 'All AutoCycle tests passed.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

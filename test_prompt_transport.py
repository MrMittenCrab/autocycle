"""Exercise stage's actual launch blocks through the real adjudicator."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_flow import BASE, REAL_GIT, TEST_ENV


class PromptTransportTests(unittest.TestCase):
    def test_exact_small_and_oversized_prompts(self):
        source = (BASE/'stage').read_text()
        launches = []
        for log in ('REVIEW_LOG', 'PLAN_LOG'):
            end = source.index('EXIT_CODE=$?', source.index('>"$'+log+'" 2>&1'))
            start = source.rfind('set +e', 0, end)
            launches.append((log, source[start:end] + 'EXIT_CODE=$?\nexit "$EXIT_CODE"\n'))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run([REAL_GIT, 'init', '-q', str(root)], env=TEST_ENV, check=True)
            provider = root/'codex'
            provider.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:3] == ['exec', '--sandbox', 'read-only'], args
data = sys.stdin.buffer.read() if args[-1] == '-' else os.fsencode(args[-1])
Path('received').write_bytes(data)
Path(args[args.index('--output-last-message')+1]).write_bytes(b'answer')
print('provider stdout')
print('provider stderr', file=sys.stderr)
sys.exit(int(os.environ.get('PROVIDER_EXIT', '0')))
''')
            provider.chmod(0o755)
            env = dict(TEST_ENV, PATH=str(root)+os.pathsep+os.environ['PATH'])
            literal = 'quotes: \' " $HOME $(touch NO) `touch NO` \\ 中文\r\n'.encode()
            for size in ('small', 'oversized'):
                payload = literal if size == 'small' else literal * (2*1024*1024//len(literal)+1)
                for log, launch in launches:
                    for code in (0, 7):
                        with self.subTest(size=size, stage=log, exit=code):
                            (root/'prompt').write_bytes(payload)
                            script = root/'launch.sh'
                            script.write_text('set -euo pipefail\n'
                                'ADJUDICATOR="$1"\n'
                                + next(line for line in source.splitlines() if line.startswith('adjudication()'))+'\n'
                                'IFS= read -r -d "" PROMPT < prompt || true\n'
                                'ANSWER=answer\nREVIEW_LOG=provider.log\nPLAN_LOG=provider.log\n'
                                + launch)
                            result = subprocess.run(['/bin/bash', str(script), str(BASE/'adjudication.py')],
                                cwd=root, env=dict(env, PROVIDER_EXIT=str(code)), capture_output=True, timeout=15)
                            log_bytes = (root/'provider.log').read_bytes()
                            self.assertEqual(result.returncode, code, (result.stderr, log_bytes))
                            self.assertEqual((root/'received').read_bytes(), payload)
                            self.assertEqual((root/'answer').read_bytes(), b'answer')
                            self.assertIn(b'provider stdout', log_bytes)
                            self.assertIn(b'provider stderr', log_bytes)
                            self.assertFalse((root/'NO').exists())


if __name__ == '__main__':
    unittest.main()

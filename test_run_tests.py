"""Scoped runner regression checks; full-mode dispatch is mocked, never executed."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import run_tests


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in ('test_a.py', 'test_b.py', 'test_response_parser.js'):
            (self.root / name).write_text('print("synthetic test")\n')
        self.root_patch = patch.object(run_tests, 'root', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def dispatch(self, targets, fail=None):
        def execute(command, **kwargs):
            return subprocess.CompletedProcess(command, int(Path(command[-1]).name == fail), '', '')
        with patch.object(run_tests.subprocess, 'run', side_effect=execute) as call, contextlib.redirect_stdout(io.StringIO()):
            result = run_tests.main(targets)
        return result, [item.args[0] for item in call.call_args_list]

    def test_selected_python_only_and_duplicate_paths(self):
        result, commands = self.dispatch(['test_a.py', str(self.root / 'test_a.py')])
        self.assertEqual(result, 0)
        self.assertEqual(commands, [[sys.executable, str(self.root / 'test_a.py')]])

    def test_multiple_selected_and_js_only(self):
        _, commands = self.dispatch(['./test_a.py', 'test_b.py'])
        self.assertEqual({Path(c[-1]).name for c in commands}, {'test_a.py', 'test_b.py'})
        _, commands = self.dispatch(['test_response_parser.js'])
        self.assertEqual(commands, [['node', str(self.root / 'test_response_parser.js')]])

    def test_no_arguments_preserve_full_dispatch_without_running_suite(self):
        result, commands = self.dispatch([])
        self.assertEqual(result, 0)
        self.assertEqual(commands[:4], [['/bin/bash', '-n', str(self.root / name)]
                                       for name in ('autocycle', 'stage', 'sync', 'checkpoint')])
        self.assertEqual({Path(c[-1]).name for c in commands[4:]},
                         {'test_a.py', 'test_b.py', 'test_response_parser.js'})
        self.assertEqual(len(commands), 7)

    def test_reject_unsafe_or_unknown_before_any_execution(self):
        (self.root / 'test_link.py').symlink_to(Path(__file__).resolve())
        (self.root / 'test_directory.py').mkdir()
        for target in ('missing.py', 'install.py', '../test_a.py', 'test_link.py',
                       'test_directory.py', str(Path(__file__).resolve()), 'test_*.py'):
            with self.subTest(target=target), patch.object(run_tests.subprocess, 'run') as call, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    run_tests.main(['test_a.py', target])
                self.assertEqual(error.exception.code, 2)
                call.assert_not_called()

    def test_failure_propagates_for_python_and_js(self):
        for target in ('test_a.py', 'test_response_parser.js'):
            result, commands = self.dispatch([target], fail=target)
            self.assertEqual(result, 1)
            self.assertEqual(len(commands), 1)

    def test_real_scoped_child_execution(self):
        marker = self.root / 'ran'
        (self.root / 'test_a.py').write_text('from pathlib import Path\nPath("ran").write_text("selected")\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_tests.main(['test_a.py']), 0)
        self.assertEqual(marker.read_text(), 'selected')


if __name__ == '__main__':
    unittest.main()

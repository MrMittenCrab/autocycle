"""Installer cannot write a live runtime before verification and stop checks."""
from pathlib import Path
import subprocess,tempfile
from unittest.mock import patch
import install

def main():
    def build(root, output):output.write_bytes((root/'autocycle-office-capture').read_bytes())
    mocked_build=patch.object(install, 'build_native', side_effect=build)
    mocked_build.start()
    # A caller discussing the command is not itself a controller.
    with patch.object(install.subprocess,'check_output',return_value='codex exec Please install autocycle and do not run autocycle --resume\n'):
        install.assert_stopped()
    for command in ('autocycle --resume', '/Users/example/bin/autocycle 5',
                    '/bin/bash /Users/example/bin/autocycle 5', 'bash -e /Users/example/bin/autocycle --resume'):
        with patch.object(install.subprocess,'check_output',return_value=command+'\n'):
            try:install.assert_stopped()
            except RuntimeError:pass
            else:raise AssertionError('missed active controller: '+command)
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)/'source';home=Path(temp)/'home';root.mkdir()
        for name in install.RUNTIME:(root/name).write_text('candidate '+name)
        old=install.destination(home,'autocycle');old.parent.mkdir(parents=True);old.write_text('live')
        with patch.object(install.subprocess,'check_output',return_value=''), patch.object(install.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['tests'])):
            try:install.install(root,home)
            except subprocess.CalledProcessError:pass
            else:raise AssertionError('failed tests installed')
        assert old.read_text()=='live'
        with patch.object(install.subprocess,'run'),patch.object(install.subprocess,'check_output',return_value='/bin/bash /Users/example/bin/autocycle 5\n'):
            try:install.install(root,home)
            except RuntimeError:pass
            else:raise AssertionError('active controller installed')
        assert old.read_text()=='live'
        with patch.object(install.subprocess,'run'),patch.object(install.subprocess,'check_output',return_value=''):
            install.install(root,home)
        assert old.read_text()=='candidate autocycle'
        assert next((home/'.autocycle/backups').glob('*/autocycle')).read_text()=='live'
        for name in install.RUNTIME:assert install.destination(home,name).read_bytes()==(root/name).read_bytes()
    mocked_build.stop()
    print('PASS installer verification, active-controller refusal, backup and replacement')

# All installation fixtures below use a temporary home, a fake native build,
# and substituted certification. No live runtime or complete suite is used.
import contextlib
import io
import os
import sys
import unittest


class InstallModesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'source'
        self.home = Path(self.temp.name)/'home'
        self.root.mkdir()
        for name in install.RUNTIME:
            (self.root/name).write_text('candidate '+name)
            dest = install.destination(self.home, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text('old '+name)
        self.build = patch.object(install, 'build_native', side_effect=lambda root, output: output.write_text('compiled helper')).start()
        self.stopped = patch.object(install, 'assert_stopped').start()
        self.suite = patch.object(install.subprocess, 'run').start()
        self.addCleanup(patch.stopall)

    def unchanged(self):
        for name in install.RUNTIME:
            self.assertEqual(install.destination(self.home, name).read_text(), 'old '+name)

    def test_cli_keeps_certified_default_and_explicit_verified_mode(self):
        for flag, certify in (('--install', True), ('--install-verified', False)):
            with patch.object(sys, 'argv', ['install.py', flag]), patch.object(install, 'install') as call:
                install.main()
            self.assertEqual(call.call_args.kwargs.get('certify', True), certify)
            self.assertEqual(call.call_count, 1)
        with patch.object(sys, 'argv', ['install.py']):
            install.main()
        self.suite.assert_called_once_with([sys.executable, str(Path(install.__file__).resolve().parent/'run_tests.py')],
                                          cwd=Path(install.__file__).resolve().parent, check=True)
        with patch.object(sys, 'argv', ['install.py', '--install', '--install-verified']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                install.main()

    def test_certification_failure_precedes_all_replacement(self):
        self.suite.side_effect = subprocess.CalledProcessError(1, ['mock certification'])
        with patch.object(install.os, 'replace') as replace:
            with self.assertRaises(subprocess.CalledProcessError):
                install.install(self.root, self.home)
            replace.assert_not_called()
        self.suite.assert_called_once_with([sys.executable, str(self.root/'run_tests.py')], cwd=self.root, check=True)
        self.unchanged()
        self.assertFalse((self.home/'.autocycle/backups').exists())

    def test_verified_prepares_backups_then_atomic_replacements_controller_last(self):
        real_replace = os.replace
        replaced = []
        def replace(temp, dest):
            backup, = (self.home/'.autocycle/backups').iterdir()
            for name in install.RUNTIME:
                self.assertEqual((backup/name).read_text(), 'old '+name)
                if name not in replaced:
                    self.assertEqual(len(list(install.destination(self.home, name).parent.glob('.'+name+'.*'))), 1)
            replaced.append(dest.name)
            real_replace(temp, dest)
        with patch.object(install.os, 'replace', side_effect=replace):
            install.install(self.root, self.home, certify=False)
        self.suite.assert_not_called()
        self.build.assert_called_once()
        self.assertEqual(self.stopped.call_count, 3)
        self.assertEqual(set(replaced), set(install.RUNTIME))
        self.assertEqual(replaced[-1], 'autocycle')
        for name in install.RUNTIME:
            self.assertEqual(install.destination(self.home, name).read_text(),
                             'compiled helper' if name == 'autocycle-office-capture' else 'candidate '+name)

    def test_verified_refuses_active_controller_at_every_guard(self):
        for guard in range(3):
            with self.subTest(guard=guard):
                self.stopped.side_effect = [None]*guard + [RuntimeError('active controller')]
                with self.assertRaisesRegex(RuntimeError, 'active controller'):
                    install.install(self.root, self.home, certify=False)
                self.unchanged()
        self.suite.assert_not_called()

    def test_build_failure_cannot_replace_runtime(self):
        self.build.side_effect = RuntimeError('build failed')
        with self.assertRaisesRegex(RuntimeError, 'build failed'):
            install.install(self.root, self.home, certify=False)
        self.unchanged()
        self.suite.assert_not_called()

    def test_source_changes_during_build_or_certification_block_install(self):
        def mutate(*args, **kwargs):
            (self.root/'stage').write_text('changed')
        self.build.side_effect = mutate
        with self.assertRaisesRegex(RuntimeError, 'Source changed'):
            install.install(self.root, self.home, certify=False)
        self.unchanged()
        self.build.side_effect = lambda root, output: output.write_text('compiled helper')
        self.suite.side_effect = lambda *args, **kwargs: (self.root/'stage').write_text('changed again')
        with self.assertRaisesRegex(RuntimeError, 'Source changed'):
            install.install(self.root, self.home)
        self.unchanged()

    def test_preparation_failure_or_source_change_blocks_all_replacement(self):
        real_copy = install.shutil.copyfile
        def mutate(source, dest, **kwargs):
            result = real_copy(source, dest, **kwargs)
            if Path(source).name == 'stage' and Path(source).parent == self.root:
                (self.root/'stage').write_text('mutated during preparation')
            return result
        def fail_copy(source, dest, **kwargs):
            if Path(source).parent == self.root:
                raise OSError('copy failed after temporary file creation')
            return real_copy(source, dest, **kwargs)
        for effect in (fail_copy, mutate):
            with self.subTest(effect=effect), patch.object(install.shutil, 'copyfile', side_effect=effect), patch.object(install.os, 'replace') as replace:
                with self.assertRaises((OSError, RuntimeError)):
                    install.install(self.root, self.home, certify=False)
                replace.assert_not_called()
                self.unchanged()
                for name in install.RUNTIME:
                    self.assertEqual(list(install.destination(self.home, name).parent.glob('.'+name+'.*')), [])


if __name__ == '__main__':
    main()
    unittest.main()

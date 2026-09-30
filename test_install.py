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

if __name__=='__main__':main()

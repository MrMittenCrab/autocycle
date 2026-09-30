#!/usr/bin/env python3
"""Verify this source, then explicitly install it with backups; never run a project."""
import argparse
import datetime
import hashlib
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

RUNTIME=('autocycle','stage','instructions.py','adjudication.py','migration.py',
         'progress.py','remote_docs.py','implementation_response.js','stage_display.js','excel_verification.py','native_office.py','sync','checkpoint','autocycle-office-capture')

def destination(home,name):
    return home/('bin' if name in ('autocycle','sync','checkpoint') else '.autocycle')/name

def snapshot(root):
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()
            if p.is_file() and (p.name in RUNTIME or p.name in ('run_tests.py', 'install.py', 'office_capture.swift') or p.name.startswith('test_'))}

def assert_stopped():
    output=subprocess.check_output(['ps','-axo','command='],text=True)
    for line in output.splitlines():
        # Match the executable (possibly launched by a shell), not arbitrary
        # arguments such as an editor/agent prompt mentioning autocycle.
        if re.search(r'^\s*(?:(?:\S*/)?(?:bash|sh|zsh)(?:\s+-\S+)*\s+)?(?:\S*/)?autocycle(?:\s|$)',line):
            raise RuntimeError('Stop active AutoCycle controllers before installing.')

def build_native(root, output):
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-O',
                    '-target', platform.machine()+'-apple-macos14.0',
                    str(root/'office_capture.swift'), '-o', str(output)], check=True)


def install(root,home):
    assert_stopped()
    with tempfile.TemporaryDirectory(prefix='autocycle-build-') as directory:
        helper = Path(directory)/'autocycle-office-capture'
        before = snapshot(root)
        build_native(root, helper)
        if snapshot(root) != before:
            raise RuntimeError('Source changed during compilation; rerun installation.')
        install_verified(root, home, helper)


def install_verified(root,home,helper):
    before=snapshot(root)
    subprocess.run([sys.executable,str(root/'run_tests.py')],cwd=root,check=True)
    if snapshot(root)!=before:raise RuntimeError('Source changed during verification; rerun installation.')
    assert_stopped()
    backup=home/'.autocycle/backups'/('session-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    # All backups and temporary files are prepared before replacing anything.
    prepared=[]
    try:
        for name in RUNTIME:
            dest=destination(home,name);dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists():shutil.copy2(dest,backup/name)
            fd,temp=tempfile.mkstemp(prefix='.'+name+'.',dir=dest.parent)
            os.close(fd);temp=Path(temp)
            shutil.copyfile(helper if name == 'autocycle-office-capture' else root/name,temp)
            temp.chmod(0o755 if name in ('autocycle','stage','sync','checkpoint','autocycle-office-capture') else 0o600)
            with temp.open('rb') as f:os.fsync(f.fileno())
            prepared.append((temp,dest))
        assert_stopped()
        for temp,dest in sorted(prepared,key=lambda pair:pair[1].name=='autocycle'):
            os.replace(temp,dest)
        print('Installed verified AutoCycle. Backup:',backup)
    finally:
        for temp,_ in prepared:temp.unlink(missing_ok=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install',action='store_true',help='install after the full test suite passes')
    args=parser.parse_args();root=Path(__file__).resolve().parent
    if args.install:install(root,Path.home())
    else:subprocess.run([sys.executable,str(root/'run_tests.py')],cwd=root,check=True)

if __name__=='__main__':main()

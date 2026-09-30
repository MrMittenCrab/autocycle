"""Run the real shell scripts and prompt flow under each requested Bash binary."""
from pathlib import Path
import shutil
import subprocess
import sys
from test_flow import BASE, Case, ok


def verify_shell(shell):
    shell=Path(shell).resolve()
    version=subprocess.check_output([str(shell),'--version'],text=True).splitlines()[0]
    for name in ('autocycle','stage','sync','checkpoint'):
        subprocess.run([str(shell),'-n',str(BASE/name)],check=True)
    c=Case()
    try:
        # Test-only interpreter selection: every production shell script uses
        # this binary, even when launched indirectly by the controller.
        for name in ('autocycle','stage','sync','checkpoint'):
            path=c.bin/name
            source=path.read_text()
            path.write_text('#!'+str(shell)+'\n'+source.split('\n',1)[1])
        payload='SHELL_COMPAT_SENTINEL: keep the work\'s ID (unchanged); "quotes"; $(touch SHOULD_NOT_EXIST) `touch ALSO_NOT`\n中文 follow-up'
        c.enqueue(payload)
        r=c.run('1')
        ok(r)
        events=c.events()
        for kind in ('review','plan'):
            prompt=next(e['prompt'] for e in events if e['kind']==kind)
            assert 'SHELL_COMPAT_SENTINEL' in prompt
            assert "work's ID (unchanged)" in prompt
            assert '$(touch SHOULD_NOT_EXIST)' in prompt and '`touch ALSO_NOT`' in prompt
            assert '中文 follow-up' in prompt
            assert 'AUTOCYCLE_WORK_CONTEXT:' in prompt
        assert 'AUTOCYCLE_REVIEW:' in next(e['prompt'] for e in events if e['kind']=='plan')
        assert not (c.repo/'SHOULD_NOT_EXIST').exists() and not (c.repo/'ALSO_NOT').exists()
        assert c.git('status','--porcelain')==''
        assert c.git('rev-parse','HEAD')==c.git('rev-parse','origin/checkpoint/test')
        assert c.rows()[0]['state']=='archived'
        ok(c.run('--extend','1',REVIEW_STATUS='DONE'))
        assert c.rows()[0]['state']=='archived'
        print('PASS syntax, literal prompts, checkpoint and following Review: '+version,flush=True)
    finally:c.close()


if __name__=='__main__':
    shells=sys.argv[1:] or ['/bin/bash',shutil.which('bash')]
    for shell in dict.fromkeys(str(Path(s).resolve()) for s in shells if s):
        verify_shell(shell)

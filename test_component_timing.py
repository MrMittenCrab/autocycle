"""Controller charges durable boundaries to the three visible stage clocks."""
import re, json
from test_flow import Case, ok


def duration(text):
    m,s=map(int,text.split(':'));return m*60+s


def test_three_stages_and_boundary_accounting():
    c=Case()
    try:
        for name in ('sync','checkpoint'):
            path=c.bin/name
            path.write_text(path.read_text().replace('set -euo pipefail','set -euo pipefail\nsleep 1.1',1))
        result=c.run('1');ok(result)
        assert '\x1b' not in result.stdout,result.stdout
        lines=result.stdout.splitlines()
        stages={label:re.findall(r'^'+label+r' +✓ (\d+:\d+)$',result.stdout,re.M) for label in ('Review','Plan','Implement')}
        assert all(len(values)==1 for values in stages.values()),result.stdout
        assert not any(re.match(r'^(Sync|Checkpoint|Observer|Controller|Office)\s',line) for line in lines),result.stdout
        assert re.search(r'^    ✓ Baseline [0-9a-f]+$',result.stdout,re.M),result.stdout
        assert re.search(r'^    ✓ Checkpoint [0-9a-f]+$',result.stdout,re.M),result.stdout
        total=re.findall(r'^Cycle +(\d+:\d+)$',result.stdout,re.M)
        assert len(total)==1,result.stdout
        assert duration(total[0])==sum(duration(values[0]) for values in stages.values()),result.stdout
        assert duration(stages['Plan'][0])>=1 and duration(stages['Implement'][0])>=1,result.stdout
        assert lines.index(next(x for x in lines if x.startswith('    ✓ Baseline '))) < lines.index(next(x for x in lines if x.startswith('Implement '))),result.stdout
    finally:c.close()

def test_network_is_excluded_from_cycle():
    from test_network_resume import fixture,ERROR
    c=Case()
    try:
        fixture(c)
        result=c.run('1',TRANSPORT_FAILURES='2',TRANSPORT_ERROR=ERROR);ok(result)
        values=[]
        for label in ('Review','Plan','Implement'):
            found=re.search(r'^'+label+r' +✓ (\d+:\d+)$',result.stdout,re.M) or re.search(r'^    ✓ '+label+r' active (\d+:\d+)$',result.stdout,re.M)
            assert found,result.stdout
            values.append(duration(found[1]))
        total=re.search(r'^Cycle +(\d+:\d+)$',result.stdout,re.M)
        assert total and duration(total[1])==sum(values),result.stdout
        waits=re.findall(r'^Network +✓ (\d+:\d+)$',result.stdout,re.M)
        assert len(waits)==2 and sum(map(duration,waits))>=6,result.stdout
        assert [e['kind'] for e in c.events()]==['review','plan','implement','implement','implement']
    finally:c.close()


def test_access_once_per_invocation_even_with_network():
    from test_native_office_flow import prepare
    c=Case()
    try:
        prepare(c)
        controller=c.bin/'autocycle';text=controller.read_text()
        text=text.replace('network_recover() { return 0; }','network_recover() { sleep 1.1; return 0; }')
        text=text.replace('    controller_stage Access\n','    controller_stage Access\n    network_wait\n',1)
        controller.write_text(text)
        result=c.run('2');ok(result)
        assert len(re.findall(r'^Access +',result.stdout,re.M))==1,result.stdout
        assert len(re.findall(r'^Cycle +\d+:\d+$',result.stdout,re.M))==2,result.stdout
        assert len(re.findall(r'^Network +✓ ',result.stdout,re.M))==1,result.stdout
        assert '    ✓ Access active ' in result.stdout,result.stdout
        result=c.run('--resume');ok(result)
        assert len(re.findall(r'^Access +',result.stdout,re.M))==1,result.stdout
        assert not re.search(r'^Cycle +\d+:\d+$',result.stdout,re.M),result.stdout
    finally:c.close()


def test_git_transport_is_stage_local():
    from test_flow import fail
    for phase in ('Plan','Implement'):
        c=Case()
        try:
            shim=c.bin/'git'
            shim.write_text('''#!/bin/bash
if [[ "${AUTOCYCLE_STAGE_LABEL:-}" == "'''+phase+'''" && "$*" == *push* && ! -f "'''+str(c.a/'git-network-once')+'''" ]]; then
 touch "'''+str(c.a/'git-network-once')+'''"
 echo "fatal: unable to access 'https://fixture/': Could not resolve host: fixture" >&2
 exit 128
fi
exec "$REAL_GIT" "$@"
''')
            result=c.run('1');ok(result)
            assert len(re.findall(r'^'+phase+r' +',result.stdout,re.M))==1,result.stdout
            assert len(re.findall(r'^Network +✓ ',result.stdout,re.M))==1,result.stdout
            assert '    ✓ '+phase+' active ' in result.stdout,result.stdout
            assert [e['kind'] for e in c.events()]==['review','plan','implement']
            values=[]
            for label in ('Review','Plan','Implement'):
                match=re.search(r'^'+label+r' +✓ (\d+:\d+)$',result.stdout,re.M) or re.search(r'^    ✓ '+label+r' active (\d+:\d+)$',result.stdout,re.M)
                assert match,result.stdout
                values.append(duration(match[1]))
            assert duration(re.search(r'^Cycle +(\d+:\d+)$',result.stdout,re.M)[1])==sum(values),result.stdout
        finally:c.close()


def test_git_terminal_transport_and_permission():
    from test_flow import fail
    for transport in (True,False):
        c=Case()
        try:
            diagnostic="fatal: unable to access 'https://fixture/': Could not resolve host: fixture" if transport else 'fatal: permission denied'
            shim=c.bin/'git'
            shim.write_text('''#!/bin/bash
if [[ "${AUTOCYCLE_STAGE_LABEL:-}" == Implement && "$*" == *push* ]]; then
 echo "'''+diagnostic+'''" >&2
 exit 128
fi
exec "$REAL_GIT" "$@"
''')
            result=c.run('1')
            if transport:
                fail(result)
                assert result.returncode==76,(result.stdout,result.stderr)
                assert 'Network     ✗ ' in result.stdout and 'retry exhausted' in result.stdout,result.stdout
                assert 'STAGE=checkpoint_pending' in (c.a/'resume-state').read_text()
                assert len(re.findall(r'^Implement +',result.stdout,re.M))==1,result.stdout
                assert not re.search(r'^Implement +✗',result.stdout,re.M),result.stdout
            else:
                assert 'Network ' not in result.stdout,result.stdout
            assert [e['kind'] for e in c.events()]==['review','plan','implement']
        finally:c.close()


def test_plan_publication_and_resume():
    from test_flow import fail
    c=Case()
    try:
        controller=c.bin/'autocycle';original=controller.read_text()
        marker='            [[ "$(remote_sha)" == "$PUBLISHED_PLAN_SHA" ]]'
        assert marker in original
        controller.write_text(original.replace(marker,'''            ADVANCED=$(git commit-tree "$(git rev-parse origin/$(branch)^{tree})" -p "$(remote_sha)" -m 'Concurrent publication')
            git push origin "$ADVANCED:refs/heads/$(branch)" >/dev/null 2>&1
            fetch_remote
'''+marker))
        result=c.run('1');fail(result)
        assert 'published Plan changed before Baseline' in result.stdout,result.stdout
        assert not any(e['kind']=='implement' for e in c.events())
        assert not (c.a/'implementation-baseline.json').exists()
    finally:c.close()
    c=Case()
    try:
        controller=c.bin/'autocycle';original=controller.read_text()
        marker='            run_engine_stage --plan-only 0'
        controller.write_text(original.replace(marker,'            exit 94\n'+marker))
        fail(c.run('1'))
        controller.write_text(original)
        result=c.run('--resume');ok(result)
        assert len(re.findall(r'^Plan +',result.stdout,re.M))==1,result.stdout
        assert [e['kind'] for e in c.events()]==['review','plan','implement']
    finally:c.close()
    c=Case()
    try:
        controller=c.bin/'autocycle';original=controller.read_text()
        marker='            [[ "$(remote_sha)" == "$PUBLISHED_PLAN_SHA" ]]'
        controller.write_text(original.replace(marker,'            exit 94\n'+marker))
        fail(c.run('1'))
        published=(c.a/'planned-commit').read_text().split()[0]
        controller.write_text(original)
        result=c.run('--resume');ok(result)
        assert [e['kind'] for e in c.events()]==['review','plan','implement'],result.stdout
        assert len(re.findall(r'^Plan +',result.stdout,re.M))==1,result.stdout
        assert json.loads((c.a/'implementation-baseline.json').read_text())['head']==published
    finally:c.close()


if __name__=='__main__':
    test_git_terminal_transport_and_permission();print('PASS terminal Git transport stops; permission is not Network',flush=True)
    test_git_transport_is_stage_local();print('PASS Git publication Network remains stage-local',flush=True)
    test_plan_publication_and_resume();print('PASS exact published Plan and single resumed Plan',flush=True)
    test_three_stages_and_boundary_accounting();print('PASS stage boundary accounting',flush=True)
    test_network_is_excluded_from_cycle();print('PASS Network excluded from stages and Cycle',flush=True)
    test_access_once_per_invocation_even_with_network();print('PASS Access once per invocation, Network outside Cycle',flush=True)

"""Standalone Network records pause a stage across renderer reconstruction."""
import json
import subprocess
import tempfile
from pathlib import Path
from test_implement_display import screen


def test_network_clock_and_timeline():
    for label,tty in ((label,tty) for label in ('Review','Plan','Implement','Access') for tty in (True,False)):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(['node', '-e', r'''
const {StageDisplay}=require(process.argv[1]);
Object.defineProperty(process.stdout,'isTTY',{value:process.argv[3]==='true'});
let now=100000, output=''; const emit=s=>output+=s, file=process.argv[2];
const label=process.argv[4];
let d=new StageDisplay(file,label,()=>now,emit); const identity=d.state.id;
now+=37000;d.event('✓ Found existing evidence');d.networkStart();
now+=18000;d.tick();
if(d.elapsed()!=='00:37')throw Error('network counted in Review');
d=new StageDisplay(file,label,()=>now,emit);
if(d.state.id!==identity)throw Error('stage identity changed');
d.networkEnd('✓');
now+=5000;d.event('▶ Prune retained evidence');d.networkStart();
now+=22000;d.networkEnd('✓');
now+=10000;d.finish('✓');
if(d.elapsed()!=='00:52')throw Error('stage active duration changed');
if(d.state.networkMs!==40000)throw Error('network time counted incorrectly');
process.stdout.write(JSON.stringify(output));
''', str(Path(__file__).with_name('stage_display.js')), str(Path(tmp)/'display'), str(tty).lower(),label], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            output=json.loads(result.stdout)
            lines=screen(output) if tty else output.splitlines()
            assert sum(line.startswith(label+' ') for line in lines)==1,lines
            assert 'Network     ✓ 00:18' in lines,lines
            assert 'Network     ✓ 00:22' in lines,lines
            if tty:
                timeline=screen(output,include_blank=True)
                for i,line in enumerate(timeline):
                    if line.startswith('Network '):
                        assert timeline[i-1]=='' and timeline[i+1]=='',timeline
                        assert i<2 or timeline[i-2]!='',timeline
                assert lines[0]==label.ljust(12)+'✓ 00:52',lines
                assert lines.index('    ✓ Found existing evidence') < lines.index('Network     ✓ 00:18') < lines.index('    ▶ Prune retained evidence') < lines.index('Network     ✓ 00:22'),lines
            else:
                assert '\x1b' not in output and '\r' not in output
                assert '\n\nNetwork     ' in output,repr(output)

def test_adjacent_network_spacing_and_clocks():
    module = Path(__file__).with_name("stage_display.js").resolve()
    for tty in ('true','false'):
        with tempfile.TemporaryDirectory() as temporary:
            result=subprocess.run(['node','-e',r'''
    const {StageDisplay}=require(process.argv[1]);
    Object.defineProperty(process.stdout,'isTTY',{value:process.argv[3]==='true'});
    let output='';let now=1000;
    const display=new StageDisplay(process.argv[2],'Review',()=>now,x=>output+=x);
    now+=37000;display.networkStart();now+=18000;display.networkEnd('✓');
    now+=5000;display.networkStart();now+=22000;display.networkEnd('✓');
    now+=10000;display.finish('✓');
    if(display.elapsed()!=='00:52'||display.state.networkMs!==40000)throw Error('clock changed');
    process.stdout.write(JSON.stringify(output));
    ''',str(module),str(Path(temporary)/'display'),tty],capture_output=True,text=True,check=True)
            output=json.loads(result.stdout)
            lines=screen(output,include_blank=True) if tty=='true' else output.splitlines()
            for i,line in enumerate(lines):
                if line.startswith('Network '):
                    assert lines[i-1]=='' and lines[i+1]=='',lines
                    assert i<2 or lines[i-2]!='',lines
            assert sum(line.startswith('Review ') for line in lines)==1,lines
            print('PASS adjacent Network spacing and clocks, tty='+tty)

if __name__=='__main__':
    test_adjacent_network_spacing_and_clocks()
    test_network_clock_and_timeline()
    print('PASS stage active clock excludes distinct chronological Network interruptions')

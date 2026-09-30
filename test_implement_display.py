"""Exercise real Implement rendering, including retry display reconstruction."""
import json
import re
import subprocess
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent

def render(tty, width=80):
    script = r'''
const {StageDisplay} = require(process.argv[1]);
Object.defineProperty(process.stdout, 'isTTY', {value: process.argv[3] === 'true'});
Object.defineProperty(process.stdout, 'columns', {value: Number(process.argv[4])});
let now=100000, output='';
const emit=s=>output+=s, file=process.argv[2];
let d=new StageDisplay(file,'Implement',()=>now,emit);
d.tasks([{content:'Read plans and evidence',status:'in_progress'}]);
d.tasks([{content:'Read plans and evidence',status:'completed'},
         {content:'Inspect rendered hypotheses',status:'in_progress'}]);
now+=1082000;
d.tick();
const active=output;
d.event('↻ network interruption; partial work saved, retry 1/2');
d=new StageDisplay(file,'Implement',()=>now,emit);
d.tasks([{content:'Read plans and evidence',status:'completed'},
         {content:'Inspect rendered hypotheses',status:'completed'},
         {content:'Record RESULT.md evidence',status:'in_progress'}]);
now+=1000;d.tick();
const retry=output;
d.tasks([{content:'Record RESULT.md evidence',status:'completed'}]);
d.finish('↪');
d.finish('↪');
process.stdout.write(JSON.stringify({active,retry,final:output}));
'''
    with tempfile.TemporaryDirectory() as directory:
        r = subprocess.run(['node','-e',script,str(BASE/'stage_display.js'),
                            str(Path(directory)/'display'),str(tty).lower(),str(width)],
                           capture_output=True,text=True,check=True)
        return json.loads(r.stdout)

def screen(output, width=80, include_blank=False):
    """Small terminal interpreter: preserves overwritten rows and cursor movement."""
    rows=[[]]; y=x=0; i=0
    while i<len(output):
        if output[i]=='\x1b':
            m=re.match(r'\x1b\[(\d*)([ABGK])',output[i:])
            assert m,repr(output[i:])
            n=int(m[1] or '1');op=m[2];i+=len(m[0])
            if op=='A':y=max(0,y-n)
            elif op=='B':y+=n
            elif op=='G':x=n-1
            elif op=='K':
                assert n in (0,2)
                rows[y]=[] if n==2 else rows[y][:x]
            continue
        c=output[i];i+=1
        if c=='\r':x=0;continue
        if c=='\n':y+=1;x=0;continue
        if x>=width:y+=1;x=0
        while len(rows)<=y:rows.append([])
        while len(rows[y])<=x:rows[y].append(' ')
        rows[y][x]=c;x+=1
    if include_blank:
        while len(rows)<=y:rows.append([])
    return [''.join(row).rstrip() for row in rows if row or include_blank]

def test_tty_timer_stays_on_single_header_across_events_and_retry():
    output=render(True)
    for key,time in [('active','18:02'),('retry','18:03'),('final','18:03')]:
        lines=screen(output[key])
        assert sum(line.startswith('Implement ') for line in lines)==1,lines
        assert time in lines[0],lines
        assert all(not re.match(r'^\s*(?:[↪✓–]\s*)?\d+:\d+$',s) for s in lines[1:]),lines
        assert '    ✓ Read plans and evidence' in lines,lines
    assert '    ▶ Inspect rendered hypotheses' in screen(output['active'])
    assert '    ✓ Inspect rendered hypotheses' in screen(output['retry'])
    assert '    ▶ Record RESULT.md evidence' in screen(output['retry'])
    assert '    ✓ Record RESULT.md evidence' in screen(output['final'])
    assert output['final'].count('Implement ')==1,output
    for frame in output.values():
        lines=screen(frame)
        for label in ('Read plans and evidence','Inspect rendered hypotheses','Record RESULT.md evidence'):
            assert sum(label in line for line in lines)<=1,lines

def test_non_tty_has_one_final_header_and_ordered_events_without_ansi():
    output=render(False)['final']
    assert '\x1b' not in output and '\r' not in output,repr(output)
    lines=output.splitlines()
    assert lines[0]=='Implement   ↪ 18:03',lines
    assert sum(line.startswith('Implement ') for line in lines)==1,lines
    assert '    ↪ 18:03' not in lines,lines
    assert lines.index('    ✓ Read plans and evidence') < lines.index('    ↻ network interruption; partial work saved, retry 1/2') < lines.index('    ✓ Inspect rendered hypotheses'),lines
    assert lines[-1]=='    ✓ Record RESULT.md evidence',lines

def test_wrapped_event_rows_do_not_move_the_header_update():
    output=render(True,32)
    for phase in ('retry','final'):
        lines=screen(output[phase],32)
        assert lines[0].startswith('Implement   '),lines
        assert '18:03' in lines[0],lines
        assert sum(line.startswith('Implement ') for line in lines)==1,lines
        assert '    ✓ Read plans and evidence' in lines,lines
        assert '    ✓ Record RESULT.md evidence' in screen(output['final'],32)


def test_planned_tty_tasks_keep_their_rows_through_every_state():
    script = r'''
const {StageDisplay} = require(process.argv[1]);
Object.defineProperty(process.stdout, 'isTTY', {value: true});
let now=0, output='';
const emit=s=>output+=s, file=process.argv[2], frames=[];
let d=new StageDisplay(file,'Implement',()=>now,emit);
const tasks=['Inspect existing build','Publish STYLE and Drivers',
             'Wire Lululemon build','Verify and record RESULT']
             .map(content=>({content,status:'pending'}));
d.tasks(tasks);frames.push(output);
for (let i=0;i<tasks.length;i++) {
  if(i) tasks[i-1].status='completed';
  tasks[i].status='in_progress';
  // Input order changes must not reorder already allocated terminal rows.
  d.tasks(i ? [...tasks].reverse() : tasks);
  now+=151000;d.tick();frames.push(output);
  // Retry reconstruction must retain row positions and statuses.
  d=new StageDisplay(file,'Implement',()=>now,emit);
  d.tasks(tasks);frames.push(output);
}
tasks[3].status='completed';d.tasks(tasks);
now=1190000;d.finish('✓');frames.push(output);
process.stdout.write(JSON.stringify(frames));
'''
    with tempfile.TemporaryDirectory() as directory:
        result=subprocess.run(['node','-e',script,str(BASE/'stage_display.js'),
                               str(Path(directory)/'display')],
                              capture_output=True,text=True,check=True)
    frames=[screen(frame) for frame in json.loads(result.stdout)]
    labels=['Inspect existing build','Publish STYLE and Drivers',
            'Wire Lululemon build','Verify and record RESULT']
    assert frames[0]==['Implement   … 00:00']+['    ○ '+label for label in labels],frames[0]
    for i in range(4):
        for lines in frames[1+2*i:3+2*i]:
            expected=['    '+('✓' if j<i else '▶' if j==i else '○')+' '+label
                      for j,label in enumerate(labels)]
            assert lines[1:]==expected,lines
            assert sum('▶' in line for line in lines)==1,lines
            elapsed=151*(i+1)
            assert lines[0]==f'Implement   … {elapsed//60:02}:{elapsed%60:02}',lines
    assert frames[-1]==['Implement   ✓ 19:50']+['    ✓ '+label for label in labels],frames[-1]


def test_tty_cancelled_task_and_failure_header_stay_in_place():
    script = r'''
const {StageDisplay} = require(process.argv[1]);
Object.defineProperty(process.stdout, 'isTTY', {value: true});
let output='';
const d=new StageDisplay(process.argv[2],'Implement',()=>0,s=>output+=s);
d.tasks([{content:'Inspect build',status:'in_progress'},
         {content:'Publish drivers',status:'pending'}]);
d.tasks([{content:'Inspect build',status:'cancelled'}]);
d.finish('✗');
process.stdout.write(JSON.stringify(output));
'''
    with tempfile.TemporaryDirectory() as directory:
        result=subprocess.run(['node','-e',script,str(BASE/'stage_display.js'),
                               str(Path(directory)/'display')],
                              capture_output=True,text=True,check=True)
    assert screen(json.loads(result.stdout))==['Implement   ✗ 00:00',
                                   '    – Inspect build','    ○ Publish drivers']

if __name__=='__main__':
    for name in [n for n in globals() if n.startswith('test_')]:
        globals()[name]();print('PASS '+name)

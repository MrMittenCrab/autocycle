"""Run actual shell controller/engine, sync/checkpoint and local Git; mock providers only."""
from pathlib import Path
import subprocess,tempfile,os,sys,time,json,signal,shutil,sqlite3
BASE=Path(__file__).resolve().parent
REAL_GIT=shutil.which('git')
COMMON=r'''
import json,sys,os,time,subprocess
from pathlib import Path
root=Path(subprocess.check_output([os.environ['REAL_GIT'],'rev-parse','--show-toplevel'],text=True).strip())
a=root/'.git/autocycle';a.mkdir(exist_ok=True)
def git(*args):return subprocess.check_output([os.environ['REAL_GIT'],*args],cwd=root,text=True,stderr=subprocess.PIPE).strip()
def event(kind,prompt=''):
 with (a/'events.jsonl').open('a') as f:f.write(json.dumps({'kind':kind,'prompt':prompt})+'\n')
 if os.environ.get('BLOCK_STAGE')==kind:
  (a/'ready').write_text(kind)
  while not (a/'release').exists():time.sleep(.02)
 if os.environ.get('FAIL_STAGE')==kind:sys.exit(1)
'''
CODEX='#!/usr/bin/env python3\n'+COMMON+r'''
if sys.argv[1:]==['login','status']:
 print('Logged in using ChatGPT');sys.exit(0)
prompt=sys.argv[-1];kind='review' if prompt.startswith('Review HEAD') else 'plan'
event(kind,prompt)
if kind=='review':
 print('REVIEW_STATUS: '+os.environ.get('REVIEW_STATUS','PASS'))
 print('REVIEW: verified')
 print('NEXT_STEP: next bounded task')
 print('REVIEWED_SHA: '+git('rev-parse','HEAD'))
else:
 if os.environ.get('PLAN_BLOCKED')=='1':print('PLAN_STATUS: BLOCKED');sys.exit(0)
 count=sum(json.loads(x)['kind']=='plan' for x in (a/'events.jsonl').read_text().splitlines())
 print('BEGIN_IMPLEMENTATION_MD')
 print('# Step Q'+str(count)+' — deterministic test plan\n\n### Task 1: Verify work\nRecord measured results in RESULT.md.')
 print('END_IMPLEMENTATION_MD')
'''
AGENT='#!/usr/bin/env python3\n'+COMMON+r'''
for line in sys.stdin:
 msg=json.loads(line)
 if 'method' not in msg:continue
 result={}
 if msg['method']=='session/new':result={'sessionId':'test'}
 if msg['method']=='session/prompt':
  event('implement',msg['params']['prompt'][0]['text'])
  count=sum(json.loads(x)['kind']=='implement' for x in (a/'events.jsonl').read_text().splitlines())
  if os.environ.get('NO_CHANGE')!='1':(root/'RESULT.md').write_text('Measured successful run '+str(count))
  if os.environ.get('EDIT_PLAN')=='1':(root/'IMPLEMENTATION.md').write_text('forbidden')
  statuses=['completed','cancelled']
  if os.environ.get('TODO_PENDING')=='1':statuses=['completed','pending']
  print(json.dumps({'jsonrpc':'2.0','method':'cursor/update_todos','params':{'todos':[{'id':str(i),'content':'Task '+str(i),'status':s} for i,s in enumerate(statuses)],'merge':False}}),flush=True)
  result={'stopReason':'end_turn'}
 print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)
'''
GIT='#!/usr/bin/env python3\n'+r'''
import os,sys,subprocess
args=sys.argv[1:]
fail=os.environ.get('GIT_FAULT')
is_plan_push='push' in args and any(x.startswith('HEAD:refs/heads/') for x in args)
is_sync='merge' in args and '--ff-only' in args
is_checkpoint_push='push' in args and '-u' in args
if fail=='checkpoint_before_push' and is_checkpoint_push:sys.exit(1)
r=subprocess.run([os.environ['REAL_GIT'],*args])
if r.returncode==0 and ((fail=='plan_after_push' and is_plan_push) or (fail=='sync_after_merge' and is_sync) or (fail=='checkpoint_after_push' and is_checkpoint_push)):sys.exit(1)
sys.exit(r.returncode)
'''

class Case:
 def __init__(self,initial='checkpoint/test'):
  self.temp=tempfile.TemporaryDirectory();self.p=Path(self.temp.name);self.repo=self.p/'repo';self.repo.mkdir();self.bin=self.p/'bin';self.bin.mkdir()
  self.git('init','-q','-b',initial);self.git('config','user.name','Test');self.git('config','user.email','test@example.com')
  (self.repo/'TARGET.md').write_text('target');(self.repo/'IMPLEMENTATION.md').write_text('# Baseline');self.git('add','.');self.git('commit','-qm','baseline')
  subprocess.run([REAL_GIT,'init','--bare','-q',str(self.p/'origin')],check=True)
  self.git('remote','add','origin',str(self.p/'origin'));self.git('push','-qu','origin',initial)
  self.a=self.repo/'.git/autocycle';self.a.mkdir()
  helper=self.p/'instructions.py';helper.write_bytes((BASE/'instructions.py').read_bytes())
  for name in ('autocycle','stage'):
   text=(BASE/name).read_text().replace('QUEUE_HELPER="$HOME/.autocycle/instructions.py"','QUEUE_HELPER='+str(helper)).replace('ENGINE="$HOME/.autocycle/stage"','ENGINE='+str(self.bin/'stage'))
   if name=='autocycle':
    # Provider network is isolated; all actual Git commands still use local origin.
    text=text.replace('if [[ "$MODE" != "--dry-run" ]]; then\n    pause_if_requested','network_ready() { return 0; }\nensure_network() { return 0; }\nif [[ "$MODE" != "--dry-run" ]]; then\n    pause_if_requested')
   (self.bin/name).write_text(text);(self.bin/name).chmod(0o755)
  for name in ('sync','checkpoint'):(self.bin/name).write_bytes((BASE/name).read_bytes());(self.bin/name).chmod(0o755)
  for name,text in [('codex',CODEX),('agent',AGENT),('git',GIT)]:
   (self.bin/name).write_text(text);(self.bin/name).chmod(0o755)
  self.env=dict(os.environ,REAL_GIT=REAL_GIT,PATH=str(self.bin)+os.pathsep+os.environ['PATH'])
 def git(self,*args):return subprocess.check_output([REAL_GIT,*args],cwd=self.repo,text=True,stderr=subprocess.PIPE).strip()
 def run(self,*args,**env):return subprocess.run([str(self.bin/'autocycle'),*args],cwd=self.repo,env=dict(self.env,**env),capture_output=True,text=True,timeout=25)
 def start(self,*args,**env):return subprocess.Popen([str(self.bin/'autocycle'),*args],cwd=self.repo,env=dict(self.env,**env),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
 def ready(self,p):
  deadline=time.monotonic()+12
  while not (self.a/'ready').exists() and p.poll() is None and time.monotonic()<deadline:time.sleep(.02)
  if not (self.a/'ready').exists():raise AssertionError(p.communicate(timeout=3))
 def kill(self,p):
  os.killpg(p.pid,signal.SIGTERM);p.communicate(timeout=5)
  (self.a/'ready').unlink(missing_ok=True);(self.a/'release').unlink(missing_ok=True)
 def enqueue(self,text):
  r=self.run('--instruct',text);assert r.returncode==0,(r.stdout,r.stderr);return r.stdout.split()[-1]
 def events(self):return [json.loads(x) for x in (self.a/'events.jsonl').read_text().splitlines()]
 def rows(self):
  c=sqlite3.connect(self.a/'instructions.sqlite3');c.row_factory=sqlite3.Row
  rows=[dict(x) for x in c.execute('SELECT * FROM instructions ORDER BY seq')];c.close();return rows
 def close(self):self.temp.cleanup()

def ok(r):assert r.returncode==0,(r.stdout,r.stderr)
def fail(r):assert r.returncode!=0,(r.stdout,r.stderr)

def main():
 c=Case();p=c.start('2',BLOCK_STAGE='implement');c.ready(p)
 ident=c.enqueue('SENTINEL queued midcycle')
 assert c.git('status','--porcelain')==''
 # A second controller is rejected, but queue/list/stop remain usable.
 fail(c.run('2'));ok(c.run('--instructions'))
 (c.a/'release').touch();out,err=p.communicate(timeout=25);assert p.returncode==0,(out,err)
 rev=[e for e in c.events() if e['kind']=='review'];plans=[e for e in c.events() if e['kind']=='plan']
 assert len(rev)==len(plans)==2
 assert 'SENTINEL' not in rev[0]['prompt']+plans[0]['prompt']
 assert 'SENTINEL' in rev[1]['prompt'] and 'SENTINEL' in plans[1]['prompt']
 assert c.rows()[0]['state']=='archived';assert c.git('status','--porcelain')==''
 print('PASS concurrent enqueue, current-cycle isolation, next-cycle pickup, single controller');c.close()

 for stage in ('review','plan','implement'):
  c=Case();c.enqueue('FIRST frozen');p=c.start('1',BLOCK_STAGE=stage);c.ready(p)
  batch=c.rows()[0]['batch'];c.enqueue('LATER pending');c.kill(p)
  r=c.run('1');ok(r)
  rows=c.rows();assert rows[0]['state']=='archived' and rows[0]['batch']==batch and rows[1]['state']=='pending'
  assert all('LATER' not in e['prompt'] for e in c.events() if e['kind'] in ('review','plan'))
  print('PASS interruption/resume during '+stage+' keeps the same batch');c.close()

 for fault in ('plan_after_push','sync_after_merge','checkpoint_before_push','checkpoint_after_push'):
  c=Case();c.enqueue('one input');r=c.run('1',GIT_FAULT=fault);fail(r)
  assert c.rows()[0]['state']=='active'
  r=c.run('1');ok(r)
  assert len([e for e in c.events() if e['kind']=='plan'])==1
  assert c.rows()[0]['state']=='archived'
  # Restarting after completion cannot reapply the old instruction.
  r=c.run('1',REVIEW_STATUS='DONE');ok(r)
  assert len([e for e in c.events() if e['kind']=='plan'])==1
  print('PASS '+fault+' recovery, no duplicate plan or instruction');c.close()

 c=Case();ids=[c.enqueue(t) for t in ('ORDER_A','CANCEL_ME','ORDER_B')]
 ok(c.run('--cancel-instruction',ids[1]));r=c.run('1');ok(r)
 prompt=next(e['prompt'] for e in c.events() if e['kind']=='plan')
 assert prompt.index('ORDER_A')<prompt.index('ORDER_B') and 'CANCEL_ME' not in prompt
 assert [r['state'] for r in c.rows()]==['archived','cancelled','archived']
 print('PASS ordered batch and pending cancellation');c.close()

 c=Case();ident=c.enqueue('immutable');p=c.start('1',BLOCK_STAGE='review');c.ready(p)
 fail(c.run('--cancel-instruction',ident));ok(c.run('--stop'));(c.a/'release').touch();out,err=p.communicate(timeout=15);assert p.returncode==0,(out,err)
 assert 'STAGE=review_done' in (c.a/'resume-state').read_text();ok(c.run('1'));assert c.rows()[0]['state']=='archived'
 print('PASS active cancellation rejected; clean stop/resume retains batch');c.close()

 c=Case();c.enqueue('safe ownership');fail(c.run('1',EDIT_PLAN='1'))
 assert c.rows()[0]['state']=='active' and 'Plan: ' in c.git('log','-1','--format=%s')
 assert c.git('status','--porcelain')
 print('PASS Cursor planner-owned edit rejected before checkpoint');c.close()

 c=Case();c.enqueue('cancelled task is optional');ok(c.run('1'));assert c.rows()[0]['state']=='archived'
 print('PASS completed plus cancelled Cursor todos succeed');c.close()
 c=Case();c.enqueue('unfinished task');fail(c.run('1',TODO_PENDING='1'));assert c.rows()[0]['state']=='active'
 print('PASS unfinished Cursor todo fails without consuming input');c.close()

 for control in ({'REVIEW_STATUS':'BLOCKED'},{'PLAN_BLOCKED':'1'},{'REVIEW_STATUS':'DONE'}):
  c=Case();c.enqueue('conflict A');c.enqueue('conflict B');fail(c.run('1',**control))
  assert all(r['state']=='active' for r in c.rows())
  assert c.git('log','-1','--format=%s')=='baseline'
  print('PASS conflicting/blocked or premature DONE input retained:',control);c.close()

 c=Case();ok(c.run('1',NO_CHANGE='1'));assert c.git('status','--porcelain')==''
 assert not (c.a/'resume-state').exists();assert c.rows()==[]
 print('PASS empty queue, autonomous no-change run');c.close()

 c=Case();c.enqueue('human no-change verification');ok(c.run('1',NO_CHANGE='1'));assert c.rows()[0]['state']=='archived'
 print('PASS instructed no-change completion archives only after verified publication');c.close()

if __name__=='__main__':main()

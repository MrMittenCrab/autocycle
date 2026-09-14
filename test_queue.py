import importlib.util,os,json,subprocess,tempfile,uuid,sqlite3,sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('queue_impl',BASE/'instructions.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as d:
 q=m.Queue(d)
 first=q.enqueue('first');second=q.enqueue('second');cancel=q.enqueue('cancel')
 q.cancel(cancel);bid=uuid.uuid4().hex
 assert q.freeze(bid,'test','a'*40,True)==2
 assert [i['id'] for i in q.items(bid)]==[first,second]
 late=q.enqueue('late')
 assert q.freeze(bid,'test','a'*40,True)==2
 assert [r['id'] for r in q.listing() if r['state']=='pending']==[late]
 try:q.cancel(first)
 except RuntimeError:pass
 else:raise AssertionError('active input cancelled')
 print('PASS freeze/order/cancel/late-arrival/restart')
 q.db.close()
with tempfile.TemporaryDirectory() as d:
 q=m.Queue(d);bid=uuid.uuid4().hex
 assert q.freeze(bid,'test','a'*40,False)==0
 ident=q.enqueue('later');assert q.freeze(bid,'test','a'*40,True)==0
 assert q.listing()[0]['state']=='pending'
 print('PASS explicit empty batch remains empty on restart');q.db.close()
with tempfile.TemporaryDirectory() as d:
 q=m.Queue(d);q.enqueue('one');q.enqueue('two')
 q.db.execute("CREATE TRIGGER fail_freeze BEFORE UPDATE ON instructions BEGIN SELECT RAISE(ABORT,'simulated interruption'); END")
 bid=uuid.uuid4().hex
 try:q.freeze(bid,'test','a'*40,True)
 except sqlite3.IntegrityError:pass
 else:raise AssertionError('fault not triggered')
 assert not q.db.execute('SELECT * FROM batches').fetchall()
 assert [r['state'] for r in q.listing()]==['pending','pending']
 q.db.execute('DROP TRIGGER fail_freeze');assert q.freeze(bid,'test','a'*40,True)==2
 print('PASS atomic promotion rollback and retry');q.db.close()
with tempfile.TemporaryDirectory() as d:
 procs=[subprocess.Popen([sys.executable,str(BASE/'instructions.py'),'--dir',d,'--','--instruct',f'concurrent {i}'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(20)]
 ids=[]
 for p in procs:
  out,err=p.communicate(timeout=40);assert p.returncode==0,(out,err);ids.append(out.strip().split()[-1])
 assert len(set(ids))==20
 q=m.Queue(d);rows=q.listing();assert len(rows)==20
 assert [r['seq'] for r in rows]==list(range(1,21))
 bid=uuid.uuid4().hex;q.freeze(bid,'test','a'*40,True)
 assert [r['id'] for r in q.items(bid)]==[r['id'] for r in rows]
 print('PASS 20 simultaneous submitters, unique ordered IDs');q.db.close()

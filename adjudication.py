#!/usr/bin/env python3
"""Local-only blocker evidence and policy identity; never edits project files."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys

POLICY = 'five-stage-v2'

def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE)

def digest(data):
    return hashlib.sha256(data).hexdigest()

def location():
    return Path(os.fsdecode(git('rev-parse', '--git-path', 'autocycle')).strip()).resolve()

def read(path):
    return path.read_bytes() if path.is_file() else b''

def atomic(path, data):
    tmp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    with tmp.open('w') as f:
        json.dump(data, f, sort_keys=True)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def files(code_only=False):
    result = {}
    paths = set(git('ls-files', '-z').split(b'\0') + git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')) - {b''}
    for raw in sorted(paths):
        name = os.fsdecode(raw)
        if code_only and name in ('IMPLEMENTATION.md', 'RESULT.md'):
            continue
        p = Path(name)
        if p.is_symlink(): value = 'link:' + os.readlink(p)
        elif p.is_file(): value = str(p.stat().st_mode & 0o777) + ':' + digest(p.read_bytes())
        elif p.is_dir(): value = 'directory:' + git('status','--porcelain','--',name).decode()
        else: value = 'missing'
        result[name] = value
    return result

def input_context(batch):
    if not batch:
        return {'batch':'', 'count':0, 'allowed':['NONE'], 'items':[]}
    db=sqlite3.connect('file:'+str(location()/'instructions.sqlite3')+'?mode=ro',uri=True)
    try:
        b=db.execute('SELECT branch,base_sha FROM batches WHERE id=?',(batch,)).fetchone()
        if not b:raise ValueError('review instruction batch is missing')
        if b[0]!=git('branch','--show-current').decode().strip():raise ValueError('instruction branch mismatch')
        subprocess.run(['git','merge-base','--is-ancestor',b[1],'HEAD'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        has_members=db.execute("SELECT 1 FROM sqlite_master WHERE name='batch_members'").fetchone()
        query=('SELECT i.seq,i.id,i.text FROM instructions i JOIN batch_members m ON m.seq=i.seq WHERE m.batch=? ORDER BY i.seq'
               if has_members else 'SELECT seq,id,text FROM instructions WHERE batch=? ORDER BY seq')
        items=db.execute(query,(batch,)).fetchall()
        return {'batch':batch,'count':len(items),'allowed':['COMPLETE','PENDING'] if items else ['NONE'],'items':items}
    finally:db.close()

def identity(batch):
    ac = location()
    evidence = {}
    for name in ('candidate.json', 'latest-implementation', 'implementation-result.json'):
        evidence[name] = digest(read(ac/name))
    instructions=input_context(batch)['items']
    latest = read(ac/'latest-implementation').decode(errors='replace')
    for line in latest.splitlines():
        if line.startswith('LOG: '): evidence['implementation_log'] = digest(read(Path(line[5:])))
    return digest(json.dumps({'policy': POLICY, 'cycle': os.environ.get('AUTOCYCLE_REVIEW_CYCLE','manual'), 'branch': git('branch','--show-current').decode().strip(),
        'head': git('rev-parse','HEAD').decode().strip(), 'files': files(),
        'index': digest(git('ls-files','--stage','-z')), 'batch': batch, 'instructions': instructions, 'evidence': evidence}, sort_keys=True).encode())

def fields(path):
    result = {}
    for line in path.read_text().splitlines():
        if ': ' in line:
            key, value = line.split(': ',1)
            result[key] = value
    return result

def valid(path, batch):
    try:
        f = fields(path)
        lines=path.read_text().splitlines()
        if any(sum(line.startswith(k+': ') for line in lines)!=1 for k in f):return False
        return (f.get('REVIEW_POLICY') == POLICY and f.get('REVIEW_IDENTITY') == identity(batch)
            and f.get('CACHE_BRANCH') == git('branch','--show-current').decode().strip()
            and f.get('REVIEWED_SHA') == git('rev-parse','HEAD').decode().strip()
            and f.get('INPUT_BATCH','') == batch
            and f.get('REVIEW_STATUS') in ('PASS','PROBLEMS','DONE','BLOCKED')
            and bool(f.get('REVIEW','').strip()) and bool(f.get('NEXT_STEP','').strip())
            and f.get('INPUT_STATUS') in input_context(batch)['allowed']
            and (f.get('REVIEW_STATUS') != 'DONE' or f.get('INPUT_STATUS') != 'PENDING')
            and bool(f.get('BLOCKER_KEY','').strip()) and bool(f.get('HUMAN_ACTION','').strip())
            and (f.get('REVIEW_STATUS') != 'BLOCKED' or f.get('HUMAN_ACTION') != 'NONE'))
    except (OSError,ValueError,sqlite3.Error,subprocess.SubprocessError): return False

def main():
    cmd, *args = sys.argv[1:]
    ac = location();ac.mkdir(exist_ok=True)
    if cmd == 'run':
        timeout=int(args[0])
        if timeout<1: raise ValueError('provider timeout must be positive')
        proc=subprocess.Popen(args[1:],start_new_session=True)
        def stop(signum=None, frame=None):
            if proc.poll() is None:
                os.killpg(proc.pid,signal.SIGTERM)
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            if signum is not None: sys.exit(128+signum)
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        try: code=proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop();print('Provider timed out; evidence preserved. Restore provider availability and resume.',file=sys.stderr);code=124
        sys.exit(code)
    elif cmd == 'context':
        context=input_context(args[0] if args else '')
        context['digest']=digest(json.dumps(context.pop('items'),sort_keys=True).encode())
        print(json.dumps(context,sort_keys=True))
    elif cmd == 'candidate-key': print(digest(read(ac/'candidate.json')))
    elif cmd == 'identity': print(identity(args[0] if args else ''))
    elif cmd == 'valid': sys.exit(0 if valid(Path(args[0]),args[1]) else 1)
    elif cmd == 'baseline':
        p=ac/'implementation-baseline.json'
        if not p.exists():
            atomic(p, {'head':git('rev-parse','HEAD').decode().strip(),
                'branch':git('branch','--show-current').decode().strip(),
                'clean':not bool(git('status','--porcelain')), 'files':files()})
        elif git('status','--porcelain'):
            b=json.loads(p.read_text());b['clean']=False;atomic(p,b)
    elif cmd == 'implementation-result':
        atomic(ac/'implementation-result.json',{'head':git('rev-parse','HEAD').decode().strip(),
            'branch':git('branch','--show-current').decode().strip(),'files':files()})
    elif cmd == 'checkpoint-safe':
        b=json.loads((ac/'implementation-baseline.json').read_text())
        # A dirty baseline could contain manual edits; never sweep them into add -A.
        ok=(b['clean'] and b['head']==git('rev-parse','HEAD').decode().strip()
            and b['branch']==git('branch','--show-current').decode().strip()
            and b['branch'].startswith('checkpoint/')
            and not git('ls-files','-u') and not git('diff','--name-only','--diff-filter=U'))
        # Refuse to sweep edits that arrived after the provider saved its result.
        result_path=ac/'candidate.json' if (ac/'candidate.json').exists() else ac/'implementation-result.json'
        if result_path.exists():
            result=json.loads(result_path.read_text())
            ok=ok and result['head']==b['head'] and result['branch']==b['branch'] and result['files']==files()
        else:ok=False
        # Submodules and protected documents require separate human handling.
        for name in ('TARGET.md','IMPLEMENTATION.md'):
            ok = ok and files().get(name)==b['files'].get(name)
        ok = ok and all(not Path(p).is_dir() for p in files())
        sys.exit(0 if ok else 1)
    elif cmd == 'candidate':
        kind, log, batch = args
        path=Path(log)
        body=read(path).decode(errors='replace')
        record={'policy':POLICY,'kind':kind,'branch':git('branch','--show-current').decode().strip(),
            'head':git('rev-parse','HEAD').decode().strip(),'batch':batch,'log':str(path),
            'log_digest':digest(read(path)),'report':body,'files':files(),
            'diff':git('diff','HEAD','--binary').decode(errors='replace')}
        # Keep an immutable copy for subsequent diagnoses, even when a newer candidate replaces the pointer.
        atomic(ac/('candidate-'+digest(json.dumps(record,sort_keys=True).encode())+'.json'),record)
        atomic(ac/'candidate.json',record)
    elif cmd == 'count':
        cache=Path(args[0]); limit=int(args[1])
        if limit < 1: raise ValueError('no-progress limit must be positive')
        f=fields(cache);p=ac/'blocker-progress.json'
        old=json.loads(p.read_text()) if p.exists() else {}
        # HEAD and generated plans/results are deliberately absent: plan churn is not progress.
        key=re.sub(r'\s+',' ',f.get('BLOCKER_KEY','').lower()).strip()
        if not key: raise ValueError('adjudication missing BLOCKER_KEY')
        code=digest(json.dumps(files(True),sort_keys=True).encode())
        branch=git('branch','--show-current').decode().strip()
        scope=[branch,digest(json.dumps(input_context(os.environ.get('AUTOCYCLE_INPUT_BATCH',''))['items'],sort_keys=True).encode())]
        decision=digest(read(cache))
        if old.get('decision') != decision:
            n=old.get('count',0)+1 if old.get('key')==key and old.get('code')==code and old.get('scope')==scope else 1
            old={'key':key,'code':code,'scope':scope,'count':n,'decision':decision}
            atomic(p,old)
        print(old['count'])
        sys.exit(3 if old['count']>=limit else 0)
    elif cmd == 'numbering':
        # Inspect all reachable plan contents, not just titles or a truncated history.
        ids=set()
        for rev in git('rev-list','--all','--','IMPLEMENTATION.md').decode().splitlines():
            blob=subprocess.run(['git','show',rev+':IMPLEMENTATION.md'],capture_output=True)
            ids.update(re.findall(r'\b\d+[A-Z]+(?:\.\d+[A-Z]*)*\b',blob.stdout.decode(errors='replace')))
        ids.update(re.findall(r'\b\d+[A-Z]+(?:\.\d+[A-Z]*)*\b',read(Path('IMPLEMENTATION.md')).decode(errors='replace')))
        print('Previously used IDs (do not reuse for new work): '+', '.join(sorted(ids)))
        title=read(Path('IMPLEMENTATION.md')).decode(errors='replace').splitlines()
        match=re.search(r'\b\d+[A-Z]+(?:\.\d+[A-Z]*)*\b',title[0] if title else '')
        if match:
            parent=match.group(); n=1
            while parent+'.'+str(n) in ids:n+=1
            print('First unused child of '+parent+': '+parent+'.'+str(n))
    else: raise ValueError('unknown adjudication operation')

if __name__ == '__main__':
    try: main()
    except Exception as e:
        print('Adjudication: '+str(e),file=sys.stderr);sys.exit(1)

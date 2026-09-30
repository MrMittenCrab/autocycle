#!/usr/bin/env python3
"""Local-only blocker evidence and policy identity; never edits project files."""
import hashlib
import contextlib
import datetime
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import signal
import shlex
import stat
import sqlite3
import subprocess
import sys

POLICY = 'five-component-v14'
import progress

def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE,
                                   env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})

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
    # Keep deletion tombstones after checkpoint stages them. ls-files alone
    # drops staged deletions, changing the saved work fingerprint under add -A.
    paths = set(git('ls-tree', '-r', '-z', '--name-only', 'HEAD').split(b'\0')
                + git('ls-files', '-z').split(b'\0')
                + git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')) - {b''}
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
        return {'batch':'', 'count':0, 'allowed':['NONE'], 'items':[], 'awaiting_ids':[]}
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
        return {'batch':batch,'count':len(items),'allowed':['COMPLETE','PENDING'] if items else ['NONE'],'items':items,
                'awaiting_ids':[row[0] for row in db.execute("SELECT id FROM instructions WHERE batch=? AND state='active'",(batch,))]}
    finally:db.close()

def identity(batch):
    ac = location()
    with progress.state() as work_state:
        work_binding = progress.binding(work_state)
    evidence = {'work_binding': digest(progress.encoded(work_binding).encode())}
    for name in ('candidate.json', 'latest-implementation', 'implementation-result.json', 'remote-docs-current.json', 'implementation-baseline.json'):
        evidence[name] = digest(read(ac/name))
    # Only baseline fields affect historical Review evidence; stage/cycle writes
    # must not invalidate a Review between publication and controller admission.
    baseline_fields = {}
    for line in read(ac/'resume-state').decode().splitlines():
        key, sep, value = line.partition('=')
        if key in ('IMPLEMENT_BASE_SHA', 'PLAN_SHA'):
            values = shlex.split(value)
            if not sep or len(values) > 1 or key in baseline_fields:
                raise ValueError('invalid saved implementation baseline')
            baseline_fields[key] = values[0] if values else ''
    # Before Plan the controller clears IMPLEMENT_BASE_SHA after ownership
    # checks. The immutable normal baseline still identifies that same attempt.
    # Bind the effective historical reference, not its temporary storage slot.
    baseline_record = read(ac/'implementation-baseline.json')
    baseline_head = json.loads(baseline_record)['head'] if baseline_record else ''
    evidence['implementation_base'] = (baseline_fields.get('IMPLEMENT_BASE_SHA')
        or baseline_fields.get('PLAN_SHA') or baseline_head
        or (work_binding.get('attempt') or {}).get('plan_sha', ''))
    instructions=input_context(batch)['items']
    latest = read(ac/'latest-implementation').decode(errors='replace')
    for line in latest.splitlines():
        if line.startswith('LOG: '): evidence['implementation_log'] = digest(read(Path(line[5:])))
    return digest(json.dumps({'policy': POLICY, 'cycle': os.environ.get('AUTOCYCLE_REVIEW_CYCLE','manual'), 'new_session': os.environ.get('AUTOCYCLE_NEW_SESSION','0'), 'session_number': os.environ.get('AUTOCYCLE_SESSION_NUMBER','1'), 'branch': git('branch','--show-current').decode().strip(),
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
        with progress.state() as work_state:
            progress.review_report(work_state, path.read_text(), batch)
        if not f.get('REVIEW_TOKEN'): return False
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

def interruption_snapshot():
    ac = location()
    return {'head':git('rev-parse','HEAD').decode().strip(),
            'branch':git('branch','--show-current').decode().strip(),
            'baseline':digest(read(ac/'implementation-baseline.json')),
            'files':files(), 'index':digest(git('ls-files','--stage','-z'))}

def preserve_interruption(log):
    ac = location()
    b=json.loads((ac/'implementation-baseline.json').read_text())
    snapshot=interruption_snapshot()
    if (not b['clean'] or snapshot['head']!=b['head'] or snapshot['branch']!=b['branch']
            or git('ls-files','-u')
            or any(snapshot['files'].get(n)!=b['files'].get(n) for n in ('TARGET.md','SESSION.md','IMPLEMENTATION.md'))
            or any(Path(n).is_dir() for n in snapshot['files'])):
        raise ValueError('transport interruption changed ownership boundaries; preserve work for reconciliation')
    record={'snapshot':snapshot,'log':str(Path(log).resolve()),'log_digest':digest(read(Path(log)))}
    atomic(ac/('interruption-'+digest(json.dumps(record,sort_keys=True).encode())+'.json'),record)
    atomic(ac/'implementation-interruption.json',record)


def adoption_snapshot():
    """Read exact recovery boundaries without initializing/migrating any state."""
    require = progress.require
    require(not any(os.environ.get(k) for k in ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE',
                'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')),
            'adoption requires the normal repository and index, without Git overrides')
    root = Path(os.fsdecode(git('rev-parse', '--show-toplevel')).strip()).resolve()
    require(Path.cwd().resolve() == root, 'run recovery from the repository root')
    ac = location()
    gitdir = Path(os.fsdecode(git('rev-parse', '--absolute-git-dir')).strip()).resolve()
    def identity(path):
        info = path.stat()
        return [str(path), info.st_dev, info.st_ino]
    session = {}
    raw_session = (ac/'resume-state').read_bytes()
    for line in raw_session.decode().splitlines():
        key, sep, value = line.partition('=')
        require(sep and key not in session, 'invalid or duplicate saved Session field')
        values = shlex.split(value)
        require(len(values) <= 1, 'invalid saved Session value')
        session[key] = values[0] if values else ''
    require(session.get('STATE_VERSION') == '2' and session.get('STAGE') == 'implementing'
            and session.get('INPUT_CYCLE_ID')
            and re.fullmatch(r'[1-9][0-9]*', session.get('SESSION_NUMBER', '')),
            'adoption requires a saved implementing Session')
    head = git('rev-parse', 'HEAD').decode().strip()
    branch = git('branch', '--show-current').decode().strip()
    require(branch.startswith('checkpoint/') and session.get('STATE_BRANCH') == branch
            and session.get('PLAN_SHA') == session.get('IMPLEMENT_BASE_SHA') == head,
            'repository HEAD/branch and saved implementation do not match')
    baseline_raw = (ac/'implementation-baseline.json').read_bytes()
    baseline = json.loads(baseline_raw)
    require(baseline.get('clean') is True and baseline.get('head') == head
            and baseline.get('branch') == branch, 'original clean baseline does not match')
    ledger_raw = (ac/'work-state.json').read_bytes()
    ledger = json.loads(ledger_raw)
    require(ledger.get('version') == progress.VERSION, 'unsupported work state')
    state = ledger['branches'][branch]
    work, attempt = state['work'], progress.latest(state)
    require(work and not work.get('complete') and not state.get('block') and attempt
            and attempt['work_id'] == work['id'] and attempt['kind'] == 'implementation'
            and attempt['phase'] == 'running' and attempt.get('outcome') is None
            and attempt.get('checkpoint_sha') is None and attempt['plan_sha'] == head,
            'current work/implementation identity is not an interrupted running attempt')
    index = git('ls-files', '--stage', '-z')
    require(not git('ls-files', '-u') and not any(row.startswith(b'160000 ')
            for row in index.split(b'\0')), 'unmerged index or submodules require separate recovery')
    for name in ('TARGET.md', 'SESSION.md', 'IMPLEMENTATION.md'):
        tree = git('ls-tree', '-z', head, '--', name)
        staged = git('ls-files', '--stage', '-z', '--', name)
        if tree:
            mode, kind, oid = tree.split(b'\t', 1)[0].split()
            require(kind == b'blob' and mode in (b'100644', b'100755')
                    and staged == mode+b' '+oid+b' 0\t'+name.encode()+b'\0',
                    'protected document changed in the index: '+name)
        else:
            require(not staged, 'protected document added to the index: '+name)
    tracked = files()
    require(all(tracked.get(n) == baseline['files'].get(n)
                for n in ('TARGET.md', 'SESSION.md', 'IMPLEMENTATION.md')),
            'protected Session/plan documents changed since the original baseline')
    # Include ignored files, empty directories, symlinks and HEAD/index deletion
    # tombstones. Do not follow directory symlinks or touch Git administration.
    worktree = dict(tracked)
    for directory, dirs, names in os.walk(root, onerror=lambda error: (_ for _ in ()).throw(error)):
        if Path(directory) == root:
            dirs[:] = [n for n in dirs if n != '.git']
            names = [n for n in names if n != '.git']
        for name in dirs + names:
            path = Path(directory)/name
            info = path.lstat()
            key = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode): value = 'link:'+os.readlink(path)
            elif stat.S_ISREG(info.st_mode): value = str(stat.S_IMODE(info.st_mode))+':'+digest(path.read_bytes())
            elif stat.S_ISDIR(info.st_mode): value = 'directory:'+str(stat.S_IMODE(info.st_mode))
            else: raise ValueError('unsupported worktree file type: '+key)
            after = path.lstat()
            require(all(getattr(after, field) == getattr(info, field) for field in
                        ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')),
                    'file changed while fingerprinting: '+key)
            worktree[key] = value
    return {'repository': identity(root), 'git_directory': identity(gitdir),
            'head': head, 'branch': branch, 'session': session,
            'session_digest': digest(raw_session), 'baseline': digest(baseline_raw),
            'work_binding': progress.binding(state), 'work_state_digest': digest(ledger_raw),
            'index': digest(index), 'index_flags': digest(git('ls-files', '-v', '-z')),
            'worktree': worktree, 'evidence': {n: digest(read(ac/n)) for n in
                ('candidate.json', 'implementation-result.json', 'latest-implementation',
                 'remote-docs-current.json', 'implementation-interruption.json')}}


@contextlib.contextmanager
def recovery_lock():
    """Use the controller's own lock, then the progress writer's lock."""
    ac = location()
    with (ac/'controller.lock').open('a+b') as controller, (ac/'work-state.lock').open('a+b') as work:
        for lock in (controller, work):
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('controller or progress writer is active; recovery refused') from None
        yield


def adoption_archive(ac, record):
    return ac/('human-adoption-'+digest(json.dumps(record, sort_keys=True).encode())+'.json')


def verify_adoption():
    """Read-only validation, also used immediately before single-use consumption."""
    ac = location()
    record = json.loads((ac/'human-adoption.json').read_text())
    progress.require(record.get('version') == 1
                     and record['authority']['source'] == 'explicit-human-authorization'
                     and record['authority']['attestation'].strip(), 'invalid human authorization')
    progress.require(json.loads(adoption_archive(ac, record).read_text()) == record,
                     'human adoption archive does not match the active receipt')
    index_path = Path(os.fsdecode(git('rev-parse', '--git-path', 'index')).strip()).resolve()
    progress.require(digest(index_path.read_bytes()) == record['original_index_bytes_digest'],
                     'index changed since human adoption; work preserved for reconciliation')
    progress.require(record['snapshot'] == adoption_snapshot(),
                     'state changed since human adoption; work preserved for reconciliation')
    return record


def adopt_current_worktree(attestation):
    """No authorship inference: only an explicit caller attestation can enter here."""
    progress.require(isinstance(attestation, str) and attestation.strip(), 'human attestation is required')
    ac = location()
    with recovery_lock():
        receipt = ac/'human-adoption.json'
        progress.require(not receipt.exists() and not (ac/'implementation-interruption.json').exists(),
                         'an ownership receipt already exists; refusing to replace it')
        index_path = Path(os.fsdecode(git('rev-parse', '--git-path', 'index')).strip()).resolve()
        index_bytes = index_path.read_bytes()
        snapshot = adoption_snapshot()
        record = {'version': 1, 'authority': {'source': 'explicit-human-authorization',
                  'attestation': attestation, 'user': getpass.getuser(),
                  'time': datetime.datetime.now(datetime.timezone.utc).isoformat()},
                  'snapshot': snapshot, 'original_index_bytes_digest': digest(index_bytes)}
        def unchanged():
            progress.require(snapshot == adoption_snapshot() and index_path.read_bytes() == index_bytes,
                             'state changed during human adoption; recovery refused')
        unchanged()
        # Archive first, publish last. An archive alone never grants authority.
        atomic(adoption_archive(ac, record), record)
        unchanged()
        published = False
        try:
            atomic(receipt, record)
            published = True
            unchanged()
            verify_adoption()
            fd = os.open(ac, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        except BaseException:
            if published: receipt.unlink(missing_ok=True)
            raise
        return record


def native_identity():
    with progress.state() as s:
        a = progress.latest(s)
        progress.require(a and a['kind']=='implementation', 'native observation needs implementation attempt')
        return dict(repository=git('rev-parse','--show-toplevel').decode().strip(),
            branch=git('branch','--show-current').decode().strip(),
            binding=progress.binding(s), kind=a['kind'],
            session_number=int(os.environ.get('AUTOCYCLE_SESSION_NUMBER','1')),
            session_sha256=digest(read(Path('SESSION.md'))),
            baseline_sha256=digest(read(location()/'implementation-baseline.json')))


def native_snapshot():
    identity=native_identity()
    progress.require(identity['binding']['attempt']['phase']=='running', 'native attempt is not running')
    return dict(identity=identity, head=git('rev-parse','HEAD').decode().strip(),
                existing=[p.stem for p in (location()/'office/requests').glob('*.json')])


def observation_snapshot(caller):
    progress.require(caller in ('Review', 'Implement'), 'Planner cannot call Observer')
    snapshot = interruption_snapshot()
    snapshot['caller'] = caller
    snapshot['cycle'] = os.environ.get('AUTOCYCLE_REVIEW_CYCLE', 'manual')
    if caller == 'Implement':
        snapshot['identity'] = native_identity()
    else:
        progress.require(not git('status', '--porcelain'), 'Review observes checkpointed state only')
    return snapshot


def observation_request(caller, text):
    """Admit a native read-only view; no semantic acceptance is inferred."""
    import native_office as office
    lines = [line for line in text.splitlines() if 'OBSERVER_REQUEST' in line]
    progress.require(len(lines) == 1 and lines[0].startswith('OBSERVER_REQUEST: '), 'ambiguous Observer request')
    progress.require(not re.search(r'^(?:IMPLEMENT_STATUS|REVIEW_STATUS|NATIVE_HANDOFF):', text, re.M),
                     'Observer requests return to their caller before a final result')
    declaration = json.loads(lines[0].split(': ', 1)[1])
    progress.require(isinstance(declaration, dict) and set(declaration) == {'requests'} and
                     isinstance(declaration['requests'], list) and declaration['requests'], 'empty Observer request')
    ac = location(); path = ac/'observer-call.json'
    progress.require(not path.exists(), 'an Observer call is already pending')
    snapshot = observation_snapshot(caller)
    apps = office.capabilities(Path.cwd()); workspace = office.Workspace(ac)
    requests = []
    for item in declaration['requests']:
        progress.require(isinstance(item, dict) and item.get('app') in apps, 'Observer application is not enabled')
        progress.require(not any(k in item for k in ('work_id','attempt_id','recovery_id','requested_head','request_id')),
                         'Observer request cannot assign Controller ownership')
        progress.require(item.get('operation', 'capture') in ('capture', 'navigation'), 'unsupported Observer operation')
        source = Path(item['source']).resolve(strict=True)
        progress.require(source.is_relative_to(Path.cwd().resolve()), 'Observer source is outside the target project')
        office.safe_slot(Path(item['source']))
        progress.require(item.get('source_sha256') == office.digest(source), 'Observer source hash mismatch')
        office.MacOffice.validate_request(item['app'], item)
        if caller == 'Review':
            relative = source.relative_to(Path.cwd().resolve()).as_posix()
            progress.require(not relative.startswith('.git/') and digest(git('show', 'HEAD:'+relative)) == item['source_sha256'],
                             'Review observation source is not the checkpointed artifact')
        if item.get('operation') == 'navigation':
            compiled = office.navigation_request(item.get('requirement', {}))
            progress.require(compiled and Path(compiled['source']).resolve() == source and
                             all(item.get(k) == compiled[k] for k in ('app','source_sha256','worksheet')),
                             'Observer navigation binding mismatch')
        queued = office.enqueue(workspace, item, snapshot['head'])
        requests.append({'id': queued.stem, 'request_sha256': digest(queued.read_bytes()), 'app': item['app']})
    progress.require(snapshot == observation_snapshot(caller), 'project changed while admitting Observer call')
    atomic(path, {'snapshot': snapshot, 'requests': requests, 'state': 'pending'})


def observation_call(returned=False):
    import native_office as office
    record = json.loads((location()/'observer-call.json').read_text())
    caller = record['snapshot']['caller']
    progress.require(record['snapshot'] == observation_snapshot(caller), 'Observer continuation ownership changed')
    workspace = office.Workspace(location())
    for item in record['requests']:
        path = workspace.root/'requests'/(item['id']+'.json')
        office.safe_slot(path)
        progress.require(digest(path.read_bytes()) == item['request_sha256'], 'Observer request mutated')
        request = json.loads(path.read_text())
        progress.require(office.digest(Path(request['source'])) == request['source_sha256'], 'Observer source changed')
        if returned:
            _, result, receipt = office.request_record(workspace, item['id'])
            progress.require(digest(receipt.read_bytes()) == item['receipt_sha256'], 'Observer receipt mutated')
    return record


def observation_result():
    import native_office as office
    record = observation_call(); workspace = office.Workspace(location())
    for item in record['requests']:
        _, result, receipt = office.request_record(workspace, item['id'])
        item['receipt_sha256'] = digest(receipt.read_bytes())
        item['path'] = str(receipt)
        item['request_id'] = item['id']
        item['status'] = result['status']
    record['state'] = 'returned'
    # These are provenance references only. Reviewer owns their meaning.
    with progress.state() as current:
        observations = current.setdefault('observations', [])
        for item in record['requests']:
            if not any(old['request_id'] == item['request_id'] for old in observations):
                observations.append(item)
    atomic(location()/'observer-call.json', record)


def observation_consume(caller):
    path = location()/'observer-call.json'
    if not path.exists(): return
    record = observation_call(returned=True)
    progress.require(record['state'] == 'returned' and record['snapshot']['caller'] == caller,
                     'Observer must return to the same caller')
    path.unlink()


def native_handoff_ids():
    candidate=location()/'candidate.json'
    if candidate.exists() and json.loads(candidate.read_text()).get('kind') in ('implementation','checkpoint'):
        return []  # An implementation candidate grants no native delegation.
    path=location()/'native-handoff.json'
    result_path=location()/'implementation-result.json'
    result=json.loads(result_path.read_text()) if result_path.exists() else {}
    if not path.exists():
        progress.require(not result.get('native_handoff_sha256'), 'native handoff record missing')
        return None
    record=json.loads(path.read_text())
    progress.require(result.get('native_handoff_sha256')==digest(path.read_bytes()), 'native handoff ownership changed')
    with progress.state() as s:
        ended=s.get('last_session_end',{})
        if (progress.latest(s) is None and s['work'] is None and
            ended.get('status')=='completed' and
            ended.get('number')==record['identity']['session_number'] and
            int(os.environ.get('AUTOCYCLE_SESSION_NUMBER','1'))>ended['number']):
            return []  # Completed Session evidence cannot authorize a new Session.
    current=native_identity(); saved=record['identity']
    # Checkpoint changes phase and checkpoint_sha, never the admitted work/attempt.
    for identity in (current,saved):
        identity['binding']['attempt'].pop('phase',None)
        identity['binding']['attempt'].pop('checkpoint_sha',None)
    progress.require(current==saved, 'native handoff belongs to another implementation')
    progress.require(subprocess.run(['git','merge-base','--is-ancestor',record['head'],'HEAD'],
        stdout=subprocess.DEVNULL,stderr=subprocess.PIPE).returncode==0, 'native handoff HEAD is not an ancestor')
    for item in record['requests']:
        request=location()/'office/requests'/(item['id']+'.json')
        progress.require(digest(request.read_bytes())==item['request_sha256'], 'native request mutated after handoff')
        receipt=location()/'office/receipts'/(item['id']+'.json')
        if receipt.exists():
            import native_office as office
            office.request_record(office.Workspace(location()),item['id'])
    return [r['id'] for r in record['requests']]


def main():
    cmd, *args = sys.argv[1:]
    ac = location();ac.mkdir(exist_ok=True)
    if cmd == 'native-snapshot':
        print(json.dumps(native_snapshot()))
    elif cmd == 'observe-request': observation_request(args[0], sys.stdin.read())
    elif cmd == 'observe-call': print(json.dumps(observation_call()))
    elif cmd == 'observe-result': observation_result()
    elif cmd == 'observe-consume': observation_consume(args[0])
    elif cmd == 'run':
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
    elif cmd == 'docs-baseline':
        import remote_docs
        print(remote_docs.docs_baseline(args[0]))
    elif cmd == 'policy':print(POLICY)
    elif cmd == 'context':
        context=input_context(args[0] if args else '')
        context['digest']=digest(json.dumps(context.pop('items'),sort_keys=True).encode())
        print(json.dumps(context,sort_keys=True))
    elif cmd == 'candidate-key': print(digest(read(ac/'candidate.json')))
    elif cmd == 'identity': print(identity(args[0] if args else ''))
    elif cmd == 'valid': sys.exit(0 if valid(Path(args[0]),args[1]) else 1)
    elif cmd == 'interruption':preserve_interruption(args[0])
    elif cmd == 'adopt':
        progress.require(len(args) == 2 and args[0] == '--authorize-current-worktree',
                         'use adopt --authorize-current-worktree "explicit human attestation"')
        record = adopt_current_worktree(args[1])
        print('Human adoption recorded for Session '+record['snapshot']['session']['SESSION_NUMBER']+
              ', Step '+record['snapshot']['work_binding']['work']['step_id']+'. No implementation was run.')
    elif cmd == 'verify-adoption':
        progress.require(not args, 'verify-adoption takes no arguments')
        with recovery_lock(): verify_adoption()
        print('Human adoption receipt, archive and current recovery state match. No implementation was run.')
    elif cmd == 'baseline':
        p=ac/'implementation-baseline.json'
        if not p.exists():
            atomic(p, {'head':git('rev-parse','HEAD').decode().strip(),
                'branch':git('branch','--show-current').decode().strip(),
                'clean':not bool(git('status','--porcelain')), 'files':files()})
        # This is the state before the first provider invocation, not before
        # each retry. Interrupted implementation leaves its own edits dirty;
        # changing clean here would misclassify them as pre-existing work.
        # Keep an existing baseline immutable, including clean=False from older
        # releases: matching worktree fingerprints cannot prove a clean index.
        b=json.loads(p.read_text())
        if (b['head']!=git('rev-parse','HEAD').decode().strip()
                or b['branch']!=git('branch','--show-current').decode().strip()):
            raise ValueError('implementation baseline no longer matches HEAD/branch; work preserved for reconciliation')
        if not b['clean']:
            raise ValueError('implementation baseline has unresolved ownership; reconcile preserved work before retrying')
        receipt=ac/'implementation-interruption.json'
        human_receipt=ac/'human-adoption.json'
        if human_receipt.exists():
            # Explicit recovery is distinct from an automatic shutdown receipt.
            # Recheck every binding before consuming its one-time authority.
            verify_adoption()
            human_receipt.unlink()
        elif (ac/'observer-call.json').exists():
            observation_consume('Implement')
        elif receipt.exists():
            r=json.loads(receipt.read_text())
            if (r['snapshot']!=interruption_snapshot()
                    or r['log_digest']!=digest(read(Path(r['log'])))):
                raise ValueError('work changed since transport interruption; reconcile paused edits before resuming')
            # Single use: a crash during the next provider must not reuse old authority.
            receipt.unlink()
        elif git('status','--porcelain'):
            raise ValueError('interrupted implementation has uncheckpointed changes with unverified ownership; '
                             'work and original baseline preserved; reconcile before resuming')
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
            if 'native_handoff_sha256' in result:
                ok=ok and result['native_handoff_sha256']==digest(read(ac/'native-handoff.json'))
                if ok and (ac/'native-handoff.json').exists(): native_handoff_ids()
        else:ok=False
        # Submodules and protected documents require separate human handling.
        for name in ('TARGET.md','SESSION.md','IMPLEMENTATION.md'):
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
    elif cmd == 'numbering':
        with progress.state() as work_state:
            print(progress.encoded(progress.context(work_state, '')))
    else: raise ValueError('unknown adjudication operation')

if __name__ == '__main__':
    try: main()
    except Exception as e:
        print('Adjudication: '+str(e),file=sys.stderr);sys.exit(1)

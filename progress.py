#!/usr/bin/env python3
"""Controller-owned work identities and evidence-based, durable progress decisions.

Only .git/autocycle state and an explicitly supplied generated plan are written.
Review interprets evidence; this module validates its binding and enforces limits.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

VERSION = 1
POLICY = 'objective-progress-v1'
PLAN_FIELDS = ('objective', 'finding_key', 'kind', 'baseline', 'success', 'verification')


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE).decode().strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def acdir():
    return Path(git('rev-parse', '--git-path', 'autocycle')).resolve()


def record(text, label):
    rows = [line[len(label)+2:] for line in text.splitlines() if line.startswith(label+': ')]
    require(len(rows) == 1, 'expected exactly one '+label+' record')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate JSON key: '+key)
            result[key] = value
        return result
    value = json.loads(rows[0], object_pairs_hook=unique)
    require(isinstance(value, dict), label+' must be an object')
    return value


def heading(text):
    first = text.splitlines()[0] if text.splitlines() else ''
    match = re.match(r'^#+\s+(?:Plan:\s*)?Step\s+([A-Za-z0-9][A-Za-z0-9._-]*)(?=\s|$)', first)
    return match.group(1).rstrip('.') if match else None


def plan_text(sha):
    return git('show', sha+':IMPLEMENTATION.md')


def historical():
    """Import headings of confirmed executed plans once, never body mentions.

An immediate non-Plan checkpoint with changes outside IMPLEMENTATION.md is
execution evidence. This allocates IDs; it does not certify their acceptance.
    """
    used = {}
    previous = None
    for row in git('log', '--first-parent', '--reverse', '--format=%H %s').splitlines():
        sha, _, title = row.partition(' ')
        if previous and not title.startswith('Plan:') and re.match(r'^(Step |Checkpoint|Partial:)', title):
            changed = git('diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines()
            if any(p != 'IMPLEMENTATION.md' for p in changed):
                try:
                    ident = heading(plan_text(previous))
                    if ident:
                        used[ident] = {'source': previous, 'status': 'legacy-executed'}
                except subprocess.SubprocessError:
                    pass
        previous = sha if title.startswith('Plan:') else None
    return used


@contextlib.contextmanager
def state():
    directory = acdir(); directory.mkdir(exist_ok=True)
    path = directory/'work-state.json'
    with (directory/'work-state.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {'version': VERSION, 'branches': {}}
        require(data.get('version') == VERSION, 'unsupported progress state; preserve it for migration')
        branch = git('branch', '--show-current')
        require(branch, 'detached HEAD cannot own progress state')
        if branch not in data['branches']:
            data['branches'][branch] = {'allocated': historical(), 'work': None, 'attempts': [], 'epoch': 0, 'block': None}
        original = encoded(data)
        yield data['branches'][branch]
        if not path.exists() or encoded(data) != original:
            tmp = path.with_name(path.name+'.tmp.'+str(os.getpid()))
            with tmp.open('w') as f:
                f.write(encoded(data)); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, path)
            fd = os.open(directory, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)


def latest(s):
    return s['attempts'][-1] if s['attempts'] else None


def binding(s):
    w, a = s['work'], latest(s)
    return {'work': {k: w[k] for k in ('id','step_id',*PLAN_FIELDS)} if w else None,
            'attempt': {k: a[k] for k in ('id','plan_sha','phase','checkpoint_sha')} if a else None}


def input_ids(batch):
    if not batch:
        return []
    from adjudication import input_context
    return [row[1] for row in input_context(batch)['items']]


def context(s, batch):
    result = binding(s)
    result['allocated_ids'] = sorted(s['allocated'])
    result['complete'] = bool(s['work'] and s['work'].get('complete'))
    result['block'] = s['block']
    result['new_instruction_ids'] = sorted(set(input_ids(batch))-set((s['block'] or {}).get('instruction_ids', [])))
    result['verification_only'] = bool(latest(s) and latest(s).get('rechecks'))
    return result


def next_id(s, preferred):
    ident = preferred or 'AC.1'
    while ident in s['allocated']:
        match = re.search(r'(\d+)$', ident)
        ident = ident[:match.start()]+str(int(match.group())+1) if match else ident+'.1'
    return ident


def validate_plan(p):
    require(all(isinstance(p.get(k), str) and p[k].strip() and len(p[k]) <= 4000 for k in PLAN_FIELDS),
            'plan needs objective, finding_key, kind, baseline, success and verification')
    require(p['kind'] in ('work','administrative','verification'), 'invalid plan kind')


def prepare(s, path):
    text = path.read_text()
    p = record(text, 'AUTOCYCLE_PLAN'); validate_plan(p)
    w = s['work']
    if w and not w.get('complete'):
        # Repairs keep their original objective and acceptance, independent of prose.
        if not w.get('provisional'):
            for key in ('objective','finding_key','baseline','success'):
                p[key] = w[key]
        p['work_id'], p['step_id'] = w['id'], w['step_id']
    else:
        current = heading(Path('IMPLEMENTATION.md').read_text()) if Path('IMPLEMENTATION.md').exists() else None
        require(p['kind'] != 'administrative', 'administrative corrections must belong to an existing open work item')
        p['work_id'] = uuid.uuid4().hex
        p['step_id'] = next_id(s, current or heading(text))
    p['plan_id'] = uuid.uuid4().hex
    title = re.sub(r'^#+\s+(?:Plan:\s*)?(?:Step\s+\S+\s*)?(?:[—–:-]\s*)?', '', text.splitlines()[0])
    rows = text.splitlines()
    rows[0] = '# Step '+p['step_id']+' — '+(title or p['objective'])
    rows = [('AUTOCYCLE_PLAN: '+encoded(p)) if row.startswith('AUTOCYCLE_PLAN: ') else row for row in rows]
    path.write_text('\n'.join(rows)+'\n')
    # Preparing/proposing a plan deliberately does not allocate its step ID.


def admit(s, sha, batch, legacy=False):
    a = next((a for a in s['attempts'] if a['plan_sha'] == sha and a['kind']=='implementation'), None)
    if a:
        require(a is latest(s), 'cannot replay an older implementation attempt')
        return a
    require(not s['block'], 'progress is BLOCKED; a reviewed recovery is required')
    previous = latest(s)
    require(not previous or previous.get('outcome') is not None, 'preceding attempt still needs opening Review')
    text = plan_text(sha)
    if 'AUTOCYCLE_PLAN: ' in text:
        p = record(text, 'AUTOCYCLE_PLAN'); validate_plan(p)
        require(re.fullmatch(r'[a-f0-9]{32}', p.get('work_id','')), 'invalid admitted work ID')
        require(heading(text) == p.get('step_id'), 'plan heading and admitted step ID differ')
    else:
        require(legacy, 'plan is missing its progress contract; run opening Review and Plan')
        ident = heading(text) or next_id(s, 'AC.1')
        p = {'work_id': uuid.uuid4().hex, 'step_id': ident, 'objective': 'legacy:'+ident,
             'finding_key':'legacy:'+ident, 'kind':'work', 'baseline':'Legacy execution; inspect recorded starting evidence',
             'success':'Satisfy the original acceptance criteria in IMPLEMENTATION.md without weakening them',
             'verification':'Read the existing plan, implementation evidence and relevant checks'}
    w = s['work']
    if w and not w.get('complete'):
        # An unnumbered planning candidate can become a real plan under the same objective.
        require(p['work_id']==w['id'] and p['step_id']==w['step_id'], 'unresolved work identity cannot change')
        require(w.get('provisional') or all(p[k]==w[k] for k in ('objective','finding_key','baseline','success')), 'original work acceptance changed')
        allocated=s['allocated'].get(p['step_id'])
        require(not allocated or allocated.get('work_id')==w['id'], 'open work cannot reuse another allocated ID')
        if w.pop('provisional',False):
            w.update({k:p[k] for k in PLAN_FIELDS})
    else:
        allocated = s['allocated'].get(p['step_id'])
        require(not allocated or (legacy and allocated.get('source')==sha), 'new work attempted to reuse an allocated step ID')
        w = {k:p[k] for k in PLAN_FIELDS}
        w.update(id=p['work_id'], step_id=p['step_id'], complete=False)
        s['work'] = w
        s['epoch'] = len(s['attempts'])
    s['allocated'][p['step_id']] = {'source':sha,'work_id':w['id'],'status':'opened'}
    a = {'id':uuid.uuid4().hex, 'work_id':w['id'], 'plan_sha':sha, 'batch':batch,
         'kind':'implementation', 'phase':'running', 'checkpoint_sha':None, 'outcome':None, 'rechecks':0}
    s['attempts'].append(a)
    return a


def begin(s, sha, batch):
    # Legacy in-flight Plan commits can be adopted once without rewriting project files.
    legacy = 'AUTOCYCLE_PLAN: ' not in plan_text(sha)
    a = admit(s, sha, batch, legacy=legacy)
    require(a['phase']=='running' and a['outcome'] is None, 'attempt already checkpointed/reviewed; resume opening Review')


def recover(s, stage, sha, baseline, batch):
    if latest(s):
        return
    review_boundary=stage in ('start','reviewing','review_done') and bool(baseline)
    if review_boundary or stage in ('implementing','implement_done','implement_nochange','candidate_checkpoint','checkpoint_pending','checkpoint_done'):
        source = sha or baseline
        if source and git('log','-1','--format=%s',source).startswith('Plan:'):
            admit(s, source, batch, legacy=True)
        elif source and stage not in ('implementing',):
            # Some old resume states pin a checkpoint instead of the original plan.
            for line in git('log','--first-parent','--format=%H %s',source).splitlines():
                rev, _, subject = line.partition(' ')
                if subject.startswith('Plan:'):
                    admit(s, rev, batch, legacy=True); break
        if review_boundary and latest(s):
            checkpoint(s,'checkpoint',batch)


def checkpoint(s, kind, batch):
    a = latest(s)
    if kind == 'planning':
        candidate = acdir()/'candidate.json'
        require(candidate.exists(), 'planning boundary lacks its candidate evidence')
        key = digest(candidate.read_bytes())
        if a and a.get('candidate_key')==key:
            return
        w = s['work']
        if not w or w.get('complete'):
            ident = next_id(s,heading(Path('IMPLEMENTATION.md').read_text()))
            w = {'id':uuid.uuid4().hex,'step_id':ident,'objective':'resume-authorized-target-work',
                 'finding_key':'planning-obstacle','kind':'work','baseline':'No executable plan is available',
                 'success':'Produce and execute an authorized plan against TARGET','verification':'Inspect original goal and candidate evidence','complete':False,'provisional':True}
            s['work']=w
        a = {'id':uuid.uuid4().hex,'work_id':w['id'],'kind':'planning','candidate_key':key,
             'plan_sha':git('rev-parse','HEAD'),'batch':batch,'phase':'running','checkpoint_sha':None,'outcome':None,'rechecks':0}
        s['attempts'].append(a)
    if a and a['phase']=='running':
        a['phase']='checkpointed'; a['checkpoint_sha']=git('rev-parse','HEAD')
    # Review-only boundaries have no new attempt and never consume an ID.


def evidence_valid(items):
    require(isinstance(items, list) and len(items) <= 20, 'evidence must be a list of at most 20 references')
    root = Path(git('rev-parse','--show-toplevel')).resolve()
    evidence_root = acdir()
    checked = []
    for e in items:
        require(isinstance(e,dict) and all(isinstance(e.get(k),str) and e[k].strip() for k in ('path','sha256','observation')),
                'evidence needs path, sha256 and an observed result')
        p = Path(e['path'])
        p = (root/p).resolve() if not p.is_absolute() else p.resolve()
        require(p.is_relative_to(root) or p.is_relative_to(evidence_root), 'evidence must be in this repository or its AutoCycle evidence directory')
        require(p.is_file() and not p.name.startswith('work-state'), 'missing or self-referential progress evidence')
        require(re.fullmatch('[a-f0-9]{64}',e['sha256']) and digest(p.read_bytes())==e['sha256'], 'progress evidence hash mismatch')
        checked.append({'path':str(p),'sha256':e['sha256'],'observation':e['observation']})
    return checked


def review_report(s, text):
    r = record(text, 'AUTOCYCLE_REVIEW')
    w, a = s['work'], latest(s)
    require(r.get('work_id')==(w['id'] if w else 'NONE'), 'Review changed the controller-owned work ID')
    require(r.get('attempt_id')==(a['id'] if a else 'NONE'), 'Review assessed the wrong attempt')
    require(isinstance(r.get('finding_key'),str) and r['finding_key'].strip(), 'missing finding key')
    require(type(r.get('blocking')) is bool, 'blocking must be boolean')
    require(isinstance(r.get('blocking_reason'),str) and r['blocking_reason'].strip(), 'missing blocking reason')
    require(not r['blocking'] or r['blocking_reason']!='NONE', 'blocking finding needs a concrete consequence')
    require(r.get('outcome') in ('UNASSESSED','NONE','VERIFIED','COMPLETE','UNKNOWN'), 'invalid progress outcome')
    require(isinstance(r.get('reason'),str) and r['reason'].strip(), 'progress outcome needs a reason')
    require(r.get('recovery','NONE') in ('NONE','NEW_EVIDENCE','REVISED_APPROACH'), 'invalid recovery mode')
    if a and a['phase']=='checkpointed':
        for revision in (a['plan_sha'],a['checkpoint_sha']):
            require(subprocess.run(['git','merge-base','--is-ancestor',revision,'HEAD'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE).returncode==0, 'attempt evidence is not an ancestor of current HEAD')
        require(r['outcome']!='UNASSESSED','checkpointed attempt requires a progress assessment')
    else:
        require(r['outcome']=='UNASSESSED','no checkpointed attempt exists to assess')
    if w:
        # Wording can drift, but the ledger always retains the original identity.
        r['finding_key']=w['finding_key']
    checked = evidence_valid(r.get('evidence',[]))
    if r['outcome'] in ('VERIFIED','COMPLETE'):
        require(checked, 'verified progress requires concrete, hash-bound evidence')
    lines = text.splitlines()
    status = next((x[15:] for x in lines if x.startswith('REVIEW_STATUS: ')), '')
    require(status!='DONE' or not a or r['outcome']=='COMPLETE', 'DONE requires verified completion of the current work')
    require(status!='BLOCKED' or r['blocking'], 'BLOCKED requires a blocking consequence')
    return r, checked


def stall_count(s):
    n = 0
    for a in s['attempts'][s['epoch']:]:
        if a['work_id'] != (s['work'] or {}).get('id') or a['outcome'] is None:
            continue
        n = 0 if a['outcome'] in ('VERIFIED','COMPLETE') else n+1
    return n


def evaluate(s, path, batch):
    text=path.read_text()
    r, checked = review_report(s, text)
    token = next((x[14:] for x in text.splitlines() if x.startswith('REVIEW_TOKEN: ')), '')
    require(token, 'cached Review is missing its unique decision token')
    a, w = latest(s), s['work']
    # Content novelty is independent of path names, array order and explanatory prose.
    evidence_key=digest(encoded(sorted({e['sha256'] for e in checked})).encode())
    content_hashes={e['sha256'] for e in checked}
    seen=set((w or {}).get('evidence_history',[]))
    block=s['block']
    if block:
        mode=r.get('recovery','NONE')
        new_ids=set(input_ids(batch))-set(block['instruction_ids'])
        requested=r.get('recovery_instruction_ids',[])
        revised=(mode=='REVISED_APPROACH' and isinstance(requested,list) and bool(requested)
                 and set(requested)<=new_ids and isinstance(r.get('recovery_reason'),str)
                 and r['recovery_reason'].strip() not in ('','NONE'))
        evidenced=(mode=='NEW_EVIDENCE' and r['outcome'] in ('VERIFIED','COMPLETE')
                   and bool(content_hashes-seen) and evidence_key!=block.get('evidence_key'))
        if not (revised or evidenced):
            return 2, block['reason']
        s.setdefault('recoveries',[]).append({'block':block,'review_token':token,'mode':mode,'reason':r.get('recovery_reason',r['reason'])})
        s['block']=None; s['epoch']=len(s['attempts'])
        if a:
            a.update(review_token=token, outcome='RECOVERED' if revised else r['outcome'], report=r, evidence_key=evidence_key, rechecks=0)
        if evidenced and w:
            w['evidence_history']=sorted(seen|content_hashes)
            w['last_verified_evidence']=evidence_key
            if r['outcome']=='COMPLETE':
                w['complete']=True
                if w['step_id'] in s['allocated']: s['allocated'][w['step_id']]['status']='completed'
        return 0, 'Recovery accepted by Review'
    if a and a['phase']=='checkpointed':
        if a.get('outcome')=='RECOVERED':
            return 0, 'Explicitly revised approach already accepted for this attempt'
        if a.get('review_token')!=token:
            outcome=r['outcome']
            # The same evidence/observation cannot be reported as incremental improvement repeatedly.
            replay=(a.get('outcome')=='VERIFIED' and a.get('evidence_key')==evidence_key)
            if outcome=='VERIFIED' and not (content_hashes-seen) and not replay:
                outcome='NONE'
            a.update(outcome=outcome, review_token=token, report=r, evidence_key=evidence_key)
            if outcome in ('VERIFIED','COMPLETE'):
                w['last_verified_evidence']=evidence_key
                w['evidence_history']=sorted(seen|content_hashes)
                if outcome=='COMPLETE':
                    w['complete']=True
                    if w['step_id'] in s['allocated']:
                        s['allocated'][w['step_id']]['status']='completed'
            elif outcome=='UNKNOWN':
                a['rechecks']+=1
        if a['outcome']=='UNKNOWN' and a['rechecks']==1:
            return 4, 'Missing progress evidence; one read-only verification retry'
        reason = 'PROGRESS_UNKNOWN' if a['outcome']=='UNKNOWN' else ('REPEATED_NO_PROGRESS' if stall_count(s)>=2 else '')
        if reason:
            s['block']={'reason':reason,'work_id':w['id'],'attempt_id':a['id'],
                        'instruction_ids':input_ids(batch),'evidence_key':evidence_key,'review_token':token,'stalled_attempts':stall_count(s)}
            return 2, reason
    return 0, 'Progress assessed'


def main():
    cmd,*args=sys.argv[1:]
    code=0; output=None
    with state() as s:
        if cmd=='context': output=encoded(context(s,args[0] if args else ''))
        elif cmd=='binding': output=digest(encoded(binding(s)).encode())
        elif cmd=='prepare': prepare(s,Path(args[0]))
        elif cmd=='begin': begin(s,args[0],args[1] if len(args)>1 else '')
        elif cmd=='recover': recover(s,*args)
        elif cmd=='checkpoint': checkpoint(s,*args)
        elif cmd=='validate-review': review_report(s,Path(args[0]).read_text())
        elif cmd=='guard': code,output=evaluate(s,Path(args[0]),args[1] if len(args)>1 else '')
        elif cmd=='blocked': code=0 if s['block'] else 1
        else: raise ValueError('unknown progress operation')
    if output is not None: print(output)
    return code


if __name__=='__main__':
    try: sys.exit(main())
    except (Exception,KeyboardInterrupt) as error:
        print('Progress: '+str(error),file=sys.stderr); sys.exit(1)

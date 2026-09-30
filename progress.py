#!/usr/bin/env python3
"""Controller-owned work identities and evidence-based, durable progress decisions.

Only .git/autocycle state and an explicitly supplied generated plan are written.
Review interprets evidence; this module validates its binding and work identity.
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
import time
import uuid

VERSION = 1
POLICY = 'objective-progress-v1'
PLAN_FIELDS = ('objective', 'finding_key', 'kind')


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
            data['branches'][branch] = {'allocated': historical(), 'work': None, 'attempts': [], 'block': None}
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
    attempts = s['attempts'][s.get('session_start',0):]
    return attempts[-1] if attempts else None


def binding(s):
    w, a = s['work'], latest(s)
    return {'work': {k: w[k] for k in ('id','step_id',*PLAN_FIELDS)} if w else None,
            'attempt': {k: a[k] for k in ('id','plan_sha','phase','checkpoint_sha')} if a else None}


def input_ids(batch):
    if not batch:
        return []
    from adjudication import input_context
    return [row[1] for row in input_context(batch)['items']]


def stagnation(s):
    """Advisory consecutive no-progress judgments, once per bounded attempt."""
    w = s['work']
    if not w or w.get('complete'):return 0
    count = 0
    for a in reversed(s['attempts']):
        if a['work_id']!=w['id'] or a['kind']!='implementation':continue
        outcome = a.get('outcome')
        if outcome=='RECOVERED':outcome=a.get('report',{}).get('outcome')
        if outcome in ('VERIFIED','COMPLETE'):break
        if outcome=='NONE':count+=1
    return count


def context(s, batch):
    result = binding(s)
    if result['work']:
        result['work']['provisional'] = bool(s['work'].get('provisional'))
    if result['attempt']:
        result['attempt']['kind'] = latest(s)['kind']
    result['allocated_ids'] = sorted(s['allocated'])
    result['session_number'] = int(os.environ.get('AUTOCYCLE_SESSION_NUMBER', '1'))
    result['complete'] = bool(s['work'] and s['work'].get('complete'))
    result['stagnation'] = stagnation(s)
    result['block'] = s['block']
    result['legacy_observation_review'] = s.get('legacy_observation_review')
    result['unresolved_facts'] = s.get('unresolved_facts', [])
    result['legacy_evidence_review'] = bool(s.get('legacy_evidence_review') or s.get('missing_evidence'))
    result['verification_capability_gap'] = s.get('verification_capability_gap')
    result['new_instruction_ids'] = sorted(set(input_ids(batch))-set((s['block'] or {}).get('instruction_ids', [])))
    result['verification_only'] = bool(latest(s) and latest(s)['kind']=='implementation'
                                       and latest(s).get('rechecks') and latest(s).get('review_status')!='PROBLEMS')
    result['prior_reviews'] = prior_reviews(s)
    return result


def prior_reviews(s):
    """Locators from retained reports, never certificates of current validity."""
    # The active attempt's report is replaced on re-review. It cannot be its
    # own historical provenance; inspect its current facts directly instead.
    active = latest(s)
    return [{'attempt_id': a['id'], 'plan_sha': a['plan_sha'],
             'checkpoint_sha': a['checkpoint_sha'],
             'evidence': a['report'].get('evidence', []),
             'endpoint_evidence': a['report'].get('endpoint', {}).get('evidence', [])}
            for a in s['attempts'] if a is not active and a['phase']=='checkpointed'
            and a.get('report', {}).get('outcome') in ('VERIFIED','COMPLETE')]


def new_session(s, status, number):
    require(status in ('completed','restarted'), 'invalid previous Session ending')
    # A restart is explicit controller authority after preservation, never a Review pass.
    require(status=='restarted' or not (s['block'] or s.get('missing_evidence')), 'cannot reset a progress block by starting a session')
    if status=='restarted':
        # The controller has durably saved session_restarted after preservation.
        # Retire active evidence pointers, retaining their contents for diagnosis.
        archive = acdir()/'session-history'/str(number)
        for name in ('candidate.json','implementation-baseline.json',
                     'implementation-result.json','latest-implementation','remote-docs-current.json','native-handoff.json','observer-call.json'):
            source = acdir()/name
            if source.exists():
                archive.mkdir(parents=True, exist_ok=True)
                destination = archive/name
                require(not destination.exists() or destination.read_bytes()==source.read_bytes(),
                        'conflicting previous Session evidence; restart refused')
                os.replace(source, destination)
    if status == 'completed':
        s['office_completed_sessions'] = sorted(set(s.get('office_completed_sessions', []) + [str(number)]))
    s['last_session_end'] = {'number':int(number), 'status':status, 'head':git('rev-parse','HEAD')}
    w = s['work']
    if w and not w.get('complete'):
        s.setdefault('deferred',[]).append({'work':w,
            'reason':'The prior Session was '+status+'; this bounded work remains unfinished',
            'block':s['block'],'missing_evidence':s.get('missing_evidence'),
            'instruction_ids':[], 'plan_sha':(latest(s) or {}).get('plan_sha')})
        for allocation in s['allocated'].values():
            if allocation.get('work_id')==w['id']:allocation['status']='deferred'
    s['work'] = None
    s['block'] = None
    s['session_start'] = len(s['attempts'])
    s['session_recovery_start'] = len(s.get('recoveries', []))
    s.pop('office_retention', None)
    s.pop('observations', None)
    s.pop('legacy_observation_review', None)
    s.pop('missing_evidence',None)
    for key in ('unresolved_facts','legacy_evidence_review','verification_capability_gap','evidence_admission'):
        s.pop(key, None)


def completion(text, required=True):
    sections = re.findall(r'^## Completion[ \t]*\n(.*?)(?=^#{1,2} |\Z)', text, re.M | re.S)
    require(len(sections)<=1, 'plan needs one concise Completion statement')
    value = re.split(r'\n[ \t]*\n|^#{1,6} ', sections[0].strip(), maxsplit=1, flags=re.M)[0].strip() if sections else ''
    require(not required or value, 'plan needs a Completion statement')
    return value


def parent_completion(s):
    # The admitted Markdown is authoritative; do not copy Completion into state.
    w = s['work']
    for a in s['attempts']:
        if w and a['work_id']==w['id'] and a['kind']=='implementation':
            value = completion(plan_text(a['plan_sha']), required=False)
            if value:return value
    return ''


def next_id(s, minor=None):
    session = int(os.environ.get('AUTOCYCLE_SESSION_NUMBER', '1'))
    prefix = str(session)+'.'
    w = s['work']
    if minor is not None:
        require(type(minor) is int and minor>0, 'minor must be a positive integer, never nested')
        require(w and not w.get('complete') and not w.get('provisional') and re.fullmatch(r'[1-9]\d*\.[1-9]\d*(?:\.[1-9]\d*)?',w['step_id']),
                'a continuation requires an open major Step; initial work has no minor')
        a = latest(s)
        require(a and a['work_id']==w['id'] and (a.get('outcome') in ('VERIFIED','NONE','RECOVERED') or
                    (a.get('outcome')=='UNKNOWN' and a.get('review_status')=='PROBLEMS')),
                'continuation requires opening Review of the previous bounded attempt')
        parts=w['step_id'].split('.')
        require(minor==(int(parts[2])+1 if len(parts)==3 else 1), 'continuation must use the next sub-step')
        return '.'.join(parts[:2])+'.'+str(minor)
    used = [int(m.group(1)) for ident in s['allocated']
            if (m := re.fullmatch(re.escape(prefix)+r'(\d+)(?:\.\d+)?', ident))]
    return prefix+str(max(used, default=0)+1)


def validate_plan(p):
    require(all(isinstance(p.get(k), str) and p[k].strip() and len(p[k]) <= 4000 for k in PLAN_FIELDS),
            'invalid plan tracking metadata')
    require(p['kind'] in ('work','administrative','verification'), 'invalid plan kind')


def prepare(s, path, review_path=None):
    text = path.read_text()
    p = record(text, 'AUTOCYCLE_PLAN') if 'AUTOCYCLE_PLAN: ' in text else {}
    title = re.sub(r'^#+\s+(?:Plan:\s*)?(?:(?:Step\s+\S+)\s*(?:[—–:-]\s*)?)?', '', text.splitlines()[0])
    completion(text)
    for key in ('baseline','success','verification','completion'):p.pop(key,None)
    # Only identity metadata is stamped; optional verification guidance stays prose.
    defaults = dict(objective=title or 'Current bounded work', finding_key=title or 'Current bounded work',
                    kind='work')
    for key, value in defaults.items():p.setdefault(key, value)
    validate_plan(p)
    minor = p.get('minor')
    require(minor is None or (type(minor) is int and minor > 0), 'minor must be a positive integer, never nested')
    w = s['work']
    # A model cannot smuggle a work replacement through its proposed plan.
    p.pop('defer_work', None)
    defer = None
    if review_path:
        review_text = review_path.read_text()
        r = record(review_text, 'AUTOCYCLE_REVIEW')
        validate_evidence_routes(p, r)
        direction = instruction_direction(r, review_text, os.environ.get('AUTOCYCLE_INPUT_BATCH',''))
        defer = reviewed_deferral(s, r, direction)
        if defer:p['defer_work'] = defer
    if w and not w.get('complete') and not defer:
        original = parent_completion(s)
        require(not original or completion(text)==original, 'parent Completion cannot change during continuation')
        p['finding_key'] = w['finding_key']
        p['work_id'] = w['id']
        p['step_id'] = next_id(s, minor) if minor is not None else w['step_id']
    else:
        require(p['kind'] != 'administrative', 'administrative corrections must belong to an existing open work item')
        p['work_id'] = uuid.uuid4().hex
        require(minor is None, 'a new major Step starts without a sub-step')
        p['step_id'] = next_id(s)
    p['plan_id'] = uuid.uuid4().hex
    rows = text.splitlines()
    rows[0] = '# Step '+p['step_id']+' — '+(title or p['objective'])
    rows = [('AUTOCYCLE_PLAN: '+encoded(p)) if row.startswith('AUTOCYCLE_PLAN: ') else row for row in rows]
    if not any(row.startswith('AUTOCYCLE_PLAN: ') for row in rows):
        rows.insert(1, 'AUTOCYCLE_PLAN: '+encoded(p))
    path.write_text('\n'.join(rows)+'\n')
    if review_path and r.get('unresolved_facts'):
        s['evidence_admission'] = digest(encoded([p.get('evidence_routes', []), r.get('unresolved_facts', [])]).encode())
    # Preparing/proposing a plan deliberately does not allocate its step ID.


def validate_admitted_route(s, p):
    # Route validation belongs to Plan. Implement only consumes that exact
    # admission; an unadmitted legacy plan must return to Review/Plan.
    if s.get('unresolved_facts') or p.get('evidence_routes'):
        require(s.get('evidence_admission') == digest(encoded([p.get('evidence_routes', []),
                s.get('unresolved_facts', [])]).encode()), 'evidence route lacks current Plan admission; return to Review/Plan')


def admit(s, sha, batch, legacy=False):
    require(not s.get('missing_evidence') and not s.get('legacy_evidence_review'),
            'legacy evidence must return to ordinary Review before Implement')
    a = next((a for a in s['attempts'] if a['plan_sha'] == sha and a['kind']=='implementation'), None)
    if a:
        require(a is latest(s), 'cannot replay an older implementation attempt')
        validate_admitted_route(s, record(plan_text(sha), 'AUTOCYCLE_PLAN') if 'AUTOCYCLE_PLAN: ' in plan_text(sha) else {})
        return a
    require(not s['block'],
            'progress is BLOCKED; a reviewed recovery is required')
    previous = latest(s)
    require(not previous or previous.get('outcome') is not None, 'preceding attempt still needs opening Review')
    text = plan_text(sha)
    if 'AUTOCYCLE_PLAN: ' in text:
        p = record(text, 'AUTOCYCLE_PLAN'); validate_plan(p)
        require(re.fullmatch(r'[a-f0-9]{32}', p.get('work_id','')), 'invalid admitted work ID')
        require(heading(text) == p.get('step_id'), 'plan heading and admitted step ID differ')
    else:
        require(legacy, 'plan is missing its work identity; run opening Review and Plan')
        ident = heading(text) or next_id(s)
        p = {'work_id': uuid.uuid4().hex, 'step_id': ident, 'objective': 'legacy:'+ident,
             'finding_key':'legacy:'+ident, 'kind':'work'}
    if not legacy:completion(text)
    validate_admitted_route(s, p)
    w = s['work']
    deferred = p.get('defer_work')
    if deferred:
        require(w and not w.get('complete') and deferred.get('work_id')==w['id']
                and p['work_id']!=w['id'], 'deferred work identity changed before admission')
        require(set(deferred.get('instruction_ids',[]))<=set(input_ids(batch)),
                'deferred work lost its authorizing instruction batch')
        s.setdefault('deferred',[]).append({'work':w,'reason':deferred['reason'],
                                           'instruction_ids':deferred['instruction_ids'],'plan_sha':sha})
        for allocation in s['allocated'].values():
            if allocation.get('work_id')==w['id']:allocation['status']='deferred'
        w = None
    if w and not w.get('complete'):
        expected = next_id(s,p['minor']) if p.get('minor') is not None else w['step_id']
        require(p['work_id']==w['id'] and p['step_id']==expected, 'unresolved work identity cannot change')
        original = parent_completion(s)
        require(not original or completion(text)==original, 'parent Completion cannot change during continuation')
        allocated=s['allocated'].get(p['step_id'])
        require(not allocated or allocated.get('work_id')==w['id'], 'open work cannot reuse another allocated ID')
        w.pop('provisional',False)
        w.update({k:p[k] for k in PLAN_FIELDS})
        w['step_id']=p['step_id']
    else:
        if not legacy and re.match(r'^[0-9]+\.',p['step_id']):
            require(p['step_id']==next_id(s, minor=p.get('minor')), 'invalid new session Step')
        allocated = s['allocated'].get(p['step_id'])
        require(not allocated or (legacy and allocated.get('source')==sha), 'new work attempted to reuse an allocated step ID')
        w = {k:p[k] for k in PLAN_FIELDS}
        w.update(id=p['work_id'], step_id=p['step_id'], complete=False)
        s['work'] = w
    s['allocated'][p['step_id']] = {'source':sha,'work_id':w['id'],'status':'opened'}
    a = {'id':uuid.uuid4().hex, 'work_id':w['id'], 'plan_sha':sha, 'batch':batch,
         'kind':'implementation', 'phase':'running', 'checkpoint_sha':None, 'outcome':None, 'rechecks':0}
    s['attempts'].append(a)
    return a


def begin(s, sha, batch):
    # Legacy in-flight Plan commits can be adopted once without rewriting project files.
    text = plan_text(sha)
    legacy = 'AUTOCYCLE_PLAN: ' not in text or 'success' in record(text,'AUTOCYCLE_PLAN')
    a = admit(s, sha, batch, legacy=legacy)
    require(a['phase']=='running' and a['outcome'] is None, 'attempt already checkpointed/reviewed; resume opening Review')


def recover(s, stage, sha, baseline, batch):
    if s.get('legacy_evidence_review') or s.get('missing_evidence'):
        return  # Ordinary Review, never adopt legacy evidence execution.
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
            ident = next_id(s)
            w = {'id':uuid.uuid4().hex,'step_id':ident,'objective':'resume-authorized-target-work',
                 'finding_key':'planning-obstacle','kind':'work','complete':False,'provisional':True}
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
        require(p.is_file() and not p.name.startswith('work-state')
                and p != evidence_root/'current-review'
                and not (p.is_relative_to(evidence_root) and p.name.startswith('review-') and p.name.endswith('.answer')),
                'missing or self-referential progress evidence')
        require(re.fullmatch('[a-f0-9]{64}',e['sha256']) and digest(p.read_bytes())==e['sha256'], 'progress evidence hash mismatch')
        if p.suffix == '.json' and p.is_relative_to(evidence_root/'office'):
            value = json.loads(p.read_text())
            if isinstance(value, dict) and value.get('artifact_retention'):
                from native_office import require_live_artifacts
                require_live_artifacts(value)
            if p.parent == evidence_root/'office/receipts':
                from native_office import Workspace, request_record
                request, _, _ = request_record(Workspace(evidence_root), p.stem)
                source = Path(request['source'])
                require(source.is_file() and digest(source.read_bytes()) == request['source_sha256'],
                        'Office evidence source changed; observation is inapplicable')
        checked.append({'path':str(p),'sha256':e['sha256'],'observation':e['observation']})
        if 'applies_to' in e:
            basis = e['applies_to']
            require(isinstance(basis,list) and basis and all(isinstance(b,dict)
                    and not {'applies_to','prior_attempt_id'}.intersection(b) for b in basis),
                    'applicability requires direct factual references')
            checked[-1]['applies_to'] = evidence_valid(basis)
    return checked


def review_evidence(s, items):
    checked = evidence_valid(items)
    for item, current in zip(items, checked):
        if 'prior_attempt_id' not in item:
            continue
        prior = next((r for r in prior_reviews(s) if r['attempt_id']==item['prior_attempt_id']), None)
        require(prior is not None, 'unknown prior evidence attempt')
        require(current.get('applies_to'), 'reused evidence requires current applicability bindings')
        # The original hash must still match; a new hash cannot silently renew
        # an old acceptance. Semantic sufficiency remains Review's decision.
        root = Path(git('rev-parse','--show-toplevel'))
        require(any(str((root/e['path']).resolve())==current['path']
                    and e['sha256']==current['sha256']
                    for e in prior['evidence']+prior['endpoint_evidence']),
                'reused evidence differs from the prior factual reference')
        for revision in (prior['plan_sha'],prior['checkpoint_sha']):
            require(subprocess.run(['git','merge-base','--is-ancestor',revision,'HEAD'],
                    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE).returncode==0,
                    'prior evidence is not an ancestor of current HEAD')
    return checked


def observation_ids(s):
    return [item['replacement_request_id'] for item in
            ((s.get('block') or {}).get('observations') or {}).get('requests', [])]


def observation_boundary(continuation=False):
    import shlex
    values = {}
    for line in (acdir()/'resume-state').read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep:
            parts = shlex.split(value)
            values[key] = parts[0] if parts else ''
    require(continuation or values.get('STAGE') in ('start','reviewing','review_done'),
            'observation recovery requires an existing Review boundary')
    return {key:values[key] for key in ('SESSION_NUMBER','RUN_CYCLE','INPUT_CYCLE_ID')}


def observation_request(ident):
    from native_office import Workspace, request_record
    request,receipt,path=request_record(Workspace(acdir()), ident)
    require(request.get('operation')!='navigation','navigation facts must not use observation replacement IDs')
    return request,receipt,path


def validate_observations(s, r, text, checked):
    require(r.get('retry_observations', []) == [] and not r.get('observation_recovery_id'),
            'Review uses same-stage OBSERVER_REQUEST, not delegated observation recovery')
    require(not (s.get('block') or {}).get('observations'),
            'legacy observation state requires Controller migration before Review')


def migrate_observations(s, cache):
    """Read old associations once; retain provenance and require ordinary Review.

    This does not execute an old request, clear a semantic finding, or infer that
    an observation succeeded. New observations use the stage-local call protocol.
    """
    block = s.get('block') or {}
    obs = block.get('observations')
    text = cache.read_text() if cache.exists() else ''
    try:
        r = record(text, 'AUTOCYCLE_REVIEW') if 'AUTOCYCLE_REVIEW: ' in text else {}
    except ValueError:
        if obs: raise
        return 3, None
    ids = r.get('retry_observations', [])
    if not obs and not ids: return 3, None
    if s.get('legacy_observation_review'): return 0, None
    boundary = observation_boundary()
    head = git('rev-parse','HEAD')
    require(r.get('work_id') == (s['work']['id'] if s['work'] else 'NONE') and
            r.get('attempt_id') == ((latest(s) or {}).get('id','NONE')) and
            'REVIEWED_SHA: '+head in text.splitlines(), 'legacy observation work/checkpoint changed')
    if obs:
        require(obs['boundary'] == boundary and obs['head'] == head,
                'legacy observation boundary changed')
        items = obs['requests']
    else:
        require(isinstance(ids,list) and 0 < len(ids) <= 8 and len(set(ids)) == len(ids),
                'invalid legacy observation identities')
        items = [{'request_id':ident} for ident in ids]
    references = []
    from native_office import Workspace, request_record, safe_slot
    workspace = Workspace(acdir())
    for item in items:
        request, receipt, path = request_record(workspace, item['request_id'])
        request_path = workspace.root/'requests'/(item['request_id']+'.json')
        require(receipt['reviewed_head'] == head and
                digest(Path(request['source']).read_bytes()) == request['source_sha256'],
                'legacy observation source/checkpoint changed')
        if obs:
            require(digest(path.read_bytes()) == item['receipt_sha256'] and
                    digest(request_path.read_bytes()) == item['request_sha256'],
                    'legacy observation receipt/request changed')
        references.append({'request_id':item['request_id'],'path':str(path),'sha256':digest(path.read_bytes())})
        replacement = item.get('replacement_request_id')
        if replacement:
            require(isinstance(replacement,str) and re.fullmatch('[a-f0-9]{32}',replacement), 'invalid legacy replacement ID')
            pending = workspace.root/'requests'/(replacement+'.json')
            if pending.exists():
                safe_slot(pending); value=json.loads(pending.read_text())
                require(value.get('recovery_id') == obs['id'] and
                        value.get('replaces_request_id') == item['request_id'] and
                        value.get('source_sha256') == request['source_sha256'], 'legacy replacement binding changed')
                references.append({'request_id':replacement,'path':str(pending),'sha256':digest(pending.read_bytes())})
                if (workspace.root/'receipts'/(replacement+'.json')).exists():
                    _, _, path = request_record(workspace, replacement)
                    references.append({'request_id':replacement,'path':str(path),'sha256':digest(path.read_bytes())})
    s.setdefault('recoveries', []).append({'mode':'LEGACY_OBSERVATION_REVIEW','block':block,'report':r})
    s['legacy_observation_review'] = {'head':head,'references':references,'reason':r.get('blocking_reason')}
    if obs: s['block'] = block.get('previous_block')
    return 0, None


def missing_requirements(r):
    # Kept as a compatibility validator, not a Review-to-execution channel.
    require(r.get('missing_evidence', []) == [],
            'missing_evidence is Controller execution metadata; Review must state unresolved_facts, never unclassified')
    return []


def unresolved_facts(r):
    missing_requirements(r)
    facts = r.get('unresolved_facts', [])
    require(isinstance(facts, list) and len(facts) <= 8, 'Review needs up to eight material unresolved facts')
    for fact in facts:
        require(isinstance(fact, dict) and all(isinstance(fact.get(k), str) and fact[k].strip()
                for k in ('fact', 'material_reason')), 'Unresolved fact needs semantic uncertainty and material_reason')
        require(fact['fact'].strip().lower() != 'unclassified', 'unclassified is not a semantic fact')
    require(len({f['fact'] for f in facts}) == len(facts), 'duplicate unresolved facts')
    require(not facts or r.get('blocking'), 'material uncertainty must remain blocking')
    return facts


def validate_missing_evidence(s, r, checked):
    # Receipt provenance is checked by review_evidence. Only Review judges
    # whether observations collectively resolve a semantic fact.
    unresolved_facts(r)


def update_missing_evidence(s, cache, r, checked):
    unresolved_facts(r)
    s.pop('missing_evidence', None)
    s.pop('legacy_evidence_review', None)
    s.pop('verification_capability_gap', None)
    s['unresolved_facts'] = r.get('unresolved_facts', [])


def resume_missing_evidence(s, cache):
    """Legacy transition only: never synthesize, enqueue, execute, or retry."""
    cached_legacy = False
    if cache.is_file():
        try:
            r = record(cache.read_text(), 'AUTOCYCLE_REVIEW')
            cached_legacy = bool(r.get('missing_evidence'))
        except ValueError:
            pass  # Malformed cache is handled by ordinary Review validation.
    if s.pop('missing_evidence', None) is not None or cached_legacy:
        s['legacy_evidence_review'] = True
    if s.get('legacy_evidence_review'):
        return 0, 'Legacy evidence state requires ordinary Review'
    return 3, None


def missing_requests(s):
    return []  # Legacy callers cannot manufacture requests.


class VerificationCapabilityGap(ValueError):
    """Controller admission failure, distinct from execution or external BLOCKED."""


def validate_evidence_routes(p, r):
    """Validate finite uses of existing commands, references and Office requests.

    This checks executability, never the semantic sufficiency of their outputs.
    No actions run here and runtime failure cannot become capability absence.
    """
    import shutil
    import native_office as office
    facts = unresolved_facts(r)
    routes = p.get('evidence_routes', [])
    require(isinstance(routes, list) and len(routes) <= 8, 'invalid evidence routes')
    require(all(isinstance(route, dict) for route in routes), 'invalid evidence route')
    require(len({route.get('fact') for route in routes}) == len(routes), 'duplicate evidence routes')
    require(all(route.get('fact') in {f['fact'] for f in facts} for route in routes),
            'Plan cannot invent or strengthen the Review fact')
    root = Path(git('rev-parse', '--show-toplevel')).resolve()
    for fact in facts:
        route = next((route for route in routes if route.get('fact') == fact['fact']), {})
        def gap(reason):
            raise VerificationCapabilityGap(encoded({'semantic_fact':fact['fact'],
                'material_reason':fact['material_reason'], 'attempted_route':route,
                'missing_capability':reason}))
        if route.get('missing_capability'):
            gap(route['missing_capability'])
        if set(route) - {'fact','requests','commands','evidence','missing_capability'}:
            gap('Unsupported route fields; use existing Office requests, commands or evidence references')
        if not any(route.get(k) for k in ('requests','commands','evidence')):
            gap('Plan could not construct a finite route with existing mechanisms')
        for key in ('requests','commands','evidence'):
            require(isinstance(route.get(key, []), list) and len(route.get(key, [])) <= 64,
                    'route must contain finite '+key)
        if route.get('evidence'):
            evidence_valid(route['evidence'])
        for command in route.get('commands', []):
            require(isinstance(command, list) and command and all(isinstance(x, str) and x for x in command),
                    'command must be a nonempty argv list')
            if Path(command[0]).name in ('env','sudo','xargs','osascript','open','shortcuts'):
                gap('Use a direct existing project check; native Office actions require Office Bridge')
            executable = shutil.which(command[0])
            if not executable:gap('Existing command unavailable: '+command[0])
            # A named interpreter does not establish that a hypothetical script exists.
            if re.fullmatch(r'(python[0-9.]*|bash|sh|zsh|node)', Path(command[0]).name):
                if len(command) < 2 or command[1].startswith('-'):
                    gap('Use an existing project check script, not inline or hypothetical verification code')
                if not (root/command[1]).is_file():gap('Existing project check script unavailable: '+command[1])
        for request in route.get('requests', []):
            require(isinstance(request, dict), 'Office request must be an object')
            app = request.get('app')
            if app not in office.capabilities(root):gap('Office Bridge application is not enabled: '+str(app))
            operation = request.get('operation', 'capture')
            if operation not in ('capture', 'navigation'):gap('Unsupported Office Bridge operation: '+str(operation))
            require(not any(k in request for k in ('request_id','recovery_id','replaces_request_id')),
                    'Plan requests cannot reuse execution identities')
            source = (root/str(request.get('source', ''))).resolve()
            require(source.is_file() and source.suffix.lower() == office.EXTENSIONS[app], 'invalid Office source')
            require(source.is_relative_to(root) or source.is_relative_to(acdir()), 'Office source outside project')
            if request.get('source_sha256'):
                require(digest(source.read_bytes()) == request['source_sha256'], 'Office source changed')
            office.MacOffice.validate_request(app, request)
            if operation == 'navigation':
                compiled = office.navigation_request(request.get('requirement', {}))
                if compiled is None:gap('Unsupported Office navigation operation')
                require(app == 'excel' and Path(compiled['source']).resolve() == source,
                        'navigation source binding differs')
                require(compiled['source_sha256'] == digest(source.read_bytes()), 'navigation source changed')
    return routes


def validate_external_blocker(r, text):
    """BLOCKED needs a checked dependency record, never a capability guess.

    Producers may attach external_dependency to a factual JSON artifact. Review
    references its bytes; it cannot manufacture the record in its own response.
    Generic failures and optimistic capability labels deliberately have no adapter.
    """
    b = r.get('external_blocker')
    require(isinstance(b,dict), 'BLOCKED requires artifact-bound external dependency evidence; use PROBLEMS for unattempted work or bounded diagnosis')
    for key in ('fact','route','action'):
        require(isinstance(b.get(key),str) and b[key].strip() not in ('','NONE'),
                'external blocker requires specific '+key)
    refs = evidence_valid(b.get('evidence',[]))
    require(refs, 'BLOCKED requires artifact-bound external dependency evidence')
    for ref in refs:
        try:
            receipt = json.loads(Path(ref['path']).read_text())
        except (ValueError,UnicodeError):
            raise ValueError('external dependency evidence must be a structured factual receipt') from None
        require(isinstance(receipt,dict), 'invalid external dependency receipt')
        dependency = receipt.get('external_dependency')
        require(isinstance(dependency,dict) and
                all(dependency.get(k)==b[k] for k in ('fact','route','action')) and
                dependency.get('kind') in ('permission','access','external_artifact','external_approval') and
                dependency.get('check') in ('attempted','deterministic'),
                'receipt does not establish the claimed external action; use PROBLEMS for ambiguous or repairable failure')
        require(receipt.get('reviewed_head')==git('rev-parse','HEAD'),
                'external dependency receipt belongs to a different checkpoint')
        # Inputs bind the scope of the check (e.g. source document, request or
        # authoritative specification identifying the unavailable artifact).
        require(evidence_valid(dependency.get('inputs',[])),
                'external dependency check requires hash-bound inputs')
    lines=text.splitlines()
    for field in ('HUMAN_ACTION','NEXT_STEP'):
        require([line[len(field)+2:] for line in lines if line.startswith(field+': ')]==[b['action']],
                field+' must report the exact evidenced external action')


def review_report(s, text, batch=None):
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
        require(a['kind']=='planning' or r['outcome']!='UNASSESSED','checkpointed attempt requires a progress assessment')
    else:
        require(r['outcome']=='UNASSESSED','no checkpointed attempt exists to assess')
    if w:
        # Wording can drift, but the ledger always retains the original identity.
        r['finding_key']=w['finding_key']
    original = parent_completion(s)
    if original and w and not w.get('complete'):
        require(completion(Path('IMPLEMENTATION.md').read_text())==original, 'current plan changed the parent Completion')
    checked = review_evidence(s, r.get('evidence',[]))
    if r['outcome'] in ('VERIFIED','COMPLETE'):
        require(checked, 'verified progress requires concrete, hash-bound evidence')
    lines = text.splitlines()
    status = next((x[15:] for x in lines if x.startswith('REVIEW_STATUS: ')), '')
    direction = instruction_direction(r, text, batch if batch is not None else os.environ.get('AUTOCYCLE_INPUT_BATCH',''))
    reviewed_deferral(s, r, direction)
    if r['blocking']:
        require(status in ('PROBLEMS','BLOCKED'), 'blocking finding requires PROBLEMS or BLOCKED')
        keys = [x[13:].strip() for x in lines if x.startswith('BLOCKER_KEY: ')]
        require(len(keys)==1 and keys[0] not in ('','NONE'), 'blocking finding lacks stable blocker key')
    require(status!='DONE' or not a or r['outcome']=='COMPLETE' or Path('SESSION.md').exists(),
            'Legacy DONE requires verified completion of the current work')
    if status=='DONE':
        require(not git('status','--porcelain') and git('rev-parse','HEAD')==git('rev-parse','origin/'+git('branch','--show-current')), 'Completed Review requires clean, published checkpointed state')
    require(status!='BLOCKED' or r['blocking'], 'BLOCKED requires a blocking consequence')
    require(not unresolved_facts(r) or (status in ('PROBLEMS','BLOCKED') and r['outcome'] not in ('VERIFIED','COMPLETE')),
            'missing required facts require PROBLEMS or evidenced BLOCKED without claiming Completion')
    require(not unresolved_facts(r) or r.get('endpoint',{}).get('status')!='REACHED',
            'missing required facts cannot establish the Session Endpoint')
    if status=='BLOCKED':validate_external_blocker(r, text)
    if Path('SESSION.md').exists() and os.environ.get('AUTOCYCLE_NEW_SESSION')!='1':
        session_sections(Path('SESSION.md').read_text())
        endpoint = r.get('endpoint', {})
        require(endpoint.get('status') in ('REACHED','UNREACHED'), 'Review must assess the Session Endpoint')
        require(endpoint.get('session_sha256')==digest(Path('SESSION.md').read_bytes()), 'Endpoint assessment is stale')
        if endpoint['status']=='REACHED':
            require(not original or not w or w.get('complete') or r['outcome']=='COMPLETE', 'close the major Step Completion before claiming the Session Endpoint')
            require(review_evidence(s, endpoint.get('evidence',[])), 'Endpoint completion requires verified evidence')
            require(status=='DONE' or 'INPUT_STATUS: PENDING' in lines,
                    'Reached Endpoint must stop unless frozen instructions still need incorporation')
        require(status!='DONE' or endpoint['status']=='REACHED', 'DONE requires the Session Endpoint')
    if os.environ.get('AUTOCYCLE_NEW_SESSION')=='1':
        require(status!='DONE', 'Fresh session must establish its Endpoint before completion')
    validate_observations(s, r, text, checked)
    validate_missing_evidence(s, r, checked)
    return r, checked


def session_sections(text):
    sections = {}
    for name in ('Endpoint', 'Priority'):
        match = re.search(r'^#{1,2} '+name+r'\s*\n(.*?)(?=^#{1,2} |\Z)', text, re.M | re.S)
        require(match and match.group(1).strip(), 'SESSION.md needs a nonempty '+name)
        sections[name] = match.group(1).strip()
    return sections


def instruction_direction(review, review_text, batch):
    # Independently satisfied inputs are archived by the controller before Plan.
    # Their accepted Review grants no authority for new document changes.
    if 'INPUT_STATUS: COMPLETE' in review_text.splitlines():
        return {}
    direction = review.get('direction', {})
    require(isinstance(direction, dict), 'instruction direction must be an object')
    from adjudication import input_context
    allowed = set(input_context(batch)['awaiting_ids'])
    for level, ids in direction.items():
        require(level in ('target','endpoint','priority','implementation') and isinstance(ids,list)
                and set(ids) <= allowed, 'invalid instruction direction binding')
    classified = {ident for ids in direction.values() for ident in ids}
    require(classified==allowed, 'Review must classify every undelivered instruction before Plan')
    return direction


def reviewed_deferral(s, review, direction):
    defer = review.get('defer_work')
    if not defer:
        return None
    w = s['work']
    require(isinstance(defer,dict) and w and not w.get('complete')
            and not review.get('blocking') and review.get('outcome')!='COMPLETE'
            and defer.get('work_id')==w['id'], 'only unresolved nonblocking work can be deferred')
    authorized = set().union(*(set(direction.get(level, [])) for level in ('target','endpoint','priority')))
    ids = defer.get('instruction_ids', [])
    require(isinstance(ids,list) and ((bool(ids) and set(ids)<=authorized) or
            (not ids and review.get('outcome') in ('NONE','VERIFIED'))),
            'deferral requires a reviewed progress assessment and an inappropriate goal, or an explicit direction change')
    require(isinstance(defer.get('reason'),str) and defer['reason'].strip(), 'deferral needs its Endpoint relevance reason')
    return {'work_id':w['id'],'instruction_ids':ids,'reason':defer['reason']}


def plan_documents(answer, plan, cache, batch):
    """Publish only document edits authorized by the frozen Review instructions."""
    text = answer.read_text()
    review_text = cache.read_text()
    review = record(review_text, 'AUTOCYCLE_REVIEW')
    direction = instruction_direction(review, review_text, batch)
    for name in ('TARGET','SESSION'):
        output = plan.with_name(plan.name+'.'+name)
        output.unlink(missing_ok=True)
        begin, end = 'BEGIN_'+name+'_MD', 'END_'+name+'_MD'
        lines = text.splitlines()
        if begin not in lines and end not in lines:
            continue
        require(lines.count(begin)==lines.count(end)==1 and lines.index(begin)<lines.index(end), 'invalid document markers')
        body = '\n'.join(lines[lines.index(begin)+1:lines.index(end)]).rstrip()+'\n'
        require(body.strip(), 'empty planning document')
        current = Path(name+'.md')
        require(not current.is_symlink() and (not current.exists() or current.is_file()), 'planning documents must be regular files')
        old = current.read_text() if current.exists() else ''
        if name=='TARGET':
            require(direction.get('target'), 'Target change requires explicit reviewed instruction')
        else:
            marker = re.search(r'^Session: (.+)$', body, re.M)
            require(not marker or marker.group(1)==os.environ.get('AUTOCYCLE_SESSION_NUMBER','1'), 'incorrect Session number in SESSION.md')
            new_sections = session_sections(body)
            old_sections = session_sections(old) if old else {}
            for section, value in new_sections.items():
                require(os.environ.get('AUTOCYCLE_NEW_SESSION')=='1' or value==old_sections.get(section) or direction.get(section.lower()) or ((not old or os.environ.get('AUTOCYCLE_NEW_SESSION')=='1') and section=='Priority' and direction.get('endpoint')),
                        section+' change requires explicit reviewed instruction')
        output.write_text(body)
    for level, name in (('target','TARGET'),('endpoint','SESSION'),('priority','SESSION')):
        require(not direction.get(level) or plan.with_name(plan.name+'.'+name).exists(),
                'Instructed '+level+' must persist in '+name+'.md')
    if os.environ.get('AUTOCYCLE_NEW_SESSION')=='1':
        require(plan.with_name(plan.name+'.SESSION').exists(),
                'Fresh session needs a SESSION.md Endpoint and Priority derived from TARGET and current state')


def evaluate(s, path, batch):
    code,output=_evaluate(s,path,batch)
    r=record(path.read_text(),'AUTOCYCLE_REVIEW')
    update_missing_evidence(s,path,r,evidence_valid(r.get('evidence',[])))
    record_office_review(s, path, r)
    return code,output


def record_office_review(s, path, r):
    session = {'branch':git('branch','--show-current'),
               'number':os.environ.get('AUTOCYCLE_SESSION_NUMBER','1')}
    previous = s.get('office_retention', {})
    accepted = previous.get('accepted', []) if previous.get('session') == session else []
    facts = (list(r.get('evidence', [])) if r.get('outcome') in ('VERIFIED','COMPLETE') else [])
    facts += r.get('endpoint', {}).get('evidence', [])
    accepted = list({encoded(e):e for e in accepted + facts}.values())
    s['office_retention'] = {'session':session, 'review_sha256':digest(path.read_bytes()),
                             'admitted_ns':time.time_ns(), 'accepted':accepted}


def _evaluate(s, path, batch):
    text=path.read_text()
    r, checked = review_report(s, text, batch)
    endpoint_done = (Path('SESSION.md').exists() and 'REVIEW_STATUS: DONE' in text.splitlines()
                     and r.get('endpoint',{}).get('status')=='REACHED')
    token = next((x[14:] for x in text.splitlines() if x.startswith('REVIEW_TOKEN: ')), '')
    require(token, 'cached Review is missing its unique decision token')
    a, w = latest(s), s['work']
    # Content novelty is independent of path names, array order and explanatory prose.
    evidence_key=digest(encoded(sorted({e['sha256'] for e in checked})).encode())
    content_hashes={e['sha256'] for e in checked}
    seen=set((w or {}).get('evidence_history',[]))
    block=s['block']
    s.pop('legacy_observation_review', None)
    if a and a['kind']=='planning':
        # A retained Plan candidate is not an implementation result. Its current
        # obstacle is adjudicated by REVIEW_STATUS, not missing-progress retries.
        # Older controllers could persist a progress block against this candidate.
        # Retire that assessment only after a new, validated Review of the same work.
        if block and block.get('attempt_id')==a['id']:
            if token==block.get('review_token'):
                return 4, 'Planning candidate requires current Review'
            s.setdefault('recoveries',[]).append({'block':block,'review_token':token,
                'reason':'Planning candidate has no implementation progress to assess'})
            s['block']=None
            block=None
        if not block:
            a.update(review_token=token, outcome='UNASSESSED', report=r, rechecks=0)
            return 0, 'Planning candidate assessed by Review'
    if block and block['reason']=='REPEATED_NO_PROGRESS':
        # Retire only the old numerical stall rule; retain its evidence.
        s.setdefault('recoveries',[]).append({'block':block,'review_token':token,'reason':'Review now judges whether to continue or redirect'})
        s['block']=None; block=None
    if (block and a and block.get('attempt_id')==a['id']
            and a.get('outcome')=='UNKNOWN' and token!=block.get('review_token')
            and r.get('recovery','NONE')=='NONE' and not r.get('defer_work')):
        # A new validated observation can determine previously unknown progress.
        # Re-run the assessment below; continued uncertainty records a new block.
        # Deferring the unresolved objective still requires the existing recovery.
        s.setdefault('recoveries',[]).append({'block':block,'review_token':token,
            'reason':'Reassessing unknown progress from current Review'})
        s['block']=None; block=None
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
        s['block']=None
        if a:
            a.update(review_token=token, outcome='RECOVERED' if revised else r['outcome'], report=r, evidence_key=evidence_key, rechecks=0)
        if evidenced and w:
            w['evidence_history']=sorted(seen|content_hashes)
            w['last_verified_evidence']=evidence_key
            if r['outcome']=='COMPLETE':
                w['complete']=True
                for allocation in s['allocated'].values():
                    if allocation.get('work_id')==w['id']:allocation['status']='completed'
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
            a.update(outcome=outcome, review_token=token, report=r, evidence_key=evidence_key,
                     review_status=next((line[15:] for line in text.splitlines() if line.startswith('REVIEW_STATUS: ')),''))
            if outcome in ('VERIFIED','COMPLETE'):
                w['last_verified_evidence']=evidence_key
                w['evidence_history']=sorted(seen|content_hashes)
                if outcome=='COMPLETE':
                    w['complete']=True
                    for allocation in s['allocated'].values():
                        if allocation.get('work_id')==w['id']:allocation['status']='completed'
            elif outcome=='UNKNOWN':
                a['rechecks']+=1
        if a.get('review_status')=='PROBLEMS' and a['outcome']=='UNKNOWN':
            return 0, 'Unresolved evidence requires bounded implementation or diagnosis'
        if a['outcome']=='UNKNOWN' and a['rechecks']==1 and not endpoint_done:
            return 4, 'Missing progress evidence; one read-only verification retry'
        reason = 'PROGRESS_UNKNOWN' if a['outcome']=='UNKNOWN' else ''
        if reason and not endpoint_done:
            s['block']={'reason':reason,'work_id':w['id'],'attempt_id':a['id'],
                        'instruction_ids':input_ids(batch),'evidence_key':evidence_key,'review_token':token}
            return 2, reason
    return 0, 'Progress assessed'


def main():
    cmd,*args=sys.argv[1:]
    code=0; output=None
    with state() as s:
        if cmd=='migrate-observations': code,output=migrate_observations(s,Path(args[0]))
        elif cmd=='resume-missing-evidence': code,output=resume_missing_evidence(s,Path(args[0]))
        elif cmd=='missing-requests': output=encoded(missing_requests(s))
        elif cmd=='observation-requests': output=encoded(observation_ids(s))
        elif cmd=='context': output=encoded(context(s,args[0] if args else ''))
        elif cmd=='binding': output=digest(encoded(binding(s)).encode())
        elif cmd=='prepare':
            try: prepare(s,Path(args[0]),Path(args[1]) if len(args)>1 else None)
            except VerificationCapabilityGap as error:
                s['verification_capability_gap'] = json.loads(str(error))
                code,output = 78, 'Verification-capability gap: '+str(error)
        elif cmd=='plan-unavailable':
            r = record(Path(args[1]).read_text(), 'AUTOCYCLE_REVIEW')
            facts = unresolved_facts(r)
            if not facts:
                code = 3  # Ordinary non-evidence planning candidate.
            else:
                reason = next(line[9:] for line in Path(args[0]).read_text().splitlines() if line.startswith('BLOCKER: '))
                routes = [{'fact':fact['fact'], 'missing_capability':reason} for fact in facts]
                try: validate_evidence_routes({'evidence_routes':routes}, r)
                except VerificationCapabilityGap as error:
                    s['verification_capability_gap'] = json.loads(str(error))
                    code,output = 78, 'Verification-capability gap: '+str(error)
        elif cmd=='begin': begin(s,args[0],args[1] if len(args)>1 else '')
        elif cmd=='recover': recover(s,*args)
        elif cmd=='checkpoint': checkpoint(s,*args)
        elif cmd=='admit-office-review':
            report, _ = review_report(s,Path(args[0]).read_text())
            record_office_review(s,Path(args[0]),report)
        elif cmd=='validate-review':
            report, _ = review_report(s,Path(args[0]).read_text())
            if report['outcome']=='UNASSESSED':
                output='Progress: no checkpointed attempt exists to assess.'
        elif cmd=='guard': code,output=evaluate(s,Path(args[0]),args[1] if len(args)>1 else '')
        elif cmd=='plan-documents': plan_documents(Path(args[0]),Path(args[1]),Path(args[2]),args[3])
        elif cmd=='new-session': new_session(s,*args)
        elif cmd=='endpoint-reached': code=0 if record(Path(args[0]).read_text(),'AUTOCYCLE_REVIEW').get('endpoint',{}).get('status')=='REACHED' else 1
        elif cmd=='step-summary': output=s['work']['step_id'] if s['work'] else ''
        elif cmd=='plan-action': output='Continuing' if s['work'] and not s['work'].get('complete') and record(Path(args[0]).read_text() if Path(args[0]).is_file() else plan_text(args[0]),'AUTOCYCLE_PLAN')['work_id']==s['work']['id'] else 'Planned'
        elif cmd=='blocked': code=0 if s['block'] or s.get('missing_evidence') else 1
        else: raise ValueError('unknown progress operation')
    if output is not None: print(output)
    return code


if __name__=='__main__':
    try: sys.exit(main())
    except (Exception,KeyboardInterrupt) as error:
        print('Progress: '+str(error),file=sys.stderr); sys.exit(1)

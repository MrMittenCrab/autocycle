#!/usr/bin/env python3
"""Versioned conservative migration. Only run-state is changed; evidence is retained."""
import os, shlex, sys, subprocess, hashlib, json
from pathlib import Path

# Evidence collection must not refresh the caller's Git index.
os.environ['GIT_OPTIONAL_LOCKS']='0'

FIELDS={'STATE_VERSION','NETWORK_RETRIES','CANDIDATE_RESOLVED','RUN_MAX','RUN_CYCLE','STAGE','PLAN_SHA','IMPLEMENT_BASE_SHA','STATE_BRANCH','INPUT_CYCLE_ID','INPUT_ALLOW_NEW','NEXT_INPUT_ID','CHECKPOINT_UNSAFE','BOUNDARY_KIND'}

def migrate(path):
    raw=path.read_text(); state={}
    for line in raw.splitlines():
        if not line.strip():continue
        key,sep,value=line.partition('=')
        if not sep or key not in FIELDS:raise ValueError('unrecognized saved state field')
        values=shlex.split(value)
        if len(values)>1:raise ValueError('invalid saved state value')
        if key in state:raise ValueError('duplicate saved state field')
        state[key]=values[0] if values else ''
    required={'STATE_VERSION','RUN_CYCLE','RUN_MAX','STAGE','STATE_BRANCH'}
    if not required<=state.keys():raise ValueError('missing saved state fields')
    cycle=int(state['RUN_CYCLE']);maximum=int(state['RUN_MAX'])
    if not 1<=cycle<=maximum:raise ValueError('invalid saved cycle budget')
    stages={'start','reviewing','review_done','planning','plan_done','implementing','implement_done','implement_nochange','candidate_checkpoint','checkpoint_done','checkpoint_pending','boundary_input'}
    if state.get('STATE_VERSION')=='2':
        if state['STAGE'] not in stages:raise ValueError('unknown saved stage')
        if state['STAGE']=='boundary_input' and not state.get('NEXT_INPUT_ID'):raise ValueError('missing next input identity')
        return state
    if state.get('STATE_VERSION')!='1':raise ValueError('unsupported state version')
    cycle=int(state['RUN_CYCLE']);maximum=int(state['RUN_MAX'])
    if not 1<=cycle<=maximum:raise ValueError('invalid saved cycle budget')
    old=state['STAGE']
    def git(*args):return subprocess.check_output(['git',*args],stderr=subprocess.PIPE).decode().strip()
    actual_branch=git('branch','--show-current')
    if state.get('STATE_BRANCH') and state['STATE_BRANCH']!=actual_branch:
        raise ValueError('saved branch differs from actual branch; state preserved')
    actual_head=git('rev-parse','HEAD')
    baseline=state.get('IMPLEMENT_BASE_SHA') or state.get('PLAN_SHA')
    ancestor=False
    if baseline:
        ancestor=subprocess.run(['git','merge-base','--is-ancestor',baseline,actual_head],capture_output=True).returncode==0
    audit={'version':2,'source_digest':hashlib.sha256(raw.encode()).hexdigest(),
           'legacy_stage':old,'cycle':cycle,'budget':maximum,'branch':actual_branch,
           'head':actual_head,'baseline':baseline,'baseline_is_ancestor':ancestor,
           'dirty':bool(git('status','--porcelain')),'old_review_reusable':False}
    # A durable checkpoint stage records work, never acceptance. Unverifiable
    # ancestry is retained for read-only diagnosis; no implementation is replayed.
    if baseline and not ancestor:state['CHECKPOINT_UNSAFE']='1'
    if old in ('checkpoint_done','cycle_archiving','adjudicating','adjudicated'):
        # These v1 states are reached only after implementation/checkpoint or a
        # planning candidate. Saved answers are not v2 opening Review decisions.
        state['STAGE']='checkpoint_done'
        state['CHECKPOINT_UNSAFE']='1'
        if baseline:state['IMPLEMENT_BASE_SHA']=baseline
    elif old=='review_done':
        # v1 repair paths may already have incremented; never increment again.
        state['STAGE']='start'
    elif old not in ('start','reviewing','planning','plan_done','implementing','implement_done','implement_nochange','candidate_checkpoint'):
        raise ValueError('unknown legacy stage')
    # Existing v1 implement_done can include a commit whose push was interrupted.
    if old=='implement_done':
        head=subprocess.check_output(['git','rev-parse','HEAD']).decode().strip()
        baseline=state.get('IMPLEMENT_BASE_SHA') or state.get('PLAN_SHA')
        if baseline and head!=baseline:
            state['STAGE']='checkpoint_done';state['CHECKPOINT_UNSAFE']='1'
    if old in ('adjudicating','adjudicated'):
        candidate=path.parent/'candidate.json'
        if candidate.exists() and json.loads(candidate.read_text()).get('kind')=='planning':
            state['BOUNDARY_KIND']='planning'
    state['STATE_VERSION']='2'
    backup=path.with_name(path.name+'.v1.backup')
    if not backup.exists():
        with backup.open('x') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    audit['migrated_stage']=state['STAGE']
    audit_path=path.with_name(path.name+'.migration-v2.json')
    if not audit_path.exists():
        with audit_path.open('x') as f:json.dump(audit,f,sort_keys=True);f.flush();os.fsync(f.fileno())
    tmp=path.with_name(path.name+'.migrate.tmp')
    with tmp.open('w') as f:
        for k,v in state.items():f.write(k+'='+shlex.quote(v)+'\n')
        f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return state

if __name__=='__main__':
    try:migrate(Path(sys.argv[1]))
    except Exception as e:print('Migration: '+str(e),file=sys.stderr);sys.exit(1)

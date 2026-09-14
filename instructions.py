#!/usr/bin/env python3
"""Durable autocycle inputs. SQLite is authoritative; no instruction text is executed."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE).decode().strip()


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


class Queue:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.directory / 'instructions.sqlite3'), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        require(version in (0, 1), 'unsupported instruction database version')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS batches (
          id TEXT PRIMARY KEY, branch TEXT NOT NULL, base_sha TEXT NOT NULL,
          phase TEXT NOT NULL CHECK(phase IN ('active','archived')),
          plan_sha TEXT, checkpoint_sha TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_batch ON batches(phase) WHERE phase='active';
        CREATE TABLE IF NOT EXISTS instructions (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, created TEXT NOT NULL,
          text TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','active','archived','cancelled')),
          batch TEXT REFERENCES batches(id));
        PRAGMA user_version=1;
        ''')

    def transaction(self, action):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = action()
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def batch(self, ident):
        row = self.db.execute('SELECT * FROM batches WHERE id=?', (ident,)).fetchone()
        require(row is not None, 'missing frozen instruction batch; refusing to select new input')
        return row

    def items(self, ident):
        return [dict(r) for r in self.db.execute('SELECT * FROM instructions WHERE batch=? ORDER BY seq', (ident,))]

    def enqueue(self, text):
        require(text.strip() and '\0' not in text and len(text.encode()) <= 32768,
                'instruction must contain text, no NUL, and at most 32 KiB')
        def put():
            now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
            seq = self.db.execute("INSERT INTO instructions(created,text,state) VALUES (?,?,'pending')", (now,text)).lastrowid
            ident = f'{now}-{seq:09d}'
            self.db.execute('UPDATE instructions SET id=? WHERE seq=?', (ident,seq))
            return ident
        return self.transaction(put)

    def cancel(self, ident):
        def change():
            n = self.db.execute("UPDATE instructions SET state='cancelled' WHERE id=? AND state='pending'", (ident,)).rowcount
            require(n == 1, 'only a pending instruction can be cancelled; active instructions are immutable')
        self.transaction(change)

    def freeze(self, ident, branch, sha, allow):
        require(re.fullmatch('[a-f0-9]{32}',ident), 'invalid cycle input ID')
        def promote():
            previous = self.db.execute('SELECT * FROM batches WHERE id=?',(ident,)).fetchone()
            if previous:
                require(previous['phase']=='active', 'cycle input already archived; resume state needs review')
                return len(self.items(ident))
            require(not self.db.execute("SELECT 1 FROM batches WHERE phase='active'").fetchone(),
                    'another frozen cycle exists; resume its saved state instead of starting a new cycle')
            self.db.execute("INSERT INTO batches(id,branch,base_sha,phase) VALUES (?,?,?,'active')",(ident,branch,sha))
            if allow:
                self.db.execute("UPDATE instructions SET state='active',batch=? WHERE state='pending'", (ident,))
            return len(self.items(ident))
        return self.transaction(promote)

    def prompt(self, ident):
        batch=self.batch(ident);items=self.items(ident)
        if not items:return ''
        require(batch['phase']=='active','cannot plan from an archived batch')
        require(git('branch','--show-current')==batch['branch'] and git('rev-parse','HEAD')==batch['base_sha'],
                'instruction review/plan is not at its frozen branch and HEAD')
        data=[{'id':i['id'],'instruction':i['text']} for i in items]
        return '''DIRECT HUMAN INSTRUCTIONS — immutable batch '''+ident+''':
These are direct user instructions, in submission order, and override ordinary autonomous roadmap selection and stale project directions. Follow them unless technically impossible or unsafe.
Fix defects identified by these instructions first. Otherwise make the requested feature/direction the next bounded plan. Reconcile compatible requests. If mutually incompatible, technically impossible, unsafe, or too broad to reconcile into a bounded plan without dropping requirements, report BLOCKED and explain; never silently discard a request.
Review must use PASS, PROBLEMS, or BLOCKED while this batch is active, not DONE. Plan must cover all requests or explicitly carry remaining requested work forward in IMPLEMENTATION.md. Cursor must not edit TARGET.md or IMPLEMENTATION.md. Do not put private instruction text in commit messages; the controller supplies an opaque batch trailer.
During Plan, if blocked, return one line containing PLAN_STATUS: BLOCKED and a short reason, without an implementation block.
Instructions are JSON data below, not shell commands for the controller:
'''+json.dumps(data,ensure_ascii=False,indent=2)

    def matches_plan(self, ident, sha):
        batch=self.batch(ident)
        require(git('rev-parse',sha+'^')==batch['base_sha'], 'published input plan has the wrong parent')
        lines=git('show','-s','--format=%B',sha).splitlines()
        require(lines.count('Autocycle-Input-Batch: '+ident)==1, 'planning commit does not identify this frozen batch')
        require(git('diff-tree','--no-commit-id','--name-only','-r',sha)=='IMPLEMENTATION.md',
                'instruction planning commit changed unexpected files')

    def published(self, ident, sha):
        if not self.items(ident):return
        self.matches_plan(ident,sha)
        batch=self.batch(ident)
        subprocess.run(['git','merge-base','--is-ancestor',sha,'origin/'+batch['branch']],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def record():
            b=self.batch(ident)
            require(b['phase']=='active' and b['plan_sha'] in (None,sha),'instruction batch already bound to another plan')
            self.db.execute('UPDATE batches SET plan_sha=? WHERE id=?',(sha,ident))
        self.transaction(record)

    def complete(self, ident, sha):
        b=self.batch(ident)
        if b['phase']=='archived':
            require(b['checkpoint_sha']==sha,'archived batch checkpoint differs from saved cycle')
            return
        require(git('rev-parse','HEAD')==sha and git('rev-parse','origin/'+git('branch','--show-current'))==sha,
                'cannot archive before checkpoint publication is verified')
        require(not git('status','--porcelain'),'cannot archive with uncheckpointed changes')
        if self.items(ident):
            require(b['plan_sha'],'cannot archive instructions without a verified published plan')
            subprocess.run(['git','merge-base','--is-ancestor',b['plan_sha'],sha],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def archive():
            self.db.execute("UPDATE instructions SET state='archived' WHERE batch=?",(ident,))
            self.db.execute("UPDATE batches SET phase='archived',checkpoint_sha=? WHERE id=?",(sha,ident))
        self.transaction(archive)

    def close_empty(self, ident):
        def close():
            b=self.batch(ident)
            if b['phase']=='archived':return
            require(not self.items(ident),'active instructions cannot be discarded by clearing resume state')
            self.db.execute("UPDATE batches SET phase='archived' WHERE id=?",(ident,))
        self.transaction(close)

    def listing(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM instructions WHERE state IN ('pending','active') ORDER BY seq")]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dir')
    parser.add_argument('command')
    parser.add_argument('args',nargs='*')
    a=parser.parse_args()
    if a.command=='lock':
        try:fcntl.flock(int(a.args[0]),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('another autocycle controller is still running in this repository')
        return
    if a.command=='new-id':print(uuid.uuid4().hex);return
    directory=Path(a.dir) if a.dir else Path(git('rev-parse','--git-path','autocycle')).resolve()
    q=Queue(directory)
    try:
        c=a.command;v=a.args
        if c=='--instruct':
            require(len(v)==1,'usage: autocycle --instruct "text"')
            print('Queued      ✓ '+q.enqueue(v[0]))
        elif c=='--instructions':
            require(not v,'usage: autocycle --instructions')
            rows=q.listing()
            if not rows:print('No pending or active instructions.')
            for row in rows:print(f"{row['state'].capitalize():<11} {row['id']}  {json.dumps(row['text'],ensure_ascii=False)}")
        elif c=='--cancel-instruction':
            require(len(v)==1,'usage: autocycle --cancel-instruction <id>')
            q.cancel(v[0]);print('Cancelled   ✓ '+v[0])
        elif c=='freeze':print(q.freeze(v[0],v[1],v[2],v[3]=='1'))
        elif c=='prompt':print(q.prompt(v[0]),end='')
        elif c=='matches-plan':q.matches_plan(v[0],v[1])
        elif c=='published':q.published(v[0],v[1])
        elif c=='complete':q.complete(v[0],v[1])
        elif c=='close-empty':q.close_empty(v[0])
        elif c=='guard-manual':
            require(not q.db.execute("SELECT 1 FROM instructions WHERE state='active'").fetchone(), 'manual stages cannot replace an active instruction cycle')
        else:raise RuntimeError('unknown queue operation')
    finally:q.db.close()

if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as e:
        print('Input blocked: '+str(e),file=sys.stderr)
        sys.exit(2)

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
        with (self.directory/'input-migration.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            self.initialize()

    def initialize(self):
        self.db = sqlite3.connect(str(self.directory / 'instructions.sqlite3'), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        require(version in (0, 1, 2, 3), 'unsupported instruction database version')
        if version in (1, 2):
            backup=self.directory/('instructions.v'+str(version)+'.backup.sqlite3')
            if not backup.exists():
                temp=backup.with_suffix('.tmp')
                dest=sqlite3.connect(temp)
                try:self.db.backup(dest)
                finally:dest.close()
                with temp.open('rb') as f:os.fsync(f.fileno())
                os.replace(temp,backup)
        self.db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS batches (
          id TEXT PRIMARY KEY, branch TEXT NOT NULL, base_sha TEXT NOT NULL,
          phase TEXT NOT NULL CHECK(phase IN ('active','archived')),
          plan_sha TEXT, checkpoint_sha TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_batch ON batches(phase) WHERE phase='active';
        CREATE TABLE IF NOT EXISTS instructions (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, created TEXT NOT NULL,
          text TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','active','archived','cancelled')),
          batch TEXT REFERENCES batches(id));
        CREATE TABLE IF NOT EXISTS batch_members (
          batch TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(batch,seq));
        INSERT OR IGNORE INTO batch_members SELECT batch,seq FROM instructions WHERE batch IS NOT NULL;
        CREATE TABLE IF NOT EXISTS deliveries (
          seq INTEGER PRIMARY KEY, batch TEXT NOT NULL, plan_sha TEXT NOT NULL,
          commitment TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS input_notices (seq INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS legacy_batches (batch TEXT PRIMARY KEY);
        COMMIT;
        ''')
        if version in (1, 2):
            self.transaction(lambda: (
                self.db.execute('INSERT OR IGNORE INTO legacy_batches SELECT id FROM batches'),
                self.db.execute('INSERT OR IGNORE INTO input_notices SELECT seq FROM batch_members'),
                self.db.execute('PRAGMA user_version=3')))
        elif version == 0:
            self.db.execute('PRAGMA user_version=3')

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
        return [dict(r) for r in self.db.execute('SELECT i.* FROM instructions i JOIN batch_members m ON i.seq=m.seq WHERE m.batch=? ORDER BY i.seq', (ident,))]

    def awaiting(self, ident):
        return [i for i in self.items(ident) if i['state']=='active' and i['batch']==ident]

    def notice(self, ident):
        def record():
            count=0
            for item in self.awaiting(ident):
                count+=self.db.execute('INSERT OR IGNORE INTO input_notices VALUES (?)',(item['seq'],)).rowcount
            return count
        return self.transaction(record)

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
                require(previous['branch']==branch, 'frozen input branch mismatch')
                return len(self.items(ident))
            require(not self.db.execute("SELECT 1 FROM batches WHERE phase='active'").fetchone(),
                    'another frozen cycle exists; resume its saved state instead of starting a new cycle')
            self.db.execute("INSERT INTO batches(id,branch,base_sha,phase) VALUES (?,?,?,'active')",(ident,branch,sha))
            if allow:
                self.db.execute("UPDATE instructions SET state='active',batch=? WHERE state='pending'", (ident,))
            self.db.execute('INSERT OR IGNORE INTO batch_members SELECT batch,seq FROM instructions WHERE batch=?',(ident,))
            return len(self.items(ident))
        return self.transaction(promote)

    def prompt(self, ident):
        batch=self.batch(ident);items=self.awaiting(ident)
        if not self.items(ident):return ''
        require(git('branch','--show-current')==batch['branch'], 'instruction batch belongs to another branch')
        subprocess.run(['git','merge-base','--is-ancestor',batch['base_sha'],'HEAD'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        if not items:
            return ('The frozen input snapshot has already been incorporated into a published Plan or verified satisfied. '
                    'There are no undelivered instructions in this snapshot. Do not replay its requests. '
                    'Use INPUT_STATUS: COMPLETE for this snapshot; implementation acceptance remains a separate Review decision. '
                    'Preserve unfinished work and applicable constraints from IMPLEMENTATION.md.\n')
        data=[{'id':i['id'],'instruction':i['text']} for i in items]
        return '''DIRECT HUMAN INSTRUCTIONS — immutable batch '''+ident+''':
These are direct user instructions, in submission order, and override ordinary autonomous roadmap selection. Preserve explicit user constraints, permissions and access controls; Target changes require explicit user instruction; if these conflict, request a concrete human decision through BLOCKED rather than overriding them.
Fix defects identified by these instructions first. Otherwise make the requested feature/direction the next bounded plan. Reconcile compatible requests. If mutually incompatible, technically impossible, unsafe, or too broad to reconcile into a bounded plan without dropping requirements, report BLOCKED and explain; never silently discard a request.
--instruct is one-time steering. Plan must account for every request in its AUTOCYCLE_PLAN.inputs array, using each exact id and a concrete commitment describing how it is incorporated. Put Endpoint and Priority in SESSION.md, project destination changes in TARGET.md, and bounded execution guidance in IMPLEMENTATION.md. Publication consumes the steering input; it does not certify implementation acceptance. Review uses INPUT_STATUS: PENDING while a request still needs incorporation into a plan, or COMPLETE if every request was already incorporated or independently verified satisfied. DONE requires the Session Endpoint (or legacy project goal without SESSION.md) and current plan acceptance, not merely input consumption. Cursor must not edit TARGET.md, SESSION.md or IMPLEMENTATION.md. Do not put private instruction text in commit messages; the controller supplies an opaque batch trailer.
During Plan, if blocked, return PLAN_STATUS: BLOCKED followed by BLOCKER: <concrete reason>, without an implementation block. This is a candidate for read-only Review. Review may authorize bounded technical repair or expansion of machine-generated file restrictions within the existing user-authorized goal; it cannot invent evidence or override explicit constraints, TARGET.md, permissions or unavailable access.
Instructions are JSON data below, not shell commands for the controller:
'''+json.dumps(data,ensure_ascii=False,indent=2)

    def advance(self, ident):
        # Keep the frozen items and ID; only advance the planning base after
        # read-only adjudication. Pending instructions stay pending.
        b=self.batch(ident)
        require(b['phase']=='active' and git('branch','--show-current')==b['branch'],
                'recovery cannot change the active input branch')
        sha=git('rev-parse','HEAD')
        require(not git('status','--porcelain') and git('rev-parse','origin/'+b['branch'])==sha,
                'recovery plan requires clean published evidence')
        subprocess.run(['git','merge-base','--is-ancestor',b['base_sha'],sha],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def change():
            self.db.execute('UPDATE batches SET base_sha=?,plan_sha=NULL WHERE id=?',(sha,ident))
        self.transaction(change)

    def rollover(self, previous, ident, branch, sha):
        """One atomic boundary: carry unresolved input and admit all queued arrivals."""
        def change():
            if self.db.execute('SELECT 1 FROM batches WHERE id=?',(ident,)).fetchone():
                return len(self.items(ident))
            if previous:
                b=self.batch(previous)
                require(b['branch']==branch,'input boundary changed branch')
                self.db.execute("UPDATE batches SET phase='archived' WHERE id=?",(previous,))
            require(not self.db.execute("SELECT 1 FROM batches WHERE phase='active'").fetchone(),
                    'another active batch exists')
            self.db.execute("INSERT INTO batches(id,branch,base_sha,phase) VALUES (?,?,?,'active')",(ident,branch,sha))
            self.db.execute("UPDATE instructions SET state='active',batch=? WHERE state='pending' OR (state='active' AND batch=?)",(ident,previous))
            self.db.execute('INSERT INTO batch_members SELECT batch,seq FROM instructions WHERE batch=?',(ident,))
            return len(self.items(ident))
        return self.transaction(change)

    def matches_plan(self, ident, sha):
        batch=self.batch(ident)
        require(git('rev-parse',sha+'^')==batch['base_sha'], 'published input plan has the wrong parent')
        lines=git('show','-s','--format=%B',sha).splitlines()
        require(lines.count('Autocycle-Input-Batch: '+ident)==1, 'planning commit does not identify this frozen batch')
        require('IMPLEMENTATION.md' in git('diff-tree','--no-commit-id','--name-only','-r',sha).splitlines() and
                set(git('diff-tree','--no-commit-id','--name-only','-r',sha).splitlines()) <= {'IMPLEMENTATION.md','TARGET.md','SESSION.md'},
                'instruction planning commit changed unexpected files')

    def published(self, ident, sha):
        if not self.items(ident) or self.batch(ident)['phase']=='archived':return
        self.matches_plan(ident,sha)
        batch=self.batch(ident)
        subprocess.run(['git','merge-base','--is-ancestor',sha,'origin/'+batch['branch']],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        items=self.awaiting(ident)
        legacy=bool(self.db.execute('SELECT 1 FROM legacy_batches WHERE batch=?',(ident,)).fetchone())
        plan=git('show',sha+':IMPLEMENTATION.md')
        commitments=self.plan_commitments(ident,plan,allow_legacy=legacy)
        legacy_receipt=int(legacy and not self.has_input_record(plan))
        def record():
            b=self.batch(ident)
            require(b['phase']=='active' and b['plan_sha'] in (None,sha),'instruction batch already bound to another plan')
            self.db.execute('UPDATE batches SET plan_sha=? WHERE id=?',(sha,ident))
            for item in items:
                self.db.execute('INSERT INTO deliveries VALUES (?,?,?,?,?)',
                                (item['seq'],ident,sha,commitments[item['id']],legacy_receipt))
                self.db.execute("UPDATE instructions SET state='archived' WHERE seq=? AND state='active'",(item['seq'],))
        self.transaction(record)

    @staticmethod
    def has_input_record(text):
        rows=[line[len('AUTOCYCLE_PLAN: '):] for line in text.splitlines() if line.startswith('AUTOCYCLE_PLAN: ')]
        return len(rows)==1 and 'inputs' in json.loads(rows[0])

    def plan_commitments(self, ident, text, allow_legacy=False):
        items=self.awaiting(ident)
        if not items:return {}
        rows=[line[len('AUTOCYCLE_PLAN: '):] for line in text.splitlines() if line.startswith('AUTOCYCLE_PLAN: ')]
        require(len(rows)==1, 'input plan must contain one AUTOCYCLE_PLAN record')
        record=json.loads(rows[0])
        if allow_legacy and 'inputs' not in record:
            return {i['id']:'Incorporation imported from the previously validated published plan; implementation acceptance remains unresolved.' for i in items}
        entries=record.get('inputs')
        require(isinstance(entries,list) and len(entries)==len(items), 'plan must incorporate every undelivered instruction in AUTOCYCLE_PLAN.inputs')
        result={}
        for entry in entries:
            require(isinstance(entry,dict) and isinstance(entry.get('id'),str) and
                    isinstance(entry.get('commitment'),str) and entry['commitment'].strip() and
                    len(entry['commitment'])<=4000 and entry['id'] not in result,
                    'each input needs a unique id and concrete plan commitment')
            result[entry['id']]=entry['commitment']
        require(set(result)=={i['id'] for i in items}, 'plan input IDs differ from the frozen undelivered requests')
        return result

    def import_legacy(self):
        """Import only the controller's confirmed published Plan bindings, once."""
        branch=git('branch','--show-current')
        count=0
        candidates=self.db.execute('''SELECT DISTINCT b.id,b.plan_sha FROM batches b
            JOIN legacy_batches l ON l.batch=b.id JOIN batch_members m ON m.batch=b.id
            JOIN instructions i ON i.seq=m.seq
            WHERE b.branch=? AND b.plan_sha IS NOT NULL AND i.state='active' ORDER BY b.rowid''',(branch,)).fetchall()
        for old_batch,sha in candidates:
            # A published but not yet synced plan is handled by normal Plan resume.
            if subprocess.run(['git','merge-base','--is-ancestor',sha,'HEAD'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
                continue
            self.matches_plan(old_batch,sha)
            subprocess.run(['git','merge-base','--is-ancestor',sha,'origin/'+branch],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            text=git('show',sha+':IMPLEMENTATION.md')
            require(text.startswith('#'), 'legacy published plan has no heading')
            items=[i for i in self.items(old_batch) if i['state']=='active']
            def record():
                for item in items:
                    self.db.execute('INSERT INTO deliveries VALUES (?,?,?,?,1)',
                        (item['seq'],old_batch,sha,'Imported controller-verified published Plan; acceptance remains with Review.'))
                    self.db.execute("UPDATE instructions SET state='archived' WHERE seq=? AND state='active'",(item['seq'],))
            self.transaction(record)
            count+=len(items)
        return count

    def complete(self, ident, sha):
        # Delivery was already recorded by Plan; it does not wait on or certify
        # the later implementation checkpoint. Only still-active requests need
        # evidence-backed read-only fulfillment here.
        if not self.awaiting(ident):return
        b=self.batch(ident)
        if b['phase']=='archived':
            require(b['checkpoint_sha'] is not None,'retired boundary snapshot is not a completion record')
            return
        require(git('rev-parse','HEAD')==sha and git('rev-parse','origin/'+git('branch','--show-current'))==sha,
                'cannot archive before checkpoint publication is verified')
        require(not git('status','--porcelain'),'cannot archive with uncheckpointed changes')
        if self.items(ident):
            if b['plan_sha']:
                subprocess.run(['git','merge-base','--is-ancestor',b['plan_sha'],sha],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def archive():
            self.db.execute("UPDATE instructions SET state='archived' WHERE batch=?",(ident,))
            self.db.execute("UPDATE batches SET phase='archived',checkpoint_sha=? WHERE id=?",(sha,ident))
        self.transaction(archive)

    def close_empty(self, ident):
        def close():
            b=self.batch(ident)
            if b['phase']=='archived':return
            require(not self.awaiting(ident),'active instructions cannot be discarded by clearing resume state')
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
        elif c=='rollover':print(q.rollover(*v))
        elif c=='prompt':print(q.prompt(v[0]),end='')
        elif c=='notice':print(q.notice(v[0]))
        elif c=='validate-plan':q.plan_commitments(v[0],Path(v[1]).read_text())
        elif c=='import-legacy':
            count=q.import_legacy()
            if count:print('Input       ✓ '+str(count)+' previous instructions linked to published plans; no longer queued')
        elif c=='matches-plan':q.matches_plan(v[0],v[1])
        elif c=='published':q.published(v[0],v[1])
        elif c=='advance':q.advance(v[0])
        elif c=='complete':q.complete(v[0],v[1])
        elif c=='close-empty':q.close_empty(v[0])
        elif c=='pending-count':print(q.db.execute("SELECT count(*) FROM instructions WHERE state='pending'").fetchone()[0])
        elif c=='guard-manual':
            require(not (directory/'resume-state').exists(), 'manual stages cannot overwrite a saved Session; use --resume, --extend, or --restart')
            require(not q.db.execute("SELECT 1 FROM instructions WHERE state='active'").fetchone(), 'manual stages cannot replace an active instruction cycle')
        else:raise RuntimeError('unknown queue operation')
    finally:q.db.close()

if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as e:
        print('Input blocked: '+str(e),file=sys.stderr)
        sys.exit(2)

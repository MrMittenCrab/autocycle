"""Deterministic terminal frames, using simulated Office only."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_implement_display import screen
from test_native_office import FakeOffice
import native_office as office

class Tty(io.StringIO):
    def isatty(self): return True

class UnifiedUITests(unittest.TestCase):
    def test_access_single_row_in_both_modes(self):
        for stream in (Tty(), io.StringIO()):
            with tempfile.TemporaryDirectory() as directory:
                ws=office.Workspace(Path(directory),FakeOffice())
                self.assertTrue(office.preflight(ws,('word',),stream))
            lines=screen(stream.getvalue()) if stream.isatty() else stream.getvalue().splitlines()
            self.assertEqual(len(lines),1,lines)
            self.assertRegex(lines[0],r'^Access      ✓ \d+:\d+$')
            if not stream.isatty(): self.assertNotIn('\x1b',stream.getvalue())

    def test_access_failure_replaces_prompt_on_same_row(self):
        for stream in (Tty(), io.StringIO()):
            with tempfile.TemporaryDirectory() as directory:
                backend=FakeOffice();backend.denied=True
                ws=office.Workspace(Path(directory),backend,wait_seconds=.001,poll_seconds=.001)
                self.assertFalse(office.preflight(ws,('word',),stream))
            lines=screen(stream.getvalue()) if stream.isatty() else stream.getvalue().splitlines()
            self.assertEqual(len(lines),1,lines)
            self.assertRegex(lines[0],r'^Access      ✗ \d+:\d+  Word fixed-slot access failed:')
            self.assertNotIn('Grant Office access if prompted',lines[0])
            if not stream.isatty(): self.assertNotIn('\x1b',stream.getvalue())

    def test_review_and_plan_use_one_timed_header(self):
        for tty in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                script=r'''
const {StageDisplay}=require('./stage_display.js');
Object.defineProperty(process.stdout,'isTTY',{value:process.argv[2]==='true'});
let now=0, out='';
let d=new StageDisplay(process.argv[1]+'/review','Review',()=>now,s=>out+=s);
now=2000;d.tick();d.finish('✓','Finding text');
d.prune('▶','scanning');const active=out;
d.prune('✓','2 files · 100 B');const review=out;
d=new StageDisplay(process.argv[1]+'/plan','Plan',()=>now,s=>out+=s);
now=5000;d.tick();d.finish('✓','Next bounded step');
process.stdout.write(JSON.stringify({active,review,final:out}));
'''
                run=subprocess.run(['node','-e',script,directory,str(tty).lower()],capture_output=True,text=True)
                self.assertEqual(run.returncode,0,run.stderr)
                frames=json.loads(run.stdout)
                self.assertEqual(screen(frames['review']),['Review      ✓ 00:02','    ✓ Found   Finding text','    ✓ Pruned  2 files · 100 B'])
                self.assertEqual(screen(frames['final'])[-2:],['Plan        ✓ 00:03','    ✓ Planned Next bounded step'])
                if tty:self.assertEqual(screen(frames['active'])[-1],'    ▶ Prune   scanning')
                else:self.assertNotIn('\x1b',frames['final'])


class RetryUITests(unittest.TestCase):
    def test_failure_then_success_updates_tty_result(self):
        with tempfile.TemporaryDirectory() as directory:
            script=r"""
const {StageDisplay}=require('./stage_display.js');
Object.defineProperty(process.stdout,'isTTY',{value:true});
let out='', now=0, emit=s=>out+=s;
let d=new StageDisplay(process.argv[1],'Review',()=>now,emit);
d.networkStart();
now=2000;d=new StageDisplay(process.argv[1],'Review',()=>now,emit);
d.tick();d.networkEnd('✓');now+=2000;d.finish('✓','new finding');d.prune('✓','0 files · 0 B');
process.stdout.write(JSON.stringify(out));
"""
            run=subprocess.run(['node','-e',script,directory+'/state'],capture_output=True,text=True,check=True)
            self.assertEqual(screen(json.loads(run.stdout)),[
                'Review      ✓ 00:02','Network     ✓ 00:02','    ✓ Found   new finding','    ✓ Pruned  0 files · 0 B'])


class ControllerUITests(unittest.TestCase):
    def test_plain_cycle_has_exact_review_sublines_and_timed_stages(self):
        from test_flow import Case,ok
        c=Case();self.addCleanup(c.close)
        run=c.run('1');ok(run)
        self.assertNotIn('\x1b',run.stdout)
        lines=run.stdout.splitlines()
        review=next(i for i,line in enumerate(lines) if line.startswith('Review '))
        self.assertTrue(lines[review+1].startswith('    ✓ Found   '),run.stdout)
        self.assertEqual(lines[review+2],'    ✓ Pruned  0 files · 0 B',run.stdout)
        self.assertRegex(lines[review+3],r'^Plan        ✓ \d+:\d+$')
        self.assertTrue(lines[review+4].startswith('    ✓ Planned '),run.stdout)
        for label in ('Baseline','Checkpoint'):
            rows=[line for line in lines if line.startswith('    ✓ '+label+' ')]
            self.assertEqual(len(rows),1,run.stdout)
            self.assertRegex(rows[0],r'^    ✓ '+label+r' \S+')

    def test_blocked_review_keeps_action_in_found_row(self):
        from test_flow import Case
        c=Case();self.addCleanup(c.close)
        run=c.run('1',REVIEW_STATUS='BLOCKED')
        self.assertEqual(run.returncode,2,run.stdout+run.stderr)
        children=[line for line in run.stdout.splitlines() if line.startswith('    ')]
        self.assertEqual(len(children),2,run.stdout)
        self.assertTrue(children[0].startswith('    ✓ Found   Blocked:'),run.stdout)
        self.assertIn('Action: provide required human decision',children[0])
        self.assertTrue(children[1].startswith('    ✓ Pruned  '),run.stdout)

    def test_manual_review_has_prune_footer(self):
        from test_flow import Case,ok
        c=Case();self.addCleanup(c.close)
        run=c.run('--review-only');ok(run)
        lines=run.stdout.splitlines()
        review=next(i for i,line in enumerate(lines) if line.startswith('Review '))
        self.assertTrue(lines[review+1].startswith('    ✓ Found   '),run.stdout)
        self.assertEqual(lines[review+2],'    ✓ Pruned  0 files · 0 B',run.stdout)

if __name__=='__main__':unittest.main()

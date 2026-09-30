"""Office locator/retention fixtures: no Office GUI, provider network, or BAV."""
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import struct
import zlib
import tempfile
import time
import unittest
from unittest.mock import patch

import native_office as office
from test_native_office import FakeOffice


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.ac = self.repo/'.git/autocycle'
        self.backend = FakeOffice()
        real_capture = self.backend.capture
        self.sequence = 0
        def distinct_capture(path, routing):
            result = real_capture(path, routing)
            self.sequence += 1
            data = b'tEXtfixture\0'+str(self.sequence).encode()
            chunk = struct.pack('>I', len(data)-4)+data+struct.pack('>I', zlib.crc32(data))
            raw = path.read_bytes(); path.write_bytes(raw[:-12]+chunk+raw[-12:])
            result['word_rendered']['fixture_capture'] = self.sequence
            return result
        self.backend.capture = distinct_capture
        self.ws = office.Workspace(self.ac, self.backend)
        self.source = self.repo/'source.docx'
        self.source.write_bytes(b'authoritative')
        self.ac.mkdir(parents=True)
        (self.ac/'controller.lock').touch()
        (self.ac/'resume-state').write_text('STATE_BRANCH=main\nSESSION_NUMBER=1\nSTAGE=review_done\n')
        (self.ac/'current-review').write_text('Fixture admitted Review')
        self.state({'attempts':[]})

    def capture(self, head='head'):
        request = office.enqueue(self.ws, {'app':'word','source':str(self.source)}, head)
        receipt = Path(office.process_requests(self.ws, ('word',), head, [request.stem])[0])
        return request.stem, receipt, json.loads(receipt.read_text())

    def state(self, value):
        (self.ac/'work-state.json').write_text(json.dumps({'version':1,'branches':{'main':value}}))

    def admit_boundary(self):
        path = self.ac/'work-state.json'
        if path.is_symlink(): return
        value = json.loads(path.read_text())
        value['branches']['main']['office_retention'] = {
            'session':{'branch':'main','number':'1'}, 'accepted':[],
            'review_sha256':office.digest(self.ac/'current-review'), 'admitted_ns':time.time_ns()}
        path.write_text(json.dumps(value))
        (self.ac/'resume-state').touch()

    def gc(self, dry_run=False):
        self.admit_boundary()
        return office.garbage_collect(self.ws, self.repo, dry_run=dry_run)

    def test_large_legacy_payload_index_is_only_locator_and_can_be_followed(self):
        ident, receipt, result = self.capture()
        # Old receipts can contain multiple MiB and must remain byte-for-byte intact.
        result['native_window']['word_rendered'] = {'lines':[{'text':'OCR_SENTINEL'+'x'*2200000}]}
        receipt.chmod(0o600); receipt.write_text(json.dumps(result))
        before = receipt.read_bytes()
        locator = office.write_review_index(self.ws, self.repo, 'head', {'attempts':[]})
        self.assertLess(len(locator), 300)
        self.assertNotIn('OCR_SENTINEL', locator)
        index_path = self.ws.root/'review-index.json'
        self.assertIn(office.digest(index_path), locator)
        index = json.loads(index_path.read_text())
        self.assertLess(index_path.stat().st_size, 4000)
        entry = index['entries'][0]
        self.assertEqual(entry['request_id'], ident)
        actual = json.loads((self.repo/entry['receipt']).read_text())
        self.assertEqual(actual['native_window']['word_rendered']['lines'][0]['text'], 'OCR_SENTINEL'+'x'*2200000)
        saved = json.loads((self.repo/entry['result']).read_text())
        rendered = json.loads((self.repo/saved['native_window']['word_rendered']['path']).read_text())
        self.assertIn('Fixture rendered page one', rendered['lines'][0]['text'])
        self.assertEqual(receipt.read_bytes(), before)
        print('synthetic Office payload chars before/after:', len(json.dumps([result])), len(locator))

    def test_explicit_attempt_recovery_missing_and_legacy_selection(self):
        current, _, _ = self.capture('old-request-head')
        original, _, _ = self.capture('old')
        replacement, _, _ = self.capture('old')
        missing, _, _ = self.capture('old')
        historical, historical_path, _ = self.capture('head')
        legacy, legacy_path, legacy_result = self.capture('older')
        legacy_path.chmod(0o600); legacy_result.pop('reviewed_head'); legacy_result['request'].pop('requested_head')
        legacy_path.write_text(json.dumps(legacy_result))
        active = {'id':'attempt','phase':'checkpointed','checkpoint_sha':'head', 'native_requests':[current]}
        state = {'attempts':[{'id':'past','phase':'checkpointed','report':{'outcome':'COMPLETE','evidence':[{'path':str(historical_path)}]}},active],
                 'block':{'observations':{'requests':[{'request_id':original,'replacement_request_id':replacement}]}},
                 'missing_evidence':{'requirements':[{'attempt':{'request_id':missing}}]}}
        office.write_review_index(self.ws, self.repo, 'head', state)
        index = json.loads((self.ws.root/'review-index.json').read_text())
        self.assertEqual({e['request_id'] for e in index['entries']}, {current,original,replacement,missing})
        self.assertTrue(legacy_path.exists())
        self.assertNotIn(legacy, json.dumps(index))
        self.assertNotIn(historical, json.dumps(index))

    def test_success_externalizes_rendered_and_failed_cleanup_keeps_diagnostics(self):
        _, _, result = self.capture()
        self.assertEqual(result['status'], 'CAPTURED')
        rendered = result['native_window']['word_rendered']
        self.assertNotIn('lines', rendered)
        self.assertEqual(rendered['line_count'], 1)
        self.assertEqual(office.digest(rendered['path']), rendered['sha256'])
        self.assertTrue(Path(result['screenshot']).is_file())
        self.assertTrue(Path(result['artifact_retention']).is_file())
        original_close = self.backend.close
        def fail_after_capture(app, path, save):
            original_close(app,path,save)
            if path in self.backend.opened: raise AssertionError('fixture')
            if any(e[0]=='capture' for e in self.backend.events):
                raise office.NativeError('CLOSE_DIAGNOSTIC', 'native_access')
        self.backend.events.clear()
        self.backend.close = fail_after_capture
        _, receipt, failed = self.capture()
        self.assertEqual(failed['status'], 'BLOCKED')
        self.assertIn('CLOSE_DIAGNOSTIC', failed['action'])
        self.assertEqual(failed['failure_kind'], 'native_access')
        folder = Path(failed['evidence']).parent
        self.assertEqual([p.name for p in folder.iterdir()], ['result.json'])
        self.assertNotIn('lines', receipt.read_text())
        self.assertNotIn('screenshot', failed)
        self.assertNotIn('screenshot_sha256', failed)
        self.assertEqual(office.process_requests(self.ws, ('word',), 'head', [receipt.stem]), [])

    def test_gc_dry_run_roots_legacy_and_independent_pruning(self):
        rooted, rooted_receipt, kept = self.capture()
        unused, _, pruned = self.capture('old')
        legacy = self.ws.root/'evidence/word/legacy'
        legacy.mkdir(); (legacy/'view.png').write_bytes(b'legacy')
        (legacy/'result.json').write_text(json.dumps({'status':'CAPTURED','screenshot':str(legacy/'view.png')}))
        self.state({'attempts':[{'phase':'checkpointed','report':{'outcome':'COMPLETE','evidence':[{'path':str(rooted_receipt),'sha256':office.digest(rooted_receipt)}]}}]})
        before = {p: p.read_bytes() for p in self.ws.root.rglob('*') if p.is_file()}
        report = self.gc(dry_run=True)
        self.assertGreater(report['retainable']['bytes'], 0)
        self.assertGreater(report['prunable']['bytes'], 0)
        self.assertGreater(report['ambiguous']['bytes'], 0)
        self.assertEqual(before, {p:p.read_bytes() for p in before})
        report = self.gc(dry_run=False)
        self.assertTrue(Path(kept['screenshot']).exists())
        self.assertFalse(Path(pruned['screenshot']).exists())
        self.assertTrue((legacy/'view.png').exists())
        self.assertEqual(office.artifact_state(pruned['artifact_retention']), 'pruned')
        self.assertEqual(json.loads(Path(pruned['artifact_retention']).read_text())['state'], 'retained')
        self.assertTrue((self.ws.root/'receipts'/(unused+'.json')).exists())

    def test_gc_controller_lock_refuses_even_dry_run(self):
        self.capture()
        with (self.ac/'controller.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex((ValueError, RuntimeError), 'active|running'):
                self.gc(dry_run=True)

    def test_gc_durable_hash_and_unresolved_reference_retain(self):
        _, _, kept = self.capture('old')
        _, _, free = self.capture('old')
        # Hash-only references are durable; malformed/unresolved individual paths
        # retain their evidence family without stopping independent safe pruning.
        (self.ac/'candidate.json').write_text(json.dumps({'evidence':[{'path':kept['evidence'],'sha256':'0'*64}]}))
        report = self.gc(dry_run=False)
        self.assertTrue(Path(kept['screenshot']).exists())
        self.assertFalse(Path(free['screenshot']).exists())
        self.assertTrue(report['unresolved'])



    def test_gc_all_durable_root_forms_and_current_checkpoint(self):
        captures = [self.capture('current' if i==0 else 'old') for i in range(9)]
        self.state({'work':{'id':'work'}, 'attempts':[
            {'phase':'checkpointed','report':{'outcome':'COMPLETE','endpoint':{'evidence':[{'path':str(captures[1][1]),'sha256':office.digest(captures[1][1])}]}}},
            {'id':'active','phase':'checkpointed','checkpoint_sha':'current'}],
            'missing_evidence':{'requirements':[{'attempt':{'request_id':captures[2][0]}}]},
            'block':{'observations':{'requests':[{'request_id':captures[3][0],'replacement_request_id':captures[4][0]}]}}})
        (self.ac/'current-review').write_text('AUTOCYCLE_REVIEW: '+json.dumps({'evidence':[{'path':captures[5][2]['screenshot'],'sha256':captures[5][2]['screenshot_sha256']}]}))
        (self.ac/'candidate.json').write_text(json.dumps({'durable_hash':office.digest(captures[6][1])}))
        (self.ac/'implementation-result.json').write_text(json.dumps({'durable_path':captures[7][2]['evidence']}))
        report = self.gc()
        for _, _, result in captures[:-1]: self.assertTrue(Path(result['screenshot']).exists())
        self.assertFalse(Path(captures[-1][2]['screenshot']).exists())
        self.assertEqual(report['deleted_files'], 2)

    def test_explicit_empty_handoff_suppresses_head_fallback(self):
        self.capture()
        current = {'work':{'id':'work'}, 'attempts':[{'id':'active','phase':'checkpointed','checkpoint_sha':'head'}]}
        (self.ac/'native-handoff.json').write_text(json.dumps({'identity':{'binding':{'work':{'id':'work'},'attempt':{'id':'active'}}},'requests':[]}))
        office.write_review_index(self.ws, self.repo, 'head', current)
        self.assertEqual(json.loads((self.ws.root/'review-index.json').read_text())['entries'], [])

    def test_failed_rendered_publication_cleans_heavy_files(self):
        publish = office.publish_json
        def fail_rendered(path, value):
            if path.name == 'rendered.json': raise OSError('fixture storage failure')
            return publish(path,value)
        with patch.object(office, 'publish_json', side_effect=fail_rendered):
            _, _, result = self.capture()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertIn('fixture storage failure', result['action'])
        self.assertEqual([p.name for p in Path(result['evidence']).parent.iterdir()], ['result.json'])

    def test_gc_rejects_redirected_and_corrupt_families_without_touching_product(self):
        _, _, corrupt = self.capture('old')
        _, _, redirected = self.capture('old')
        _, _, safe = self.capture('old')
        target = Path(corrupt['screenshot']); target.chmod(0o600); target.write_bytes(b'corrupt')
        target = Path(redirected['screenshot']); target.unlink(); target.symlink_to(self.source)
        before = self.source.read_bytes()
        report = self.gc()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(target.is_symlink())
        self.assertTrue(Path(corrupt['screenshot']).exists())
        self.assertFalse(Path(safe['screenshot']).exists())
        self.assertGreater(report['ambiguous']['files'], 0)
        self.assertTrue(report['unresolved'])


    def test_gc_missing_receipt_retains_exact_family_and_prunes_unrelated(self):
        _, receipt, kept = self.capture('old')
        _, _, safe = self.capture('other')
        (self.ac/'current-review').write_text('AUTOCYCLE_REVIEW: '+json.dumps({'outcome':'COMPLETE','evidence':[{'path':str(receipt),'sha256':office.digest(receipt)}]}))
        receipt.unlink()
        report = self.gc()
        self.assertTrue(Path(kept['screenshot']).exists())
        self.assertTrue(Path(kept['native_window']['word_rendered']['path']).exists())
        self.assertFalse(Path(safe['screenshot']).exists())
        self.assertEqual(report['ambiguous']['files'], 2)
        self.assertTrue(report['unresolved'])

    def test_gc_missing_intermediate_log_retains_ambiguity(self):
        _, _, kept = self.capture('old')
        (self.ac/'candidate.json').write_text(json.dumps({'evidence':[{'path':str(self.ac/'lost.log'),'sha256':'1'*64}]}))
        report = self.gc()
        self.assertTrue(Path(kept['screenshot']).exists())
        self.assertEqual(report['prunable']['files'], 0)
        self.assertEqual(report['ambiguous']['files'], 2)


    def test_gc_cli_dry_run_and_pruned_index_paths_are_not_live(self):
        from test_flow import TEST_ENV, REAL_GIT
        subprocess.run([REAL_GIT,'init','-q',str(self.repo)],env=TEST_ENV,check=True)
        _, _, result = self.capture('head')
        image = Path(result['screenshot']); before = image.read_bytes()
        command = [__import__('sys').executable, str(Path(office.__file__).resolve()), 'gc']
        self.admit_boundary()
        run = subprocess.run(command+['--dry-run'], cwd=self.repo, env=TEST_ENV, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['deleted_files'], 0)
        self.assertEqual(image.read_bytes(), before)
        run = subprocess.run(command, cwd=self.repo, env=TEST_ENV, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['deleted_files'], 2)
        office.write_review_index(self.ws, self.repo, 'head', {'attempts':[]})
        entry = json.loads((self.ws.root/'review-index.json').read_text())['entries'][0]
        self.assertEqual(entry['artifact_state'], 'pruned')
        self.assertNotIn('screenshot', entry)
        self.assertNotIn('rendered', entry)


    def test_gc_dangling_authoritative_state_is_ambiguous(self):
        _, _, result = self.capture('old')
        (self.ac/'work-state.json').unlink()
        (self.ac/'work-state.json').symlink_to(self.ac/'missing-state.json')
        report = self.gc()
        self.assertTrue(Path(result['screenshot']).exists())
        self.assertEqual(report['prunable']['files'], 0)
        self.assertTrue(report['unresolved'])


    def test_gc_instruction_database_references_are_retention_roots(self):
        import sqlite3
        _, _, kept = self.capture('old')
        _, _, free = self.capture('other')
        database = self.ac/'instructions.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE instructions (text TEXT)')
            db.execute('INSERT INTO instructions VALUES (?)', (json.dumps({'path':kept['evidence'],'sha256':office.digest(kept['evidence'])}),))
        before = database.read_bytes()
        report = self.gc()
        self.assertTrue(Path(kept['screenshot']).exists())
        self.assertFalse(Path(free['screenshot']).exists())
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(report['deleted_files'], 2)


class PreflightTests(unittest.TestCase):
    def test_actual_review_preflight_blocks_provider_and_counts_characters(self):
        source = Path('stage').read_text()
        start = source.index('        local PREFLIGHT_REPORT')
        end = source.index('        EXIT_CODE=$?', start)
        block = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root/'launch.sh'
            script.write_text('''set -euo pipefail
ADJUDICATOR="$1/adjudication.py"
IFS= read -r -d '' PROMPT < "$2" || true
HUMAN_INPUT_PROMPT="$PROMPT"
OFFICE_EVIDENCE='' EXCEL_EVIDENCE='' INPUT_CONTEXT_PROMPT=''
STEP_INDEX_PROMPT='' PROJECT_HISTORY='' CANDIDATE_PROMPT=''
REVIEW_LOG="$3/log" ANSWER="$3/answer"
fail() { echo "$1" >&2; exit 2; }
adjudication() { cat > "$3/received"; }
review() {
'''+block+'\n}\nreview\n')
            # Replace just the external provider boundary with an invocation marker.
            text = script.read_text().replace('adjudication() { cat > "$3/received"; }',
                'adjudication() { cat > "'+str(root/'received')+'"; }')
            script.write_text(text)
            for count, expected in ((899999,0),(900000,2),(1000001,2)):
                prompt = root/'prompt'; prompt.write_text('中'*count)
                (root/'received').unlink(missing_ok=True)
                run = subprocess.run(['/bin/bash', str(script), str(Path.cwd()), str(prompt), str(root)], capture_output=True, text=True)
                self.assertEqual(run.returncode, expected, run.stderr)
                if expected:
                    self.assertFalse((root/'received').exists())
                    self.assertIn('"total_chars": '+str(count), run.stderr)
                    self.assertIn('"human_input": '+str(count), run.stderr)
                    self.assertIn('"work_context": 0', run.stderr)
                else:
                    self.assertEqual((root/'received').read_text(), prompt.read_text())

if __name__ == '__main__':
    unittest.main()

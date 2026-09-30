"""Exact GC roots and synthetic traversal; no live Office or project data."""
import json
import unittest
from pathlib import Path
import test_office_retention as retention_tests
import native_office as office


class ExactRootsTests(unittest.TestCase):
    setUp = retention_tests.RetentionTests.setUp
    capture = retention_tests.RetentionTests.capture
    state = retention_tests.RetentionTests.state
    boundary = retention_tests.RetentionTests.boundary

    def test_instruction_superstrings_and_prose_do_not_pin(self):
        self.boundary()
        ident, receipt, result = self.capture()
        log = self.ac/'implementation-audit.log'
        log.write_text(json.dumps({'path':str(receipt)}))
        self.boundary()
        for prose in (str(log)+'.backup', 'prefix'+ident, ident+'suffix',
                      office.digest(receipt)+'z', 'the Office evidence should look polished'):
            with self.subTest(prose=prose):
                report, _ = office.gc_plan(self.ws, self.repo, [prose])
                self.assertEqual(report['prunable']['files'], 2)
                self.assertEqual(report['ambiguous']['files'], 0)

    def test_exact_instruction_identifiers_and_embedded_json_pin(self):
        self.boundary()
        ident, receipt, result = self.capture()
        _, _, free = self.capture()
        self.boundary()
        for prose in (f'Keep `{receipt}` please', f'Keep {ident}',
                      f'Keep {office.digest(receipt)}', f'sha256:{office.digest(receipt)}',
                      f'request_id={ident}', f'request_id:{ident}',
                      'Keep this '+json.dumps({'path':str(receipt),'sha256':office.digest(receipt)})+' please',
                      str(receipt.relative_to(self.repo))):
            with self.subTest(prose=prose):
                report, _ = office.gc_plan(self.ws, self.repo, [prose])
                self.assertEqual(report['retainable']['files'], 2)
                self.assertEqual(report['prunable']['files'], 2)

    def test_cyclic_structured_logs_and_missing_bound_reference(self):
        self.boundary()
        _, receipt, result = self.capture()
        a, b = self.ac/'a.json', self.ac/'b.json'
        a.write_text(json.dumps({'path':str(b)}))
        b.write_text(json.dumps([{'path':str(a)}, {'path':str(receipt)}]))
        self.boundary(roots={'work':{'path':str(a)}})
        report, _ = office.gc_plan(self.ws,self.repo)
        self.assertEqual(report['retainable']['files'],2)
        receipt.unlink()
        report, _ = office.gc_plan(self.ws,self.repo)
        self.assertEqual(report['ambiguous']['files'],2)


class TraversalTests(unittest.TestCase):
    setUp = ExactRootsTests.setUp
    capture = ExactRootsTests.capture
    state = ExactRootsTests.state
    boundary = ExactRootsTests.boundary

    def test_queue_reads_each_reached_log_once(self):
        from unittest.mock import patch
        self.boundary()
        _, receipt, result = self.capture()
        paths=[self.ac/f'log-{i:04}.json' for i in range(120)]
        for i,path in enumerate(paths):
            path.write_text(json.dumps({'path':str(paths[(i+1)%len(paths)]),
                                        'evidence':str(receipt),'detail':'x'*1024}))
        self.boundary(roots={'work':{'path':str(paths[0])}})
        reads={p:0 for p in paths}; read=Path.read_text
        def counted(path,*args,**kwargs):
            if path in reads: reads[path]+=1
            return read(path,*args,**kwargs)
        with patch.object(Path,'read_text',counted):
            report,_=office.gc_plan(self.ws,self.repo)
        self.assertEqual(set(reads.values()),{1})
        self.assertEqual(report['retainable']['files'],2)

    def test_quoted_path_with_spaces_and_hash_mismatch(self):
        self.boundary()
        _, receipt, result=self.capture()
        log=self.ac/'audit with spaces.json'
        log.write_text(json.dumps({'path':str(receipt)}))
        self.boundary()
        report,_=office.gc_plan(self.ws,self.repo,[f'Keep `{log}`'])
        self.assertEqual(report['retainable']['files'],2)
        report,_=office.gc_plan(self.ws,self.repo,['Please retain '+json.dumps({'path':str(receipt),'sha256':'0'*64},indent=2)])
        self.assertEqual(report['ambiguous']['files'],2)

if __name__ == '__main__': unittest.main()

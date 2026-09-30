"""Background positioning and user focus changes; no live GUI operations."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import native_office as office
from test_native_office import FakeOffice
from test_navigation_evidence import NavigationOffice, workbook, requirement


class FocusTests(unittest.TestCase):
    def test_positioning_keeps_native_operations_without_window_activation(self):
        backend = office.MacOffice()
        cases = [
            ('word', {'page': 3, 'zoom': 90}, ['position absolute count 3', 'select pageRange', 'to 90']),
            ('word', {'start': 42, 'end': 50}, ['select (create range targetDoc start 42 end 50)']),
            ('excel', {'worksheet': 'Detail', 'range': 'D12:F15', 'scroll_row': 10,
                       'scroll_column': 3, 'zoom': 125},
             ['activate object targetSheet', 'goto reference', 'to 125', 'to 10', 'to 3']),
        ]
        with patch.object(backend, 'find', side_effect=lambda app, path, body: body), \
             patch.object(backend, 'script') as script, patch.object(office.time, 'sleep'):
            for app, request, expected in cases:
                backend.position(app, Path('/owned') / ('view' + office.EXTENSIONS[app]), request)
                body = script.call_args.args[1]
                self.assertNotIn('activate', body.splitlines())
                self.assertNotIn('activate object window', body)
                self.assertNotIn('frontmost', body)
                self.assertIn('set bounds of window 1 of targetDoc', body)
                for operation in expected:
                    self.assertIn(operation, body)

    def test_view_never_restores_over_a_user_switch_even_on_failure(self):
        for app in office.APPS:
            for fail in (False, True):
                with self.subTest(app=app, fail=fail), tempfile.TemporaryDirectory() as directory:
                    backend = FakeOffice()
                    workspace = office.Workspace(directory, backend)
                    source = Path(directory) / ('source' + office.EXTENSIONS[app])
                    source.write_bytes(b'source')
                    focus = ['initial.app']
                    capture = backend.capture
                    def switch(*args):
                        focus[0] = 'user.chosen.app'
                        if fail:
                            raise office.NativeError('capture unavailable', 'native_capture')
                        return capture(*args)
                    backend.capture = switch
                    backend.frontmost = lambda: focus[0]
                    def restore(bundle):
                        focus[0] = bundle
                    backend.restore = restore
                    result = workspace.view({'app': app, 'source': str(source),
                                             'source_sha256': office.digest(source), 'worksheet': 'Access'})
                    self.assertEqual(result['status'], 'BLOCKED' if fail else 'CAPTURED')
                    self.assertEqual(focus[0], 'user.chosen.app')

    def test_navigation_and_preflight_do_not_restore_focus(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = NavigationOffice()
            workspace = office.Workspace(directory, backend)
            source = Path(directory) / 'source.xlsx'
            workbook(source)
            with patch.object(backend, 'frontmost', side_effect=AssertionError('focus snapshot')), \
                 patch.object(backend, 'restore', side_effect=AssertionError('focus restore')):
                self.assertTrue(office.preflight(workspace, ('word', 'excel'), io.StringIO()))
                request = office.navigation_request(requirement(source))
                result = workspace.navigate(request, workspace.folder('excel'))
                self.assertEqual(result['status'], 'PRODUCED')


if __name__ == '__main__':
    unittest.main()

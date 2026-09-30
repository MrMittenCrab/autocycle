"""Merge-selection semantics from a native Excel reproduction; no Office required."""
import unittest
from unittest.mock import patch
from native_office import MacOffice, NativeError

class MergeSelectionTests(unittest.TestCase):
    def check(self, expected, actual, merges=()):
        MacOffice.confirm_excel_selection_addresses(expected, actual, merges)

    def test_native_merged_header_expands_rectangle(self):
        self.check('$A$8:$A$14', '$A$8:$B$14', ['$A$8:$B$8'])

    def test_exact_sibling_selections(self):
        for address in ('$A$1:$B$6', '$A$8:$B$17', '$A$1:$B$8'):
            self.check(address, address)

    def test_containment_is_not_confirmation(self):
        for actual in ('$A$8:$C$14', '$A$8:$B$15', '$A$7:$B$14', '$A$8:$B$8', '$A$8:$A$14,$B$8'):
            with self.subTest(actual=actual), self.assertRaises(NativeError):
                self.check('$A$8:$A$14', actual, ['$A$8:$B$8'])

    def test_no_merge_cannot_explain_expansion(self):
        with self.assertRaises(NativeError):self.check('$A$8:$A$14', '$A$8:$B$14')

    def test_disconnected_merge_cannot_explain_expansion(self):
        with self.assertRaises(NativeError):self.check('$A$8:$A$14', '$A$8:$C$14', ['$B$8:$C$8'])

    def test_transitive_merge_closure(self):
        self.check('$A$8:$A$14', '$A$8:$C$14', ['$B$10:$C$10', '$A$8:$B$8'])

    def test_single_cell_and_vertical_merge(self):
        self.check('$C$3', '$C$3:$C$5', ['$C$3:$C$5'])

    def test_invalid_addresses_fail_closed(self):
        for bad in ('', 'A8:A14', '$A$0', '$XFE$1', '$A$1048577', '$B$14:$A$8'):
            with self.subTest(bad=bad), self.assertRaises(NativeError):self.check(bad, '$A$8:$B$14')

class ConfirmationReplyTests(unittest.TestCase):
    slot = '/private/tmp/owned/excel-view.xlsx'
    req = {'worksheet':'Overview', 'range':'A8:A14', 'bounds':[40,40,1320,1000]}

    def confirm(self, reply, app='excel'):
        backend = MacOffice()
        with patch.object(backend, 'find', return_value='native script'), patch.object(backend, 'script', return_value=reply), patch.object(backend, 'routing', return_value='verified'):
            return backend.confirm_view(app, self.slot, self.req)

    def reply(self, observed=None, selection='$A$8:$A$14|$A$8:$B$14|$A$8:$B$8;'):
        return '<<AC>>'.join((self.slot, observed or self.slot, '40,40,1320,1000', selection))

    def test_native_reply_reaches_capture_routing(self):
        self.assertEqual(self.confirm(self.reply()), 'verified')

    def test_other_workbook_with_same_name_rejected(self):
        with self.assertRaisesRegex(NativeError, 'Unexpected active document'):
            self.confirm(self.reply(observed='/private/tmp/other/excel-view.xlsx'))

    def test_invalid_selection_never_reaches_routing(self):
        with self.assertRaisesRegex(NativeError, 'Unexpected Excel selection'):
            self.confirm(self.reply(selection='$A$8:$A$14|$A$8:$C$14|$A$8:$B$8;'))

    def test_malformed_replies_rejected(self):
        for reply in ('', self.reply()+'<<AC>>extra', self.reply(selection=''), self.reply(selection='a|b')):
            with self.subTest(reply=reply), self.assertRaises(NativeError):self.confirm(reply)

    def test_word_reply_unchanged(self):
        self.assertEqual(self.confirm('<<AC>>'.join((self.slot, self.slot, '40,40,1320,1000')), 'word'), 'verified')

    def test_exact_multiarea_comparison_preserved(self):
        MacOffice.confirm_excel_selection_addresses('$A$1,$C$3', '$A$1,$C$3', [])

if __name__=='__main__':unittest.main()

#!/usr/bin/env python3
"""Opt-in native Office slots and controller-owned, file-based visual evidence."""
import argparse
import contextlib
import concurrent.futures
from collections import defaultdict, deque
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import threading
import tomllib
import uuid
import zipfile
import zlib

EXTENSIONS = {'excel': '.xlsx', 'word': '.docx'}
APPS = {'excel': 'Microsoft Excel', 'word': 'Microsoft Word'}


class NativeError(RuntimeError):
    def __init__(self, message, kind='native_access'):
        super().__init__(message)
        self.kind = kind


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def capabilities(repo):
    path = Path(repo)/'.autocycle.toml'
    if not path.exists():
        return ()
    data = tomllib.loads(path.read_text())
    caps = data.get('capabilities', {})
    if not isinstance(caps, dict):
        raise ValueError('capabilities must be a TOML table')
    apps = caps.get('native_office', [])
    if not isinstance(apps, list) or any(not isinstance(a, str) or a not in APPS for a in apps):
        raise ValueError('native_office must be an array containing only excel and word')
    return tuple(dict.fromkeys(apps))


def safe_directory(path):
    path = Path(path).absolute()
    # Reject redirected descendants, including evidence/requests directories.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise NativeError('Office directory is a symbolic link: '+str(part))
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_slot(path):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise NativeError('Office slot aliases another file: '+str(path))


def harmless_document(app):
    """Small valid OOXML files; no dependencies, macros or external links."""
    root = 'http://schemas.openxmlformats.org/'
    relationships = root+'package/2006/relationships'
    office = root+'officeDocument/2006/relationships'
    main = 'xl/workbook.xml' if app == 'excel' else 'word/document.xml'
    content_type = ('spreadsheetml.sheet' if app == 'excel' else 'wordprocessingml.document')
    types = f'<Types xmlns="{root}package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/{main}" ContentType="application/vnd.openxmlformats-officedocument.{content_type}.main+xml"/>'
    files = {'_rels/.rels': f'<Relationships xmlns="{relationships}"><Relationship Id="rId1" Type="{office}/officeDocument" Target="{main}"/></Relationships>'}
    if app == 'excel':
        types += '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        files[main] = f'<workbook xmlns="{root}spreadsheetml/2006/main" xmlns:r="{office}"><sheets><sheet name="Access" sheetId="1" r:id="rId1"/></sheets></workbook>'
        files['xl/_rels/workbook.xml.rels'] = f'<Relationships xmlns="{relationships}"><Relationship Id="rId1" Type="{office}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        files['xl/worksheets/sheet1.xml'] = f'<worksheet xmlns="{root}spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>AutoCycle access check</t></is></c></row></sheetData></worksheet>'
    else:
        files[main] = f'<w:document xmlns:w="{root}wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>AutoCycle access check</w:t></w:r></w:p><w:sectPr/></w:body></w:document>'
    files['[Content_Types].xml'] = types+'</Types>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as package:
        for name, data in files.items():
            package.writestr(name, data)
    return output.getvalue()


class MacOffice:
    """Apple events target exact paths only. Never automate security dialogs."""
    def __init__(self):
        self.opened = {}
        self.word_open_seconds = 300
        self.permission_poll_seconds = 2

    def script(self, app, body, *args, timeout=12, reference=False):
        # Resolve dynamic POSIX paths outside Office's tell block: Excel can
        # dispatch that coercion as an Apple event and return parameter error -50.
        code = 'on run argv\nset slotPath to item 1 of argv\nset slotFile to (POSIX file slotPath) as text\nwith timeout of '+str(timeout)+' seconds\ntell application "'+APPS[app]+'"\n'+body+'\nend tell\nend timeout\nend run'
        result = subprocess.run(['/usr/bin/osascript', *(['-s', 's'] if reference else []), '-e', code, '--', *map(str, args)], capture_output=True, text=True, timeout=timeout+2)
        if result.returncode:
            raise NativeError((result.stderr or result.stdout).strip() or 'Office automation failed')
        return result.stdout.strip()

    @staticmethod
    def lock_state(path):
        path = Path(path)
        # Word replaces the first two basename characters in its owner file.
        name = path.name[2:] if path.suffix == '.docx' else path.name
        lock = path.with_name('~$'+name)
        if lock.is_symlink():
            raise NativeError('Office lock is a symbolic link')
        try:
            stat = lock.stat()
            return (stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns)
        except FileNotFoundError:
            return None

    @staticmethod
    def reference(app, path):
        # Only these literal names can enter AppleScript source. The native
        # open result must match this specifier before we retain ownership.
        path = Path(path)
        if path.name not in (f'{app}-verify{EXTENSIONS[app]}', f'{app}-view{EXTENSIONS[app]}'):
            raise NativeError('Only fixed AutoCycle slots may be controlled')
        kind = 'workbook' if app == 'excel' else 'document'
        return f'{kind} "{path.name}" of application "{APPS[app]}"'

    def is_open(self, app, path):
        ref = self.reference(app, path)
        key = (app, str(path))
        owned = self.opened.get(key)
        state = self.lock_state(path)
        exists = self.script(app, f'if exists ({ref}) then return "OPEN"\nreturn "CLOSED"', path, timeout=3) == 'OPEN'
        if owned is None:
            if state is not None or exists:
                raise NativeError('Fixed slot is already open or has an unowned Office lock')
            return False
        if not exists and state is None:
            del self.opened[key]
            return False
        if state is None or state != owned[1] or not exists:
            raise NativeError('Fixed slot ownership changed; refusing to control another document')
        return True

    def find(self, app, path, body):
        if not self.is_open(app, path):
            raise NativeError('Fixed slot is not open')
        ref, _ = self.opened[(app, str(path))]
        return 'set targetDoc to '+ref+'\n'+body

    def close(self, app, path, save):
        option = 'yes' if save else 'no'
        self.script(app, self.find(app, path, 'close targetDoc saving '+option+'\nreturn "CLOSED"'), path)
        if self.lock_state(path) is not None:
            raise NativeError('Office did not release the fixed slot lock')
        del self.opened[(app, str(path))]

    @staticmethod
    def word_permission_pending():
        # Observation only: never select, click, dismiss, or approve a dialog.
        code = ('tell application "System Events" to tell process "Microsoft Word" '
                'to get name of every window')
        try:
            result = subprocess.run(['/usr/bin/osascript', '-e', code],
                                    capture_output=True, text=True, timeout=3)
            return result.returncode == 0 and 'Grant File Access' in result.stdout
        except (OSError, subprocess.SubprocessError):
            return False

    def word_open(self, body, path):
        # One Apple event stays alive across human approval. Never retry an
        # uncertain open or infer ownership from a later lock/metadata query.
        observed_permission = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.script, 'word', body, path,
                                  timeout=self.word_open_seconds, reference=True)
            while True:
                try:
                    return pending.result(timeout=self.permission_poll_seconds)
                except concurrent.futures.TimeoutError:
                    if pending.done():
                        error = pending.exception()
                        if error is None:
                            return pending.result()
                        break
                    if self.word_permission_pending():
                        if not observed_permission and not getattr(self, 'access_display_active', False):
                            print('Word is waiting for human Grant File Access approval '
                                  f'(bounded to {self.word_open_seconds} seconds): {path}',
                                  file=sys.stderr, flush=True)
                        observed_permission = True
                except (NativeError, subprocess.SubprocessError) as exc:
                    error = exc
                    break
        if observed_permission:
            blocked = NativeError('External action required: resolve Word’s Grant File Access '
                                  'request for '+str(path)+'. The bounded owned open did not '
                                  'complete; no document ownership was acquired.')
            blocked.permission_action = str(blocked)
            raise blocked from error
        raise error

    def open(self, app, path):
        if self.is_open(app, path):
            raise NativeError('Fixed slot is already owned')
        if app == 'excel':
            body = 'open workbook workbook file name slotFile update links do not update links read only false password "" write reserved password "" ignore read only recommended true add to mru false'
        else:
            body = 'open file name slotFile confirm conversions false read only false add to recent files false'
        # Keep the open event alive while the user grants access. Its returned
        # object, not a later metadata query or lock alone, establishes ownership.
        open_body = 'set targetDoc to '+body+'\nreturn targetDoc'
        ref = (self.word_open(open_body, path) if app == 'word' else
               self.script(app, open_body, path, timeout=45, reference=True))
        state = self.lock_state(path)
        if ref != self.reference(app, path) or state is None:
            raise NativeError('Office open did not return the fixed slot reference and a fresh lock')
        self.opened[(app, str(path))] = (ref, state)

    def calculate_save(self, app, path):
        action = '''repeat 2 times
repeat with targetSheet in (get worksheets of targetDoc)
calculate (used range of targetSheet)
end repeat
end repeat
set saved of targetDoc to false
save targetDoc''' if app == 'excel' else 'repaginate targetDoc\nsave targetDoc'
        self.script(app, self.find(app, path, action+'\nreturn "SAVED"')+'\nerror "Fixed slot is not open"', path, timeout=120)

    @staticmethod
    def validate_request(app, request):
        bounds = request.get('bounds', [40, 60, 1240, 860])
        zoom = request.get('zoom', 100)
        if type(zoom) is not int or not 10 <= zoom <= 400:
            raise NativeError('Zoom must be 10..400', 'native_capture')
        if not isinstance(bounds, list) or len(bounds) != 4 or any(type(n) is not int for n in bounds) or not (200 <= bounds[2]-bounds[0] <= 8000 and 150 <= bounds[3]-bounds[1] <= 8000):
            raise NativeError('Invalid window bounds', 'native_capture')
        if app == 'excel':
            if not isinstance(request.get('worksheet'), str) or not request['worksheet'] or not isinstance(request.get('range', 'A1'), str):
                raise NativeError('Excel view requires worksheet and range strings', 'native_capture')
            if any(type(request.get(k, 1)) is not int or request.get(k, 1) < 1 for k in ('scroll_row', 'scroll_column')):
                raise NativeError('Invalid scroll position', 'native_capture')
        else:
            start, end = request.get('start', 0), request.get('end', request.get('start', 0))
            page = request.get('page')
            if type(start) is not int or type(end) is not int or not 0 <= start <= end:
                raise NativeError('Invalid Word character range', 'native_capture')
            if page is not None and (type(page) is not int or page < 1):
                raise NativeError('Invalid Word page', 'native_capture')

    def position(self, app, path, request):
        bounds = request.get('bounds', [40, 60, 1240, 860])
        zoom = request.get('zoom', 100)
        if not isinstance(zoom, int) or not 10 <= zoom <= 400:
            raise NativeError('Zoom must be 10..400', 'native_capture')
        if not isinstance(bounds, list) or len(bounds) != 4 or any(type(n) is not int for n in bounds) or not (200 <= bounds[2]-bounds[0] <= 8000 and 150 <= bounds[3]-bounds[1] <= 8000):
            raise NativeError('Invalid window bounds', 'native_capture')
        # Native range/sheet operations establish the owned view in background.
        # Application/window activation steals focus and is not needed for the
        # exact-window capture; keep every mutation scoped to targetDoc.
        common = 'set bounds of window 1 of targetDoc to {'+','.join(map(str,bounds))+'}\n'
        if app == 'excel':
            row, column = request.get('scroll_row', 1), request.get('scroll_column', 1)
            if type(row) is not int or type(column) is not int or row < 1 or column < 1:
                raise NativeError('Invalid scroll position', 'native_capture')
            action = common+'''set targetSheet to worksheet (item 2 of argv) of targetDoc
activate object targetSheet
goto reference (range (item 3 of argv) of targetSheet) scroll true
set zoom of window 1 of targetDoc to '''+str(zoom)+f'\nset scroll row of window 1 of targetDoc to {row}\nset scroll column of window 1 of targetDoc to {column}'
            args = (path, request['worksheet'], request.get('range', 'A1'))
        else:
            start, end = request.get('start', 0), request.get('end', request.get('start', 0))
            page = request.get('page')
            if type(start) is not int or type(end) is not int or not 0 <= start <= end:
                raise NativeError('Invalid Word character range', 'native_capture')
            if page is not None and (type(page) is not int or page < 1):
                raise NativeError('Invalid Word page', 'native_capture')
            # Selecting the native range reveals it; changing selection offsets
            # or the pane's scroll percentage alone does not reliably do so.
            action = common+f'\nset view type of view of window 1 of targetDoc to print view\nrepaginate targetDoc\nset percentage of zoom of view of window 1 of targetDoc to {zoom}\n'
            if page is not None:
                action += (
                    f'set pageCount to (get range information (create range targetDoc start 0 end 0) information type number of pages in document) as integer\n'
                    f'if {page} > pageCount then error "Word page exceeds document pagination"\n'
                    f'set pageRange to navigate (create range targetDoc start 0 end 0) to goto a page item position absolute count {page}\n'
                    'select pageRange\n'
                )
            else:
                action += f'select (create range targetDoc start {start} end {end})\n'
            args = (path,)
        self.script(app, self.find(app, path, action+'\nreturn "POSITIONED"')+'\nerror "Fixed slot is not open"', *args, timeout=45 if app == 'word' else 12)
        if app == 'word':
            # Word's window/title and canvas updates trail the Apple event.
            # The capture still needs exact routing and independent OCR proof.
            time.sleep(.3)
        return bounds

    IDENTITY_REPLY_SEP = '<<AC>>'

    @staticmethod
    def confirm_owned_identity(path, expected, observed):
        # Full POSIX paths only. Filename equality is not identity: the same
        # basename at another location, a blank reading, or a specifier-only
        # match must not reach capture.
        slot = str(Path(path))
        expected = (expected or '').strip()
        observed = (observed or '').strip()
        if not expected or not observed:
            raise NativeError('Active document identity unavailable expected:'+expected+' observed:'+observed,'native_capture')
        if expected != slot:
            raise NativeError('Owned document path mismatch expected:'+slot+' observed:'+expected,'native_capture')
        if observed != expected:
            raise NativeError('Unexpected active document expected:'+expected+' observed:'+observed,'native_capture')

    @staticmethod
    def parse_identity_reply(result):
        # Quoted sentinel, not AppleScript tab. Excel's dictionary defines
        # class tab (Xtab); inside tell Excel that identifier is not ASCII 9.
        parts = (result or '').split(MacOffice.IDENTITY_REPLY_SEP)
        if len(parts) != 3:
            raise NativeError('Active document identity unavailable: malformed identity reply','native_capture')
        return parts

    @staticmethod
    def confirm_excel_selection_addresses(expected, actual, merges):
        """Exact rectangular closure of the requested cells under native merges.

        Excel's Go To expands a rectangular selection when it intersects a
        merged cell. Extra rows/columns without that merge justification fail.
        """
        if expected and actual == expected:
            return  # Preserve the existing exact native-address comparison.
        def rectangle(address):
            match = re.fullmatch(r'\$([A-Z]{1,3})\$([1-9][0-9]*)(?::\$([A-Z]{1,3})\$([1-9][0-9]*))?', address)
            if not match:
                raise NativeError('Invalid Excel selection address: '+address, 'native_capture')
            c1, r1, c2, r2 = match.groups()
            def column(value):
                result = 0
                for letter in value:
                    result = result*26 + ord(letter)-64
                return result
            rect = (column(c1), int(r1), column(c2 or c1), int(r2 or r1))
            if not (1 <= rect[0] <= rect[2] <= 16384 and 1 <= rect[1] <= rect[3] <= 1048576):
                raise NativeError('Invalid Excel selection bounds', 'native_capture')
            return rect
        wanted, observed = rectangle(expected), rectangle(actual)
        merged = [rectangle(address) for address in merges]
        while True:
            previous = wanted
            for area in merged:
                if area[0] <= wanted[2] and area[2] >= wanted[0] and area[1] <= wanted[3] and area[3] >= wanted[1]:
                    wanted = (min(wanted[0], area[0]), min(wanted[1], area[1]),
                              max(wanted[2], area[2]), max(wanted[3], area[3]))
            if wanted == previous:
                break
        if observed != wanted:
            raise NativeError('Unexpected Excel selection expected:'+expected+' observed:'+actual+
                              ' merge closure:'+str(wanted), 'native_capture')

    def confirm_view(self, app, path, request):
        active = 'active workbook' if app == 'excel' else 'active document'
        # Compare POSIX paths, not AppleScript object specifiers. Excel's
        # `active workbook is not targetDoc` raises -2700 even when both
        # refer to the owned slot because the specifiers are distinct.
        check = (
            'try\n'
            'set expectedId to POSIX path of ((full name of targetDoc) as text)\n'
            'set observedId to POSIX path of ((full name of '+active+') as text)\n'
            'on error errMsg number errNum\n'
            'error "Active document identity unavailable (" & errNum & "): " & errMsg\n'
            'end try\n'
        )
        minimized = 'window state minimized' if app == 'excel' else 'window state minimize'
        check += 'if window state of window 1 of targetDoc is '+minimized+' then error "Owned Office window is minimized"\n'
        args = (path,)
        if app == 'excel':
            check += 'if (name of active sheet of targetDoc) is not (item 2 of argv) then error "Unexpected worksheet"\n'
            args += (request['worksheet'], request.get('range', 'A1'))
            # Collect merge evidence in the same native read as identity/view.
            # Only mismatches need inspection; bound work and reject multi-area
            # mismatches. No selection/navigation mutation occurs here.
            check += """set expectedSelection to get address (range (item 3 of argv) of worksheet (item 2 of argv) of targetDoc)
set selectedRange to selection of window 1 of targetDoc
set actualSelection to get address selectedRange
set mergeAddresses to ""
if actualSelection is not expectedSelection then
if (count of (get areas of selectedRange)) is not 1 then error "Unexpected Excel selection: multiple areas"
if (count of cells of selectedRange) > 1024 then error "Excel merge confirmation limit exceeded"
repeat with selectedCell in (get cells of selectedRange)
set mergedAddress to get address (merge area of selectedCell)
if mergedAddress contains ":" then set mergeAddresses to mergeAddresses & mergedAddress & ";"
end repeat
end if
set selectionReply to expectedSelection & "|" & actualSelection & "|" & mergeAddresses
"""
        else:
            page = request.get('page')
            if page is None:
                check += f'if selection start of selection of window 1 of targetDoc is not {request.get("start", 0)} then error "Unexpected Word selection"\n'
            else:
                check += f'set selPage to (get selection information (selection of window 1 of targetDoc) information type active end page number) as integer\n'
                check += f'if selPage is not {page} then error "Unexpected Word page"\n'
        if app == 'excel':
            check += f'if zoom of window 1 of targetDoc is not {request.get("zoom", 100)} then error "Unexpected Excel zoom"\n'
            check += f'if scroll row of window 1 of targetDoc is not {request.get("scroll_row", 1)} then error "Unexpected Excel scroll row"\n'
            check += f'if scroll column of window 1 of targetDoc is not {request.get("scroll_column", 1)} then error "Unexpected Excel scroll column"\n'
        else:
            page = request.get('page')
            if page is None:
                check += f'if selection end of selection of window 1 of targetDoc is not {request.get("end", request.get("start", 0))} then error "Unexpected Word selection end"\n'
            check += f'if percentage of zoom of view of window 1 of targetDoc is not {request.get("zoom", 100)} then error "Unexpected Word zoom"\n'
        sep = self.IDENTITY_REPLY_SEP
        check += 'set rect to bounds of window 1 of targetDoc\nreturn expectedId & "'+sep+'" & observedId & "'+sep+'" & (item 1 of rect as text) & "," & (item 2 of rect as text) & "," & (item 3 of rect as text) & "," & (item 4 of rect as text)'
        if app == 'excel':
            check += ' & "'+sep+'" & selectionReply'
        result = self.script(app, self.find(app, path, check)+'\nerror "Fixed slot is not open"', *args)
        selection = None
        if app == 'excel':
            fields = result.split(sep)
            if len(fields) != 4:
                raise NativeError('Malformed Excel confirmation reply', 'native_capture')
            result = sep.join(fields[:3])
            selection = fields[3].split('|')
            if len(selection) != 3:
                raise NativeError('Malformed Excel selection reply', 'native_capture')
        parts = self.parse_identity_reply(result)
        self.confirm_owned_identity(path, parts[0], parts[1])
        if selection is not None:
            self.confirm_excel_selection_addresses(selection[0], selection[1],
                                                   [a for a in selection[2].split(';') if a])
        bounds = [int(float(n)) for n in parts[2].split(',')]
        if bounds != request.get('bounds', [40, 60, 1240, 860]):
            raise NativeError('Unexpected Office window geometry', 'native_capture')
        return self.routing(app, path, bounds)

    def word_content(self, path):
        return self.script('word', self.find('word', path,
                           'return content of text object of targetDoc'), path).strip()

    def word_page_map(self, path, request):
        """Native pagination supplies expectations, never proof of visibility.

        Word's navigate also changes selection: collect before final positioning.
        """
        separator = '<<AC-PAGE-'+uuid.uuid4().hex+'>>'
        body = """set view type of view of window 1 of targetDoc to print view
repaginate targetDoc
set baseRange to create range targetDoc start 0 end 0
set pageCount to (get range information baseRange information type number of pages in document) as integer
if pageCount < 1 or pageCount > 500 then error "Word pagination exceeds capture limit"
"""
        if request.get('page') is not None:
            body += f'set requestedPage to {request["page"]}\n'
        else:
            body += ('set requestedPage to (get range information (create range targetDoc '
                     f'start {request.get("start", 0)} end {request.get("start", 0)}) '
                     'information type active end page number) as integer\n')
        body += """if requestedPage > pageCount then error "Word page exceeds document pagination"
set reply to requestedPage as text
repeat with i from 1 to pageCount
set firstRange to navigate baseRange to goto a page item position absolute count i
set firstOffset to start of content of firstRange
if i < pageCount then
set nextRange to navigate baseRange to goto a page item position absolute count (i + 1)
set lastOffset to start of content of nextRange
else
set lastOffset to end of content of text object of targetDoc
end if
set pageText to content of (create range targetDoc start firstOffset end lastOffset)
if pageText contains (item 2 of argv) then error "Ambiguous page delimiter"
set reply to reply & (item 2 of argv) & pageText
if (count characters of reply) > 2000000 then error "Word page text exceeds capture limit"
end repeat
return reply"""
        reply = self.script('word', self.find('word', path, body), path, separator, timeout=60)
        fields = reply.split(separator)
        if len(fields) < 2 or not fields[0].isdigit() or not 1 <= int(fields[0]) < len(fields):
            raise NativeError('Invalid native Word page map', 'native_capture')
        return {'page': int(fields[0]), 'pages': fields[1:]}

    @staticmethod
    def verify_word_rendered(path, image, page_map, provenance):
        rendered = (provenance or {}).get('word_rendered', {})
        if not isinstance(rendered, dict) or rendered.get('method') != 'vision-document-canvas':
            raise NativeError('Independent Word viewport evidence unavailable', 'native_capture')
        def words(text):
            return re.findall(r'\w+', text.casefold())
        page = page_map['page']
        pages = [' '+' '.join(words(text))+' ' for text in page_map['pages']]
        lines = rendered.get('lines', [])
        if not isinstance(lines, list):
            raise NativeError('Malformed Word rendered text evidence', 'native_capture')
        observed = words(' '.join(line['text'] for line in lines
                                 if isinstance(line, dict) and isinstance(line.get('text'), str)
                                 and isinstance(line.get('confidence'), (int, float))
                                 and line['confidence'] >= .8))
        # Four whole words (including at least 16 characters) must occur only on
        # this page. No fuzzy matching, status-bar numbers or caller markers.
        for i in range(len(observed)-3):
            phrase = ' '.join(observed[i:i+4])
            bounded = ' '+phrase+' '
            if len(phrase) >= 16 and bounded in pages[page-1] and all(
                    bounded not in text for j, text in enumerate(pages) if j != page-1):
                return {'method':'unique-page-text-in-captured-canvas', 'page':page,
                        'page_count':len(pages), 'matched_text':phrase,
                        'owned_document':str(path), 'copy_sha256':digest(path),
                        'screenshot_sha256':digest(image),
                        'page_text_sha256':hashlib.sha256(page_map['pages'][page-1].encode()).hexdigest(),
                        'pagination_sha256':hashlib.sha256(json.dumps(page_map, sort_keys=True).encode()).hexdigest()}
        raise NativeError(f'Requested Word page {page} is not independently visible: '
                          'no unique page text in the captured document canvas', 'native_capture')

    def navigation_action(self, path, operation, worksheet, cell=None):
        """One native action and its Excel-reported endpoints; never a model claim."""
        read = lambda prefix: (
            'set '+prefix+'Path to POSIX path of ((full name of active workbook) as text)\n'
            'set '+prefix+'Sheet to name of active sheet of targetDoc\n'
            'set '+prefix+'Range to get address (selection of window 1 of targetDoc)\n'
            'set '+prefix+'Cell to get address (active cell)\n')
        if operation == 'follow_hyperlink':
            action = ('if (name of active sheet of targetDoc) is not (item 2 of argv) then error "Unexpected navigation start"\n'
                      'follow (hyperlink 1 of range (item 3 of argv) of worksheet (item 2 of argv) of targetDoc) new window false\n')
        elif operation == 'activate_worksheet':
            # The return restores the selected worksheet, not a product hyperlink.
            action = 'activate object worksheet (item 2 of argv) of targetDoc\n'
        else:
            raise ValueError('Unsupported Excel action')
        sep = self.IDENTITY_REPLY_SEP
        body = read('before') + 'if beforePath is not slotPath then error "Unexpected active workbook"\n' + action + read('after')
        body += 'return '+(' & "'+sep+'" & ').join(('beforePath','beforeSheet','beforeRange','beforeCell','afterPath','afterSheet','afterRange','afterCell'))
        reply = self.script('excel', self.find('excel',path,body),path,worksheet,cell or '')
        fields = reply.split(sep)
        if len(fields)!=8 or not all(fields):
            raise NativeError('Malformed Excel action receipt')
        self.confirm_owned_identity(path,fields[0],fields[4])
        def state(values):
            return dict(zip(('workbook','worksheet','selection','active_cell'),values))
        return {'operation':operation,'control':{'worksheet':worksheet,'cell':cell},
                'before':state(fields[:4]),'after':state(fields[4:])}

    def verify_document(self, app, path):
        observed = self.script(app, self.find(app, path,
            'return POSIX path of ((full name of targetDoc) as text)'), path)
        self.confirm_owned_identity(path, observed, observed)

    def routing(self, app, path, bounds):
        title = self.script(app, self.find(app, path, 'return name of window 1 of targetDoc'), path)
        bundle = {'excel': 'com.microsoft.Excel', 'word': 'com.microsoft.Word'}[app]
        code = 'ObjC.import("AppKit"); JSON.stringify(ObjC.unwrap($.NSWorkspace.sharedWorkspace.runningApplications).filter(a=>ObjC.unwrap(a.bundleIdentifier)==='+json.dumps(bundle)+').map(a=>Number(a.processIdentifier)))'
        process = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', code],
                                 capture_output=True, text=True, check=True, timeout=5)
        pids = json.loads(process.stdout)
        if len(pids) != 1 or not title:
            raise NativeError('Office process/window routing unavailable', 'native_capture')
        left, top, right, bottom = bounds
        return {'pid': pids[0], 'bundle_id': bundle, 'title': title,
                'frame': [left, top, right-left, bottom-top]}

    def capture(self, path, routing):
        helper = Path(__file__).resolve().with_name('autocycle-office-capture')
        request = dict(routing, output=str(Path(path).absolute()))
        try:
            completed = subprocess.run([str(helper)], input=json.dumps(request),
                capture_output=True, text=True, timeout=20)
            if completed.returncode:
                raise ValueError('helper exited '+str(completed.returncode)+': '+completed.stderr.strip()[:1000])
            result = json.loads(completed.stdout)
            if not isinstance(result, dict):
                raise ValueError('result must be an object')
            status = result.get('status')
            if status != 'success':
                if status not in ('no_window', 'ambiguous', 'capture_failure', 'permission_failure', 'invalid_request'):
                    raise ValueError('unknown helper status')
                error = NativeError('Native window capture '+status+': '+str(result.get('message', '')),
                                    'native_access' if status == 'permission_failure' else 'native_capture')
                error.capture_status = status
                raise error
            if completed.returncode or any(result.get(k) != request[k] for k in ('pid', 'bundle_id', 'title', 'frame', 'output')):
                raise ValueError('helper provenance does not match verified routing')
            if any(type(result.get(k)) is not int or result[k] <= 0 for k in ('window_id', 'width', 'height')):
                raise ValueError('invalid native window/image metadata')
            return result
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise NativeError('Native capture helper unavailable or invalid: '+str(exc), 'native_capture') from exc

    def frontmost(self):
        try:
            return subprocess.run(['/usr/bin/osascript', '-e', 'id of application (path to frontmost application as text)'], capture_output=True, text=True, timeout=3).stdout.strip()
        except subprocess.SubprocessError:
            return ''

    def restore(self, bundle):
        if re.fullmatch(r'[A-Za-z0-9._-]+', bundle or ''):
            try:
                subprocess.run(['/usr/bin/osascript','-e',f'tell application id "{bundle}" to activate'], capture_output=True, timeout=3)
            except subprocess.SubprocessError:
                pass


class Workspace:
    def __init__(self, ac_dir, backend=None, wait_seconds=15, poll_seconds=.25):
        self.root = Path(ac_dir).resolve()/'office'
        self.backend = backend or MacOffice()
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds

    def slot(self, app, role):
        if app not in EXTENSIONS or role not in ('verify','view'):
            raise ValueError('Unknown Office slot')
        return self.root/f'{app}-{role}{EXTENSIONS[app]}'

    @contextlib.contextmanager
    def lock(self):
        safe_directory(self.root)
        path = self.root/'verification.lock'
        safe_slot(path)
        with path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise NativeError('Another native Office operation is running') from None
            yield

    def close(self, app, path):
        if path not in (self.slot(app,'verify'), self.slot(app,'view')):
            raise ValueError('Only fixed AutoCycle slots may be closed')
        safe_slot(path)
        if self.backend.is_open(app,path):
            self.backend.close(app,path,path == self.slot(app,'verify'))
        if self.backend.is_open(app,path):
            raise NativeError('Office did not close the fixed slot')

    def populate(self, app, role, source=None):
        path = self.slot(app,role)
        safe_directory(self.root);safe_slot(path)
        self.close(app,path)
        # Recheck after native close/save, which may replace a file.
        safe_slot(path)
        if source is not None:
            source = Path(source).resolve(strict=True)
            if source.suffix.lower() != EXTENSIONS[app] or source in [self.slot(a,r) for a in APPS for r in ('view','verify')]:
                raise NativeError('Use an authoritative source or immutable saved copy, not a mutable slot')
            content = source.read_bytes()
        else:
            content = harmless_document(app)
        path.write_bytes(content)  # Retain the inode/grant whenever Office permits.
        if path.read_bytes() != content:
            raise NativeError('Fixed slot copy verification failed')
        return path

    def open(self, app, path):
        if path not in (self.slot(app,'verify'),self.slot(app,'view')):
            raise ValueError('Only fixed AutoCycle slots may be opened')
        error = None
        try:
            self.backend.open(app,path)
        except (NativeError,subprocess.SubprocessError) as exc:
            if getattr(exc, 'permission_action', None):
                raise
            error = exc
        deadline = time.monotonic()+self.wait_seconds
        # Backend-owned open state is authoritative. A native timeout without
        # a returned reference cannot establish ownership, even with a lock.
        while time.monotonic() < deadline:
            try:
                if self.backend.is_open(app,path):
                    return
            except (NativeError,subprocess.SubprocessError) as exc:
                error = exc
            time.sleep(self.poll_seconds)
        raise NativeError(APPS[app]+' fixed-slot open failed'+(': '+str(error) if error else ''))

    def folder(self, app):
        parent = safe_directory(self.root/'evidence'/app)
        folder = parent/uuid.uuid4().hex
        folder.mkdir()
        return folder

    def verify(self, app, source, command):
        source = Path(source).resolve(strict=True)
        if not command or '{document}' not in command:
            raise ValueError('Verifier command needs a separate {document} argument')
        with self.lock():
            folder = self.folder(app)
            result = {'status':'BLOCKED','source':str(source),'source_sha256':digest(source),'copy':str(self.slot(app,'verify')),'evidence':str(folder/'result.json')}
            path = self.slot(app,'verify')
            try:
                self.populate(app,'verify',source);self.open(app,path)
                self.backend.calculate_save(app,path);self.close(app,path)
                # A verifier only reads the saved state; native state is frozen
                # even on a failing project check and never reopened in Office.
                snapshot = folder/('saved-copy'+EXTENSIONS[app])
                snapshot.write_bytes(path.read_bytes());snapshot.chmod(0o444)
                result.update(snapshot=str(snapshot),copy_sha256=digest(snapshot))
                checked = subprocess.run([str(path) if arg=='{document}' else arg for arg in command],capture_output=True,text=True,timeout=180)
                (folder/'verification.log').write_text(checked.stdout+checked.stderr)
                if digest(snapshot) != result['copy_sha256'] or digest(path) != result['copy_sha256']:
                    raise NativeError('Verifier modified native saved state','verification')
                if checked.returncode:
                    raise NativeError('Native saved-state verification failed: '+(checked.stderr or checked.stdout),'verification')
                result.update(status='VERIFIED',action='NONE')
            except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
                result.update(action=str(exc),failure_kind=getattr(exc,'kind','native_access'))
                if getattr(exc, 'permission_action', None):
                    result['external_dependency'] = permission_dependency(exc, source, result['source_sha256'])
            finally:
                try:self.close(app,path)
                except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
                    if result.get('external_dependency', {}).get('kind') == 'permission':
                        result['cleanup_error'] = str(exc)
                    else:
                        result.update(status='BLOCKED',action=str(exc),failure_kind='native_access')
            if not source.exists() or digest(source) != result['source_sha256']:
                result.update(status='BLOCKED',action='Authoritative source changed during verification',failure_kind='verification')
            self.finish(folder,result)
            return result

    @staticmethod
    def finish(folder, result):
        target = folder/'result.json'
        publish_json(target,result)
        for path in folder.iterdir():
            if path.is_file():path.chmod(0o444)

    def capture_owned(self, app, path, request, image):
        """Capture a fixed slot in the existing session; caller supplies Office view.

        Does not replace or close an owned document. Serializes with other Office
        operations. The controller's view(request) also uses this transaction.
        """
        with self.lock():
            return self._capture_owned(app, path, request, image)

    def _capture_owned(self, app, path, request, image):
        path, image = Path(path), Path(image)
        if path not in (self.slot(app, 'view'), self.slot(app, 'verify')):
            raise NativeError('Only fixed AutoCycle slots may be captured', 'native_capture')
        safe_slot(path)
        MacOffice.validate_request(app, request)
        for attempt in range(2):
            # Never reuse a native binding across attempts. Ownership is checked
            # even when a previous capture succeeded and the session is reused.
            if not self.backend.is_open(app, path):
                self.open(app, path)
            self.backend.verify_document(app, path)
            page_map = self.backend.word_page_map(path, request) if app == 'word' else None
            copy_sha = digest(path)
            if app == 'word' and request.get('source_sha256') and copy_sha != request['source_sha256']:
                raise NativeError('Word owned copy no longer matches the requested source', 'native_capture')
            if attempt or app == 'word':
                self.backend.position(app, path, request)
            else:
                try:
                    self.backend.confirm_view(app, path, request)
                except NativeError:
                    self.backend.position(app, path, request)
            before = self.backend.confirm_view(app, path, request)
            try:
                provenance = self.backend.capture(image, before)
            except NativeError as exc:
                image.unlink(missing_ok=True)
                if attempt == 0 and getattr(exc, 'capture_status', '') == 'no_window':
                    continue  # one owned-window reposition and fresh resolution
                raise
            try:
                after = self.backend.confirm_view(app, path, request)
                if after != before:
                    raise NativeError('Office precheck/postcheck disagreement', 'native_capture')
                metadata = validate_png(image)
                if provenance and (metadata['width'], metadata['height']) != (provenance['width'], provenance['height']):
                    raise NativeError('Native image dimensions disagree with helper', 'native_capture')
                result = {'image': metadata, 'native_window': provenance}
                if app == 'word':
                    if self.backend.word_content(path) != ''.join(page_map['pages']).strip() or digest(path) != copy_sha:
                        raise NativeError('Word content changed during capture', 'native_capture')
                    result['word_visible_page'] = MacOffice.verify_word_rendered(path, image, page_map, provenance)
                return result
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                image.unlink(missing_ok=True)
                if attempt:
                    raise
        raise NativeError('Native capture attempts exhausted', 'native_capture')

    def navigate(self, request, folder):
        source = Path(request['source']).resolve(strict=True)
        path = self.slot('excel','view')
        result = {'status':'UNAVAILABLE','source':str(source),'source_sha256':digest(source),
                  'request':request,'evidence':str(folder/'result.json'),'actions':[]}
        with self.lock():
            try:
                if result['source_sha256']!=request['source_sha256']:
                    raise NativeError('Navigation source changed')
                validate_navigation_links(request['requirement'])
                self.populate('excel','view',source);self.open('excel',path)
                failures=[]
                for link in request['requirement']['links']:
                    view=dict(request,range=link['cell'])
                    self.backend.position('excel',path,view)
                    self.backend.confirm_view('excel',path,view)
                    self.backend.verify_document('excel',path)
                    outbound=self.backend.navigation_action(path,'follow_hyperlink',request['worksheet'],link['cell'])
                    result['actions'].append(outbound)
                    publish_json(folder/('action-'+str(len(result['actions']))+'.json'),outbound)
                    returned=self.backend.navigation_action(path,'activate_worksheet',request['requirement']['return_sheet'])
                    result['actions'].append(returned)
                    publish_json(folder/('action-'+str(len(result['actions']))+'.json'),returned)
                    if (outbound['before']['worksheet']!=request['worksheet'] or
                        outbound['before']['active_cell'].replace('$','').upper()!=link['cell'].upper() or
                        returned['before']['worksheet']!=outbound['after']['worksheet'] or
                        outbound['after']['worksheet']!=link['sheet'] or
                        outbound['after']['active_cell'].replace('$','').upper()!=link['range'].upper() or
                        returned['after']['worksheet']!=request['requirement']['return_sheet']):
                        failures.append('Unexpected native navigation destination or return: '+link['cell'])
                        break
                result.update(status='FAILED' if failures else 'PRODUCED',action='; '.join(failures) or 'NONE')
            except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as exc:
                result.update(status='FAILED' if getattr(exc,'kind','')=='verification' else 'UNAVAILABLE',
                              action=str(exc),failure_kind=getattr(exc,'kind','native_access'))
            finally:
                try:self.close('excel',path)
                except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
                    result.update(status='UNAVAILABLE',action=str(exc),failure_kind='native_access')
            if not source.exists() or digest(source)!=result['source_sha256']:
                result.update(status='UNAVAILABLE',action='Source changed during navigation',failure_kind='verification')
            result['completed_at_ns']=time.time_ns()
            self.finish(folder,result)
            return result

    def view(self, request, folder=None, request_id=None):
        app = request['app']
        source = Path(request['source']).resolve(strict=True)
        with self.lock():
            folder = folder if folder is not None else self.folder(app)
            result = {'status':'BLOCKED','source':str(source),'source_sha256':digest(source),'request':request,'evidence':str(folder/'result.json')}
            if request_id is not None: result['request_id'] = request_id
            path = self.slot(app,'view');phase='native_access'
            with tempfile.TemporaryDirectory(prefix='.capture-', dir=folder) as temporary:
                try:
                    if result['source_sha256'] != request['source_sha256']:
                        raise NativeError('View source changed since request; submit a fresh request','native_capture')
                    self.populate(app,'view',source);self.open(app,path)
                    phase='native_capture'
                    image = Path(temporary)/'view.png'
                    captured = self._capture_owned(app,path,request,image)
                    result.update(status='CAPTURED',screenshot=str(image),screenshot_sha256=digest(image),**captured,action='NONE')
                except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as exc:
                    result.update(action=str(exc),failure_kind=getattr(exc, 'kind', phase))
                    if getattr(exc, 'permission_action', None):
                        result['external_dependency'] = permission_dependency(exc, source, result['source_sha256'])
                    if getattr(exc,'capture_status',None)=='permission_failure':
                        result['external_dependency']={
                            'fact':'native document capture', 'route':'owned-window capture helper',
                            'kind':'permission', 'check':'attempted',
                            'action':'Grant Screen Recording permission to autocycle-office-capture in macOS System Settings Privacy & Security, then resume AutoCycle.',
                            'inputs':[{'path':str(source),'sha256':result['source_sha256'],
                                       'observation':'Source document for the attempted owned-window capture'}]}

                finally:
                    try:self.close(app,path)
                    except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
                        if result.get('external_dependency', {}).get('kind') == 'permission':
                            result['cleanup_error'] = str(exc)
                        else:
                            result.update(status='BLOCKED',action=str(exc),failure_kind='native_access')
                if not source.exists() or digest(source) != result['source_sha256']:
                    result.update(status='BLOCKED',action='View source changed during inspection',failure_kind='verification')
                retain_capture(folder, result, image if 'image' in locals() else None)
            self.finish(folder,result)
            return result


def replace_json(path, value):
    """Atomic publication for mutable locators/lifecycle records, not receipts."""
    safe_directory(path.parent); safe_slot(path)
    temporary = path.with_name(path.name+'.tmp.'+uuid.uuid4().hex)
    try:
        with temporary.open('x') as out:
            json.dump(value, out, indent=2); out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def saved_office_session(ac):
    """Read only the existing durable controller state; never infer ownership."""
    path = ac/'resume-state'
    confined_file(path, ac)
    values = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep:
            words = shlex.split(value)
            values[key] = words[0] if len(words) == 1 else ''
    if not values.get('STATE_BRANCH') or not values.get('SESSION_NUMBER', '').isdigit():
        raise ValueError('No explicit Office Session binding')
    return {'branch':values['STATE_BRANCH'], 'number':values['SESSION_NUMBER']}, values.get('STAGE')


def prune_ledger(root):
    path = root/'prune-ledger.json'
    if not path.exists(): return {'version':1, 'families':{}}
    confined_file(path, root)
    value = json.loads(path.read_text())
    if value.get('version') != 1 or not isinstance(value.get('families'), dict):
        raise ValueError('Unknown Office prune ledger')
    return value


def artifact_state(manifest):
    manifest = Path(manifest)
    root = next(p for p in manifest.parents if p.name == 'office')
    entry = prune_ledger(root)['families'].get(str(manifest))
    return entry['state'] if entry else json.loads(manifest.read_text())['state']


def require_live_artifacts(result):
    manifest = result.get('artifact_retention')
    if not manifest: return
    if artifact_state(manifest) != 'retained':
        raise ValueError('Office visual artifacts were pruned; fresh capture/reverification required')
    for artifact in json.loads(Path(manifest).read_text())['artifacts']:
        path = Path(artifact['path'])
        if not path.is_file() or digest(path) != artifact['sha256']:
            raise ValueError('Office visual artifact unavailable; fresh capture/reverification required')


def retain_capture(folder, result, image):
    """Publish heavy files only after capture, cleanup and source validation pass.

    Immutable receipts point to artifact_retention: consumers must check that
    lifecycle record before using artifact paths. GC never rewrites their bytes.
    """
    native = result.get('native_window') or {}
    rendered = native.pop('word_rendered', None)
    if result['status'] != 'CAPTURED':
        for key in ('screenshot', 'screenshot_sha256'):
            result.pop(key, None)
        native.pop('output', None)
        return
    target = folder/('view-'+uuid.uuid4().hex+'.png' if result['request'].get('recovery_id') else 'view.png')
    rendered_path = folder/'rendered.json'
    lifecycle = folder/'artifact-retention.json'
    try:
        os.replace(image, target)
        result['screenshot'] = str(target)
        if 'output' in native: native['output'] = str(target)
        artifacts = [{'kind':'screenshot','path':str(target),'sha256':digest(target)}]
        if rendered is not None:
            publish_json(rendered_path, rendered)
            native['word_rendered'] = {'path':str(rendered_path), 'sha256':digest(rendered_path),
                'method':rendered.get('method'), 'viewport':rendered.get('viewport'),
                'line_count':len(rendered.get('lines', []))}
            artifacts.append({'kind':'rendered','path':str(rendered_path),'sha256':digest(rendered_path)})
        result['artifact_retention'] = str(lifecycle)
        ownership = {}
        try:
            ac = next(p.parent for p in folder.parents if p.name == 'office')
            session, stage = saved_office_session(ac)
            if stage != 'session_complete': ownership = {'session':session, 'captured_ns':time.time_ns()}
        except (OSError, ValueError, StopIteration):
            pass  # Ambiguous legacy/manual captures are never assigned by guesswork.
        replace_json(lifecycle, {'version':1, 'state':'retained', 'result':str(folder/'result.json'),
                                 'artifacts':artifacts, **ownership})
    except (OSError, ValueError, RuntimeError) as exc:
        for path in (target, rendered_path, lifecycle):
            path.unlink(missing_ok=True)
        result.update(status='BLOCKED', action='Capture retention failed: '+str(exc), failure_kind='native_capture')
        for key in ('screenshot', 'screenshot_sha256', 'artifact_retention'):
            result.pop(key, None)
        native.pop('word_rendered', None)
        native.pop('output', None)


def permission_dependency(error, source, source_sha):
    return {'fact':'native document open', 'route':'owned Word open event',
            'kind':'permission', 'check':'attempted', 'action':error.permission_action,
            'inputs':[{'path':str(source),'sha256':source_sha,
                       'observation':'Source for the owned Word copy'}]}


def validate_word_receipt(result):
    require_live_artifacts(result)
    if result.get('request', {}).get('app') != 'word':
        return
    proof = result.get('word_visible_page') or {}
    request = result['request']
    if (proof.get('method') != 'unique-page-text-in-captured-canvas' or
            proof.get('copy_sha256') != result['source_sha256'] or
            proof.get('screenshot_sha256') != result['screenshot_sha256'] or
            type(proof.get('page')) is not int or proof['page'] < 1 or
            (request.get('page') is not None and proof['page'] != request['page'])):
        raise NativeError('Retained Word capture lacks bound visible-page evidence', 'native_capture')


def validate_png(path):
    """Decode 8-bit macOS PNG pixels; test only size and gross capture failure."""
    try:
        data=Path(path).read_bytes()
        if len(data)<512 or data[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('missing/empty or nontrivial PNG unavailable')
        pos=8;compressed=bytearray();header=None;ended=False
        while pos+12<=len(data):
            length=struct.unpack('>I',data[pos:pos+4])[0];kind=data[pos+4:pos+8];payload=data[pos+8:pos+8+length]
            crc=data[pos+8+length:pos+12+length]
            if len(crc)!=4 or struct.unpack('>I',crc)[0] != zlib.crc32(kind+payload):raise ValueError('invalid PNG checksum')
            if kind==b'IHDR':header=struct.unpack('>IIBBBBB',payload)
            elif kind==b'IDAT':compressed.extend(payload)
            elif kind==b'IEND':ended=True;break
            pos+=12+length
        if not header or not ended:raise ValueError('incomplete PNG')
        width,height,depth,color,compression,filter_method,interlace=header
        channels={0:1,2:3,4:2,6:4}.get(color)
        if not (200<=width<=16000 and 150<=height<=16000 and width*height<=40000000) or depth!=8 or not channels or compression or filter_method or interlace:
            raise ValueError('implausible dimensions or unsupported PNG encoding')
        stride=width*channels;expected=(stride+1)*height
        decoder=zlib.decompressobj();raw=decoder.decompress(bytes(compressed),expected+1)
        if len(raw)!=expected or not decoder.eof:raise ValueError('invalid PNG pixels')
        previous=bytearray(stride);samples=[]
        for y in range(height):
            offset=y*(stride+1);mode=raw[offset];row=bytearray(raw[offset+1:offset+1+stride])
            if mode>4:raise ValueError('invalid PNG filter')
            for x in range(stride):
                a=row[x-channels] if x>=channels else 0;b=previous[x];c=previous[x-channels] if x>=channels else 0
                if mode==1:value=a
                elif mode==2:value=b
                elif mode==3:value=(a+b)//2
                elif mode==4:
                    p=a+b-c;pa,pb,pc=abs(p-a),abs(p-b),abs(p-c)
                    value=a if pa<=pb and pa<=pc else b if pb<=pc else c
                else:value=0
                row[x]=(row[x]+value)&255
            if y%max(1,height//200)==0:
                for x in range(0,width,max(1,width//200)):
                    pixel=row[x*channels:(x+1)*channels]
                    samples.append(sum(pixel[:3])/3 if color in (2,6) else pixel[0])
            previous=row
        mean=sum(samples)/len(samples);variance=sum((v-mean)**2 for v in samples)/len(samples)
        if mean<2 or variance<4:raise ValueError('effectively uniform/black capture')
        return {'width':width,'height':height,'bytes':len(data)}
    except (OSError,ValueError,struct.error,zlib.error) as exc:
        raise NativeError('Native capture failure: '+str(exc),'native_capture') from exc


def preflight(workspace, apps, output=None):
    output=output or sys.stdout
    if not apps:return True
    started = time.monotonic()
    tty = output.isatty()
    stop = threading.Event()
    def row(symbol, detail=''):
        seconds = int(time.monotonic() - started)
        text = f'Access      {symbol} {seconds//60:02}:{seconds%60:02}'
        if detail: text += '  '+detail
        if tty:
            try: width = os.get_terminal_size(output.fileno()).columns
            except (OSError, ValueError): width = 80
            text = text[:max(1, width-1)]
        print(('\r\033[2K' if tty else '')+text, end='' if tty else '\n', file=output, flush=True)
    def tick():
        while not stop.wait(1): row('…', 'Grant Office access if prompted')
    timer = None
    if tty:
        row('…', 'Grant Office access if prompted')
        timer = threading.Thread(target=tick, daemon=True)
        timer.start()
    def finish(symbol, detail=''):
        stop.set()
        if timer: timer.join()
        row(symbol, detail)
        if tty: print(file=output, flush=True)
    app=apps[0]
    previous_display = getattr(workspace.backend, 'access_display_active', False)
    workspace.backend.access_display_active = True
    try:
        with workspace.lock():
            for app in apps:
                for role in ('verify','view'):
                    path=workspace.slot(app,role)
                    opening_error = None
                    try:
                        workspace.populate(app,role);workspace.open(app,path)
                    except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
                        opening_error = exc
                        raise
                    finally:
                        try:
                            workspace.close(app,path)
                        except (OSError,RuntimeError,subprocess.SubprocessError):
                            if not getattr(opening_error, 'permission_action', None):
                                raise
    except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
        finish('✗', app.title()+' fixed-slot access failed: '+str(exc).splitlines()[0][:180])
        return False
    finally:
        stop.set()
        if timer: timer.join()
        workspace.backend.access_display_active = previous_display
    finish('✓')
    return True


def check_power(pid, attempts=10):
    for _ in range(attempts):
        result=subprocess.run(['/usr/bin/pmset','-g','assertions'],capture_output=True,text=True,timeout=5)
        own='\n'.join(line for line in result.stdout.splitlines() if re.search(r'\bpid\s+'+str(pid)+r'\(caffeinate\)',line))
        if result.returncode==0 and 'PreventUserIdleDisplaySleep' in own and 'PreventUserIdleSystemSleep' in own:return
        time.sleep(.1)
    raise NativeError('Controller caffeinate assertion could not be verified','power')


def publish_json(path, value):
    """Publish complete immutable records, never leave a partial receipt."""
    temporary = path.with_name(path.name+'.tmp.'+uuid.uuid4().hex)
    try:
        with temporary.open('x') as out:
            json.dump(value,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        temporary.chmod(0o444)
        try: os.link(temporary,path)
        except FileExistsError:
            if json.loads(path.read_text())!=value: raise ValueError('Conflicting immutable record: '+str(path))
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:temporary.unlink(missing_ok=True)


def request_record(workspace, ident):
    if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):
        raise ValueError('Invalid observation request ID')
    path=workspace.root/'requests'/(ident+'.json')
    receipt=workspace.root/'receipts'/(ident+'.json')
    safe_slot(path);safe_slot(receipt)
    request=json.loads(path.read_text());result=json.loads(receipt.read_text())
    if result.get('request_id')!=ident or result.get('request')!=request:
        raise ValueError('Observation receipt/request binding mismatch')
    if result.get('status') not in ('CAPTURED','BLOCKED','PRODUCED','FAILED','UNAVAILABLE'):
        raise ValueError('Observation request has no completed receipt')
    if result.get('status') == 'CAPTURED':
        validate_word_receipt(result)
    return request,result,receipt


def navigation_request(requirement):
    """Translate a supported fact into an ordinary request, without old IDs."""
    if requirement.get('fact')!='workbook_navigation':
        return None
    for key in ('source','source_sha256','worksheet','return_sheet'):
        if not isinstance(requirement.get(key),str) or not requirement[key]:
            raise ValueError('Navigation requirement needs '+key)
    links=requirement.get('links')
    if not isinstance(links,list) or not 1<=len(links)<=32:
        raise ValueError('Navigation requires one to 32 links')
    for link in links:
        if (not isinstance(link,dict) or not isinstance(link.get('sheet'),str) or not link['sheet'] or
            any(not isinstance(link.get(k),str) or not re.fullmatch('[A-Z]{1,3}[1-9][0-9]*',link[k]) for k in ('cell','range'))):
            raise ValueError('Navigation needs link cell and expected sheet/range')
    return {'app':'excel','source':requirement['source'],'source_sha256':requirement['source_sha256'],
            'worksheet':requirement['worksheet'],'operation':'navigation','requirement':requirement}


def validate_navigation_links(requirement):
    """Check authored OOXML links independently of native transition evidence."""
    import posixpath
    import xml.etree.ElementTree as ET
    main='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    rel='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    def member(base,target):
        name=posixpath.normpath(posixpath.join(posixpath.dirname(base),target)) if not target.startswith('/') else target[1:]
        if not name.startswith('xl/'):raise NativeError('External worksheet relationship','verification')
        return name
    with zipfile.ZipFile(requirement['source']) as z:
        relationships={r.attrib['Id']:member('xl/workbook.xml',r.attrib['Target']) for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        sheets={r.attrib['name']:relationships[r.attrib[rel+'id']] for r in ET.fromstring(z.read('xl/workbook.xml')).findall(main+'sheets/'+main+'sheet')}
        if requirement['worksheet'] not in sheets or requirement['return_sheet'] not in sheets:
            raise NativeError('Missing navigation or return worksheet','verification')
        sheet=sheets[requirement['worksheet']]
        relpath=posixpath.dirname(sheet)+'/_rels/'+posixpath.basename(sheet)+'.rels'
        targets={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(z.read(relpath))} if relpath in z.namelist() else {}
        links={r.attrib['ref']:r.attrib.get('location') or targets.get(r.attrib.get(rel+'id')) for r in ET.fromstring(z.read(sheet)).findall(main+'hyperlinks/'+main+'hyperlink')}
        for link in requirement['links']:
            target=links.get(link['cell']) or ''
            expected="'"+link['sheet'].replace("'","''")+"'!"+link['range']
            if link['sheet'] not in sheets or target.lstrip('#').replace('$','') not in (expected,link['sheet']+'!'+link['range']):
                raise NativeError('Authored link destination mismatch: '+link['cell'],'verification')


def enqueue(workspace, request, head, request_id=None):
    app=request.get('app')
    if app not in APPS:raise ValueError('Request app must be excel or word')
    if app=='excel' and not isinstance(request.get('worksheet'),str):raise ValueError('Excel view requires worksheet')
    source=Path(request['source']).resolve(strict=True)
    if source.suffix.lower()!=EXTENSIONS[app]:raise ValueError('Unexpected source extension')
    request=dict(request,source=str(source),source_sha256=digest(source),requested_head=head)
    if request.get('operation')=='navigation':
        navigation_request(request['requirement'])
        request.setdefault('created_at_ns',time.time_ns())
        if request['source_sha256']!=request['requirement']['source_sha256']:raise ValueError('Navigation source changed')
    folder=safe_directory(workspace.root/'requests');path=folder/((request_id or uuid.uuid4().hex)+'.json')
    publish_json(path,request)
    return path


def process_requests(workspace, apps, head, request_ids=None):
    if not apps:return []
    folder=safe_directory(workspace.root/'receipts');results=[]
    if request_ids is not None and (not isinstance(request_ids,list) or
            any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{32}',i) for i in request_ids)):
        raise ValueError('Invalid controller observation request IDs')
    paths = ([workspace.root/'requests'/(i+'.json') for i in request_ids] if request_ids is not None
             else sorted((workspace.root/'requests').glob('*.json')))
    for path in paths:
        receipt=folder/path.name
        safe_slot(receipt)
        if receipt.exists():continue  # No automatic replay, including failed captures.
        request=None
        try:
            if path.is_symlink():raise ValueError('Request cannot be a symbolic link')
            request=json.loads(path.read_text())
            if request.get('app') not in apps:raise ValueError('Requested application is not enabled')
            # Implementation requests commonly precede their checkpoint. The
            # content hash, not equality with the later commit, binds the input.
            if digest(Path(request['source'])) != request['source_sha256']:
                raise ValueError('Observation source changed before capture')
            # The exact request owns a stable result location. An interruption
            # after capture but before receipt publication reuses that result.
            evidence=safe_directory(workspace.root/'evidence'/request['app']/path.stem)
            result_path=evidence/'result.json'
            if result_path.exists():
                result=json.loads(result_path.read_text())
                if result.get('request')!=request:raise ValueError('Recovery result binding mismatch')
                if result.get('status')=='CAPTURED':
                    validate_word_receipt(result)
                    validate_png(result['screenshot'])
                    if digest(Path(result['screenshot']))!=result['screenshot_sha256']:
                        raise ValueError('Recovery image changed')
            else:
                if request.get('operation')=='navigation':
                    started=evidence/'started.json'
                    if started.exists():
                        result={'status':'UNAVAILABLE','failure_kind':'interrupted',
                                'action':'Navigation interrupted; partial action receipts retained, no blind replay',
                                'source':request['source'],'source_sha256':request['source_sha256'],
                                'request':request,'completed_at_ns':time.time_ns(),
                                'actions':[json.loads(p.read_text()) for p in sorted(evidence.glob('action-*.json'))],
                                'evidence':str(result_path)}
                        workspace.finish(evidence,result)
                    else:
                        publish_json(started,{'request':request})
                        result=workspace.navigate(request,folder=evidence)
                else:result=workspace.view(request,folder=evidence,request_id=path.stem)
        except (OSError,ValueError,KeyError,RuntimeError) as exc:
            result={'status':'BLOCKED','failure_kind':getattr(exc,'kind','native_capture'),'action':str(exc)}
            if isinstance(request,dict):result['request']=request
        result.update(request_id=path.stem,controller_pid=os.getppid(),reviewed_head=head)
        publish_json(receipt,result)
        results.append(str(receipt))
    return results


def review_evidence(workspace, request_ids=None):
    records=[]
    paths = ([workspace.root/'receipts'/(i+'.json') for i in request_ids] if request_ids is not None
             else sorted((workspace.root/'receipts').glob('*.json')))
    for path in paths:
        try:
            result=json.loads(path.read_text())
            if result.get('status')=='CAPTURED':
                source=Path(result['source'])
                if not source.exists() or digest(source)!=result['source_sha256']:continue
                validate_word_receipt(result)
                validate_png(result['screenshot'])
                if digest(result['screenshot'])!=result['screenshot_sha256']:
                    raise NativeError('Retained screenshot changed','native_capture')
            records.append(dict(result,receipt=str(path)))
        except (OSError,ValueError,KeyError,RuntimeError) as exc:
            records.append({'status':'BLOCKED','failure_kind':'native_capture','action':str(exc),'receipt':str(path)})
    return records


def reference_tokens(value):
    """Exact IDs/hashes/paths in controller records, including embedded reports."""
    if isinstance(value, dict):
        return set().union(*(reference_tokens(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(reference_tokens(v) for v in value)) if value else set()
    if isinstance(value, str):
        return {value, *re.findall(r'(?<![a-f0-9])[a-f0-9]{32}(?![a-f0-9])|(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])', value)}
    return set()


def local_path(path, repo):
    path = Path(path)
    try: return str(path.relative_to(repo))
    except ValueError: return str(path)


def office_receipts(workspace):
    records = []
    for path in sorted((workspace.root/'receipts').glob('*.json')):
        try:
            safe_slot(path)
            result = json.loads(path.read_text())
            if not isinstance(result, dict): raise ValueError('Receipt must be an object')
        except (OSError, ValueError, RuntimeError) as exc:
            result = {'locator_error':str(exc)}
        records.append((path, result))
    return records


def record_is_named(path, result, tokens, repo):
    candidates = {path.stem, str(path), local_path(path, repo)}
    recovery = (result.get('request') or {}).get('recovery_id')
    if recovery: candidates.add(recovery)
    for artifact in (result.get('screenshot'), ((result.get('native_window') or {}).get('word_rendered') or {}).get('path')):
        if isinstance(artifact, str): candidates.update((artifact, local_path(artifact, repo)))
    if path.is_file() and not path.is_symlink(): candidates.add(digest(path))
    evidence = result.get('evidence')
    if isinstance(evidence, str):
        target = Path(evidence)
        candidates.update((str(target), local_path(target, repo)))
        if target.is_file() and not target.is_symlink(): candidates.add(digest(target))
    return bool(candidates.intersection(tokens))


def selected_receipts(workspace, repo, head, current):
    attempts = current.get('attempts', [])
    active = attempts[current.get('session_start', 0):]
    attempt = active[-1] if active else {}
    roots = [current.get('work'), attempt, current.get('block'), current.get('missing_evidence'), current.get('observations', []), current.get('legacy_observation_review')]
    bound_handoff = False
    # Match the persisted handoff's exact work/attempt identity; no latest-file rule.
    handoff_path = workspace.root.parent/'native-handoff.json'
    if handoff_path.exists():
        handoff = json.loads(handoff_path.read_text())
        binding = handoff.get('identity', {}).get('binding', {})
        if (attempt.get('id') and (binding.get('attempt') or {}).get('id') == attempt['id']
                and (binding.get('work') or {}).get('id') == (current.get('work') or {}).get('id')):
            roots.append(handoff.get('requests', []))
            bound_handoff = True  # An explicit empty handoff is also authoritative.
    candidate_path = workspace.root.parent/'candidate.json'
    if candidate_path.exists():
        candidate = json.loads(candidate_path.read_text())
        if candidate.get('head') == head: roots.append(candidate)
    tokens = reference_tokens(roots)
    records = office_receipts(workspace)
    explicit = [pair for pair in records if record_is_named(*pair, tokens, repo)]
    # Strong request bindings (including queued requests with no receipt yet)
    # suppress broad current-head fallback, even if none has completed.
    request_ids = {p.stem for p in (workspace.root/'requests').glob('*.json')}
    def request_binding(value):
        if isinstance(value, dict):
            return (any(value.get(k) for k in ('request_id','replacement_request_id','replaces_request_id'))
                    or any(request_binding(v) for v in value.values()))
        return isinstance(value, list) and any(request_binding(v) for v in value)
    strong = bool(bound_handoff or request_binding(roots) or explicit or request_ids.intersection(tokens))
    prior_tokens = reference_tokens([a for a in attempts if a is not attempt])
    selected = []
    for path, result in records:
        if (path, result) in explicit:
            selected.append((path, result)); continue
        request = result.get('request') or {}
        # Legacy state lacks an attempt handoff; only an exact revision binding
        # can admit a record, never source similarity, age or directory order.
        if (not strong and head and (result.get('reviewed_head') == head or
                (not result.get('reviewed_head') and request.get('requested_head') == head))
                and not record_is_named(path, result, prior_tokens, repo)):
            selected.append((path, result))
    return selected


def write_review_index(workspace, repo, head, current):
    entries = []
    for path, result in selected_receipts(workspace, repo, head, current):
        request = result.get('request') or {}
        entry = {'request_id':path.stem, 'receipt':local_path(path, repo)}
        if path.is_file() and not path.is_symlink(): entry['receipt_sha256'] = digest(path)
        for key in ('status','source','source_sha256','reviewed_head','locator_error'):
            value = result.get(key, request.get(key))
            if isinstance(value, (str, int, bool)): entry[key] = value
        for key in ('app','operation','worksheet','range','page','requested_head','recovery_id','replaces_request_id'):
            if isinstance(request.get(key), (str, int, bool)): entry[key] = request[key]
        if result.get('evidence'):
            entry['result'] = local_path(result['evidence'], repo)
            target = Path(result['evidence'])
            if target.is_file() and not target.is_symlink(): entry['result_sha256'] = digest(target)
        retained = True
        if result.get('artifact_retention'):
            entry['artifact_retention'] = local_path(result['artifact_retention'], repo)
            try:
                lifecycle = json.loads(Path(result['artifact_retention']).read_text())
                entry['artifact_state'] = artifact_state(result['artifact_retention'])
                retained = entry['artifact_state'] == 'retained'
            except (OSError, ValueError, KeyError):
                entry['artifact_state'] = 'unresolved'; retained = False
        if retained:
            if result.get('screenshot'):
                entry['screenshot'] = local_path(result['screenshot'], repo)
                entry['screenshot_sha256'] = result.get('screenshot_sha256')
            rendered = (result.get('native_window') or {}).get('word_rendered') or {}
            if rendered.get('path'):
                entry['rendered'] = local_path(rendered['path'], repo)
                entry['rendered_sha256'] = rendered.get('sha256')
        entries.append(entry)
    path = workspace.root/'review-index.json'
    replace_json(path, {'version':1, 'reviewed_head':head, 'entries':entries})
    return 'Native Office evidence index:\npath: '+local_path(path, repo)+'\nsha256: '+digest(path)+'\nentries: '+str(len(entries))


def review_preflight(stream):
    """NUL-delimited name/value pairs on stdin avoid argv/environment limits."""
    fields = stream.read().split('\0')
    if fields[-1] == '': fields.pop()
    if len(fields) % 2: raise ValueError('Malformed Review prompt size input')
    components = dict(zip(fields[::2], fields[1::2]))
    prompt = components.pop('prompt')
    sizes = {name:len(value) for name, value in components.items()}
    sizes['instructions_and_other'] = len(prompt)-sum(sizes.values())
    report = {'total_chars':len(prompt), 'limit_chars':900000, 'components':sizes}
    if len(prompt) >= 900000:
        print('Review prompt preflight refused: '+json.dumps(report), file=sys.stderr)
        return 2
    return 0


def confined_file(path, root):
    """No redirected ancestors, hard links, directories, or out-of-tree targets."""
    path = Path(path).absolute()
    if not path.is_relative_to(root) or '..' in path.parts:
        raise ValueError('Artifact is outside Office: '+str(path))
    for part in (path, *path.parents):
        if part.is_symlink(): raise ValueError('Symbolic link: '+str(part))
    safe_slot(path)
    return path


def retention_roots(workspace, repo, instruction_rows=(), instruction_errors=()):
    """Only live Session roots; historical records remain readable, not GC roots."""
    ac = workspace.root.parent
    documents, unresolved = list(instruction_rows), list(instruction_errors)
    try:
        confined_file(ac/'work-state.json', ac)
        states = json.loads((ac/'work-state.json').read_text())['branches']
        for state in states.values():
            attempts = state.get('attempts', [])[state.get('session_start', 0):]
            documents.extend([state.get('work'), attempts[-1:] , state.get('block'),
                              state.get('missing_evidence'), state.get('observations', []), state.get('legacy_observation_review'), state.get('recoveries', [])[state.get('session_recovery_start', 0):],
                              state.get('office_retention', {}).get('accepted', [])])
            for attempt in attempts:
                report = attempt.get('report', {})
                if report.get('outcome') in ('VERIFIED','COMPLETE'):
                    documents.extend([report.get('evidence', []), report.get('endpoint', {}).get('evidence', [])])
            if attempts and attempts[-1].get('checkpoint_sha'):
                # Match the active attempt; recovery roots must not suppress its legacy fallback.
                documents.extend({'path':str(path)} for path, _ in
                    selected_receipts(workspace, repo, attempts[-1]['checkpoint_sha'],
                                      {'work':state.get('work'), 'attempts':attempts[-1:]}))
        for name in ('current-review','candidate.json','implementation-result.json',
                     'implementation-baseline.json','native-handoff.json','observer-call.json','latest-implementation',
                     'remote-docs-current.json'):
            path = ac/name
            if not path.exists() and not path.is_symlink(): continue
            confined_file(path, ac)
            documents.append(json.loads(path.read_text()) if path.suffix == '.json' else path.read_text())
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        unresolved.append({'path':str(ac/'work-state.json'),'reason':str(exc),'global':True})
    return documents, unresolved


def retention_boundary(workspace):
    """A Review admission alone is insufficient: the controller must save next."""
    ac = workspace.root.parent
    try:
        session, stage = saved_office_session(ac)
        if stage not in ('review_done','session_complete'): return None
        confined_file(ac/'work-state.json', ac)
        state = json.loads((ac/'work-state.json').read_text())['branches'][session['branch']]
        admitted = state.get('office_retention', {})
        confined_file(ac/'current-review', ac)
        if admitted.get('session') != session or admitted.get('review_sha256') != digest(ac/'current-review'):
            return None
        # The save must follow admission even when a resumed Review is unchanged.
        if (ac/'resume-state').stat().st_mtime_ns < admitted['admitted_ns']: return None
        return session, stage, admitted['admitted_ns']
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def gc_plan(workspace, repo, instruction_rows=(), instruction_errors=()):
    root = workspace.root
    boundary = retention_boundary(workspace)
    prune_ledger(root)
    completed = set()
    if boundary:
        states = json.loads((root.parent/'work-state.json').read_text())['branches']
        completed = {(branch, number) for branch, state in states.items()
                     for number in state.get('office_completed_sessions', [])}
        if boundary[1] == 'session_complete':
            completed.add((boundary[0]['branch'], boundary[0]['number']))
    documents, unresolved = retention_roots(workspace, repo, (), instruction_errors)
    if boundary and boundary[1] == 'session_complete':
        documents, unresolved, instruction_rows = [], [], ()  # Durable completion releases this Session's families only.
    # Only traverse repository-local state. Product files are never deletion
    # candidates. Follow explicit references into logs/receipts, not all logs.
    index = defaultdict(list)
    for path in root.parent.rglob('*'):
        if path.is_file() and path.name not in ('review-index.json','prune-ledger.json') and not path.name.endswith('.lock'):
            try:
                confined_file(path, root.parent)
                keys = {str(path), local_path(path, repo), digest(path)}
                if path.parent.name in ('receipts', 'requests'): keys.add(path.stem)
                for key in keys: index[key].append(path)
            except (OSError, ValueError, RuntimeError):
                pass
    marked, checked_refs, seen_tokens = set(), set(), set()
    pending = deque()
    # Delimiters separate complete identifiers; dots, slashes, hyphens and
    # alphanumeric suffixes stay inside tokens. Never recover a basename,
    # partial hash, or a prefix of a longer artifact path.
    token_pattern = re.compile(r"[^\s\"'`<>{}\[\](),;]+")

    def resolve(token):
        if token in seen_tokens: return
        seen_tokens.add(token)
        for path in index.get(token, ()):
            if path not in marked:
                marked.add(path)
                pending.append(path)

    def check_path(name, sha=None):
        p = Path(name) if Path(name).is_absolute() else repo/name
        key = (str(p), sha if isinstance(sha, str) else repr(sha))
        if p.is_relative_to(root.parent) and key not in checked_refs:
            checked_refs.add(key)
            try:
                confined_file(p, root.parent)
                if not p.is_file(): raise ValueError('Referenced artifact is missing')
                if sha and digest(p) != sha: raise ValueError('Referenced artifact hash mismatch')
            except (OSError, ValueError, RuntimeError) as exc:
                unresolved.append({'path':str(p), 'reason':str(exc)})

    def add(value, instruction=False):
        objects = [value]
        while objects:
            obj = objects.pop()
            if isinstance(obj, dict):
                name = obj.get('path')
                if isinstance(name, str): check_path(name, obj.get('sha256'))
                objects.extend(obj.values())
            elif isinstance(obj, list):
                objects.extend(obj)
            elif isinstance(obj, str):
                resolve(obj)
                for match in re.finditer(r"`([^`\n]+)`|\"([^\"\n]+)\"|'([^'\n]+)'", obj):
                    resolve(next(value for value in match.groups() if value is not None))
                for match in token_pattern.finditer(obj): resolve(match.group())
                # Labels may touch an exact ID/hash (sha256:..., request_id=...).
                # Word/path characters on either side still reject superstrings.
                for match in re.finditer(r'(?<![\w./-])(?:[a-f0-9]{64}|[a-f0-9]{32})(?![\w./-])', obj):
                    resolve(match.group())
                # Free-form instruction words (including nonexistent path-like
                # words) cannot make the whole store ambiguous. Explicit JSON
                # path/hash bindings still receive normal integrity checks.
                if not instruction and obj.startswith((str(root.parent)+'/', '.git/autocycle/')) and '\n' not in obj:
                    check_path(obj)
                if not instruction:
                    for line in obj.splitlines():
                        if line.startswith('LOG: '): check_path(line[5:])
                # Find balanced JSON spans in one lexical pass, including
                # pretty-printed or multiple inline objects. Quoted braces do
                # not affect nesting; invalid spans are never reparsed as a
                # sequence of overlapping suffixes.
                brackets, start = [], None
                for match in re.finditer(r'"(?:\\.|[^"\\])*"|[{}\[\]]', obj):
                    token = match.group()
                    if token.startswith('"'): continue
                    if token in ('{', '['):
                        if not brackets: start = match.start()
                        brackets.append(token)
                    elif brackets:
                        if brackets[-1] != ('{' if token == '}' else '['):
                            brackets.clear(); start = None
                            continue
                        brackets.pop()
                        if not brackets:
                            try: embedded = json.loads(obj[start:match.end()])
                            except (ValueError, RecursionError): continue
                            objects.append(embedded)

    for value in documents: add(value)
    for value in instruction_rows: add(value, instruction=True)
    # Each catalog record is enqueued and parsed at most once, even in cycles.
    while pending:
        path = pending.popleft()
        if path.suffix in ('.png','.docx','.xlsx') or path.name == 'rendered.json': continue
        try:
            raw = path.read_text()
            add(json.loads(raw) if path.suffix == '.json' else raw)
        except (OSError, ValueError, UnicodeError) as exc:
            unresolved.append({'path':str(path),'reason':str(exc)})

    groups, known, ambiguous = [], set(), {}
    for manifest in (root/'evidence').rglob('artifact-retention.json'):
        artifacts = []
        try:
            confined_file(manifest, root)
            value = json.loads(manifest.read_text())
            if value.get('version') != 1 or value.get('state') not in ('retained','pruning','pruned'):
                raise ValueError('Unknown retention format/state')
            result_path = manifest.parent/'result.json'
            confined_file(result_path, root)
            result = json.loads(result_path.read_text())
            if (value.get('result') != str(result_path) or result.get('artifact_retention') != str(manifest)
                    or result.get('status') != 'CAPTURED'):
                raise ValueError('Retention/result binding mismatch')
            lifecycle_state = artifact_state(manifest)
            for artifact in value['artifacts']:
                path = confined_file(Path(artifact['path']), root)
                if path.parent != manifest.parent or artifact['kind'] not in ('screenshot','rendered'):
                    raise ValueError('Artifact does not belong to capture family')
                if ((artifact['kind']=='rendered' and path.name!='rendered.json') or
                        (artifact['kind']=='screenshot' and not re.fullmatch(r'view(?:-[a-f0-9]{32})?\.png',path.name))
                        or path in artifacts):
                    raise ValueError('Not a unique managed heavy capture artifact')
                expected = ({'path':result.get('screenshot'),'sha256':result.get('screenshot_sha256')}
                            if artifact['kind']=='screenshot' else (result.get('native_window') or {}).get('word_rendered', {}))
                if artifact['path'] != expected.get('path') or artifact['sha256'] != expected.get('sha256'):
                    raise ValueError('Artifact/result hash binding mismatch')
                if not path.exists():
                    if lifecycle_state in ('pruned','pruning'): continue
                    raise ValueError('Retained artifact is missing: '+str(path))
                if digest(path) != artifact['sha256']: raise ValueError('Artifact hash mismatch: '+str(path))
                artifacts.append(path)
            if not artifacts: continue
            known.update(artifacts)
            rooted = any(p in marked for p in (manifest, result_path, *artifacts))
            def depends_on_unresolved(item):
                if item.get('global'): return True
                path = Path(item['path'])
                if path.parent == manifest.parent or manifest.is_relative_to(path): return True
                if path.parent in (root/'receipts', root/'requests'):
                    # New captures retain the exact request ID in result.json,
                    # so even a lost receipt cannot sever its retention root.
                    # Weakly bound legacy families remain ambiguous, never free.
                    return not result.get('request_id') or result['request_id'] == path.stem
                # A missing/corrupt intermediate log/state reference could name
                # any family. There is no basis for declaring it unreferenced.
                if path.is_relative_to(root/'evidence') and path.parent.joinpath('artifact-retention.json').is_file():
                    return False  # Another explicitly owned capture family.
                return True
            uncertain = any(depends_on_unresolved(u) for u in unresolved)
            session = value.get('session') or {}
            finished = (session.get('branch'), session.get('number')) in completed
            owned = boundary and session == boundary[0]
            eligible = finished or (owned and isinstance(value.get('captured_ns'), int)
                                    and value['captured_ns'] <= boundary[2])
            release_roots = boundary and boundary[1] == 'session_complete' and finished
            category = ('ambiguous' if (uncertain and not release_roots) or not session else
                        'retainable' if not eligible or (rooted and not release_roots) else 'prunable')
            groups.append((manifest, value, artifacts, category))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            unresolved.append({'path':str(manifest),'reason':str(exc)})
            for p in manifest.parent.iterdir():
                if p.is_file() and (p.suffix=='.png' or p.name=='rendered.json'):
                    ambiguous[p] = 'Unresolved capture family: '+str(exc)
    # No ownership/lifecycle manifest means legacy or interrupted: never infer
    # disposability from age, absence from this Review, or an incomplete record.
    for path in (root/'evidence').rglob('*'):
        if path.is_file() and (path.suffix == '.png' or path.name == 'rendered.json') and path not in known:
            ambiguous.setdefault(path, 'No validated retention manifest (legacy or interrupted)')
        elif path.name == 'result.json' and path.is_file() and not (path.parent/'artifact-retention.json').exists():
            try:
                value = json.loads(path.read_text())
                if (value.get('native_window') or {}).get('word_rendered', {}).get('lines'):
                    ambiguous[path] = 'Legacy inline rendered evidence; no migration or pruning'
            except (OSError, ValueError, AttributeError): pass
    report = {key:{'files':0,'bytes':0,'artifacts':[]} for key in ('retainable','prunable','ambiguous')}
    for _, _, artifacts, category in groups:
        for path in artifacts:
            if path in ambiguous: category_for_file = 'ambiguous'
            else: category_for_file = category
            report[category_for_file]['artifacts'].append({'path':str(path),'bytes':path.stat().st_size})
    for path, reason in ambiguous.items():
        if path not in known:
            report['ambiguous']['artifacts'].append({'path':str(path),'bytes':path.lstat().st_size,'reason':reason})
    for bucket in report.values():
        bucket['files'] = len(bucket['artifacts']); bucket['bytes'] = sum(a['bytes'] for a in bucket['artifacts'])
    report['unresolved'] = unresolved
    return report, groups


def garbage_collect(workspace, repo, dry_run=False, controller_fd=None):
    # Use the exact controller lock for the entire transaction, not a racy PID
    # check. Opening existing state locks does not change non-Office state.
    ac = workspace.root.parent
    with contextlib.ExitStack() as stack:
        controller = ac/'controller.lock'
        if not controller.exists():
            raise NativeError('Controller lock absent; cannot establish AutoCycle is inactive')
        if controller_fd is not None:
            confined_file(controller, ac)
            st, inherited = controller.stat(), os.fstat(controller_fd)
            if (st.st_dev, st.st_ino) != (inherited.st_dev, inherited.st_ino):
                raise NativeError('Inherited controller lock does not match')
            fcntl.flock(controller_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in ((controller,) if controller_fd is None else ()) + (ac/'work-state.lock',):
            if not path.exists(): continue
            confined_file(path, ac)
            lock = stack.enter_context(path.open('rb'))
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise NativeError('AutoCycle controller or progress writer is active; GC refused') from None
        stack.enter_context(workspace.lock())
        instruction_rows, instruction_errors = [], []
        database = ac/'instructions.sqlite3'
        if database.exists() or database.is_symlink():
            try:
                confined_file(database, ac)
                db = stack.enter_context(contextlib.closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)))
                db.execute('BEGIN')
                # Instruction submission can run independently of the controller.
                # Hold its rollback-journal read lock until GC has completed.
                tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if db.execute('PRAGMA journal_mode').fetchone()[0] not in ('delete','truncate','persist'):
                    raise ValueError('Instruction storage cannot hold a stable GC read lock')
                for (table,) in tables:
                    quoted = '"'+table.replace('"','""')+'"'
                    for row in db.execute('SELECT * FROM '+quoted):
                        instruction_rows.extend(v for v in row if isinstance(v, str))
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                instruction_errors.append({'path':str(database),'reason':str(exc),'global':True})
        report, groups = gc_plan(workspace, repo, instruction_rows, instruction_errors)
        report['dry_run'] = dry_run
        report['deleted_files'] = 0
        report['deleted_bytes'] = 0
        sizes = {a['path']:a['bytes'] for a in report['prunable']['artifacts']}
        if not dry_run:
            ledger = prune_ledger(workspace.root)
            for manifest, value, artifacts, category in groups:
                if category != 'prunable': continue
                # Validate every file again before journaling or unlinking any
                # member. An individually unresolved family is never deleted.
                try:
                    for path in artifacts:
                        confined_file(path, workspace.root)
                        expected = next(a['sha256'] for a in value['artifacts'] if a['path']==str(path))
                        if digest(path) != expected: raise ValueError('Artifact changed during GC')
                except (OSError, ValueError, RuntimeError) as exc:
                    report['unresolved'].append({'path':str(manifest),'reason':str(exc)})
                    continue
                entry = {'state':'pruning', 'session':value.get('session'),
                         'manifest_sha256':digest(manifest)}
                ledger['families'][str(manifest)] = entry
                replace_json(workspace.root/'prune-ledger.json', ledger)
                for path in artifacts:
                    path.unlink(); report['deleted_files'] += 1
                    report['deleted_bytes'] += sizes[str(path)]
                entry['state'] = 'pruned'
                replace_json(workspace.root/'prune-ledger.json', ledger)
        return report


def controller_parent():
    command=subprocess.run(['ps','-p',str(os.getppid()),'-o','command='],capture_output=True,text=True,check=True).stdout
    words=shlex.split(command)
    # This is a context guard, not an OS security boundary. Providers only queue.
    if not any(Path(word).name=='autocycle' for word in words[:3]):
        raise NativeError('Native capture must be dispatched by the AutoCycle controller','native_capture')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('capabilities','preflight','probe','power-check','request','process','review-evidence','review-preflight','gc','verify'))
    parser.add_argument('args',nargs='*')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--controller-fd', type=int)
    args=parser.parse_args()
    if args.operation=='review-preflight': return review_preflight(sys.stdin)
    from adjudication import location
    repo=Path(subprocess.check_output(['git','rev-parse','--show-toplevel'],text=True).strip())
    workspace=Workspace(location());apps=capabilities(repo)
    if args.operation=='capabilities':print(' '.join(apps))
    elif args.operation in ('preflight','probe'):
        selected=tuple(args.args) if args.operation=='probe' else apps
        if any(a not in APPS for a in selected):raise ValueError('Unsupported Office application')
        if os.environ.get('AUTOCYCLE_ACCESS_QUIET') == '1':
            output = io.StringIO()
            passed = preflight(workspace, selected, output)
            if not passed: print(output.getvalue(), end='')
            return 0 if passed else 2
        return 0 if preflight(workspace,selected) else 2
    elif args.operation=='power-check':check_power(int(args.args[0]))
    elif args.operation=='request':
        request=json.loads(Path(args.args[0]).read_text())
        if request.get('app') not in apps:raise ValueError('Project has not opted into this application')
        print(enqueue(workspace,request,subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()))
    elif args.operation=='process':
        if apps:
            controller_parent()
            from adjudication import native_handoff_ids
            selected=json.loads(args.args[0]) if args.args else native_handoff_ids()
            process_requests(workspace,apps,subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
                             selected)
    elif args.operation=='review-evidence':
        from progress import state
        with state() as current:
            print(write_review_index(workspace, repo, subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(), current))
    elif args.operation=='gc':
        print(json.dumps(garbage_collect(workspace, repo, args.dry_run, args.controller_fd), indent=2))
    elif args.operation=='verify':
        app,source,*command=args.args
        if app not in apps:raise ValueError('Project has not opted into this application')
        result=workspace.verify(app,source,command);print(json.dumps(result,indent=2))
        return 0 if result['status']=='VERIFIED' else 2
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(2)

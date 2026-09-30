#!/usr/bin/env python3
"""Recalculate a stable verification copy in native Excel, then run the project verifier."""
import argparse
import hashlib
import fcntl
import json
import platform
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from native_office import safe_directory, safe_slot

# Never calculate application/all workbooks, change global settings, or use the
# active workbook. Arguments are data, not interpolated AppleScript or shell.
APPLESCRIPT = r'''
on run argv
    set copyPath to item 1 of argv
    set excelPath to (POSIX file copyPath) as text
    set operation to "open"
    with timeout of 120 seconds
        tell application "Microsoft Excel"
            activate
            try
                set verificationBook to open workbook workbook file name excelPath update links 0 read only false password "" write reserved password "" ignore read only recommended true add to mru false
                set operation to "confirm copy identity"
                if (POSIX path of ((full name of verificationBook) as text)) is not copyPath then
                    error "Excel returned a different workbook; no save or close attempted"
                end if
                set operation to "recalculate"
                -- Range calculation is scoped to this workbook. Revisit sheets
                -- for cross-sheet dependencies; the project verifier remains
                -- authoritative about required cached values and correctness.
                repeat 2 times
                    repeat with targetSheet in (get worksheets of verificationBook)
                        calculate (used range of targetSheet)
                    end repeat
                end repeat
                set operation to "save"
                set saved of verificationBook to false
                save verificationBook
                set operation to "close"
                close verificationBook saving no
                repeat with existingBook in (get workbooks)
                    try
                        set existingPath to POSIX path of ((full name of existingBook) as text)
                    on error
                        set existingPath to ""
                    end try
                    if existingPath is copyPath then error "Fixed verification slot is still open"
                end repeat
                return "Excel recalculated, saved and closed verification copy"
            on error messageText number errorNumber
                error "Excel " & operation & " failed: " & messageText number errorNumber
            end try
        end tell
    end timeout
end run
'''

# Only the owned fixed verify slot may be saved/closed before reuse.
# Other workbooks (including historical copies) are never lifecycle blockers.
ENSURE_CLOSED = r'''
on run argv
    set copyPath to item 1 of argv
    with timeout of 30 seconds
        tell application "Microsoft Excel"
            repeat with existingBook in (get workbooks)
                try
                    set existingPath to POSIX path of ((full name of existingBook) as text)
                on error
                    set existingPath to ""
                end try
                if existingPath is copyPath then
                    close existingBook saving yes
                end if
            end repeat
            repeat with existingBook in (get workbooks)
                try
                    set existingPath to POSIX path of ((full name of existingBook) as text)
                on error
                    set existingPath to ""
                end try
                if existingPath is copyPath then
                    error "Fixed verification slot is still open"
                end if
            end repeat
        end tell
    end timeout
end run
'''

def native_available():
    if platform.system() != 'Darwin':
        raise RuntimeError('Native Microsoft Excel verification requires macOS')
    # macOS Launch Services also supports Excel installed outside /Applications.
    result = subprocess.run(['/usr/bin/osascript','-e','POSIX path of (path to application id "com.microsoft.Excel")'],capture_output=True,text=True,timeout=15)
    if result.returncode:
        raise RuntimeError('Microsoft Excel is not installed or cannot be located: '+result.stderr.strip())

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verify(source, command, evidence_dir, timeout=180):
    source=Path(source).resolve(strict=True)
    if source.suffix.lower() != '.xlsx':
        raise ValueError('Use a macro-free .xlsx verification source')
    if '{workbook}' not in command:
        raise ValueError('Verifier command must include a separate literal {workbook} argument')
    evidence_dir=Path(evidence_dir);evidence_dir.mkdir(parents=True,exist_ok=True)
    stable=safe_directory(evidence_dir.resolve()/'office')
    archive=safe_directory(stable/'evidence'/'excel')
    folder=Path(tempfile.mkdtemp(prefix='verify-',dir=archive)).resolve()
    copy=stable/'excel-verify.xlsx'
    before=digest(source)
    result={'status':'BLOCKED','source':str(source),'source_sha256':before,'copy':str(copy),'action':'','evidence':str(folder/'result.json')}
    phase='prepare copy'
    lock=None
    try:
        stable.mkdir(parents=True,exist_ok=True)
        safe_slot(stable/'verification.lock')
        lock=(stable/'verification.lock').open('a')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another verification or native Office operation is running; retry after it finishes')
        safe_slot(copy)
        if copy.is_symlink() or (copy.exists() and copy.samefile(source)):
            raise RuntimeError('Verification path aliases another file; refusing to overwrite it')
        phase='locate native Excel'
        native_available()
        runner=[sys.executable,str(Path(__file__).with_name('adjudication.py')),'run',str(timeout)]
        phase='check verification workbook is closed'
        preflight=folder/'ensure_closed.applescript';preflight.write_text(ENSURE_CLOSED)
        checked=subprocess.run(runner+['osascript',str(preflight),str(copy)],capture_output=True,text=True)
        (folder/'preflight.log').write_text(checked.stdout+checked.stderr)
        if checked.returncode:
            detail=(checked.stderr or checked.stdout).strip()
            if checked.returncode==124 or '-1712' in detail:
                raise RuntimeError('Excel automation timed out; resolve any pending Grant Access dialog for the fixed slot. '+detail)
            if '-1743' in detail:
                raise RuntimeError('Allow macOS Automation permission to control Microsoft Excel. '+detail)
            raise RuntimeError(detail or 'Excel open-workbook check failed (exit '+str(checked.returncode)+')')
        phase='update verification copy'
        # Truncate/write in place; unlink/rename replacement can lose a grant.
        safe_slot(copy)
        shutil.copyfile(source,copy)
        if digest(copy)!=before:
            raise RuntimeError('Fixed slot copied bytes differ from source')
        script=folder/'recalculate.applescript';script.write_text(APPLESCRIPT)
        runner=[sys.executable,str(Path(__file__).with_name('adjudication.py')),'run',str(timeout)]
        phase='native Excel automation'
        native=subprocess.run(runner+['osascript',str(script),str(copy)],capture_output=True,text=True)
        (folder/'excel.log').write_text(native.stdout+native.stderr)
        if native.returncode:
            detail=(native.stderr or native.stdout).strip()
            if native.returncode==124 or '-1712' in detail:
                raise RuntimeError('Excel automation timed out. '+detail+' If Excel shows a Grant Access dialog for this verification path, grant that file once and reuse the same path. A timeout alone does not establish a permission failure.')
            if '-1743' in detail:
                raise RuntimeError('Allow macOS Automation permission for the invoking terminal/agent to control Microsoft Excel, then retry. '+detail)
            raise RuntimeError(detail or 'Excel automation exited '+str(native.returncode))
        if digest(source)!=before:
            raise RuntimeError('Source workbook changed during verification; preserve both files and investigate before resuming')
        phase='cached-value verification'
        saved_digest=digest(copy)
        args=[str(copy) if arg=='{workbook}' else arg for arg in command]
        checked=subprocess.run(runner+args,capture_output=True,text=True)
        (folder/'verification.log').write_text(checked.stdout+checked.stderr)
        phase='preserve saved verification evidence'
        snapshot=folder/'saved-copy.xlsx'
        shutil.copyfile(copy,snapshot)
        snapshot.chmod(0o444)
        result.update(copy_sha256=digest(copy),snapshot=str(snapshot))
        phase='cached-value verification'
        if digest(copy)!=saved_digest:
            raise RuntimeError('Verifier modified the native saved state')
        if checked.returncode:
            raise RuntimeError('Native Excel saved the copy but cached-value verification failed (exit '+str(checked.returncode)+'): '+(checked.stderr or checked.stdout).strip())
        result.update(status='VERIFIED',action='NONE')
    except (OSError,RuntimeError,subprocess.SubprocessError) as error:
        result['action']=f'Resolve {phase}: {error}. Verification copy: {copy}. Evidence: {folder}. Only the fixed slot may be saved/closed on retry; leave historical copies and immutable snapshots untouched.'
    finally:
        if lock is not None:
            lock.close()
    # A verifier must not be able to hide a concurrent source change as success.
    if not source.exists() or digest(source)!=before:
        result.update(status='BLOCKED',action='Source workbook changed during verification; preserve files and investigate. Evidence: '+str(folder))
    (folder/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    for item in folder.iterdir():
        if item.is_file():item.chmod(0o444)
    return result

def resume_blocker(cache):
    """Retry only a saved Excel-cache blocker; leave acceptance to fresh Review."""
    fields = dict(line.split(': ', 1) for line in Path(cache).read_text().splitlines() if ': ' in line)
    key = fields.get('BLOCKER_KEY', '').lower()
    reason = fields.get('REVIEW', '').lower()
    if fields.get('REVIEW_STATUS') != 'BLOCKED' or not (
        ('excel' in key and 'cached' in key) or
        ('excel' in reason and re.search(r'cached[- ]value', reason) and 'unavailable' in reason)
    ):
        return 3
    from adjudication import location
    evidence = location()
    folder = Path(tempfile.mkdtemp(prefix='excel-resume-', dir=evidence))
    answer = folder/'request.json'
    # Legacy blockers did not persist a machine-readable verifier command. Resolve
    # it read-only from project evidence, then use the same native helper as usual.
    prompt = f"""Resolve the saved Excel cached-value verification request only.
Saved Review: {Path(cache).read_text()}
Inspect IMPLEMENTATION.md, RESULT.md and existing verification scripts/logs to identify the
source workbook and the existing project cached-value verifier. Do not execute recovery,
edit files, rerun completed implementation, or weaken verification. Return only JSON:
{{"source":"path/to/source.xlsx","command":["existing-verifier","{{workbook}}"]}}
Use a separate literal {{workbook}} argument for the verification copy. Preserve required
independent-reference comparisons, formula preservation and error checks. If the existing
verifier cannot be identified safely, return {{"action":"concrete missing requirement"}}.
"""
    try:
        resolved = subprocess.run([sys.executable, str(Path(__file__).with_name('adjudication.py')),
            'run', os.environ.get('AUTOCYCLE_PROVIDER_TIMEOUT', '900'), 'codex', 'exec',
            '--sandbox', 'read-only', '--output-last-message', str(answer), '-'],
            input=prompt, capture_output=True, text=True)
        (folder/'resolver.log').write_text(resolved.stdout + resolved.stderr)
        if resolved.returncode:
            raise RuntimeError('Cannot resolve Excel verification command: ' +
                               (resolved.stderr or resolved.stdout or str(resolved.returncode)).strip())
        request = json.loads(answer.read_text())
        if request.get('action'):
            raise RuntimeError(request['action'])
        command = request['command']
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError('Existing Excel verifier must be an argument list')
        result = verify(request['source'], command, evidence)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        result = {'status': 'BLOCKED', 'action': str(error), 'evidence': str(folder/'result.json')}
    result['reviewed_sha'] = fields.get('REVIEWED_SHA', '')
    result['blocker_key'] = fields.get('BLOCKER_KEY', '')
    (folder/'result.json').write_text(json.dumps(result, indent=2) + '\n')
    if result['status'] == 'BLOCKED':
        print('    Blocked  ✗')
        print('    Action: ' + result['action'])
        return 2
    print(result['evidence'])
    return 0


def review_evidence(head):
    """Retain the latest recovery result across a fresh Review process."""
    from adjudication import location
    paths = sorted(Path(location()).glob('excel-resume-*/result.json'),
                   key=lambda p: p.stat().st_mtime_ns, reverse=True)
    unreadable = False
    for path in paths:
        try:
            result = json.loads(path.read_text())
            if not isinstance(result, dict):
                raise ValueError('Expected an Excel recovery result object')
            if result.get('reviewed_sha') != head:
                continue
            source = result.get('source')
            if source and (not Path(source).is_file() or digest(Path(source)) != result.get('source_sha256')):
                continue
            # Do not offer old success as fresh verification when a newer
            # interrupted record cannot be read. Known failure reasons remain.
            if unreadable and result.get('status') == 'VERIFIED':
                continue
            return str(path)
        except (OSError, ValueError, TypeError) as error:
            unreadable = True
            print('Unreadable Excel recovery evidence: '+str(path)+': '+str(error), file=sys.stderr)
    return 'NONE'


def failure_action(path, key):
    if path == 'NONE':
        return ''
    result = json.loads(Path(path).read_text())
    if result.get('status') == 'BLOCKED' and result.get('blocker_key') == key:
        return ' '.join(result['action'].splitlines())
    return ''


def main():
    if sys.argv[1:2] == ['--review-evidence']:
        print(review_evidence(sys.argv[2])); return 0
    if sys.argv[1:2] == ['--failure-action']:
        print(failure_action(sys.argv[2], sys.argv[3])); return 0
    if sys.argv[1:2] == ['--resume-blocker']:
        return resume_blocker(sys.argv[2])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('verifier',nargs=argparse.REMAINDER,help='-- existing verification command with a literal {workbook} path argument')
    args=parser.parse_args()
    command=args.verifier[1:] if args.verifier[:1]==['--'] else args.verifier
    # Reuse the controller evidence location, including Git worktree support.
    from adjudication import location
    try:
        result=verify(args.source,command,location())
    except (ValueError,OSError) as error:
        parser.error(str(error))
    print(json.dumps(result,indent=2))
    if result['status']=='BLOCKED':
        print('BLOCKER: '+result['action'])
        return 2
    return 0

if __name__=='__main__':sys.exit(main())

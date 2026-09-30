#!/usr/bin/env python3
"""Explicit supervised native acceptance. Never invokes AutoCycle or edits sources."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
import zipfile
import native_office as office
from install import build_native


def fixture_bytes():
    with zipfile.ZipFile(io.BytesIO(office.harmless_document('word'))) as z:
        parts={name:z.read(name) for name in z.namelist()}
    body=''.join('<w:p><w:pPr>'+('<w:pageBreakBefore/>' if i>1 else '')+
                 '</w:pPr><w:r><w:rPr><w:b/><w:sz w:val="48"/></w:rPr><w:t>VIEWPORT TEST PAGE '+str(i)+
                 '</w:t></w:r></w:p>' for i in range(1,7))
    parts['word/document.xml']=('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+body+
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>').encode()
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in parts.items():z.writestr(name,data)
    return out.getvalue()


def documents():
    return subprocess.check_output(['/usr/bin/osascript','-e',
        'tell application "Microsoft Word" to get {full name, saved} of every document'],text=True,timeout=10).strip()


def inspect(w,source,folder,fixture=False):
    folder.mkdir()
    b=w.backend; path=w.slot('word','view'); source=source.resolve(strict=True)
    result={'status':'BLOCKED','source':str(source),'source_sha256_before':office.digest(source),
            'documents_before':documents(),'pages':[]}
    front=b.frontmost()
    with w.lock():
        try:
            w.populate('word','view',source); w.open('word',path); b.verify_document('word',path)
            result['owned_document']=str(path)
            result['copy_sha256_before']=office.digest(path)
            page_map=b.word_page_map(path,{'page':1})
            count=len(page_map['pages']);result['page_count']=count
            if fixture and count!=6:raise office.NativeError('Fixture must paginate to six pages')
            pages=[1,3,6] if fixture else list(dict.fromkeys([1,max(2,count//2),count]))
            for page in pages:
                request={'app':'word','page':page,'source':str(source),'source_sha256':office.digest(source)}
                image=folder/f'page-{page}.png'
                captured=w._capture_owned('word',path,request,image)
                b.verify_document('word',path)
                if fixture and captured['word_visible_page']['matched_text']!=f'viewport test page {page}':
                    raise office.NativeError('Expected fixture marker missing')
                record=dict(request=request,**captured)
                office.publish_json(folder/f'page-{page}.json',record)
                result['pages'].append(record)
                print('VERIFIED',source.name,'page',page,captured['word_visible_page']['matched_text'],flush=True)
            if fixture:
                # Deliberately reproduce the defect: page 3 selection, page 1 canvas.
                expected=b.word_page_map(path,{'page':3})
                b.position('word',path,{'page':1})
                b.script('word',b.find('word',path,
                    'set r to create range targetDoc start 42 end 42\n'
                    'set selection start of selection of window 1 of targetDoc to 42\n'
                    'set selection end of selection of window 1 of targetDoc to 42'),path)
                route=b.confirm_view('word',path,{'page':3})
                image=folder/'rejected-stuck.png';native=b.capture(image,route)
                try:office.MacOffice.verify_word_rendered(path,image,expected,native)
                except office.NativeError as exc:
                    result['stuck_viewport']={'status':'REJECTED','error':str(exc),'native_window':native,
                                              'screenshot_sha256':office.digest(image)}
                else:raise office.NativeError('Stuck viewport falsely accepted')
            result['status']='PASSED'
        except Exception as exc:
            result.update(error=str(exc),failure_kind=getattr(exc,'kind','native_capture'))
            if getattr(exc,'permission_action',None):result['external_action']=exc.permission_action
        finally:
            # Only a returned native reference + unchanged owner lock can authorize close.
            if ('word',str(path)) in b.opened:
                try:
                    b.verify_document('word',path);w.close('word',path);result['owned_closed']=True
                except Exception as exc:result.update(status='BLOCKED',cleanup_error=str(exc))
            b.restore(front)
            result['documents_after']=documents()
            result['source_sha256_after']=office.digest(source)
            result['copy_sha256_after']=office.digest(path)
            if result['documents_before']!=result['documents_after'] or result['source_sha256_before']!=result['source_sha256_after'] or result.get('copy_sha256_before')!=result['copy_sha256_after']:
                result.update(status='BLOCKED',preservation_error='Document state or bytes changed')
            office.publish_json(folder/'result.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-native',action='store_true',required=True)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--consumer',type=Path)
    args=parser.parse_args(); args.evidence.mkdir(parents=True,exist_ok=False)
    source=args.evidence/'six-page-fixture.docx';source.write_bytes(fixture_bytes())
    w=office.Workspace(args.workspace)
    # Build from maintained Swift source; never replace any installed runtime.
    with tempfile.TemporaryDirectory(prefix='autocycle-word-native-build-') as tmp:
        helper=Path(tmp)/'capture';build_native(Path(__file__).resolve().parent,helper)
        run=subprocess.run
        def dispatch(command,*a,**kw):
            if command[0]==str(Path(office.__file__).with_name('autocycle-office-capture')):
                command=[str(helper),*command[1:]]
            return run(command,*a,**kw)
        with patch.object(subprocess,'run',side_effect=dispatch):
            fixture=inspect(w,source,args.evidence/'fixture',fixture=True)
            if fixture['status']!='PASSED':raise SystemExit('Fixture BLOCKED: '+json.dumps(fixture))
            if args.consumer:
                consumer=inspect(w,args.consumer,args.evidence/'consumer')
                if consumer['status']!='PASSED':raise SystemExit('Consumer BLOCKED: '+json.dumps(consumer))
    print('Native acceptance passed:',args.evidence)

if __name__=='__main__':main()

"""Cursor's complete phase list reaches the real stage's terminal display."""
import errno
import os
import pty
import select
import subprocess
import time
from test_flow import Case
from test_implement_display import screen


def test_cursor_phases_render_once_and_update_in_place(renderer=None):
    c = Case()
    process = None
    master, slave = pty.openpty()
    try:
        if renderer:
            (c.p/'stage_display.js').write_bytes(renderer.read_bytes())
        provider = c.bin/'agent'
        source = provider.read_text()
        original = next(line for line in source.splitlines() if "'method':'cursor/update_todos'" in line)
        replacement = '''  phases=[{'id':str(i),'content':label,'status':'pending'} for i,label in enumerate(['Inspect workbook generation','Fix source bindings','Verify rendered workbook'])]
  def publish():
   print(json.dumps({'jsonrpc':'2.0','method':'cursor/update_todos','params':{'todos':phases,'merge':False}}),flush=True)
  publish()
  for phase in phases:
   phase['status']='in_progress';publish()
   phase['status']='completed';publish()
'''
        provider.write_text(source.replace(original, replacement.rstrip()))
        process = subprocess.Popen([str(c.bin/'autocycle'),'1'],cwd=c.repo,env=c.env,
                                   stdout=slave,stderr=slave,start_new_session=True)
        os.close(slave);slave = None
        output = bytearray()
        deadline = time.monotonic()+180
        while time.monotonic()<deadline:
            if select.select([master],[],[],1)[0]:
                try:
                    chunk=os.read(master,65536)
                except OSError as error:
                    if error.errno==errno.EIO:break
                    raise
                if not chunk:break
                output.extend(chunk)
        else:
            raise AssertionError('stage did not finish')
        process.wait(timeout=5)
        text=output.decode()
        assert process.returncode==0,text
        lines=screen(text)
        assert sum(line.startswith('Implement ') for line in lines)==1,lines
        assert text.count('Implement ')==1,text
        review=next(i for i,line in enumerate(lines) if line.startswith('Review '))
        assert lines[review+1].startswith('    ✓ Found   '),lines
        assert lines[review+2]=='    ✓ Pruned  0 files · 0 B',lines
        assert sum(line.startswith('Review ') for line in lines)==1,lines
        for label in ('Plan',):
            assert sum(line.startswith(label+' ') for line in lines)==1,lines
        assert not any(line.startswith(('Sync ', 'Checkpoint ')) for line in lines),lines
        assert any(line.startswith('    ✓ Baseline ') for line in lines),lines
        assert any(line.startswith('    ✓ Checkpoint ') for line in lines),lines
        for label in ('Inspect workbook generation','Fix source bindings','Verify rendered workbook'):
            assert text.count(label)==1,text
            assert '    ○ '+label in text,text
            assert '    ✓ '+label in lines,lines
        assert text.count('\x1b[5G▶')==3,text
    finally:
        if process and process.poll() is None:
            import signal
            os.killpg(process.pid,signal.SIGTERM)
            process.wait(timeout=5)
        os.close(master)
        if slave is not None:os.close(slave)
        c.close()


if __name__=='__main__':
    test_cursor_phases_render_once_and_update_in_place()
    print('PASS real Cursor stage shows all pending phases and updates each row in place')

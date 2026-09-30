// Presentation-only display state. This never supplies task completion evidence.
const fs = require('node:fs');
class StageDisplay {
  constructor(path, label, clock = Date.now, emit = text => process.stdout.write(text), deferHeader = false) {
    this.path = path; this.clock = clock; this.emit = emit;
    this.state = fs.existsSync(path) && fs.statSync(path).size ? JSON.parse(fs.readFileSync(path, 'utf8')) : null;
    if (!this.state || this.state.label !== label) {
      this.state = { id: require('node:crypto').randomUUID(), label, started: clock(), activeMs: 0, segmentStart: clock(), completed: [], shown: [], header: !deferHeader && Boolean(process.stdout.isTTY) };
      this.save();
      if (this.state.header) emit(`${label.padEnd(12)}… 00:00\n`);
    }
  }
  save() {
    const path = require('node:path');
    const temporary = path.join(path.dirname(this.path), `.display-${process.pid}.tmp`);
    fs.writeFileSync(temporary, JSON.stringify(this.state));
    fs.renameSync(temporary, this.path);
  }
  elapsed() {
    return this.duration(this.activeMs());
  }
  activeMs() {
    return (this.state.activeMs || 0) + (this.state.segmentStart === null ? 0 :
      Math.max(0, this.clock() - (this.state.segmentStart ?? this.state.started)));
  }
  duration(ms) {
    const seconds = Math.max(0, Math.floor(ms / 1000));
    return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }
  clearTimer() {
    if (this.timerVisible) { this.emit('\r\x1b[2K'); this.timerVisible = false; }
  }
  implementHeader(suffix = `… ${this.elapsed()}`) {
    if (!process.stdout.isTTY) return;
    suffix = Array.from(suffix).slice(0, Math.max(1, (process.stdout.columns || 80) - 13)).join('');
    if (!this.state.header) {
      this.emit(`${this.state.label.padEnd(12)}${suffix}\n`);
      this.state.header = true;
      this.save();
      return;
    }
    // Events leave the cursor on the next empty row. Update only the header's
    // suffix, then return there; never replay the header or completed events.
    const distance = 1 + (this.state.rows || 0);
    this.emit(`\x1b[${distance}A\x1b[13G\x1b[0K${suffix}\r\x1b[${distance}B`);
  }
  tick() {
    // Other display commands may have appended rows since this renderer began.
    // Refresh the ephemeral cursor anchor; a refresh never starts a new clock.
    this.state = JSON.parse(fs.readFileSync(this.path, 'utf8'));
    if (this.state.network) this.networkTick();
    else this.implementHeader(this.state.finished || undefined);
  }
  networkStart() {
    if (this.state.network) return;
    this.state.activeMs = this.activeMs();
    this.state.segmentStart = null;
    this.implementHeader();
    if (!process.stdout.isTTY) {
      if (!this.state.header) {
        this.emit(`${this.state.label.padEnd(12)}… ${this.elapsed()}\n`);
        this.state.header = true;
      }
      for (const text of this.state.events || []) this.emit(`    ${text}\n`);
      this.state.events = [];
      this.state.streamed = true;
    }
    const leading = this.state.networkTail ? '' : '\n';
    this.state.network = {started: this.clock(), offset: (this.state.rows || 0) + (leading ? 1 : 0)};
    this.emit(leading + 'Network     … 00:00\n\n');
    this.state.rows = (this.state.rows || 0) + (leading ? 3 : 2);
    this.state.networkTail = true;
    this.save();
  }
  networkTick(symbol = '…', detail = '') {
    const network = this.state.network;
    if (!network) return;
    const text = `Network     ${symbol} ${this.duration(this.clock() - network.started)}${detail ? '  ' + detail : ''}`;
    if (process.stdout.isTTY) {
      const distance = this.state.rows - network.offset;
      const line = Array.from(text).slice(0, (process.stdout.columns || 80) - 1).join('');
      this.emit(`\x1b[${distance}A\r\x1b[2K${line}\r\x1b[${distance}B`);
    } else if (symbol !== '…') this.emit(`${text}\n\n`);
  }
  networkEnd(symbol = '✓', detail = '') {
    if (!this.state.network) return;
    this.networkTick(symbol, detail);
    this.state.networkMs = (this.state.networkMs || 0) + this.clock() - this.state.network.started;
    delete this.state.network;
    if (symbol === '✓') this.state.segmentStart = this.clock();
    this.save();
  }
  event(text) {
    this.state.networkTail = false;
    if (!process.stdout.isTTY) {
      if (this.state.streamed || this.state.finished) this.emit(`    ${text}\n`);
      else (this.state.events ||= []).push(text);
    } else {
      this.implementHeader(this.state.finished || undefined);
      this.emit(`    ${text}\n`);
      const width = process.stdout.columns || 80;
      this.state.rows = (this.state.rows || 0) + String(text).split('\n')
        .reduce((rows, line, i) => rows + Math.max(1, Math.ceil((Array.from(line).length + (i === 0 ? 4 : 0)) / width)), 0);
    }
    this.save();
  }
  prune(symbol, detail) {
    const text = `    ${symbol} ${symbol === '▶' ? 'Prune  ' : 'Pruned '} ${detail}`;
    if (process.stdout.isTTY) {
      const width = process.stdout.columns || 80;
      // Coarse progress occupies one physical row; never wrap a cursor target.
      const line = Array.from(text).slice(0, Math.max(1, width - 1)).join('');
      if (this.state.pruneShown) {
        const distance = this.state.rows - this.state.pruneOffset;
        this.emit(`\x1b[${distance}A\r\x1b[2K${line}\r\x1b[${distance}B`);
      } else {
        this.state.networkTail = false;
        this.state.pruneOffset = this.state.rows || 0;
        this.state.rows = (this.state.rows || 0) + 1;
        this.emit(line + '\n');
      }
      this.state.pruneShown = true;
    } else if (symbol !== '▶' && !this.state.pruneShown) {
      this.event(text.slice(4));
      this.state.pruneShown = true;
    }
    this.save();
  }
  observe(app, symbol) {
    const text = `    ${symbol} Observe ${app}`;
    const offsets = this.state.observationRows ||= {};
    if (process.stdout.isTTY && symbol !== '▶' && offsets[app] !== undefined) {
      const distance = this.state.rows - offsets[app];
      this.emit(`\x1b[${distance}A\r\x1b[2K${text}\r\x1b[${distance}B`);
    } else if (process.stdout.isTTY || symbol !== '▶') {
      offsets[app] = this.state.rows || 0;
      this.event(text.slice(4));
    }
    this.save();
  }
  tasks(items) {
    if (this.state.label === 'Implement' && process.stdout.isTTY) {
      this.implementTasks(items);
      return;
    }
    for (const item of items) {
      const text = String(item.content || '').replace(/\s+/g, ' ').trim();
      const key = text.toLowerCase().replace(/[.:;]+$/, '');
      if (this.state.completed.includes(key)) continue;
      const statusKey = key + ':' + item.status;
      if (this.state.shown.includes(statusKey)) continue;
      if (item.status === 'pending') continue;
      const glyph = {completed:'✓', in_progress:'▶', cancelled:'–'}[item.status];
      if (!glyph) continue;
      this.event(`${glyph} ${text}`);
      this.state.shown.push(statusKey);
      if (item.status === 'completed') this.state.completed.push(key);
    }
    this.save();
  }
  implementTasks(items) {
    const rows = this.state.taskRows ||= [];
    const glyphs = {pending:'○', in_progress:'▶', completed:'✓', cancelled:'–'};
    let active;
    for (const item of items) {
      const text = String(item.content || '').replace(/\s+/g, ' ').trim();
      const key = text.toLowerCase().replace(/[.:;]+$/, '');
      if (!glyphs[item.status]) continue;
      let row = rows.find(row => row.key === key);
      if (!row) {
        row = {key, text, status: item.status, offset: this.state.rows || 0};
        rows.push(row);
        this.event(`○ ${text}`);
        row.glyph = '○';
      }
      if (row.status !== 'completed') row.status = item.status;
      if (row.status === 'in_progress' && !active) active = row;
    }
    active ||= rows.find(row => row.status === 'in_progress');
    for (const row of rows) {
      if (row.status === 'in_progress' && row !== active) row.status = 'pending';
      const glyph = glyphs[row.status];
      if (glyph === row.glyph) continue;
      // Replace only the symbol; labels (including wrapped labels) never move.
      // Offsets include intervening events and survive renderer reconstruction.
      const distance = this.state.rows - row.offset;
      this.emit(`\x1b[${distance}A\x1b[5G${glyph}\r\x1b[${distance}B`);
      row.glyph = glyph;
    }
    this.save();
  }
  finish(symbol, summary = '') {
    if (this.state.resultSymbol === symbol && this.state.resultSummary === summary) return;
    this.state.activeMs = this.activeMs();
    this.state.segmentStart = null;
    this.state.resultSymbol = symbol; this.state.resultSummary = summary;
    const diagnostic = symbol !== '✓' && ['Review', 'Plan', 'Access', 'Network'].includes(this.state.label) ? summary : '';
    this.state.finished = `${symbol} ${this.elapsed()}${diagnostic ? '  ' + diagnostic : ''}`;
    if (diagnostic) summary = '';
    if (process.stdout.isTTY) {
      this.implementHeader(this.state.finished);
    } else {
      this.emit(this.state.streamed ? `    ${symbol} ${this.state.label} active ${this.elapsed()}${diagnostic ? '  ' + diagnostic : ''}\n` : `${this.state.label.padEnd(12)}${this.state.finished}\n`);
      for (const text of this.state.events || []) this.emit(`    ${text}\n`);
      this.state.events = [];
      this.state.header = true;
    }
    if (summary) {
      const label = this.state.label;
      if (label === 'Review') summary = `✓ Found   ${summary.replace(/^(?:Found|Completed):\s*/, '')}`;
      if (label === 'Plan') summary = `✓ Planned ${summary.replace(/^[^:]+:\s*/, '')}`;
      this.emit(`    ${summary}\n`);
      const width = process.stdout.columns || 80;
      this.state.rows = (this.state.rows || 0) + Math.max(1, Math.ceil((Array.from(summary).length + 4) / width));
    }
    this.save();
  }

  outcome(symbol, summary = '') {
    this.state.outcome = symbol;
    if (summary && this.state.outcomeSummary !== summary) {
      this.state.outcomeSummary = summary;
      if (symbol !== '✓') this.state.diagnostic = summary;
      else if (this.state.label === 'Plan') this.event(`✓ Planned ${summary.replace(/^[^:]+:\s*/, '')}`);
      else if (this.state.label === 'Review') this.event(`✓ Found   ${summary}`);
      else this.event(summary);
    }
    this.save();
  }

  close() {
    if (!this.state.finished) this.finish(this.state.outcome || '✓', this.state.diagnostic || '');
  }

}
module.exports = { StageDisplay };
if (require.main === module) {
  const [path, label, action, ...args] = process.argv.slice(2);
  const display = new StageDisplay(path, label, Date.now, text => process.stdout.write(text), true);
  if (action === 'tick') display.tick();
  if (action === 'live') {
    const timer = setInterval(() => display.tick(), 1000);
    const ready = `${path}.renderer-${process.pid}`;
    process.on('SIGTERM', () => { clearInterval(timer); fs.rmSync(ready, {force:true}); process.exit(0); });
    fs.writeFileSync(ready, '');
    display.tick();
  }
  if (action === 'prune') display.prune(...args);
  if (action === 'observe') display.observe(...args);
  if (action === 'network-start') display.networkStart();
  if (action === 'network-end') display.networkEnd(...args);
  if (action === 'active-ms') process.stdout.write(String(display.activeMs()) + '\n');
  if (action === 'outcome') display.outcome(...args);
  if (action === 'close') display.close();
  if (action === 'interrupted' && !display.state.finished && !display.state.outcome) display.outcome('–');
  if (action === 'clock') process.stdout.write(`${Math.floor(display.state.started / 1000)} ${display.state.header ? 1 : 0}\n`);
  if (action === 'finish') display.finish(...args);
  if (action === 'event') display.event(args[0]);
}

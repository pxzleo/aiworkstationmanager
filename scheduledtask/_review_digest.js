import Database from 'bun:sqlite';
const db = new Database('C:/Users/xu/.local/share/opencode/opencode.db');
const sid = process.argv[2];
const rows = db.query('SELECT id, time_created, data FROM message WHERE session_id = ? ORDER BY time_created').all(sid);
const parts = db.query('SELECT message_id, time_created, data FROM part WHERE session_id = ? ORDER BY time_created').all(sid);
const byMsg = new Map();
for (const p of parts) {
  if (!byMsg.has(p.message_id)) byMsg.set(p.message_id, []);
  byMsg.get(p.message_id).push(p.data);
}
const out = [];
out.push('SESSION ' + sid + ' | messages: ' + rows.length + ' | parts: ' + parts.length);
for (const r of rows) {
  let m;
  try { m = JSON.parse(r.data); } catch (e) { continue; }
  const t = new Date(r.time_created).toISOString();
  const plist = (byMsg.get(r.id) ?? []).map(d => { try { return JSON.parse(d); } catch { return null; } }).filter(Boolean);
  if (m.role === 'user') {
    for (const p of plist) {
      if (p.type === 'text' && (p.text ?? '').trim()) out.push('[' + t + '] USER: ' + (p.text ?? '').replace(/\n/g, ' ⏎ ').slice(0, 1500));
      else if (p.type === 'file') out.push('[' + t + '] USER FILE: ' + (p.filename ?? '') + ' ' + String(p.url ?? '').slice(0, 100));
      else if (p.type === 'patch') out.push('[' + t + '] USER PATCH');
    }
    continue;
  }
  const seg = [];
  for (const p of plist) {
    if (p.type === 'text' && (p.text ?? '').trim()) seg.push('TEXT: ' + (p.text ?? '').replace(/\n/g, ' ⏎ '));
    else if (p.type === 'tool') {
      const st = p.state ?? {};
      const inp = st.input ?? {};
      let detail = '';
      if (p.tool === 'bash' || p.tool === 'shell') detail = 'cmd=' + String(inp.command ?? '').replace(/\s+/g, ' ').slice(0, 180);
      else if (p.tool === 'read' || p.tool === 'write' || p.tool === 'edit') detail = 'path=' + (inp.filePath ?? inp.path ?? '');
      else if (p.tool === 'webfetch') detail = 'url=' + (inp.url ?? '');
      else if (p.tool === 'websearch') detail = 'q=' + (inp.query ?? '');
      else if (p.tool === 'subagent' || p.tool === 'task') detail = 'agent=' + (inp.agent ?? '') + ' desc=' + (inp.description ?? '');
      else detail = JSON.stringify(inp).slice(0, 140);
      const err = st.error ? ' ERR=' + String(st.error).slice(0, 140) : '';
      const out2 = st.output ? ' out=' + String(st.output).replace(/\s+/g, ' ').slice(0, 100) : '';
      seg.push('TOOL: ' + p.tool + ' ' + detail + err + out2);
    }
  }
  if (seg.length) out.push('[' + t + '] ' + m.role.toUpperCase() + ': ' + seg.join(' | ').slice(0, 4000));
}
console.log(out.join('\n'));
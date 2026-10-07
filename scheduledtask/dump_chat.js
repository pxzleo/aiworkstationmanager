import Database from 'bun:sqlite';
const db = new Database('C:/Users/xu/.local/share/opencode/opencode.db');
const sid = process.argv[2];
const maxLen = process.argv[3] ? Number(process.argv[3]) : 500;
const msgs = db.query('SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created').all(sid);
for (const m of msgs) {
  const d = JSON.parse(m.data);
  const role = d.role;
  const parts = db.query('SELECT data FROM part WHERE message_id = ? ORDER BY time_created').all(m.id);
  let texts = [];
  for (const p of parts) {
    let pd;
    try { pd = JSON.parse(p.data); } catch { continue; }
    if (!pd) continue;
    if (pd.type === 'text' && pd.text && pd.text.trim()) texts.push(pd.text);
  }
  if (!texts.length) continue;
  const t = texts.join('\n');
  if (role === 'user') console.log('USER:', t.slice(0, maxLen));
  else console.log('A:', t.slice(0, maxLen));
}
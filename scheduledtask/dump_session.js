import Database from 'bun:sqlite';
const db = new Database('C:/Users/xu/.local/share/opencode/opencode.db');
const sid = process.argv[2];
const maxLen = process.argv[3] ? Number(process.argv[3]) : 400;
const msgs = db.query('SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created').all(sid);
for (const m of msgs) {
  const d = JSON.parse(m.data);
  const role = d.role;
  const parts = db.query('SELECT data FROM part WHERE message_id = ? ORDER BY time_created').all(m.id);
  for (const p of parts) {
    let pd;
    try { pd = JSON.parse(p.data); } catch { continue; }
    if (!pd) continue;
    if (pd.type === 'text') {
      if (role === 'user') console.log('USER:', (pd.text || '').slice(0, maxLen));
      else if (pd.text) console.log('A:', pd.text.slice(0, maxLen));
    } else if (pd.type === 'tool') {
      const inp = pd.state && pd.state.input ? JSON.stringify(pd.state.input) : '';
      console.log('TOOL:', pd.tool || pd.state?.toolName, inp.slice(0, 220));
    }
  }
}
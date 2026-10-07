// 每日复盘用：提取昨天所有主会话（排除子代理会话与 scheduledtask 复盘会话）的紧凑转录。
// 用法: bun run reflect-extract.js [输出目录]（默认 C:/Users/xu/AppData/Local/Temp/opencode）
import Database from 'bun:sqlite';
import { stat } from 'fs/promises';
const db = new Database('C:/Users/xu/.local/share/opencode/opencode.db', { readonly: true });
const outDir = process.argv[2] || 'C:/Users/xu/AppData/Local/Temp/opencode';
const now = new Date();
const end = new Date(now);
end.setHours(0, 0, 0, 0);
const start = new Date(now);
start.setHours(0, 0, 0, 0);
start.setDate(start.getDate() - 1);
const rows = db
  .query('SELECT id, directory, title, time_created FROM session WHERE parent_id IS NULL AND time_created >= ? AND time_created < ?')
  .all(start.getTime(), end.getTime());
const mains = rows.filter(r => !r.directory.includes('scheduledtask'));
let n = 0;
for (const s of mains) {
  n += 1;
  const lines = [];
  const msgs = db
    .query('SELECT id, time_created, data FROM message WHERE session_id = ? ORDER BY time_created')
    .all(s.id);
  for (const m of msgs) {
    const md = JSON.parse(m.data);
    const role = md.role;
    const parts = db.query('SELECT data FROM part WHERE message_id = ? ORDER BY time_created').all(m.id);
    const partLines = parts
      .map(p => {
        const d = JSON.parse(p.data);
        if (d.type === 'text') {
          const t = (d.text || '').trim();
          if (!t) return '';
          const max = role === 'user' ? 1500 : 600;
          return '  [text] ' + t.slice(0, max) + (t.length > max ? ' ...(截断)' : '');
        }
        if (d.type === 'tool') {
          const st = d.state || {};
          const err = st.error ? ' ERROR:' + JSON.stringify(st.error).slice(0, 200) : '';
          return '  [tool] ' + d.tool + (st.status === 'error' ? ' (error)' : '') + err;
        }
        return '  [' + d.type + ']';
      })
      .filter(Boolean);
    lines.push(
      role === 'user'
        ? `\n=== USER (${new Date(m.time_created).toISOString()}) ===\n${partLines.join('\n') || '  (无文本/附件)'}\n`
        : `\n--- assistant ---\n${partLines.join('\n')}`,
    );
  }
  const name = String(n).padStart(2, '0') + '-' + (s.title || s.id).replace(/[^\w\u4e00-\u9fff-]/g, '').slice(0, 40);
  const file = `${outDir}/reflect-${name}.txt`;
  await Bun.write(file, lines.join('\n'));
  const size = (await stat(file)).size.toString();
  console.log(name, s.id, 'dir=' + s.directory.split('\\').pop(), 'msgs=' + msgs.length, size + 'B');
}
console.log('total main sessions:', mains.length, 'of', rows.length);

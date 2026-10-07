import Database from 'bun:sqlite';
const db = new Database('C:/Users/xu/.local/share/opencode/opencode.db');
const rows = db.query(`SELECT id, directory, title, parent_id, time_created FROM session WHERE directory LIKE 'C:/Users/xu/.config/openchamber/chats/2026-09-2%'`).all();
const groups = new Map();
for (const r of rows) {
  const key = r.directory;
  if (!groups.has(key)) groups.set(key, []);
  groups.get(key).push(r);
}
for (const [dir, list] of groups) {
  console.log('=== ' + dir);
  const sorted = list.sort((a, b) => a.time_created - b.time_created);
  for (const r of sorted) {
    console.log(' ', r.id, r.parent_id ? ('child of ' + r.parent_id) : 'ROOT', new Date(r.time_created).toISOString(), '|', r.title);
  }
}
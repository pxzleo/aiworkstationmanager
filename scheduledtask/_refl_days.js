import Database from 'bun:sqlite';
const db = new Database('C:/Users/xu/.local/share/opencode/opencode.db');
const rows = db.query(`SELECT date(time_created/1000, 'unixepoch', '+8 hours') as day, count(*) as n FROM session WHERE time_created >= 1788192000000 GROUP BY day ORDER BY day DESC`).all();
console.log(JSON.stringify(rows));
const total = db.query('SELECT count(*) as n FROM session').get();
console.log('total:', total.n);
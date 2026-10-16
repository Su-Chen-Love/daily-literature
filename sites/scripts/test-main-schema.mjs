import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync,readdirSync} from 'node:fs';
const migrations=new URL('../drizzle/',import.meta.url),files=readdirSync(migrations).filter(n=>n.endsWith('.sql')).sort();
assert.equal(files.length,4,'Review every additional migration explicitly');
const upgraded=new DatabaseSync(':memory:'),reference=new DatabaseSync(':memory:');
for(const name of files.slice(0,2))upgraded.exec(readFileSync(new URL(name,migrations),'utf8'));
upgraded.prepare('INSERT INTO pointers VALUES(?,?)').run('fixture-private-setting','retained');
upgraded.prepare('INSERT INTO generations VALUES(?,?,?,?)').run('old-generation',1,'old-time','complete');
upgraded.prepare('INSERT INTO records VALUES(?,?,?,?,?)').run('old-generation','private','fixture','{"preserve":true}','old-checksum');
const before=JSON.stringify(upgraded.prepare('SELECT * FROM records').all());
const migration=files.slice(2).map(name=>readFileSync(new URL(name,migrations),'utf8')).join('\n');
assert.ok(!/\b(DROP|DELETE|ALTER|UPDATE)\b/i.test(migration),'Main-store installation must remain additive');
upgraded.exec(migration);reference.exec(readFileSync(new URL('../db/main-store-schema.sql',import.meta.url),'utf8'));
assert.equal(JSON.stringify(upgraded.prepare('SELECT * FROM records').all()),before);
assert.equal(upgraded.prepare("SELECT value FROM pointers WHERE key='fixture-private-setting'").get().value,'retained');
const names=db=>db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ms_%' ORDER BY name").all().map(r=>r.name);
assert.deepEqual(names(upgraded),names(reference));
for(const name of names(reference)){
 assert.deepEqual(upgraded.prepare(`PRAGMA table_info(${name})`).all(),reference.prepare(`PRAGMA table_info(${name})`).all(),name);
 const indexes=db=>db.prepare(`PRAGMA index_list(${name})`).all().map(i=>({unique:i.unique,columns:db.prepare(`PRAGMA index_info('${i.name}')`).all().map(v=>v.name)})).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
 assert.deepEqual(indexes(upgraded),indexes(reference),name+' indexes');
}
assert.equal(upgraded.prepare("SELECT revision FROM ms_heads WHERE namespace='owner'").get().revision,0);
assert.ok(upgraded.prepare("SELECT name FROM sqlite_master WHERE name='display_payloads'").get());
upgraded.close();reference.close();console.log('Additive Drizzle migration matches reference schema/indexes and preserves existing Site records/settings.');

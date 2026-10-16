import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {database} from './d1-local.mjs';
import {canonicalJSON} from '../worker/main-store.js';
import {siteBackup} from '../worker/site-backup.js';
const db=database(),hash=s=>createHash('sha256').update(s).digest('hex');
db.sql.exec(readFileSync(new URL('../db/main-store-schema.sql',import.meta.url),'utf8'));
const g=hash('first'),next=hash('next'),staging=hash('incomplete');
for(const [id,status] of [[g,'complete'],[next,'complete'],[staging,'staging']])db.sql.prepare('INSERT INTO generations VALUES(?,3,?,?)').run(id,'fixture',status);
const originals=[['meta','overview','{"fixture":true}'],['paper','large',JSON.stringify({text:'汉语😀'.repeat(80000)})],['private','profile','{"profile_cursor":1}']];
for(const [kind,key,data] of originals)db.sql.prepare('INSERT INTO records VALUES(?,?,?,?,?)').run(g,kind,key,data,hash(data));
db.sql.prepare('INSERT INTO pointers VALUES(?,?)').run('active',g);
db.sql.prepare('INSERT INTO cloud_feedback VALUES(1,?,null,null,1,?,?)').run('doi:1','unread','fixture');
const begin=await siteBackup(db,'begin');
assert.equal(begin.snapshot.feedback_cursor,1);assert.equal(begin.snapshot.profile_cursor,0);
assert.equal(begin.snapshot.main_head.revision,0);
assert.equal(begin.snapshot.excluded_uncommitted_generations,1);
assert.equal(begin.snapshot.generations.length,2);assert.equal(hash(canonicalJSON(begin.snapshot)),begin.snapshot_sha256);
// A newer publication and new feedback cannot change this backup's pinned set.
db.sql.prepare("UPDATE pointers SET value=? WHERE key='active'").run(next);
db.sql.prepare('INSERT INTO cloud_feedback VALUES(2,?,null,null,0,?,?)').run('doi:1','read','later');
const recovered=new Map();let after;
for(let count=0;count<20;count++){
 const page=await siteBackup(db,'records',{generation:g,...(after?{after}:{})});
 assert.ok(Buffer.byteLength(JSON.stringify(page))<700000);
 if(page.done){assert.equal(page.next,null);break;}
 for(const piece of page.chunks){const key=piece.kind+':'+piece.key,previous=recovered.get(key)||Buffer.alloc(0);assert.equal(piece.offset,previous.length);const bytes=Buffer.concat([previous,Buffer.from(piece.base64,'base64')]);recovered.set(key,bytes);if(bytes.length===piece.bytes)assert.equal(hash(bytes),piece.checksum);}
 assert.notDeepEqual(page.next,after);after=page.next;
 if(count===19)assert.fail('Export did not terminate');
}
assert.equal(recovered.size,originals.length);
for(const [kind,key,data] of originals)assert.equal(recovered.get(kind+':'+key).toString(),data);
const events=await siteBackup(db,'feedback',{after:0,until:begin.snapshot.feedback_cursor});
assert.equal(events.events.length,1);assert.equal(events.next,1);assert.equal(events.done,true);
assert.equal((await siteBackup(db,'profiles',{after:0,until:0})).done,true);
// A valid large Chinese profile must remain exportable, even when escaped
// canonical metadata exceeds a normal page. Chunks preserve every byte.
db.sql.prepare('INSERT INTO cloud_profile_events VALUES(1,0,?,?,?,?,?,?,?,?)').run(g,hash('base'),'{}',hash('profile'),'学术'.repeat(50000),'{}','fixture','fixture');
const large=await siteBackup(db,'profiles',{after:0,until:1});assert.ok(large.oversized.bytes>256*1024);assert.equal(large.events.length,0);
const pieces=[];let offset=0;
while(true){const part=await siteBackup(db,'event_chunks',{stream:'profiles',seq:1,offset,until:1});assert.equal(part.offset,offset);pieces.push(Buffer.from(part.base64,'base64'));offset=part.next;if(part.done)break;}
const restored=Buffer.concat(pieces);assert.equal(hash(restored),large.oversized.sha256);assert.equal(JSON.parse(restored).content,'学术'.repeat(50000));
await assert.rejects(siteBackup(db,'records',{generation:staging}),{status:409});
await assert.rejects(siteBackup(db,'records',{generation:g,after:{kind:'paper',key:'missing',offset:1}}),{status:409});
await assert.rejects(siteBackup(db,'feedback',{after:2,until:1}),{status:400});
await assert.rejects(siteBackup(db,'records',{generation:'x'}),{status:400});
db.sql.close();console.log('Pinned Site backup: immutable complete views, byte chunks, checksums, high-watermarks, limits and refusals passed.');

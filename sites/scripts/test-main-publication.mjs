import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {database} from './d1-local.mjs';
import {publishMainSnapshot} from '../worker/main-publication.js';
import worker from '../worker/index.js';
const hash=s=>createHash('sha256').update(s).digest('hex'),db=database(),origin='https://unit.invalid';
const wm={feedback_cursor:0,profile_cursor:0,request_id:0};
const get=(db,k)=>db.sql.prepare('SELECT value FROM pointers WHERE key=?').get(k)?.value;
function revision(db,n){db.sql.prepare('INSERT INTO ms_revisions VALUES(?,?,?,?,?,?,?)').run('owner',n,hash('import'),'operation-'+n,hash('payload-'+n),JSON.stringify(wm),'fixture');db.sql.prepare("UPDATE ms_heads SET revision=?,import_id=? WHERE namespace='owner'").run(n,hash('import'));}
function stage(db,title,profile_cursor=0){const records=[['paper','one',{identity:'one',title}],['private','profile',{profile_cursor}]].map(([kind,key,v])=>{const data=JSON.stringify(v);return {kind,key,data,checksum:hash(data)};}).sort((a,b)=>a.kind<b.kind?-1:1),g=hash(records.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'));db.sql.prepare('INSERT OR IGNORE INTO generations VALUES(?,?,?,?)').run(g,records.length,'fixture','complete');for(const r of records)db.sql.prepare('INSERT OR IGNORE INTO records VALUES(?,?,?,?,?)').run(g,r.kind,r.key,r.data,r.checksum);return g;}
function request(operation_id,revision,snapshot_generation,expected_generation=null,expected_published_revision=0,expected_writer_mode='local',activate=true){return {operation_id,revision,snapshot_generation,expected_generation,expected_published_revision,expected_writer_mode,activate};}
revision(db,1);const first=stage(db,'first'),a=request('a',1,first);
assert.equal((await publishMainSnapshot(db,{...a,activate:false})).preview,true);assert.equal(get(db,'active'),undefined);assert.equal(get(db,'writer_mode'),undefined);
for(const cursor of ['NaN',99])await assert.rejects(publishMainSnapshot(db,request('bad-'+cursor,1,stage(db,'invalid-'+cursor,cursor))),{status:409});
assert.equal((await publishMainSnapshot(db,a)).active,true);assert.equal(get(db,'writer_mode'),'d1-main');assert.equal(get(db,'active'),first);assert.equal(get(db,'sync_cursor_ack'),'0');
assert.equal((await publishMainSnapshot(db,a)).idempotent,true);
await assert.rejects(publishMainSnapshot(db,{...a,snapshot_generation:stage(db,'reuse')}),{status:409});
revision(db,2);const second=stage(db,'second');
await assert.rejects(publishMainSnapshot(db,request('stale',2,second,null,1,'d1-main')),{status:409});assert.equal(get(db,'active'),first);
await publishMainSnapshot(db,request('b',2,second,first,1,'d1-main'));
assert.equal((await publishMainSnapshot(db,a)).active,false);assert.equal(get(db,'active'),second);
await assert.rejects(publishMainSnapshot(db,request('backwards',1,first,second,2,'d1-main')),{status:409});
// A delayed duplicate has no receipt when it starts. Another caller commits the
// same operation and a newer one before its transaction. It must not roll back.
const raced=database();revision(raced,1);const rg=stage(raced,'r1'),old=request('race-old',1,rg);let once=false;
const wrapper={prepare:raced.prepare,batch:async statements=>{if(!once){once=true;await publishMainSnapshot(raced,old);revision(raced,2);const ng=stage(raced,'r2');await publishMainSnapshot(raced,request('race-new',2,ng,rg,1,'d1-main'));}return raced.batch(statements);}};
assert.equal((await publishMainSnapshot(wrapper,old)).active,false);assert.equal(JSON.parse(get(raced,'main_store_head')).revision,2);
// Simulate a legacy ACK that read local mode before the first main activation.
const ackdb=database();revision(ackdb,1);const ag=stage(ackdb,'ack');ackdb.sql.prepare('INSERT INTO pointers VALUES(?,?)').run('active',ag);ackdb.sql.prepare("INSERT INTO cloud_feedback VALUES(1,'one',null,null,0,'unread','fixture')").run();let switched=false;
const ackwrapper={prepare:ackdb.prepare,batch:async statements=>{if(!switched){switched=true;await publishMainSnapshot(ackdb,request('handoff',1,ag,ag));}return ackdb.batch(statements);}};
const response=await worker.fetch(new Request(origin+'/api/sync/ack',{method:'POST',headers:{Authorization:'Bearer fixture-token','Content-Type':'application/json'},body:JSON.stringify({generation:ag,cursor:1,request_id:0,profile_cursor:0})}),{DB:ackwrapper,SYNC_TOKEN:'fixture-token'});
assert.equal(response.status,409);assert.equal(get(ackdb,'sync_cursor_ack'),'0');
for(const d of [db,raced,ackdb])d.sql.close();
console.log('Main publication passed: preview, exact cursor binding, atomic activation/ACK, stale-head refusal, retained receipts, delayed-retry safety and legacy ACK race fence.');

import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {database} from './d1-local.mjs';
import worker from '../worker/index.js';
import {displayRecord} from '../worker/display-records.js';
import {siteBackup} from '../worker/site-backup.js';
const db=database(),env={DB:db,OWNER_USER_ID:'owner'},origin='https://unit.invalid',hash=s=>createHash('sha256').update(s).digest('hex');
const rows=n=>[...Array.from({length:10},(_,i)=>['paper','paper-'+i,{identity:'paper-'+i,title:'Synthetic '+i,abstract:'Shared unchanged text '.repeat(200)}]),['private','profile',{content:'Synthetic profile',profile_cursor:0}],['meta','overview',{threshold:.7}],['meta','ui:status',{run:n}]].map(([kind,key,value])=>{const data=JSON.stringify(value);return {kind,key,data,checksum:hash(data)};}).sort((a,b)=>(a.kind<b.kind?-1:a.kind>b.kind?1:0)||(a.key<b.key?-1:a.key>b.key?1:0));
const generation=rows=>hash(rows.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'));
const first=rows(0),old=generation(first);db.sql.prepare('INSERT INTO generations VALUES(?,?,?,?)').run(old,first.length,'fixture','complete');for(const r of first)db.sql.prepare('INSERT INTO records VALUES(?,?,?,?,?)').run(old,r.kind,r.key,r.data,r.checksum);db.sql.prepare('INSERT INTO pointers VALUES(?,?)').run('active',old);
async function stage(action,data){const response=await worker.fetch(new Request(origin+'/mcp',{method:'POST',headers:{'Content-Type':'application/json','oai-authenticated-user-id':'owner'},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'stage_snapshot',arguments:{action,data}}})}),env);const value=(await response.json()).result;assert.equal(value.isError,false,JSON.stringify(value));return value.structuredContent;}
let latest;
for(let n=1;n<=8;n++){
 const data=rows(n),g=generation(data);const begun=await stage('begin',{generation:g,count:data.length,manifest:data.map(({kind,key,checksum})=>({kind,key,checksum}))});
 const wanted=new Set(begun.missing.map(r=>r.kind+':'+r.key));await stage('chunk',{generation:g,records:data.filter(r=>wanted.has(r.kind+':'+r.key))});await stage('finish',{generation:g,activate:false});
 assert.equal(db.sql.prepare("SELECT value FROM pointers WHERE key='active'").get().value,old,'Staging must not change live view');
 assert.equal(db.sql.prepare("SELECT count(*) n FROM records WHERE generation=? AND data!=''").get(g).n,0);
 assert.equal((await displayRecord(db,g,'paper','paper-0')).data,first.find(r=>r.key==='paper-0').data);
 latest=g;
}
assert.equal(db.sql.prepare('SELECT count(*) n FROM display_payloads').get().n,20,'Twelve unchanged values plus eight changed status payloads, not eight full copies');
assert.equal(db.sql.prepare('SELECT count(*) n FROM records WHERE generation=? AND data!=\'\'').get(old).n,first.length,'Existing inline history stays untouched');
const page=await siteBackup(db,'records',{generation:latest});assert.ok(page.chunks.length>0);assert.equal(hash(Buffer.from(page.chunks[0].base64,'base64')),page.chunks[0].checksum);
// A legacy staging destination inherited from a managed source stays inline.
// Its later activation remains separately fenced by the main writer mode.
db.sql.prepare("UPDATE pointers SET value=? WHERE key='active'").run(latest);
const legacyRows=rows(9),legacyGeneration=generation(legacyRows);
const legacyResponse=await worker.fetch(new Request(origin+'/api/sync/begin',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer synthetic-only'},body:JSON.stringify({generation:legacyGeneration,count:legacyRows.length,manifest:legacyRows.map(({kind,key,checksum})=>({kind,key,checksum}))})}),{...env,SYNC_TOKEN:'synthetic-only'});
assert.equal(legacyResponse.status,200);
assert.equal(db.sql.prepare("SELECT count(*) n FROM records WHERE generation=? AND data!=''").get(legacyGeneration).n,12);
assert.equal(db.sql.prepare('SELECT data FROM records WHERE generation=? AND key=?').get(legacyGeneration,'paper-0').data,first.find(r=>r.key==='paper-0').data);
// Missing content is never mistaken for an empty JSON object or silently omitted.
const checksum=first.find(r=>r.key==='paper-0').checksum;db.sql.prepare('DELETE FROM display_payloads WHERE checksum=?').run(checksum);
await assert.rejects(displayRecord(db,latest,'paper','paper-0'),{status:503});
db.sql.close();console.log('Managed display generations share unchanged payloads, retain all old inline history, export exact bytes and fail closed on missing references.');

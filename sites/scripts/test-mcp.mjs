import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import worker from '../worker/index.js';
import {commitLibraryHead,readLibraryHead,streamCheckpointFile,handleCloudState} from '../worker/cloud-state.js';
import {database} from './d1-local.mjs';
const origin='https://unit.example.chatgpt.site',hash=value=>createHash('sha256').update(value).digest('hex');
const env={DB:database(),OWNER_USER_ID:'site-scoped-owner'};
async function rpc(method,params={},options={}){
 const {own=true,extraHeaders={},environment=env,id=1,...init}=options;
 const response=await worker.fetch(new Request(origin+'/mcp',{method:'POST',headers:{'Content-Type':'application/json',...(own?{'oai-authenticated-user-id':'site-scoped-owner'}:{}),...extraHeaders},body:JSON.stringify({jsonrpc:'2.0',...(id===undefined?{}:{id}),method,params}),...init}),environment);
 return {response,status:response.status,data:response.status===202?null:await response.json()};
}
const call=(name,args={},options={})=>rpc('tools/call',{name,arguments:args},options);
const value=result=>{assert.equal(result.status,200);assert.equal(result.data.result.isError,false,JSON.stringify(result.data));return result.data.result.structuredContent;};
const refused=(result,status)=>{assert.equal(result.status,200);assert.equal(result.data.result.isError,true);assert.equal(result.data.result.structuredContent.status,status);};
const initialization=await rpc('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'fixture',version:'1'}},{own:false});
assert.equal(initialization.data.result.protocolVersion,'2025-11-25');assert.deepEqual(initialization.data.result.capabilities,{tools:{}});
const listed=await rpc('tools/list',{}, {own:false});assert.equal(listed.data.result.tools.length,12);assert.ok(!JSON.stringify(listed).includes('site-scoped-owner'));assert.ok(!JSON.stringify(listed).includes('private interest'));
// MCP Tool.inputSchema requires an object root even when validation uses oneOf.
for(const tool of listed.data.result.tools)assert.equal(tool.inputSchema.type,'object',tool.name);
assert.equal((await rpc('tools/list',{_meta:{progressToken:'discovery','client/extension':true}},{own:false})).status,200);
assert.equal((await rpc('tools/list',{cursor:'',_meta:{}},{own:false})).status,200);
for(const invalid of [{_meta:null},{_meta:[]},{cursor:null},{unexpected:true}])assert.equal((await rpc('tools/list',invalid)).data.error.code,-32602);
for(const version of ['2024-11-05','2025-03-26','2025-06-18','2025-11-25']){
 const extraHeaders={Accept:'application/json, text/event-stream','MCP-Protocol-Version':version};
 const initialized=await rpc('initialize',{protocolVersion:version,capabilities:{},clientInfo:{name:'protocol-fixture',version:'1'},_meta:{}},{own:false,extraHeaders});
 assert.equal(initialized.status,200);assert.equal(initialized.data.result.protocolVersion,version);
 assert.equal(initialized.response.headers.get('MCP-Session-Id'),null); // Stateless: no session is issued or required.
 const notification=await worker.fetch(new Request(origin+'/mcp',{method:'POST',headers:{...extraHeaders,'Content-Type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized',params:{_meta:{}}})}),env);
 assert.equal(notification.status,202);assert.equal(await notification.text(),'');
 const discovery=await rpc('tools/list',{_meta:{}},{own:false,extraHeaders});
 assert.equal(discovery.status,200);assert.equal(discovery.data.result.tools.length,12);
}
assert.equal((await rpc('initialize',{protocolVersion:'future',capabilities:{},clientInfo:{name:'fixture',version:'1'}})).data.result.protocolVersion,'2025-11-25');
assert.equal((await rpc('ping')).status,200);
assert.equal((await rpc('initialize',{})).data.error.code,-32602);
assert.equal((await rpc('tools/list',{cursor:'unsupported'})).data.error.code,-32602);
assert.equal((await rpc('unknown')).data.error.code,-32601);
assert.equal((await rpc('ping',{}, {extraHeaders:{'MCP-Protocol-Version':'unsupported'}})).status,400);
for(const options of [{own:false},{extraHeaders:{'oai-authenticated-user-id':'other'}},{environment:{...env,OWNER_USER_ID:''}},{own:false,extraHeaders:{Authorization:'Bearer secret','OAI-Sites-Authorization':'Bearer service'}}]){
 const guarded={...options,environment:{...(options.environment||env),DB:{prepare(){assert.fail('Unauthorized tool reached storage');}}}};
 assert.equal((await call('read_run_status',{},guarded)).status,403);
}
for(const tool of listed.data.result.tools){
 const environment={...env,SITE_ACCESS:'public-legacy',DB:{prepare(){assert.fail('Anonymous MCP call reached storage');}}};
 assert.equal((await call(tool.name,{}, {own:false,environment})).status,403,tool.name);
}
assert.equal((await rpc('tools/list',{}, {own:false,extraHeaders:{Origin:'https://attacker.test'}})).status,403);
assert.equal((await call('read_profile',{user_id:'site-scoped-owner'})).data.error.code,-32602);
assert.equal((await call('unknown')).data.error.code,-32602);
assert.equal((await call('read_feedback',{after:-1})).data.error.code,-32602);
assert.equal((await call('read_feedback',{after:true})).data.error.code,-32602);
for(const method of ['GET','DELETE'])assert.equal((await worker.fetch(new Request(origin+'/mcp',{method}),env)).status,405);
const raw=async(body,headers={'Content-Type':'application/json'})=>worker.fetch(new Request(origin+'/mcp',{method:'POST',headers,body}),env);
assert.equal((await raw('{')).status,400);assert.equal((await raw('[]')).status,400);assert.equal((await raw('{}',{'Content-Type':'text/plain'})).status,415);assert.equal((await raw(' '.repeat(2_000_001))).status,413);
assert.equal((await raw(JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized'}))).status,202);
assert.equal((await raw(JSON.stringify({jsonrpc:'2.0',method:'tools/call',params:{name:'stage_snapshot'}}))).status,400);
assert.equal(value(await call('read_run_status')).run,null);
assert.deepEqual(value(await call('read_checkpoint_head')),{generation:0,publication:null});
assert.equal(value(await call('read_feedback',{after:0})).events.length,0);
assert.equal(value(await call('read_profile_events',{after:0})).events.length,0);
refused(await call('read_profile'),503);
const rowsFor=title=>[['paper','doi:1',{identity:'doi:1',title}],['private','profile',{content:'private interest',profile_cursor:0}],['meta','overview',{threshold:.7}],['meta','ui:status',{latest_run:{status:'succeeded'}}]].map(([kind,key,value])=>{const data=JSON.stringify(value);return {kind,key,data,checksum:hash(data)};}).sort((a,b)=>a.kind.localeCompare(b.kind)||a.key.localeCompare(b.key));
const idFor=rows=>hash(rows.map(row=>`${row.kind}:${row.key}:${row.checksum}`).join('\n'));
async function stage(title){const rows=rowsFor(title),generation=idFor(rows);value(await call('stage_snapshot',{action:'begin',data:{generation,count:rows.length}}));value(await call('stage_snapshot',{action:'chunk',data:{generation,records:rows}}));return {generation,rows};}
const first=await stage('first');
assert.equal(value(await call('read_run_status')).active_generation,undefined);
assert.equal((await call('stage_snapshot',{action:'finish',data:{generation:first.generation,activate:true}})).data.error.code,-32602);
const publication=(snapshot,number,overrides={})=>{const fields={operation_id:'run-'+number,snapshot_generation:snapshot,archive_sha256:hash('archive-'+number),archive_size:123,checkpoint_id:hash('checkpoint-'+number),library_file_id:'library-'+number,current_version:7,file_id:'file-'+number,file_name:'checkpoint.tar.gz',...overrides};return {...fields,publication_id:hash(JSON.stringify(Object.fromEntries(Object.keys(fields).sort().map(key=>[key,fields[key]]))))};};
const empty={generation:0,publication:null},p1=publication(first.generation,1);
refused(await call('commit_library_checkpoint',{data:{expected_head:empty,publication:p1}}),409);
value(await call('stage_snapshot',{action:'finish',data:{generation:first.generation}}));
assert.equal(value(await call('read_run_status')).active_generation,undefined);
const committed=value(await call('commit_library_checkpoint',{data:{expected_head:empty,publication:p1}}));
assert.deepEqual(committed.head,{generation:1,publication:p1});assert.equal(value(await call('read_run_status')).active_generation,first.generation);
assert.equal(value(await call('read_profile')).content,'private interest');
assert.equal(value(await call('commit_library_checkpoint',{data:{expected_head:empty,publication:p1}})).idempotent,true);
assert.deepEqual(value(await call('read_checkpoint_head')),committed.head);
refused(await call('commit_library_checkpoint',{data:{expected_head:empty,publication:{...p1,archive_size:124}}}),400);
refused(await call('commit_library_checkpoint',{data:{expected_head:committed.head,publication:publication(first.generation,2,{library_file_id:p1.library_file_id})}}),409);
refused(await call('commit_library_checkpoint',{data:{expected_head:committed.head,publication:publication(first.generation,2,{file_name:'../escape'})}}),400);
refused(await call('commit_library_checkpoint',{data:{expected_head:empty,publication:publication(first.generation,2)}}),409);
assert.equal(value(await call('acknowledge_snapshot',{data:{generation:first.generation,cursor:0,request_id:0}})).ok,true);
const second=await stage('second');value(await call('stage_snapshot',{action:'finish',data:{generation:second.generation}}));
const winner=publication(second.generation,2),loser=publication(first.generation,3);let raced=false;
const racedDB={prepare:env.DB.prepare,batch:async statements=>{if(!raced){raced=true;await commitLibraryHead(env.DB,{expected_head:committed.head,publication:winner});}return env.DB.batch(statements);}};
await assert.rejects(commitLibraryHead(racedDB,{expected_head:committed.head,publication:loser}),{status:409});
const latest=await readLibraryHead(env.DB);assert.equal(latest.publication.publication_id,winner.publication_id);assert.equal(value(await call('read_run_status')).active_generation,second.generation);
refused(await call('commit_library_checkpoint',{data:{expected_head:latest,publication:publication(second.generation,4,{library_file_id:p1.library_file_id})}}),409);
refused(await call('acknowledge_snapshot',{data:{generation:first.generation,cursor:0,request_id:0}}),409);
await assert.rejects(streamCheckpointFile({...env,BUCKET:{get(){assert.fail('Library PDF touched R2');}}},'fulltexts/old.pdf',hash('pdf'),4,second.generation),{status:404});
// Old R2 clients cannot silently replace a Library head, nor can the Library
// adapter interpret a previous R2 checkpoint as an empty bootstrap.
const tokenEnv={...env,SYNC_TOKEN:'fixture',BUCKET:{get(){},head(){},put(){}}};
await assert.rejects(handleCloudState(new Request(origin+'/api/state/head',{method:'POST',headers:{Authorization:'Bearer fixture','Content-Type':'application/json'},body:JSON.stringify({expected_generation:2,expected_manifest_sha256:hash('old'),manifest_sha256:hash('new'),snapshot_generation:first.generation})}),tokenEnv),{status:409});
const r2={DB:database()};await r2.DB.prepare("INSERT INTO pointers(key,value) VALUES('checkpoint_head',?)").bind(JSON.stringify({generation:1,manifest_sha256:hash('manifest'),snapshot_generation:first.generation})).run();
await assert.rejects(readLibraryHead(r2.DB),{status:409});
await assert.rejects(commitLibraryHead(r2.DB,{expected_head:empty,publication:p1}),{status:409});
console.log('Managed MCP candidate passed: protocol/discovery, default owner denial, bounded JSON/schema checks, staged-only snapshots, exact immutable Library receipts, atomic CAS/retry/race fencing, ACK binding, retained ID reuse refusal and Library PDF fail-closed.');
// Worst-case accepted Unicode and escaped profile/preference strings must make
// forward progress across bounded pages, rather than failing forever at 2 MB.
const largeContent='\u0001'.repeat(100000),largeInstructions='\u0001'.repeat(20000);
for(let seq=1;seq<=8;seq++)await env.DB.prepare('INSERT INTO cloud_profile_events(seq,base_version,base_generation,base_profile_hash,base_preferences,profile_hash,content,screening_preferences,summary,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)').bind(seq,seq-1,second.generation,hash('base'),JSON.stringify({relevance_threshold:.7,instructions:largeInstructions}),hash(largeContent),largeContent,JSON.stringify({relevance_threshold:.7,instructions:largeInstructions}),'edit '+seq,'2026-10-15T00:00:00Z').run();
let after=0,pages=0;const seen=[];
while(true){const pulled=await call('read_profile_events',{after});const page=value(pulled);assert.ok(new TextEncoder().encode(JSON.stringify(pulled.data)).length<2_000_000);assert.ok(page.cursor>after||!page.more);seen.push(...page.events.map(event=>event.seq));after=page.cursor;pages++;if(!page.more)break;}
assert.deepEqual(seen,[1,2,3,4,5,6,7,8]);assert.ok(pages>1);
for(let i=0;i<110;i++)await env.DB.prepare('INSERT INTO cloud_feedback(identity,interest,reason,favorite,reading_status,updated_at) VALUES(?,?,?,?,?,?)').bind('doi:'+i,'interested','\u0001'.repeat(3000),0,'unread','2026-10-15T00:00:00Z').run();
after=0;pages=0;let count=0;
while(true){const page=value(await call('read_feedback',{after}));assert.ok(new TextEncoder().encode(JSON.stringify(page)).length<2_000_000);count+=page.events.length;assert.ok(page.cursor>after||!page.more);after=page.cursor;pages++;if(!page.more)break;}
assert.equal(count,110);assert.ok(pages>1);
console.log('Byte-bounded profile and feedback event pagination passes with maximal escaping, monotonic cursors and no event loss.');
// A Python ensure_ascii=False/sort_keys canonicalization reference, including
// non-BMP Unicode, must be accepted without changing the returned filename.
const unicodePublication={operation_id:'run-unicode',snapshot_generation:'a'.repeat(64),archive_sha256:'b'.repeat(64),archive_size:123,checkpoint_id:'c'.repeat(64),library_file_id:'library-fixture',current_version:7,file_id:'file-fixture',file_name:'检查点📦.tar.gz',publication_id:'90f3d975a01f8464dd134f21873adcf59d7b6bd64cbf38cd73da0ec67d6424ec'};
await assert.rejects(commitLibraryHead(database(),{expected_head:empty,publication:unicodePublication}),error=>error.status===409&&error.message==='Checkpoint snapshot missing');
// Any statement failure rolls back the head, immutable-ID audit and active view.
const transactionalDB={prepare:env.DB.prepare,batch:statements=>env.DB.batch([statements[0],env.DB.prepare('INSERT INTO intentionally_missing_table(value) VALUES(1)'),...statements.slice(1)])};
const failedPublication=publication(second.generation,9);
await assert.rejects(commitLibraryHead(transactionalDB,{expected_head:latest,publication:failedPublication}));
assert.deepEqual(await readLibraryHead(env.DB),latest);
assert.equal((await env.DB.prepare("SELECT value FROM pointers WHERE key='active'").first()).value,second.generation);
assert.equal(await env.DB.prepare('SELECT value FROM pointers WHERE key=?').bind('library_archive:'+failedPublication.library_file_id).first(),null);
console.log('Library canonical Unicode receipt parity and transaction-failure rollback passed.');
// A valid large manifest can have a missing-key response too large for the
// duplicated MCP envelope. Omit optional negotiation, allowing full chunk upload.
const longManifest=Array.from({length:1300},(_,i)=>({kind:'private',key:'k'.repeat(996)+String(i).padStart(4,'0'),checksum:hash('row-'+i)}));
const longGeneration=idFor(longManifest);
const largeBegin=value(await call('stage_snapshot',{action:'begin',data:{generation:longGeneration,count:longManifest.length,manifest:longManifest}}));
assert.deepEqual(largeBegin,{generation:longGeneration,active:false});
assert.equal((await readLibraryHead(env.DB)).publication.publication_id,latest.publication.publication_id);
console.log('Large incremental manifest safely falls back to full bounded chunk upload.');

// Keep modern protocol regression coverage in the standard MCP test entrypoint.
await import("./test-mcp-modern.mjs");

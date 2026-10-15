import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import vm from 'node:vm';
import worker from '../worker/index.js';
import {readFileSync} from 'node:fs';
const client=readFileSync(new URL('../shared/static/app.js',import.meta.url),'utf8');
import {database} from './d1-local.mjs';
new vm.Script(client);
const env={SITE_ACCESS:'public-legacy',DB:database(),SYNC_TOKEN:'test-secret',OWNER_EMAIL:'owner@example.test'};
const origin='https://unit.example.chatgpt.site';
const digest=s=>createHash('sha256').update(s).digest('hex');
async function request(path,data,own=false,sync=false){const headers={};if(own)headers['oai-authenticated-user-email']=env.OWNER_EMAIL;if(sync)headers.Authorization='Bearer test-secret';if(data){headers['Content-Type']='application/json';headers.Origin=origin;}const response=await worker.fetch(new Request(origin+path,{method:data?'POST':'GET',headers,body:data?JSON.stringify(data):undefined}),env);return {status:response.status,data:await response.json()};}
const records=[['paper','doi:1',{identity:'doi:1',title:'Evidence paper',score:.8,eligibility_status:'eligible',abstract:'Original evidence',reasoning_json:'PRIVATE AUDIT REASONING',themes_json:'PRIVATE PERSONAL THEMES',score_dimensions_json:'PRIVATE AUDIT DIMENSIONS'}],['private','profile',{content:'Private personal profile'}],['feedback','doi:1',{identity:'doi:1',favorite:1,updated_at:'2026-01-01T00:00:00Z'}],['meta','overview',{threshold:.7,days:[],timezone:'Asia/Shanghai',sources:[],counts:{total:1}}]].map(([kind,key,value])=>{const data=JSON.stringify(value);return {kind,key,data,checksum:digest(data)};}).sort((a,b)=>a.kind.localeCompare(b.kind)||a.key.localeCompare(b.key));
for(const page of ['today','library','sources','profile','feedback','status']){const value={page,timezone:'Asia/Shanghai',display_timezone:'北京时间',papers:[],history_papers:[],totals:{},freshness:{last_import:'2026-01-01',is_today:true},latest_job:{imported_at:'2026-01-01T03:00:00Z'},latest_run:{status:'succeeded'},yesterday:'2026-01-01',history_date:'2026-01-01',history_days:[],sources:[],quality:{},active:{content:'Public research interests',version_number:3},versions:[],profile_review:{},items:[],stats:{},db_state:{integrity:'ok',schema_current:true},recommendation_quality:{},checks:[]};const data=JSON.stringify(value);records.push({kind:'meta',key:'ui:'+page,data,checksum:digest(data)});}records.sort((a,b)=>a.kind.localeCompare(b.kind)||a.key.localeCompare(b.key));
const generation=digest(records.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'));
assert.equal((await request('/api/sync/begin',{generation,count:records.length})).status,401);
assert.equal((await request('/api/sync/begin',{generation,count:records.length},false,true)).status,200);
assert.equal((await request('/api/sync/chunk',{generation,records:records.slice(0,1)},false,true)).status,200);
assert.equal((await request('/api/sync/finish',{generation},false,true)).status,409);
assert.equal((await request('/api/view?view=library')).status,503);
await request('/api/sync/chunk',{generation,records},false,true);
assert.equal((await request('/api/sync/finish',{generation},false,true)).status,200);
const publicView=await request('/api/view?view=library');assert.equal(publicView.status,200);assert.equal(publicView.data.papers.length,1);assert.equal(publicView.data.papers[0].feedback.favorite,1);assert.ok(!JSON.stringify(publicView).includes('Private personal profile'));
assert.ok(JSON.stringify(publicView).includes('PRIVATE AUDIT'));assert.ok(JSON.stringify(publicView).includes('PRIVATE PERSONAL THEMES'));
assert.equal((await request('/api/view?view=profile')).status,200);assert.equal((await request('/api/view?view=profile',null,true)).data.profile.content,'Private personal profile');
assert.equal((await request('/api/feedback',{identity:'doi:1',interest:null,favorite:0,reading_status:'read'})).status,200);
const patch={identity:'doi:1',interest:'interested',reason:'Specific decision mechanism',favorite:1,reading_status:'read'};
assert.equal((await request('/api/feedback',patch,true)).status,200);
const pulled=await request('/api/sync/feedback?after=0',null,false,true);assert.equal(pulled.data.events.length,2);assert.equal(pulled.data.events[0].reading_status,'read');assert.equal(pulled.data.cursor,2);
const broken='b'.repeat(64);await request('/api/sync/begin',{generation:broken,count:1},false,true);await request('/api/sync/chunk',{generation:broken,records:records.slice(0,1)},false,true);assert.equal((await request('/api/sync/finish',{generation:broken},false,true)).status,409);assert.equal((await request('/api/view?view=library')).data.generation,generation);
console.log('Worker verification passed: anonymous pages and feedback, atomic snapshots, checksum refusal, feedback sync.');
// Reuse unchanged records; only a changed paper is uploaded before atomic activation.
const next=records.map(r=>r.kind==='paper'?(()=>{const p=JSON.parse(r.data);p.score=.6;const data=JSON.stringify(p);return {...r,data,checksum:digest(data)};})():r);
const nextGeneration=digest(next.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'));
const opened=await request('/api/sync/begin',{generation:nextGeneration,count:next.length,manifest:next.map(({kind,key,checksum})=>({kind,key,checksum}))},false,true);
assert.equal(opened.status,200);assert.deepEqual(opened.data.missing,[{kind:'paper',key:'doi:1'}]);
assert.equal((await request('/api/view?view=library')).data.generation,generation);
await request('/api/sync/chunk',{generation:nextGeneration,records:next.filter(r=>r.kind==='paper')},false,true);
assert.equal((await request('/api/sync/finish',{generation:nextGeneration},false,true)).status,200);
assert.equal((await request('/api/view?view=library')).data.papers.length,0);
assert.equal((await request('/api/view?view=feedback',null,true)).data.papers.length,1);
const otherUser=await worker.fetch(new Request(origin+'/api/view?view=profile',{headers:{'oai-authenticated-user-email':'different@example.test'}}),env);
assert.equal(otherUser.status,200);
const csrf=await worker.fetch(new Request(origin+'/api/feedback',{method:'POST',headers:{'oai-authenticated-user-email':env.OWNER_EMAIL,'Content-Type':'application/json',Origin:'https://foreign.example'},body:JSON.stringify(patch)}),env);
assert.equal(csrf.status,403);
console.log('Incremental snapshots, downgraded-paper feedback, all-account access and CSRF verification passed.');

const queued=await request('/api/sync',{});assert.equal(queued.status,200);assert.equal(queued.data.request_id,1);
const latestPull=await request('/api/sync/feedback?after=2',null,false,true);assert.equal(latestPull.data.events.length,0);assert.equal(latestPull.data.active_generation,nextGeneration);assert.equal(latestPull.data.request_id,1);
await request('/api/sync',{});
const ack=await request('/api/sync/ack',{generation:nextGeneration,request_id:1,cursor:2},false,true);assert.equal(ack.status,200);assert.equal(ack.data.acknowledged_request_id,1);
assert.equal((await request('/api/sync/status')).data.pending,true);
await request('/api/sync/ack',{generation:nextGeneration,request_id:2,cursor:2},false,true);
assert.equal((await request('/api/sync/status')).data.pending,false);
assert.equal((await request('/api/sync/ack',{generation:nextGeneration,request_id:3,cursor:2},false,true)).status,400);
console.log('Monotonic manual requests, generation-bound ACK, and later-request preservation passed.');

for(const path of ['/','/library','/sources','/profile','/feedback','/status']) {
 const response=await worker.fetch(new Request(origin+path),env);assert.equal(response.status,200,path);
 const html=await response.text();assert.ok(html.includes('class="sidebar"'));assert.ok(html.includes('/static/app.css?v=0.12.0'));assert.ok(html.includes('data-sync-now'));assert.equal(html.includes('signin-with-chatgpt'),path==='/profile');
}
const style=await worker.fetch(new Request(origin+'/static/app.css'),env);assert.equal(await style.text(),readFileSync(new URL('../shared/static/app.css',import.meta.url),'utf8'));
console.log('All six anonymous shared-template pages and exact shared stylesheet passed.');

// Partial edits retain other markings; multiple writes within a frozen clock
// still have strictly increasing UTC timestamps and a deterministic winner.
const RealDate=Date;const instant=RealDate.now();globalThis.Date=class extends RealDate {constructor(...args){super(...(args.length?args:[instant]));}static now(){return instant;}};
try {
 await request('/api/feedback',{identity:'doi:1',reading_status:'read_later'});
 const first=(await request('/api/sync/feedback?after=2',null,false,true)).data.events.at(-1);
 await request('/api/feedback',{identity:'doi:1',reading_status:'read'});
 const second=(await request('/api/sync/feedback?after=2',null,false,true)).data.events.at(-1);
 assert.ok(RealDate.parse(second.updated_at)>RealDate.parse(first.updated_at));assert.equal(second.interest,'interested');assert.equal(second.favorite,1);assert.equal(second.reason,patch.reason);
 assert.equal((await request('/api/view?view=feedback')).data.papers[0].feedback.reading_status,'read');
} finally {globalThis.Date=RealDate;}
console.log('Partial-feedback preservation and same-millisecond monotonic edits passed.');

const oldDate=Date;globalThis.Date=class extends oldDate{constructor(...args){super(...(args.length?args:['2026-01-02T01:00:00Z']));}static now(){return oldDate.parse('2026-01-02T01:00:00Z');}};
try{const response=await worker.fetch(new Request(origin+'/'),env),html=await response.text();assert.ok(html.includes('今日待更新'));assert.ok(html.includes('今天尚未完成更新'));assert.ok(html.includes('value="2026-01-01"'));assert.ok(!html.includes('推荐已更新'))}finally{globalThis.Date=oldDate;}
console.log('Offline cross-midnight Today freshness and yesterday date passed.');

// Owner identity is mandatory for profile writes, including public Sites.
const beforeProfile=(await request('/api/view?view=profile')).data.profile;
const profilePatch={base_version:beforeProfile.version,generation:beforeProfile.generation,content:'Research interests <script>not executable</script>',summary:'Clarify the decision mechanism',screening_preferences:{relevance_threshold:.83,instructions:'Prefer field evidence and explicit mechanisms.'}};
assert.equal((await request('/api/profile',profilePatch)).status,403);
assert.equal((await request('/api/profile',profilePatch,false,true)).status,403);
async function directProfile(headers,data=profilePatch,overrides={}){
 const response=await worker.fetch(new Request(origin+'/api/profile',{method:'POST',headers:{'Content-Type':'application/json',Origin:origin,...headers},body:JSON.stringify(data)}),{...env,...overrides});return {status:response.status,data:await response.json()};
}
assert.equal((await directProfile({'oai-authenticated-user-email':'other@example.test'})).status,403);
assert.equal((await directProfile({'oai-authenticated-user-email':env.OWNER_EMAIL},profilePatch,{OWNER_EMAIL:''})).status,403);
assert.equal((await directProfile({'oai-authenticated-user-email':env.OWNER_EMAIL},profilePatch,{OWNER_USER_ID:'site-owner-id'})).status,403);
assert.equal((await directProfile({'oai-authenticated-user-email':env.OWNER_EMAIL,Origin:'https://evil.example'})).status,403);
for(const invalid of [{...profilePatch,base_version:-1},{...profilePatch,base_version:true},{...profilePatch,content:''},{...profilePatch,summary:''},{...profilePatch,screening_preferences:{relevance_threshold:1.1,instructions:''}},{...profilePatch,screening_preferences:{relevance_threshold:.8,instructions:'',sync_token:'forbidden'}}])assert.equal((await request('/api/profile',invalid,true)).status,400);
const contenders=await Promise.all([request('/api/profile',profilePatch,true),request('/api/profile',profilePatch,true)]);
assert.deepEqual(contenders.map(x=>x.status).sort(),[200,409]);
const edited=(await request('/api/view?view=profile')).data;
assert.equal(edited.profile.content,profilePatch.content);assert.equal(edited.profile.version,1);assert.equal(edited.profile.pending,true);assert.equal(edited.profile_editable,false);
assert.equal((await request('/api/view?view=profile',null,true)).data.profile_editable,true);
assert.equal((await request('/api/sync/profile?after=0')).status,401);
const profilePull=await request('/api/sync/profile?after=0',null,false,true);
assert.equal(profilePull.data.events.length,1);assert.equal(profilePull.data.events[0].base_version,0);assert.equal(profilePull.data.events[0].profile_hash,digest(profilePatch.content));assert.deepEqual(profilePull.data.events[0].screening_preferences,profilePatch.screening_preferences);
assert.equal((await request('/api/sync/profile?after=-1',null,false,true)).status,400);
assert.equal((await request('/api/sync/status')).data.pending,true);
assert.equal((await request('/api/sync/ack',{generation:nextGeneration,request_id:2,cursor:4,profile_cursor:1},false,true)).status,409);
const profilePage=await worker.fetch(new Request(origin+'/profile',{headers:{'oai-authenticated-user-email':env.OWNER_EMAIL}}),env);
const profileHtml=await profilePage.text();assert.ok(profileHtml.includes('action="/profile/save"'));assert.ok(profileHtml.includes('&lt;script&gt;not executable&lt;/script&gt;'));assert.ok(!profileHtml.includes('action="/profile/draft"'));assert.ok(!profileHtml.includes('action="/profile/confirm"'));
const submitted=new URLSearchParams({base_version:'0',generation:nextGeneration,content:'My unsaved text',summary:'Keep my edits',relevance_threshold:'.8',instructions:'Still retained'});
const conflict=await worker.fetch(new Request(origin+'/profile/save',{method:'POST',headers:{Origin:origin,'oai-authenticated-user-email':env.OWNER_EMAIL},body:submitted}),env);
assert.equal(conflict.status,409);const conflictHtml=await conflict.text();assert.ok(conflictHtml.includes('My unsaved text'));assert.ok(conflictHtml.includes('Still retained'));assert.ok(conflictHtml.includes('role="alert"'));
const secondEdit={...profilePatch,base_version:1,content:'Second confirmed research profile'};
assert.equal((await directProfile({'oai-authenticated-user-id':'site-owner-id'},secondEdit,{OWNER_USER_ID:'site-owner-id'})).status,200);
assert.equal((await request('/api/sync/profile?after=1',null,false,true)).data.events[0].base_profile_hash,digest(profilePatch.content));
console.log('Owner-only profile editing, absent/wrong identity denial, same-origin CSRF, validation, concurrent CAS, durable events, escaped UI and retained conflict edits passed.');

for(const path of ['/profile','/api/view?view=profile','/api/feedback','/api/sync','/api/sync/status']){
 const response=await worker.fetch(new Request(origin+path),{...env,SITE_ACCESS:'owner'});assert.equal(response.status,403,path);
}
const ownerPage=await worker.fetch(new Request(origin+'/profile',{headers:{'oai-authenticated-user-email':env.OWNER_EMAIL}}),{...env,SITE_ACCESS:'owner'});assert.equal(ownerPage.status,200);
const noIdentity=await worker.fetch(new Request(origin+'/api/view?view=profile'),{...env,SITE_ACCESS:'owner',OWNER_EMAIL:''});assert.equal(noIdentity.status,403);
const defaultPrivate=await worker.fetch(new Request(origin+'/profile'),{...env,SITE_ACCESS:undefined});assert.equal(defaultPrivate.status,403);
console.log('Whole-Site owner gate defaults private and denies absent/wrong identity; explicit public-legacy compatibility stays separate.');

// The consumed cursor is published with the matching profile. Only then can
// acknowledgement clear pending state; the append-only audit survives upload.
const consumed=next.map(row=>{if(row.kind!=='private')return row;const data=JSON.stringify({content:secondEdit.content,profile_hash:digest(secondEdit.content),profile_cursor:2,screening_preferences:secondEdit.screening_preferences});return {...row,data,checksum:digest(data)};});
const consumedGeneration=digest(consumed.map(row=>`${row.kind}:${row.key}:${row.checksum}`).join('\n'));
await request('/api/sync/begin',{generation:consumedGeneration,count:consumed.length,manifest:consumed.map(({kind,key,checksum})=>({kind,key,checksum}))},false,true);
await request('/api/sync/chunk',{generation:consumedGeneration,records:consumed.filter(r=>r.kind==='private')},false,true);
assert.equal((await request('/api/sync/finish',{generation:consumedGeneration},false,true)).status,200);
const consumedProfile=(await request('/api/view?view=profile')).data.profile;assert.equal(consumedProfile.pending,false);assert.equal(consumedProfile.version,2);assert.equal(consumedProfile.content,secondEdit.content);
const profileAck=await request('/api/sync/ack',{generation:consumedGeneration,request_id:2,cursor:4,profile_cursor:2},false,true);assert.equal(profileAck.status,200);assert.equal(profileAck.data.acknowledged_profile_cursor,2);assert.equal((await request('/api/sync/status')).data.pending,false);
assert.equal((await request('/api/sync/profile?after=0',null,false,true)).data.events.length,2);
assert.equal((await request('/api/profile',{...secondEdit,base_version:2},true)).status,409);
console.log('Consumed profile publication, generation conflict, durable audit and profile acknowledgement passed.');

// A staged snapshot never changes the reader-visible generation, and a
// checkpoint head prevents every legacy publisher from activating another one.
const stagedRows=next.map(r=>r.kind==='paper'?(()=>{const data=JSON.stringify({...JSON.parse(r.data),title:'Staged paper'});return {...r,data,checksum:digest(data)};})():r);
const stagedGeneration=digest(stagedRows.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'));
await request('/api/sync/begin',{generation:stagedGeneration,count:stagedRows.length,manifest:stagedRows.map(({kind,key,checksum})=>({kind,key,checksum}))},false,true);
await request('/api/sync/chunk',{generation:stagedGeneration,records:stagedRows},false,true);
const staged=await request('/api/sync/finish',{generation:stagedGeneration,activate:false},false,true);assert.equal(staged.status,200);assert.equal(staged.data.staged,true);
assert.equal((await request('/api/view?view=profile')).data.generation,consumedGeneration);
await env.DB.prepare("INSERT INTO pointers(key,value) VALUES('checkpoint_head','{}')").run();
assert.equal((await request('/api/sync/finish',{generation:stagedGeneration},false,true)).status,409);
assert.equal((await request('/api/view?view=profile')).data.generation,consumedGeneration);
console.log('Staged snapshots and SQL-fenced legacy activation passed.');

assert.equal((await request('/api/sync/finish',{generation:stagedGeneration,activate:false},false,true)).status,200);

// Owner PDF viewing is tied to the current committed checkpoint; generic blob
// transport and public-legacy visibility never confer file-read authority.
const pdfBytes=new TextEncoder().encode('%PDF-1.4\nSynthetic owner-only test PDF\n%%EOF');
const pdfHash=digest(pdfBytes),descriptorBytes=new TextEncoder().encode('{"synthetic":true}'),descriptorHash=digest(descriptorBytes);
const pdfMetadata={identity:'doi:1',path:'payload/fulltexts/test.pdf',sha256:pdfHash,size_bytes:pdfBytes.length,filename:'研究结果\r\nX-Injected: bad.pdf'};
const pdfRows=consumed.map(row=>{if(row.kind!=='paper')return row;const data=JSON.stringify({...JSON.parse(row.data),fulltext_id:7});return {...row,data,checksum:digest(data)};});
const pdfData=JSON.stringify(pdfMetadata);pdfRows.push({kind:'private',key:'fulltext:7',data:pdfData,checksum:digest(pdfData)});pdfRows.sort((a,b)=>a.kind.localeCompare(b.kind)||a.key.localeCompare(b.key));
const pdfGeneration=digest(pdfRows.map(row=>`${row.kind}:${row.key}:${row.checksum}`).join('\n'));
await request('/api/sync/begin',{generation:pdfGeneration,count:pdfRows.length},false,true);
await request('/api/sync/chunk',{generation:pdfGeneration,records:pdfRows},false,true);
assert.equal((await request('/api/sync/finish',{generation:pdfGeneration,activate:false},false,true)).status,200);
const fileManifest={format:'academic-radar-chunks-v1',checkpoint_id:'f'.repeat(64),files:[{path:'manifest.json',size:descriptorBytes.length,sha256:descriptorHash,chunks:[{size:descriptorBytes.length,sha256:descriptorHash}]},{path:pdfMetadata.path,size:pdfBytes.length,sha256:pdfHash,chunks:[{size:pdfBytes.length,sha256:pdfHash}]}]};
const manifestBytes=new TextEncoder().encode(JSON.stringify(fileManifest)),manifestHash=digest(manifestBytes);
const objects=new Map([[`private/checkpoints/v1/blob/${pdfHash}`,pdfBytes],[`private/checkpoints/v1/blob/${descriptorHash}`,descriptorBytes],[`private/checkpoints/v1/manifest/${manifestHash}`,manifestBytes]]);
const bucketObject=bytes=>({size:bytes.length,checksums:{sha256:Uint8Array.from(Buffer.from(digest(bytes),'hex')).buffer},body:new Response(bytes).body,arrayBuffer:async()=>bytes.slice().buffer});
const pdfEnv={...env,BUCKET:{head:async key=>objects.has(key)?bucketObject(objects.get(key)):null,get:async key=>objects.has(key)?bucketObject(objects.get(key)):null}};
const fileRequest=(suffix,owner=false,extra={})=>worker.fetch(new Request(origin+'/fulltext/file'+suffix,{headers:owner?{'oai-authenticated-user-email':env.OWNER_EMAIL}:undefined,...extra}),pdfEnv);
assert.equal((await fileRequest('?id=7')).status,403);
assert.equal((await fileRequest('?id=7',false,{headers:{Authorization:'Bearer test-secret'}})).status,403);
assert.equal((await fileRequest('?id=7',false,{headers:{'oai-authenticated-user-email':'other@example.test'}})).status,403);
await env.DB.prepare("UPDATE pointers SET value=? WHERE key='active'").bind(pdfGeneration).run();
await env.DB.prepare("UPDATE pointers SET value=? WHERE key='checkpoint_head'").bind(JSON.stringify({generation:1,manifest_sha256:manifestHash,snapshot_generation:consumedGeneration})).run();
assert.equal((await fileRequest('?id=7',true)).status,409);
await env.DB.prepare("UPDATE pointers SET value=? WHERE key='checkpoint_head'").bind(JSON.stringify({generation:2,manifest_sha256:manifestHash,snapshot_generation:pdfGeneration})).run();
const pdfResponse=await fileRequest('?identity=doi%3A1',true);assert.equal(pdfResponse.status,200);assert.equal(pdfResponse.headers.get('Content-Type'),'application/pdf');assert.equal(pdfResponse.headers.get('Cache-Control'),'private, no-store');assert.equal(pdfResponse.headers.get('Content-Security-Policy'),'sandbox');assert.ok(pdfResponse.headers.get('Content-Disposition').includes('filename*=UTF-8'));assert.deepEqual(new Uint8Array(await pdfResponse.arrayBuffer()),pdfBytes);
const pdfHead=await fileRequest('?id=7',true,{method:'HEAD'});assert.equal(pdfHead.status,200);assert.equal(pdfHead.headers.get('Content-Length'),String(pdfBytes.length));assert.equal(await pdfHead.text(),'');
assert.equal((await fileRequest('?id=999',true)).status,404);
assert.equal((await fileRequest('?id=../private',true)).status,400);
assert.equal((await fileRequest('?identity=missing',true)).status,404);
console.log('Owner-only PDF route, identity/id lookup, committed-snapshot fence, safe headers, Unicode filename, byte-exact stream and HEAD passed.');

const pdfRange=await fileRequest('?id=7',true,{headers:{'oai-authenticated-user-email':env.OWNER_EMAIL,Range:'bytes=0-4'}});assert.equal(pdfRange.status,200);assert.equal(pdfRange.headers.get('Accept-Ranges'),'none');assert.equal(pdfRange.headers.get('Content-Range'),null);assert.equal(pdfRange.headers.has('X-Injected'),false);assert.ok(!/[\r\n]/.test(pdfRange.headers.get('Content-Disposition')));assert.deepEqual(new Uint8Array(await pdfRange.arrayBuffer()),pdfBytes);
const noLookup=await worker.fetch(new Request(origin+'/fulltext/file?id=7'),{...pdfEnv,DB:{prepare(){throw new Error('Unauthorized metadata lookup');}}});assert.equal(noLookup.status,403);
console.log('PDF Range requests safely receive full 200 bytes; unauthorized reads stop before metadata lookup and filenames cannot inject headers.');

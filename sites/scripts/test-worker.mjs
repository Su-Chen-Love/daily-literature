import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import vm from 'node:vm';
import worker from '../worker/index.js';
import {readFileSync} from 'node:fs';
const client=readFileSync(new URL('../shared/static/app.js',import.meta.url),'utf8');
import {database} from './d1-local.mjs';
new vm.Script(client);
const env={DB:database(),SYNC_TOKEN:'test-secret',OWNER_EMAIL:'owner@example.test'};
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
 const html=await response.text();assert.ok(html.includes('class="sidebar"'));assert.ok(html.includes('/static/app.css?v=daily-20261018'));assert.ok(html.includes('data-sync-now'));assert.ok(!html.includes('signin-with-chatgpt'));
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

await import('./test-profile-ui.mjs');

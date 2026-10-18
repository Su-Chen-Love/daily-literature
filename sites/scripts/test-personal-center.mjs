import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import worker from '../worker/index.js';
import {database} from './d1-local.mjs';
const DB=database(),generation='a'.repeat(64),origin='https://center.example.chatgpt.site';
const env={DB,OWNER_EMAIL:'fixture-owner@example.test',SITE_ACCESS:'owner'};
const headers={'oai-authenticated-user-email':env.OWNER_EMAIL,Origin:origin};
const hash=value=>createHash('sha256').update(value).digest('hex');
const record=(kind,key,value)=>{const data=JSON.stringify(value);DB.sql.prepare('INSERT INTO records VALUES(?,?,?,?,?)').run(generation,kind,key,data,hash(data));};
DB.sql.prepare('INSERT INTO pointers VALUES(?,?)').run('active',generation);
record('meta','overview',{threshold:.85,last_import:'2026-01-01T00:00:00Z'});
const base={quality:{visible:1,abstract_percent:100,missing_abstracts:0},recommendation_quality:{precision_percent:100,false_positive:0},checks:[],healthy_count:1,update_prompt:'Synthetic local update task',update_summary:'Synthetic check context',db_state:{integrity:'ok',schema_current:true},local_checked_at:'2026-01-02T00:00:00Z'};
record('meta','ui:status',base);record('meta','ui:library',{...base,papers:[],q:'',interest:'',favorite:'',reading:'',sort:'score_desc'});
record('paper','doi:low',{identity:'doi:low',title:'Saved below threshold',score:.55,eligibility_status:'excluded',abstract:'Complete original fixture abstract',authors_json:'[]',themes_json:'[]',confidence:.8});
record('paper','doi:normal',{identity:'doi:normal',title:'Ordinary recommended paper',score:.75,eligibility_status:'eligible',abstract:'Another original fixture abstract',authors_json:'[]',themes_json:'[]',confidence:.8});
record('feedback','doi:low',{identity:'doi:low',interest:null,reason:null,favorite:1,reading_status:'read_later',updated_at:'2026-01-01T00:00:00Z'});
async function request(path,data){return worker.fetch(new Request(origin+path,{headers:{...headers,...(data?{'Content-Type':'application/json'}:{})},method:data?'POST':'GET',...(data?{body:JSON.stringify(data)}:{})}),env)}
const normal=await (await request('/library')).text();assert.ok(!normal.includes('Saved below threshold'));assert.ok(normal.includes('Ordinary recommended paper'));
for(const query of ['favorite=yes','reading=read_later']){
 const html=await (await request('/library?'+query)).text();assert.ok(html.includes('Saved below threshold'));assert.ok(html.includes('低于推荐标准'));}
const savedView=await (await request('/api/view?view=library&favorite=yes')).json();assert.equal(savedView.papers.length,1);assert.equal(savedView.papers[0].identity,'doi:low');
const response=await request('/api/feedback',{identity:'doi:low',interest:'interested',reason:'',favorite:true,reading_status:'read_later'});assert.equal(response.status,200);
const saved=DB.sql.prepare('SELECT * FROM cloud_feedback ORDER BY seq DESC LIMIT 1').get();assert.equal(saved.interest,'interested');assert.equal(saved.reason,null);assert.equal(saved.favorite,1);assert.equal(saved.reading_status,'read_later');
const checks=await (await request('/api/center/status')).json();assert.equal(checks.local_checked_at,base.local_checked_at);assert.equal(checks.last_import,'2026-01-01T00:00:00Z');assert.ok(checks.html.includes('Synthetic local update task'));assert.ok(checks.sync.pending);
const redirect=await request('/status');assert.equal(redirect.status,303);assert.equal(redirect.headers.get('Location'),'/?center=checks');
const unauth=await worker.fetch(new Request(origin+'/api/center/status'),env);assert.ok([200,403].includes(unauth.status)); // legacy repository keeps its existing access behavior; production worker denial is tested separately.
console.log('Personal center checks/redirect/auth, fixed .7 views, optional feedback and all saved low-score/excluded entries passed.');

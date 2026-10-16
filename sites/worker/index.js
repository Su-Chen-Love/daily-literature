import {render,staticAsset} from './render.js';
import {handleCloudState,streamCheckpointFile,readLibraryHead,commitLibraryHead} from './cloud-state.js';
import {handleMcp} from './mcp.js';
import {eventPage} from './events.js';
import {appendFeedback} from './feedback.js';
import {mainStoreOperation} from './main-store.js';
import {publishMainSnapshot} from './main-publication.js';
import {siteBackup} from './site-backup.js';
import {displayRecord,displayRecords} from './display-records.js';
import {isOwner,profileState,profileEvents,profileHistory,saveProfile} from './profile.js';
const h={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY'};
const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{...h,'Content-Type':'application/json; charset=utf-8'}});
const fail=(message,status=400)=>{throw Object.assign(new Error(message),{status});};
const hash=async text=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))).map(n=>n.toString(16).padStart(2,'0')).join('');
const owner=(r,e)=>isOwner(r,e)||e.SITE_ACCESS==='public-legacy';
async function pointer(db,key){return (await db.prepare('SELECT value FROM pointers WHERE key=?').bind(key).first())?.value;}
async function syncState(db){const request_id=Number(await pointer(db,'sync_request')||0),acknowledged_request_id=Number(await pointer(db,'sync_request_ack')||0),cursor=Number((await db.prepare('SELECT MAX(seq) seq FROM cloud_feedback').first())?.seq||0),acknowledged_cursor=Number(await pointer(db,'sync_cursor_ack')||0);const profile_cursor=Number((await db.prepare('SELECT MAX(seq) seq FROM cloud_profile_events').first())?.seq||0),acknowledged_profile_cursor=Number(await pointer(db,'sync_profile_cursor_ack')||0);return {profile_events_version:1,profile_cursor,acknowledged_profile_cursor,request_id,acknowledged_request_id,cursor,acknowledged_cursor,pending:request_id>acknowledged_request_id||cursor>acknowledged_cursor||profile_cursor>acknowledged_profile_cursor,synced_at:await pointer(db,'synced_at'),active_generation:await active(db)};}
async function requestSync(r,e,u){if(r.headers.get('Origin')!==u.origin)fail('请求来源无效',403);await e.DB.prepare("INSERT INTO pointers(key,value) VALUES('sync_request','1') ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)").run();return json({...(await syncState(e.DB)),message:'同步请求已保存；云端任务会在下次运行时处理。'});}
async function boundedText(r){const reader=r.body?.getReader();if(!reader)return '';const chunks=[];let size=0;try{while(true){const part=await reader.read();if(part.done)break;size+=part.value.byteLength;if(size>2000000){await reader.cancel();fail('请求过大',413);}chunks.push(part.value);}}finally{reader.releaseLock();}const bytes=new Uint8Array(size);let offset=0;for(const part of chunks){bytes.set(part,offset);offset+=part.byteLength;}try{return new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch{fail('请求编码无效');}}
async function body(r){if((r.headers.get('content-type')||'').split(';')[0].trim()!=='application/json')fail('需要 JSON 请求',415);const t=await boundedText(r);try{return JSON.parse(t);}catch{fail('JSON 格式无效');}}
const gid=v=>{if(!/^[a-f0-9]{64}$/.test(v||''))fail('快照编号无效');return v;};
async function active(db){return(await db.prepare("SELECT value FROM pointers WHERE key='active'").first())?.value;}
async function record(db,g,k,key){const r=await displayRecord(db,g,k,key);return r?JSON.parse(r.data):null;}
async function all(db,g,k){return(await displayRecords(db,g,k)).map(r=>JSON.parse(r.data));}
async function feedback(db,g){const m=new Map((await all(db,g,'feedback')).map(r=>[r.identity,r]));for(const r of(await db.prepare('SELECT * FROM cloud_feedback ORDER BY seq').all()).results){const p=m.get(r.identity);if(!p||Date.parse(r.updated_at)>Date.parse(p.updated_at))m.set(r.identity,r);}return m;}
async function requireSyncAuth(r,e){const token=r.headers.get('Authorization');if(!e.SYNC_TOKEN||!token?.startsWith('Bearer ')||await hash(token.slice(7))!==await hash(e.SYNC_TOKEN))fail('同步凭据无效',401);}
async function sync(r,e,u){
 await requireSyncAuth(r,e);
 const db=e.DB;
 if(u.pathname==='/api/sync/profile'&&r.method==='GET')return json(await profileEvents(db,Number(u.searchParams.get('after')||0)));
 if(u.pathname==='/api/sync/feedback'&&r.method==='GET'){const n=Number(u.searchParams.get('after')||0);if(!Number.isSafeInteger(n)||n<0)fail('游标无效');return json({...await syncState(db),...await eventPage(db,'cloud_feedback',n,500)});}
 if(r.method!=='POST')fail('不支持此方法',405);return syncOperation(e,u.pathname,await body(r));
}
// Authentication belongs to each transport; this shared implementation never
// constructs synthetic trusted identity headers or consumes host credentials.
async function syncOperation(e,path,d,{deduplicate=false}={}){
 const db=e.DB,g=gid(d.generation);
 if(path==='/api/sync/ack'){
  if(await pointer(db,'writer_mode')==='d1-main')fail('Main-store publication acknowledges edits atomically',409);
  const state=await syncState(db);if(state.active_generation!==g)fail('快照已更新，请重新检查',409);
  if(!Number.isSafeInteger(d.request_id)||d.request_id<0||d.request_id>state.request_id||!Number.isSafeInteger(d.cursor)||d.cursor<0||d.cursor>state.cursor)fail('确认游标无效');
  const profileCursor=d.profile_cursor??0;if(!Number.isSafeInteger(profileCursor)||profileCursor<0||profileCursor>state.profile_cursor)fail('画像确认游标无效');
  const profileSnapshot=await record(db,g,'private','profile');if(profileCursor>Number(profileSnapshot?.profile_cursor||0))fail('画像尚未包含在已激活快照中',409);
  const acknowledgements=await db.batch([
   ['sync_profile_cursor_ack',profileCursor],['sync_request_ack',d.request_id],['sync_cursor_ack',d.cursor]
  ].map(([key,value])=>db.prepare("INSERT INTO pointers(key,value) SELECT ?,? WHERE (SELECT value FROM pointers WHERE key='active')=? AND NOT EXISTS(SELECT 1 FROM pointers WHERE key='writer_mode' AND value='d1-main') ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)").bind(key,String(value),g)));
  if(!acknowledgements[0]?.meta?.changes)fail('快照已更新，请重新检查',409);
  const done=await syncState(db);return json({ok:true,acknowledged_request_id:done.acknowledged_request_id,acknowledged_cursor:done.acknowledged_cursor,acknowledged_profile_cursor:done.acknowledged_profile_cursor});
 }
 if(path==='/api/sync/begin'){
  if(!Number.isSafeInteger(d.count)||d.count<1||d.count>100000)fail('记录数量无效');
  await db.prepare('INSERT INTO generations(id,count,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING').bind(g,d.count,new Date().toISOString()).run();
  const existing=await db.prepare('SELECT count,status FROM generations WHERE id=?').bind(g).first();if(existing.count!==d.count)fail('快照不一致',409);
  if(deduplicate)await db.prepare("INSERT INTO display_generations(generation) SELECT ? WHERE EXISTS(SELECT 1 FROM generations WHERE id=? AND status='staging') ON CONFLICT DO NOTHING").bind(g,g).run();
  if(await active(db)===g)return json({generation:g,active:true});
  if(d.manifest){
   if(!Array.isArray(d.manifest)||d.manifest.length!==d.count)fail('快照清单无效');
   const keys=new Set();for(const m of d.manifest){if(!['paper','history','feedback','private','meta'].includes(m.kind)||typeof m.key!=='string'||m.key.length>1000||!/^[a-f0-9]{64}$/.test(m.checksum||''))fail('快照清单无效');const key=m.kind+':'+m.key;if(keys.has(key))fail('重复记录');keys.add(key);}
   const base=await active(db);
   if(base&&existing.status==='staging')await db.batch([
    db.prepare(`INSERT INTO display_payloads(checksum,data) SELECT r.checksum,r.data FROM json_each(?) m JOIN records r ON r.generation=? AND r.kind=json_extract(m.value,'$.kind') AND r.key=json_extract(m.value,'$.key') AND r.checksum=json_extract(m.value,'$.checksum') WHERE r.data!='' AND EXISTS(SELECT 1 FROM display_generations WHERE generation=?) AND EXISTS(SELECT 1 FROM generations WHERE id=? AND status='staging') ON CONFLICT(checksum) DO NOTHING`).bind(JSON.stringify(d.manifest),base,g,g),
    db.prepare(`INSERT OR IGNORE INTO records(generation,kind,key,data,checksum)
     SELECT ?,r.kind,r.key,CASE WHEN EXISTS(SELECT 1 FROM display_generations WHERE generation=?) THEN '' ELSE COALESCE(NULLIF(r.data,''),p.data) END,r.checksum FROM json_each(?) m JOIN records r
     ON r.generation=? AND r.kind=json_extract(m.value,'$.kind') AND r.key=json_extract(m.value,'$.key')
     AND r.checksum=json_extract(m.value,'$.checksum') LEFT JOIN display_payloads p ON p.checksum=r.checksum WHERE EXISTS(SELECT 1 FROM generations WHERE id=? AND status='staging')`).bind(g,g,JSON.stringify(d.manifest),base,g),
   ]);
   const ready=new Set((await db.prepare('SELECT kind,key FROM records WHERE generation=?').bind(g).all()).results.map(r=>r.kind+':'+r.key));
   return json({generation:g,active:false,missing:d.manifest.filter(m=>!ready.has(m.kind+':'+m.key)).map(m=>({kind:m.kind,key:m.key}))});
  }
  return json({generation:g,active:false});
 }
 const generation=await db.prepare('SELECT * FROM generations WHERE id=?').bind(g).first();if(!generation)fail('请先建立快照');
 if(path==='/api/sync/chunk'){
  if(!Array.isArray(d.records)||!d.records.length||d.records.length>40)fail('批次无效');const keys=new Set();
  for(const row of d.records){if(!['paper','history','feedback','private','meta'].includes(row.kind)||typeof row.key!=='string'||row.key.length>1000||typeof row.data!=='string'||row.data.length>1000000)fail('快照记录无效');if(await hash(row.data)!==row.checksum)fail('记录校验失败');const p=JSON.parse(row.data);if(row.kind==='paper'&&p.identity!==row.key)fail('身份不一致');const key=row.kind+':'+row.key;if(keys.has(key))fail('重复记录');keys.add(key);}
  if(generation.status==='staging'){const raw=JSON.stringify(d.records);await db.batch([
   db.prepare("INSERT INTO display_payloads(checksum,data) SELECT json_extract(j.value,'$.checksum'),json_extract(j.value,'$.data') FROM json_each(?) j WHERE EXISTS(SELECT 1 FROM display_generations WHERE generation=?) AND EXISTS(SELECT 1 FROM generations WHERE id=? AND status='staging') ON CONFLICT(checksum) DO NOTHING").bind(raw,g,g),
   db.prepare("INSERT INTO records(generation,kind,key,data,checksum) SELECT ?,json_extract(j.value,'$.kind'),json_extract(j.value,'$.key'),CASE WHEN EXISTS(SELECT 1 FROM display_generations WHERE generation=?) THEN '' ELSE json_extract(j.value,'$.data') END,json_extract(j.value,'$.checksum') FROM json_each(?) j WHERE EXISTS(SELECT 1 FROM generations WHERE id=? AND status='staging') AND (NOT EXISTS(SELECT 1 FROM display_generations WHERE generation=?) OR EXISTS(SELECT 1 FROM display_payloads p WHERE p.checksum=json_extract(j.value,'$.checksum') AND p.data=json_extract(j.value,'$.data'))) ON CONFLICT(generation,kind,key) DO NOTHING").bind(g,g,raw,g,g),
  ]);}return json({accepted:d.records.length});
 }
 if(path==='/api/sync/finish'){
  // Seal before hashing. Every ingestion statement rechecks staging in SQL,
  // so a delayed chunk cannot append to a verified immutable generation.
  let seal=null;
  if(generation.status!=='complete'){
   const expired=generation.status.startsWith('verifying:')&&Date.now()-Number(generation.status.split(':')[1])>600000;
   if(generation.status!=='staging'&&!expired)fail('快照正在校验，请稍后重试',409);
   seal='verifying:'+Date.now()+':'+crypto.randomUUID();
   const lock=await db.prepare('UPDATE generations SET status=? WHERE id=? AND status=?').bind(seal,g,generation.status).run();
   if(!lock.meta?.changes)fail('快照校验状态已变化，请重试',409);
  }
  let rows;
  try{
   rows=(await db.prepare('SELECT kind,key,checksum FROM records WHERE generation=? ORDER BY kind,key').bind(g).all()).results;
   if(await db.prepare("SELECT 1 missing FROM records r LEFT JOIN display_payloads p ON p.checksum=r.checksum WHERE r.generation=? AND r.data='' AND p.data IS NULL LIMIT 1").bind(g).first())fail('快照内容引用不完整',409);
   if(rows.length!==generation.count||await hash(rows.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'))!==g)fail('快照尚不完整，原数据仍可访问',409);
   if(seal){const done=await db.prepare("UPDATE generations SET status='complete' WHERE id=? AND status=?").bind(g,seal).run();if(!done.meta?.changes)fail('快照校验租约已变化，请重试',409);}
  }catch(error){if(seal)await db.prepare("UPDATE generations SET status='staging' WHERE id=? AND status=?").bind(g,seal).run();throw error;}
  if(d.activate===false)return json({generation:g,count:rows.length,staged:true});
  // Legacy publication cannot race the first cloud checkpoint commit. Both
  // pointer updates test the fence inside the same D1 transaction.
  const published=await db.batch([db.prepare("UPDATE generations SET status='complete' WHERE id=?").bind(g),db.prepare("INSERT INTO pointers(key,value) SELECT 'active',? WHERE NOT EXISTS(SELECT 1 FROM pointers WHERE key='checkpoint_head') AND NOT EXISTS(SELECT 1 FROM pointers WHERE key='writer_mode' AND value='d1-main') ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(g),db.prepare("INSERT INTO pointers(key,value) SELECT 'synced_at',? WHERE NOT EXISTS(SELECT 1 FROM pointers WHERE key='checkpoint_head') AND NOT EXISTS(SELECT 1 FROM pointers WHERE key='writer_mode' AND value='d1-main') ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(new Date().toISOString())]);
  if(!published[1]?.meta?.changes)fail('云端主数据库已启用；请通过云端检查点提交发布',409);
  // Immutable snapshots are retained. Garbage collection must be fenced by the
  // checkpoint head and all in-flight commits, not a racy last-two heuristic.
  return json({generation:g,count:rows.length});
 }
 fail('同步操作不存在',404);
}
async function view(r,e,u){
 const db=e.DB,g=await active(db),own=owner(r,e);if(!g)return json({ready:false,owner:own,message:'数据库正在首次同步，请稍后刷新。'},503);
 const meta=await record(db,g,'meta','overview'),result={ready:true,owner:own,overview:meta,generation:g,synced_at:(await db.prepare("SELECT value FROM pointers WHERE key='synced_at'").first())?.value};
 const v=u.searchParams.get('view')||'today';
 if(v==='profile'){result.profile={...await record(db,g,'private','profile'),...await profileState(db,g)};result.profile_editable=isOwner(r,e);return json(result);}
 if(v==='sources'||v==='status'){if(!own)result.overview.sources=meta.sources.map(s=>({...s,health:{status:s.health?.status,last_success_at:s.health?.last_success_at}}));return json(result);}
 let papers=await all(db,g,'paper');
 if(v==='today'){const today=new Date().toLocaleDateString('en-CA',{timeZone:meta.timezone||'Asia/Shanghai'}),day=u.searchParams.get('day')||today;let history=await record(db,g,'history',day===today?'current':day);if(day===today&&history?.day!==today)history=null;const index=new Map(papers.map(p=>[p.identity,p]));result.day=day;result.batches=history?.batches||0;result.incomplete=history?.incomplete||false;papers=(history?.papers||[]).filter(p=>index.has(p.identity)).map(p=>({...index.get(p.identity),...p}));}
 else if(v==='library')papers=papers.filter(p=>p.eligibility_status==='eligible'&&p.score>=(meta.threshold??.70));else if(v!=='feedback')fail('页面不存在',404);
 const f=own?await feedback(db,g):new Map();
 const publicFields=['identity','doi','title','abstract','venue','published','published_precision','url','authors_json','abstract_source','abstract_source_url','abstract_retrieved_at','eligibility_status','publication_type','score','reasons','confidence','rubric_version','needs_rescreen'];
 result.papers=papers.map(p=>own?{...p,...(f.has(p.identity)?{feedback:f.get(p.identity)}:{})}:Object.fromEntries(publicFields.filter(k=>k in p).map(k=>[k,p[k]])));
 if(v==='feedback')result.papers=result.papers.filter(p=>p.feedback);return json(result);
}
async function saveFeedback(r,e,u){
 if(r.headers.get('Origin')!==u.origin)fail('请求来源无效',403);
 const form=!(r.headers.get('content-type')||'').includes('application/json');
 const d=form?Object.fromEntries(await r.formData()):await body(r),g=await active(e.DB);
 if(!g||!await record(e.DB,g,'paper',d.identity))fail('论文不存在',404);
 const previous=(await feedback(e.DB,g)).get(d.identity)||{interest:null,reason:null,favorite:0,reading_status:'unread'};
 const has=key=>Object.prototype.hasOwnProperty.call(d,key);
 let patch={...previous,...d,interest:has('interest')?d.interest||null:previous.interest,favorite:form?(d.favorite==='on'?1:0):has('favorite')?Number(d.favorite):previous.favorite,reading_status:has('reading_status')?d.reading_status||'unread':previous.reading_status};
 if(u.pathname==='/api/favorite')patch={...previous,favorite:Number(Boolean(d.favorite))};
 if(u.pathname==='/feedback/clear')patch={interest:null,reason:null,favorite:0,reading_status:'unread'};
 if(![null,'interested','not_interested'].includes(patch.interest)||![0,1].includes(patch.favorite)||!['unread','read','read_later'].includes(patch.reading_status))fail('反馈格式无效');
 const reason=String(patch.reason||'').trim();if(reason.length>3000||patch.interest&&!reason)fail('请填写具体偏好理由');const stamp=new Date(Math.max(Date.now(),Date.parse(previous.updated_at||'')+1||0)).toISOString();
 const changedFields=u.pathname==='/feedback/clear'?['interest','reason','favorite','reading_status']:
  u.pathname==='/api/favorite'?['favorite']:['interest','reason','favorite','reading_status'].filter(key=>has(key)||(form&&key==='favorite'));
 const edits=Object.fromEntries(changedFields.map(key=>[key,key==='reason'?reason||null:patch[key]]));
 const saved=await appendFeedback(e.DB,g,d.identity,previous,edits,stamp);
 if(form){const target=String(d.return_to||'/feedback');return new Response(null,{status:303,headers:{...h,Location:target.startsWith('/')&&!target.startsWith('//')?target:'/feedback'}});}
 const labels={interested:'已完成 · 感兴趣',not_interested:'已完成 · 不感兴趣'};
 return json({ok:true,saved:true,changed:saved.changed,updated_at:saved.updated_at,interest:saved.interest,favorite:Boolean(saved.favorite),reading_status:saved.reading_status,status_label:labels[saved.interest]||'',message:'反馈已保存到云端，将用于后续推荐。'});
}
const binary=(a,b)=>a===b?0:a==null?-1:b==null?1:a<b?-1:1;
const fold=s=>String(s||'').replace(/[A-Z]/g,c=>c.toLowerCase());
const paperOrder=(a,b)=>Number(Boolean(a.interest))-Number(Boolean(b.interest))+0 || Number(a.interest==='not_interested')-Number(b.interest==='not_interested') || b.score-a.score || binary(b.published,a.published) || binary(a.identity,b.identity);
function searchMatches(value,query){const pattern=fold(query).replace(/[.*+?^${}()|[\]\\]/g,'\\$&').replace(/%/g,'.*').replace(/_/g,'.');return new RegExp(pattern,'su').test(fold(value));}
async function pageContext(r,e,u,page){
 const db=e.DB,g=await active(db);if(!g)fail('数据库正在同步，请稍后刷新',503);
 const meta=await record(db,g,'meta','overview'),base=await record(db,g,'meta','ui:'+page);if(!base)fail('界面数据正在更新，请稍后刷新',503);
 const state=await syncState(db),f=await feedback(db,g),papers=await all(db,g,'paper');
 const merge=p=>{const saved=f.get(p.identity)||{};return {...p,...saved,feedback_reason:saved.reason||'',reasons:p.reasons,reading_status:saved.reading_status||'unread',favorite:saved.favorite||0};};
 const index=new Map(papers.map(p=>[p.identity,merge(p)]));
 const context={...base,site_mode:'cloud',state_path:'云端主数据库',cloud_url:u.origin,cloud_sync_status:{status:'succeeded',synced_at:state.synced_at},csrf_token:'same-origin-public',request:{url:{path:u.pathname,query:u.search.slice(1)}}};
 if(page==='profile'){context.cloud_profile=await profileState(db,g);context.profile_editable=isOwner(r,e);context.cloud_profile_history=await profileHistory(db);context.profile_saved=u.searchParams.get('saved')==='1';}
 const sp=u.searchParams;
 if(page==='today'){
  const timezone=meta.timezone||'Asia/Shanghai',dateLabel=d=>d.toLocaleDateString('en-CA',{timeZone:timezone}),today=dateLabel(new Date()),current=await record(db,g,'history','current');
  context.yesterday=new Date(new Date(today+'T00:00:00Z').getTime()-86400000).toISOString().slice(0,10);
  context.history_date=context.yesterday;context.history_days=(meta.days||[]).filter(d=>d<=context.yesterday);
  context.freshness={...context.freshness,is_today:Boolean(context.latest_job?.imported_at&&dateLabel(new Date(context.latest_job.imported_at))===today)};
  context.papers=current?.day===today?(current.papers||[]).filter(p=>index.has(p.identity)).map(p=>merge({...index.get(p.identity),...p})).sort(paperOrder):[];
  const chosen=sp.get('history_date')||context.history_date;
  if(!/^\d{4}-\d{2}-\d{2}$/.test(chosen)||chosen>context.yesterday)fail('历史日期无效');
  const history=await record(db,g,'history',chosen);
  context.history_date=chosen;context.history_runs=history?.batches||0;context.history_incomplete=history?.incomplete||false;
  context.history_papers=(history?.papers||[]).filter(p=>index.has(p.identity)).map(p=>merge({...index.get(p.identity),...p})).sort(paperOrder);
  const d=new Date(chosen+'T00:00:00Z');context.history_previous=new Date(d.getTime()-86400000).toISOString().slice(0,10);context.history_next=chosen<context.yesterday?new Date(d.getTime()+86400000).toISOString().slice(0,10):null;
  context.totals={...context.totals,favorites:[...f.values()].filter(x=>x.favorite).length,read_later:[...f.values()].filter(x=>x.reading_status==='read_later').length};
 }
 if(page==='library'){
  Object.assign(context,{q:sp.get('q')||'',interest:sp.get('interest')||'',reading:sp.get('reading')||'',favorite:sp.get('favorite')||'',sort:sp.get('sort')||'score_desc',page_no:Math.max(1,Math.min(100000,parseInt(sp.get('page_no')||'1')||1))});
  let selected=[...index.values()].filter(p=>p.eligibility_status==='eligible'&&p.score>=(meta.threshold??.7));
  const q=context.q;selected=selected.filter(p=>(!q||[p.title,p.abstract,p.venue].some(v=>searchMatches(v,q)))&&(!['interested','not_interested'].includes(context.interest)||p.interest===context.interest)&&(!['unread','read_later','read'].includes(context.reading)||p.reading_status===context.reading)&&(context.favorite!=='yes'||p.favorite));
  if(!['score_desc','score_asc','date_desc','date_asc','title_asc'].includes(context.sort))context.sort='score_desc';const sort=context.sort;
  selected.sort((a,b)=>(sort==='score_asc'?(a.score-b.score||binary(b.published,a.published)):sort==='date_asc'?(binary(a.published,b.published)||b.score-a.score):sort==='date_desc'?(binary(b.published,a.published)||b.score-a.score):sort==='title_asc'?binary(fold(a.title),fold(b.title)):(b.score-a.score||binary(b.published,a.published)))||binary(a.identity,b.identity));
  context.total=selected.length;context.papers=selected.slice((context.page_no-1)*24,context.page_no*24);context.has_next=context.page_no*24<selected.length;
  const query=n=>{const params=new URLSearchParams(sp);params.set('page_no',String(n));return params.toString();};context.previous_query=query(context.page_no-1);context.next_query=query(context.page_no+1);
 }
 if(page==='feedback'){
  context.interest=sp.get('interest')||'';context.favorite=sp.get('favorite')||'';context.sort=sp.get('sort')||'updated';
  const values=[...f.values()].filter(p=>p.interest||p.favorite||p.reading_status!=='unread'||p.reason);
  context.stats={...context.stats,total:values.length,interested:values.filter(p=>p.interest==='interested').length,not_interested:values.filter(p=>p.interest==='not_interested').length,favorites:values.filter(p=>p.favorite).length};
  context.items=values.filter(p=>index.has(p.identity)).map(p=>({...index.get(p.identity),...p})).filter(p=>(!['interested','not_interested'].includes(context.interest)||p.interest===context.interest)&&(context.favorite!=='yes'||p.favorite)).sort((a,b)=>context.sort==='score'?(b.score??-1)-(a.score??-1):Date.parse(b.updated_at)-Date.parse(a.updated_at));
 }
 return context;
}
export default {async fetch(r,e){const u=new URL(r.url);try{
 if(u.pathname==='/mcp')return await handleMcp(r,e,{
  read_run_status:async()=>{const state=await syncState(e.DB);return {...state,run:state.active_generation?await record(e.DB,state.active_generation,'meta','ui:status'):null};},
  read_feedback:async({after})=>({...await syncState(e.DB),...await eventPage(e.DB,'cloud_feedback',after,500)}),
  read_profile_events:async({after})=>profileEvents(e.DB,after),
  read_profile:async()=>{const generation=await active(e.DB);if(!generation)fail('No active profile snapshot',503);return profileState(e.DB,generation);},
  stage_snapshot:async({action,data})=>syncOperation(e,'/api/sync/'+action,action==='finish'?{...data,activate:false}:data,{deduplicate:true}),
  acknowledge_snapshot:async({data})=>syncOperation(e,'/api/sync/ack',data),
  read_checkpoint_head:async()=>readLibraryHead(e.DB),
  commit_library_checkpoint:async({data})=>commitLibraryHead(e.DB,data),
  read_main_store:async({action,data})=>mainStoreOperation(e.DB,action,data),
  write_main_store:async({action,data})=>mainStoreOperation(e.DB,action,data),
  publish_main_snapshot:async({data})=>publishMainSnapshot(e.DB,data),
  read_site_backup:async({action,data})=>siteBackup(e.DB,action,data),
 });
 const asset=staticAsset(u.pathname);if(asset)return asset;
 if(u.pathname==='/healthz')return json({ok:!!e.DB,ready:!!(e.DB&&await active(e.DB))});
 if(!e.DB&&u.pathname.startsWith('/api/'))fail('云端数据库暂时不可用',503);
 if(u.pathname.startsWith('/api/state/')){await requireSyncAuth(r,e);return await handleCloudState(r,e,u.pathname);}
 if(e.SITE_ACCESS!=='public-legacy'&&(!u.pathname.startsWith('/api/sync/')||u.pathname==='/api/sync/status')&&!isOwner(r,e))fail('请使用站点所有者的 ChatGPT 账号登录',403);
 if(u.pathname==='/api/profile'&&r.method==='POST'){if(!isOwner(r,e))fail('只有已登录的站点所有者可以修改研究兴趣和筛选偏好',403);return json(await saveProfile(r,e,u,await body(r)));}
 if(u.pathname==='/profile/save'&&r.method==='POST'){
  if(!isOwner(r,e))fail('只有已登录的站点所有者可以修改研究兴趣和筛选偏好',403);
  if(r.headers.get('Origin')!==u.origin)fail('请求来源无效',403);
  if((r.headers.get('content-type')||'').split(';')[0].trim()!=='application/x-www-form-urlencoded')fail('需要表单请求',415);
  const raw=Object.fromEntries(new URLSearchParams(await boundedText(r))),data={base_version:Number(raw.base_version),generation:raw.generation,content:raw.content,summary:raw.summary,screening_preferences:{relevance_threshold:Number(raw.relevance_threshold),instructions:raw.instructions||''}};
  try{await saveProfile(r,e,u,data);return new Response(null,{status:303,headers:{...h,Location:'/profile?saved=1'}});}
  catch(error){if(!error.status)throw error;const context=await pageContext(r,e,u,'profile');context.profile_error=error.message;context.profile_submission=data;return new Response(render('profile',context),{status:error.status,headers:{...h,'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}});}
 }
 if(u.pathname==='/api/sync'&&r.method==='POST')return await requestSync(r,e,u);
 if(u.pathname==='/api/sync/status'&&r.method==='GET'){const state=await syncState(e.DB);return json({...state,message:state.pending?'已保存更新，等待云端任务处理。':'云端数据已更新；暂无待处理更新。'});}
 if(u.pathname.startsWith('/api/sync/'))return await sync(r,e,u);
 if(u.pathname==='/api/view'&&r.method==='GET')return await view(r,e,u);
 if(['/api/feedback','/api/favorite','/feedback','/feedback/clear'].includes(u.pathname)&&r.method==='POST')return await saveFeedback(r,e,u);
 if(u.pathname==='/fulltext/file'){
  if(!isOwner(r,e))fail('只有已登录的站点所有者可以查看全文 PDF',403);
  if(!['GET','HEAD'].includes(r.method))fail('不支持此方法',405);
  const generation=await active(e.DB);if(!generation)fail('数据库正在同步，请稍后刷新',503);
  let id=u.searchParams.get('id');const identity=u.searchParams.get('identity');
  if(identity){if(identity.length>1000)fail('论文编号无效');const paper=await record(e.DB,generation,'paper',identity);if(!paper?.fulltext_id)fail('全文 PDF 不存在',404);id=String(paper.fulltext_id);}
  if(!/^[1-9][0-9]*$/.test(id||'')||!Number.isSafeInteger(Number(id)))fail('全文编号无效');
  const file=await record(e.DB,generation,'private','fulltext:'+id);
  if(!file||(identity&&file.identity!==identity))fail('全文 PDF 不存在',404);
  if(typeof file.path!=='string'||!file.path.startsWith('payload/fulltexts/'))fail('全文文件索引无效',409);
  const response=await streamCheckpointFile(e,file.path.slice('payload/'.length),file.sha256,file.size_bytes,generation);
  const headers=new Headers(response.headers);let filename=String(file.filename||'paper.pdf').split(/[\\/]/).at(-1).replace(/[\u0000-\u001f\u007f]/g,'').slice(0,255)||'paper.pdf';
  const encoded=encodeURIComponent(filename).replace(/[!'()*]/g,char=>'%'+char.charCodeAt(0).toString(16).toUpperCase());
  headers.set('Content-Disposition',`inline; filename="paper.pdf"; filename*=UTF-8''${encoded}`);
  headers.set('X-Frame-Options','DENY');headers.set('Referrer-Policy','no-referrer');
  if(r.method==='HEAD'){await response.body?.cancel();return new Response(null,{headers});}
  return new Response(response.body,{headers});
 }
 if(r.method==='POST')fail('这项操作需要在本地站点执行；保存的反馈和阅读标记可在两端直接修改。',409);
 if(!['/','/library','/sources','/profile','/feedback','/status'].includes(u.pathname))fail('页面不存在',404);
 const name=u.pathname==='/'?'today':u.pathname.slice(1);
 return new Response(render(name,await pageContext(r,e,u,name)),{headers:{...h,'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}});
 }catch(error){if(!error.status)console.error('radar storage error',error.name);return json({error:error.status?error.message:'数据暂时无法读取或保存，请稍后重试'},error.status||503);}}};

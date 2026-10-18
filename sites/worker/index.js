import {render,staticAsset} from './render.js';
const h={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','X-Frame-Options':'DENY'};
const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{...h,'Content-Type':'application/json; charset=utf-8'}});
const fail=(message,status=400)=>{throw Object.assign(new Error(message),{status});};
const hash=async text=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))).map(n=>n.toString(16).padStart(2,'0')).join('');
const owner=()=>true;
async function pointer(db,key){return (await db.prepare('SELECT value FROM pointers WHERE key=?').bind(key).first())?.value;}
async function syncState(db){const request_id=Number(await pointer(db,'sync_request')||0),acknowledged_request_id=Number(await pointer(db,'sync_request_ack')||0),cursor=Number((await db.prepare('SELECT MAX(seq) seq FROM cloud_feedback').first())?.seq||0),acknowledged_cursor=Number(await pointer(db,'sync_cursor_ack')||0);return {request_id,acknowledged_request_id,cursor,acknowledged_cursor,pending:request_id>acknowledged_request_id||cursor>acknowledged_cursor,synced_at:await pointer(db,'synced_at'),active_generation:await active(db)};}
async function requestSync(r,e,u){if(r.headers.get('Origin')!==u.origin)fail('请求来源无效',403);await e.DB.prepare("INSERT INTO pointers(key,value) VALUES('sync_request','1') ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)").run();return json({...(await syncState(e.DB)),message:'同步请求已保存；本地在线时会在下一次检查中完成（最多约五分钟）。'});}
async function body(r){if(!(r.headers.get('content-type')||'').includes('application/json'))fail('需要 JSON 请求',415);const t=await r.text();if(t.length>2000000)fail('请求过大',413);try{return JSON.parse(t);}catch{fail('JSON 格式无效');}}
const gid=v=>{if(!/^[a-f0-9]{64}$/.test(v||''))fail('快照编号无效');return v;};
async function active(db){return(await db.prepare("SELECT value FROM pointers WHERE key='active'").first())?.value;}
async function record(db,g,k,key){const r=await db.prepare('SELECT data FROM records WHERE generation=? AND kind=? AND key=?').bind(g,k,key).first();return r?JSON.parse(r.data):null;}
async function all(db,g,k){return(await db.prepare('SELECT data FROM records WHERE generation=? AND kind=? ORDER BY key').bind(g,k).all()).results.map(r=>JSON.parse(r.data));}
async function feedback(db,g){const m=new Map((await all(db,g,'feedback')).map(r=>[r.identity,r]));for(const r of(await db.prepare('SELECT * FROM cloud_feedback ORDER BY seq').all()).results){const p=m.get(r.identity);if(!p||Date.parse(r.updated_at)>Date.parse(p.updated_at))m.set(r.identity,r);}return m;}
async function sync(r,e,u){
 if(!e.SYNC_TOKEN||await hash(r.headers.get('Authorization')?.replace(/^Bearer /,'')||'')!==await hash(e.SYNC_TOKEN))fail('同步凭据无效',401);
 const db=e.DB;
 if(u.pathname==='/api/sync/feedback'&&r.method==='GET'){const n=Number(u.searchParams.get('after')||0);if(!Number.isSafeInteger(n)||n<0)fail('游标无效');const a=(await db.prepare('SELECT * FROM cloud_feedback WHERE seq>? ORDER BY seq LIMIT 500').bind(n).all()).results;return json({...await syncState(db),events:a,cursor:a.length?a.at(-1).seq:n,more:a.length===500});}
 if(r.method!=='POST')fail('不支持此方法',405);const d=await body(r),g=gid(d.generation);
 if(u.pathname==='/api/sync/ack'){
  const state=await syncState(db);if(state.active_generation!==g)fail('快照已更新，请重新检查',409);
  if(!Number.isSafeInteger(d.request_id)||d.request_id<0||d.request_id>state.request_id||!Number.isSafeInteger(d.cursor)||d.cursor<0||d.cursor>state.cursor)fail('确认游标无效');
  await db.batch([db.prepare("INSERT INTO pointers(key,value) VALUES('sync_request_ack',?) ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)").bind(String(d.request_id)),db.prepare("INSERT INTO pointers(key,value) VALUES('sync_cursor_ack',?) ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)").bind(String(d.cursor))]);
  const done=await syncState(db);return json({ok:true,acknowledged_request_id:done.acknowledged_request_id,acknowledged_cursor:done.acknowledged_cursor});
 }
 if(u.pathname==='/api/sync/begin'){
  if(!Number.isSafeInteger(d.count)||d.count<1||d.count>100000)fail('记录数量无效');
  await db.prepare('INSERT INTO generations(id,count,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING').bind(g,d.count,new Date().toISOString()).run();
  const existing=await db.prepare('SELECT count,status FROM generations WHERE id=?').bind(g).first();if(existing.count!==d.count)fail('快照不一致',409);
  if(await active(db)===g)return json({generation:g,active:true});
  if(d.manifest){
   if(!Array.isArray(d.manifest)||d.manifest.length!==d.count)fail('快照清单无效');
   const keys=new Set();for(const m of d.manifest){if(!['paper','history','feedback','private','meta'].includes(m.kind)||typeof m.key!=='string'||m.key.length>1000||!/^[a-f0-9]{64}$/.test(m.checksum||''))fail('快照清单无效');const key=m.kind+':'+m.key;if(keys.has(key))fail('重复记录');keys.add(key);}
   const base=await active(db);
   if(base&&existing.status==='staging')await db.prepare(`INSERT OR IGNORE INTO records(generation,kind,key,data,checksum)
     SELECT ?,r.kind,r.key,r.data,r.checksum FROM json_each(?) m JOIN records r
     ON r.generation=? AND r.kind=json_extract(m.value,'$.kind') AND r.key=json_extract(m.value,'$.key')
     AND r.checksum=json_extract(m.value,'$.checksum')`).bind(g,JSON.stringify(d.manifest),base).run();
   const ready=new Set((await db.prepare('SELECT kind,key FROM records WHERE generation=?').bind(g).all()).results.map(r=>r.kind+':'+r.key));
   return json({generation:g,active:false,missing:d.manifest.filter(m=>!ready.has(m.kind+':'+m.key)).map(m=>({kind:m.kind,key:m.key}))});
  }
  return json({generation:g,active:false});
 }
 const generation=await db.prepare('SELECT * FROM generations WHERE id=?').bind(g).first();if(!generation)fail('请先建立快照');
 if(u.pathname==='/api/sync/chunk'){
  if(!Array.isArray(d.records)||!d.records.length||d.records.length>40)fail('批次无效');const statements=[];
  for(const row of d.records){if(!['paper','history','feedback','private','meta'].includes(row.kind)||typeof row.key!=='string'||row.key.length>1000||typeof row.data!=='string'||row.data.length>1000000)fail('快照记录无效');if(await hash(row.data)!==row.checksum)fail('记录校验失败');const p=JSON.parse(row.data);if(row.kind==='paper'&&p.identity!==row.key)fail('身份不一致');statements.push(db.prepare('INSERT INTO records(generation,kind,key,data,checksum) VALUES(?,?,?,?,?) ON CONFLICT(generation,kind,key) DO NOTHING').bind(g,row.kind,row.key,row.data,row.checksum));}
  if(generation.status==='staging')await db.batch(statements);return json({accepted:d.records.length});
 }
 if(u.pathname==='/api/sync/finish'){
  const rows=(await db.prepare('SELECT kind,key,checksum FROM records WHERE generation=? ORDER BY kind,key').bind(g).all()).results;
  if(rows.length!==generation.count||await hash(rows.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'))!==g)fail('快照尚不完整，原数据仍可访问',409);
  await db.batch([db.prepare("UPDATE generations SET status='complete' WHERE id=?").bind(g),db.prepare("INSERT INTO pointers(key,value) VALUES('active',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(g),db.prepare("INSERT INTO pointers(key,value) VALUES('synced_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(new Date().toISOString())]);
  const keep=(await db.prepare("SELECT id FROM generations WHERE status='complete' ORDER BY created_at DESC LIMIT 2").all()).results.map(r=>r.id);
  for(const s of(await db.prepare("SELECT id FROM generations WHERE id<>? AND (status='complete' OR created_at<?)").bind(g,new Date(Date.now()-7*86400000).toISOString()).all()).results){if(keep.includes(s.id))continue;await db.batch([db.prepare('DELETE FROM records WHERE generation=?').bind(s.id),db.prepare('DELETE FROM generations WHERE id=?').bind(s.id)]);}
  return json({generation:g,count:rows.length});
 }
 fail('同步操作不存在',404);
}
async function view(r,e,u){
 const db=e.DB,g=await active(db),own=owner(r,e);if(!g)return json({ready:false,owner:own,message:'数据库正在首次同步，请稍后刷新。'},503);
 const meta=await record(db,g,'meta','overview'),result={ready:true,owner:own,overview:meta,generation:g,synced_at:(await db.prepare("SELECT value FROM pointers WHERE key='synced_at'").first())?.value};
 const v=u.searchParams.get('view')||'today';
 if(v==='profile'){result.profile=await record(db,g,'private','profile');return json(result);}
 if(v==='sources'||v==='status'){if(!own)result.overview.sources=meta.sources.map(s=>({...s,health:{status:s.health?.status,last_success_at:s.health?.last_success_at}}));return json(result);}
 let papers=await all(db,g,'paper');
 if(v==='today'){const today=new Date().toLocaleDateString('en-CA',{timeZone:meta.timezone||'Asia/Shanghai'}),day=u.searchParams.get('day')||today;let history=await record(db,g,'history',day===today?'current':day);if(day===today&&history?.day!==today)history=null;const index=new Map(papers.map(p=>[p.identity,p]));result.day=day;result.batches=history?.batches||0;result.incomplete=history?.incomplete||false;papers=(history?.papers||[]).filter(p=>index.has(p.identity)).map(p=>({...index.get(p.identity),...p}));}
 else if(v==='library'){const personalView=u.searchParams.get('favorite')==='yes'||['read_later','read'].includes(u.searchParams.get('reading'));papers=papers.filter(p=>personalView||(p.eligibility_status==='eligible'&&p.score>=.7));}else if(v!=='feedback')fail('页面不存在',404);
 const f=own?await feedback(db,g):new Map();
 const publicFields=['identity','doi','title','abstract','venue','published','published_precision','url','authors_json','abstract_source','abstract_source_url','abstract_retrieved_at','eligibility_status','publication_type','score','reasons','confidence','rubric_version','needs_rescreen'];
 result.papers=papers.map(p=>own?{...p,...(f.has(p.identity)?{feedback:f.get(p.identity)}:{})}:Object.fromEntries(publicFields.filter(k=>k in p).map(k=>[k,p[k]])));
 if(v==='library'){
  if(u.searchParams.get('favorite')==='yes')result.papers=result.papers.filter(p=>p.feedback?.favorite);
  const reading=u.searchParams.get('reading');if(['unread','read_later','read'].includes(reading))result.papers=result.papers.filter(p=>(p.feedback?.reading_status||'unread')===reading);
 }
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
 const reason=String(patch.reason||'').trim();if(reason.length>3000)fail('理由最多3000字');const stamp=new Date(Math.max(Date.now(),Date.parse(previous.updated_at||'')+1||0)).toISOString();
 // One SQL statement assigns a strictly newer logical UTC time, even when two
 // requests read the same previous row during the same millisecond.
 await e.DB.prepare("INSERT INTO cloud_feedback(identity,interest,reason,favorite,reading_status,updated_at) SELECT ?,?,?,?,?,CASE WHEN MAX(julianday(updated_at))>=julianday(?) THEN strftime('%Y-%m-%dT%H:%M:%fZ',MAX(julianday(updated_at))+1.0/86400000) ELSE ? END FROM cloud_feedback WHERE identity=?").bind(d.identity,patch.interest,reason||null,patch.favorite,patch.reading_status,stamp,stamp,d.identity).run();
 if(form){const target=String(d.return_to||'/feedback');return new Response(null,{status:303,headers:{...h,Location:target.startsWith('/')&&!target.startsWith('//')?target:'/feedback'}});}
 const labels={interested:'已完成 · 感兴趣',not_interested:'已完成 · 不感兴趣'};
 return json({ok:true,saved:true,updated_at:stamp,interest:patch.interest,favorite:Boolean(patch.favorite),reading_status:patch.reading_status,status_label:labels[patch.interest]||'',message:'反馈已保存到云端；本地在线时自动同步。'});
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
 const context={...base,site_mode:'cloud',state_path:'与本地数据库同步',cloud_url:u.origin,cloud_sync_status:{status:'succeeded',synced_at:state.synced_at},csrf_token:'same-origin-public',request:{url:{path:u.pathname,query:u.search.slice(1)}}};
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
  const personalView=context.favorite==='yes'||['read_later','read'].includes(context.reading);
  let selected=[...index.values()].filter(p=>personalView||(p.eligibility_status==='eligible'&&p.score>=.7));
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
 const asset=staticAsset(u.pathname);if(asset)return asset;
 if(u.pathname==='/healthz')return json({ok:!!e.DB,ready:!!(e.DB&&await active(e.DB))});
 if(!e.DB&&u.pathname.startsWith('/api/'))fail('云端数据库暂时不可用',503);
 if(u.pathname==='/status'&&r.method==='GET')return new Response(null,{status:303,headers:{...h,Location:'/?center=checks'}});
 if(u.pathname==='/api/center/status'&&r.method==='GET'){
  const context=await pageContext(r,e,u,'status'),state=await syncState(e.DB);
  const generation=await active(e.DB),overview=await record(e.DB,generation,'meta','overview');
  return json({html:render('_center_checks',context),local_checked_at:context.local_checked_at||null,last_import:overview?.last_import||null,sync:{...state,status:state.pending?'pending':'succeeded',message:state.pending?'已保存更新，等待本地接收。':'最近同步已完成。'}});
 }
 if(u.pathname==='/api/sync'&&r.method==='POST')return await requestSync(r,e,u);
 if(u.pathname==='/api/sync/status'&&r.method==='GET'){const state=await syncState(e.DB);return json({...state,message:state.pending?'已保存更新，等待本地在线后同步。':'两端同步已完成；暂无待同步更新。'});}
 if(u.pathname.startsWith('/api/sync/'))return await sync(r,e,u);
 if(u.pathname==='/api/view'&&r.method==='GET')return await view(r,e,u);
 if(['/api/feedback','/api/favorite','/feedback','/feedback/clear'].includes(u.pathname)&&r.method==='POST')return await saveFeedback(r,e,u);
 if(u.pathname==='/fulltext/file')return new Response(render('error',{page:'error',title:'全文保存在本地',message:'PDF 文件保存在本机，请在本地文献库打开这篇论文查看全文。',action:'云端可阅读原始摘要、推荐理由并保存反馈。',csrf_token:'same-origin-public',request:{url:{path:u.pathname,query:u.search.slice(1)}},site_mode:'cloud'}),{status:409,headers:{...h,'Content-Type':'text/html; charset=utf-8'}});
 if(r.method==='POST')fail('这项操作需要在本地站点执行；保存的反馈和阅读标记可在两端直接修改。',409);
 if(!['/','/library','/sources','/profile','/feedback','/status'].includes(u.pathname))fail('页面不存在',404);
 const name=u.pathname==='/'?'today':u.pathname.slice(1);
 return new Response(render(name,await pageContext(r,e,u,name)),{headers:{...h,'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}});
 }catch(error){
  if(!error.status)console.error('radar storage error',error.name);
  const status=error.status||503,message=error.status?error.message:'数据暂时无法读取或保存，请稍后重试';
  const url=new URL(r.url);
  if(r.method==='GET'&&(r.headers.get('Accept')||'').includes('text/html')&&!url.pathname.startsWith('/api/')&&!url.pathname.startsWith('/static/')&&url.pathname!=='/fulltext/file'&&status!==403){
    return new Response(render('error',{page:'error',site_mode:'cloud',cloud_sync_status:{},title:status===404?'页面未找到':'这次操作没有完成',message,action:'检查输入后重试，或从导航继续。'}),{status,headers:{...h,'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}});
  }
  return json({error:message},status);
 }}};

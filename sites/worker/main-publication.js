import {displayRecord} from './display-records.js';
// Publication is separate from main-store staging. A committed main revision
// alone never changes the live Site or fences its existing publisher.
const fail=(message,status=400)=>{throw Object.assign(new Error(message),{status});};
const h=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
const int=v=>Number.isSafeInteger(v)&&v>=0;
const hash=async s=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s)))).map(n=>n.toString(16).padStart(2,'0')).join('');
const pointer=async(db,key)=>(await db.prepare('SELECT value FROM pointers WHERE key=?').bind(key).first())?.value;
export async function publishMainSnapshot(db,data){
 const keys=['operation_id','revision','snapshot_generation','expected_generation','expected_published_revision','expected_writer_mode','activate'];
 if(!data||Object.keys(data).length!==keys.length||keys.some(k=>!Object.hasOwn(data,k))||
  !/^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/.test(data.operation_id||'')||!int(data.revision)||data.revision<1||
  !h(data.snapshot_generation)||!(data.expected_generation===null||h(data.expected_generation))||!int(data.expected_published_revision)||
  !['local','d1-main'].includes(data.expected_writer_mode)||typeof data.activate!=='boolean')fail('Invalid main publication');
 const raw=JSON.stringify(data),receiptKey='main_publication:'+data.operation_id;
 const prior=await pointer(db,receiptKey);
 if(prior){const saved=JSON.parse(prior);if(saved.request!==raw)fail('Publication operation reused with different input',409);return {ok:true,idempotent:true,publication:saved.publication,active:await pointer(db,'active')===saved.publication.snapshot_generation};}
 const revision=await db.prepare("SELECT import_id,watermarks FROM ms_revisions WHERE namespace='owner' AND revision=?").bind(data.revision).first();
 if(!revision)fail('Committed main revision unavailable',409);
 const wm=JSON.parse(revision.watermarks),g=data.snapshot_generation;
 const generation=await db.prepare('SELECT count,status FROM generations WHERE id=?').bind(g).first();
 if(!generation||generation.status!=='complete')fail('Display generation must be complete',409);
 const rows=(await db.prepare('SELECT kind,key,checksum FROM records WHERE generation=? ORDER BY kind,key').bind(g).all()).results;
 if(rows.length!==generation.count||await hash(rows.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'))!==g)fail('Display generation integrity mismatch',409);
 const profile=await displayRecord(db,g,'private','profile');
 if(!profile||!int(JSON.parse(profile.data).profile_cursor??0)||(JSON.parse(profile.data).profile_cursor??0)!==wm.profile_cursor)fail('Display lacks committed profile edits',409);
 const publication={revision:data.revision,import_id:revision.import_id,snapshot_generation:g,watermarks:wm,operation_id:data.operation_id};
 // The same predicate is evaluated inside the transaction that elects the
 // publisher. A stale local snapshot, revision, mode or ACK cannot win a race.
 const fence=`EXISTS(SELECT 1 FROM ms_heads WHERE namespace='owner' AND revision=? AND import_id=?)
  AND COALESCE((SELECT value FROM pointers WHERE key='active'),'')=?
  AND COALESCE((SELECT json_extract(value,'$.revision') FROM pointers WHERE key='main_store_head'),0)=?
  AND COALESCE((SELECT value FROM pointers WHERE key='writer_mode'),'local')=?
  AND NOT EXISTS(SELECT 1 FROM pointers WHERE key='checkpoint_head')
  AND ?<=(SELECT COALESCE(MAX(seq),0) FROM cloud_feedback)
  AND ?<=(SELECT COALESCE(MAX(seq),0) FROM cloud_profile_events)
  AND ?<=COALESCE((SELECT CAST(value AS INTEGER) FROM pointers WHERE key='sync_request'),0)
  AND ?>=COALESCE((SELECT CAST(value AS INTEGER) FROM pointers WHERE key='sync_cursor_ack'),0)
  AND ?>=COALESCE((SELECT CAST(value AS INTEGER) FROM pointers WHERE key='sync_profile_cursor_ack'),0)
  AND ?>=COALESCE((SELECT CAST(value AS INTEGER) FROM pointers WHERE key='sync_request_ack'),0)`;
 const args=[data.revision,revision.import_id,data.expected_generation??'',data.expected_published_revision,data.expected_writer_mode,wm.feedback_cursor,wm.profile_cursor,wm.request_id,wm.feedback_cursor,wm.profile_cursor,wm.request_id];
 if(data.revision<data.expected_published_revision)fail('Publication cannot move backwards',409);
 if(!data.activate){if(!(await db.prepare(`SELECT 1 ok WHERE ${fence}`).bind(...args).first()))fail('Publication fence differs',409);return {ok:true,preview:true,publication};}
 // A per-attempt nonce prevents a delayed duplicate request from using an old
 // receipt to re-activate its obsolete generation after a newer publication.
 const receipt=JSON.stringify({request:raw,publication,attempt:crypto.randomUUID()}),winner='EXISTS(SELECT 1 FROM pointers WHERE key=? AND value=?)';
 const updates=[['main_store_head',JSON.stringify(publication)],['writer_mode','d1-main'],['active',g],['synced_at',new Date().toISOString()],['sync_cursor_ack',String(wm.feedback_cursor)],['sync_profile_cursor_ack',String(wm.profile_cursor)],['sync_request_ack',String(wm.request_id)]];
 await db.batch([
  db.prepare(`INSERT INTO pointers(key,value) SELECT ?,? WHERE ${fence} ON CONFLICT(key) DO NOTHING`).bind(receiptKey,receipt,...args),
  ...updates.map(([key,value])=>db.prepare(`INSERT INTO pointers(key,value) SELECT ?,? WHERE ${winner} ON CONFLICT(key) DO UPDATE SET value=excluded.value`).bind(key,value,receiptKey,receipt)),
 ]);
 const saved=await pointer(db,receiptKey);
 if(saved!==receipt){if(saved&&JSON.parse(saved).request===raw)return {ok:true,idempotent:true,publication:JSON.parse(saved).publication,active:await pointer(db,'active')===g};fail('Publication fence changed; no display or ACK changed',409);}
 return {ok:true,idempotent:false,publication,active:true};
}

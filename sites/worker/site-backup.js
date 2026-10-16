import {canonicalJSON} from './main-store.js';
import {displayData,displayJoin,requireDisplayData} from './display-records.js';
// Owner-only export of a pinned Site state. Complete display generations and
// append-only events remain readable after newer views/edits are published.
// This module performs no writes and never exports credentials or bindings.
const fail=(message,status=400)=>{throw Object.assign(new Error(message),{status});};
const hash=async bytes=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(n=>n.toString(16).padStart(2,'0')).join('');
const enc=new TextEncoder(),LIMIT=256*1024;
const integer=v=>Number.isSafeInteger(v)&&v>=0;
const digest=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
function base64(bytes){let s='';for(let i=0;i<bytes.length;i+=8192)s+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(s);}
export async function siteBackup(db,action,data={}){
 if(action==='begin'){
  // One statement gives a single SQLite read snapshot, not separately-read
  // pointers and event limits that could straddle a publication.
  const result=await db.prepare(`SELECT json_object(
   'pointers',json((SELECT json_group_array(json_object('key',key,'value',value)) FROM (SELECT key,value FROM pointers ORDER BY key))),
   'generations',json((SELECT json_group_array(json_object('id',id,'count',count,'created_at',created_at,'status',status)) FROM (SELECT * FROM generations WHERE status='complete' ORDER BY id))),
   'excluded_uncommitted_generations',(SELECT count(*) FROM generations WHERE status!='complete'),
   'feedback_cursor',(SELECT coalesce(max(seq),0) FROM cloud_feedback),
   'profile_cursor',(SELECT coalesce(max(seq),0) FROM cloud_profile_events),
   'main_head',json((SELECT json_object('revision',revision,'import_id',import_id) FROM ms_heads WHERE namespace='owner'))
  ) AS snapshot`).first();
  const snapshot={format:'academic-radar-site-backup',version:1,...JSON.parse(result.snapshot)};
  const active=snapshot.pointers.find(p=>p.key==='active')?.value;
  if(active&&!snapshot.generations.some(g=>g.id===active))fail('Active view is not a retained complete generation',409);
  if(enc.encode(JSON.stringify(snapshot)).length>600000)fail('Backup manifest requires paginated export support',413);
  return {snapshot,snapshot_sha256:await hash(enc.encode(canonicalJSON(snapshot))),hash_algorithm:'sha256-canonical-json-v1',captured_at:new Date().toISOString()};
 }
 if(action==='records'){
  if(!digest(data.generation))fail('Invalid generation');
  const generation=await db.prepare("SELECT count,status FROM generations WHERE id=?").bind(data.generation).first();
  if(!generation||generation.status!=='complete')fail('Pinned generation unavailable',409);
  const after=data.after||{kind:'',key:'',offset:0};
  if(typeof after.kind!=='string'||typeof after.key!=='string'||!integer(after.offset)||after.key.length>1000||after.kind.length>20)fail('Invalid backup cursor');
  const rows=(await db.prepare(`WITH page AS (
    SELECT r.kind,r.key,r.checksum,${displayData} data FROM ${displayJoin} WHERE r.generation=? AND
     (r.kind>? OR (r.kind=? AND (r.key>? OR (? > 0 AND r.key=?)))) ORDER BY r.kind,r.key LIMIT 500
   ), sized AS (SELECT *,sum(length(CAST(data AS BLOB))) OVER (ORDER BY kind,key) AS cumulative,
    row_number() OVER (ORDER BY kind,key) AS ordinal FROM page)
   SELECT kind,key,checksum,data FROM sized WHERE cumulative<=? OR ordinal=1 ORDER BY kind,key`)
   .bind(data.generation,after.kind,after.kind,after.key,after.offset,after.key,LIMIT).all()).results;
  if(after.offset&&(!rows.length||rows[0].kind!==after.kind||rows[0].key!==after.key))fail('Pinned partial record is unavailable',409);
  const chunks=[];let used=0,encoded=0,next=null;
  for(const row of rows){
   requireDisplayData(row);
   const bytes=enc.encode(row.data),offset=after.offset&&row.kind===after.kind&&row.key===after.key?after.offset:0;
   if(offset>=bytes.length&&offset!==0)fail('Invalid byte offset');
   const piece=bytes.subarray(offset,offset+LIMIT-used);
   const chunk={kind:row.kind,key:row.key,checksum:row.checksum,bytes:bytes.length,offset,base64:base64(piece)},size=enc.encode(JSON.stringify(chunk)).length;
   if(encoded+size>600000){if(!chunks.length)fail('Record metadata exceeds export page limit',413);break;}
   chunks.push(chunk);encoded+=size;
   used+=piece.length;next={kind:row.kind,key:row.key,offset:offset+piece.length<bytes.length?offset+piece.length:0};
   if(next.offset||used>=LIMIT)break;
  }
  if(!chunks.length&&after.offset)fail('Pinned partial record is unavailable',409);
  // A final empty page is the explicit terminal marker. Consumers verify the
  // pinned count and generation hash after reassembling each checksum.
  return {generation:data.generation,expected_count:generation.count,chunks,next:chunks.length?next:null,done:chunks.length===0};
 }
 if(action==='feedback'||action==='profiles'){
  if(!integer(data.after)||!integer(data.until)||data.after>data.until)fail('Invalid event bounds');
  const table=action==='feedback'?'cloud_feedback':'cloud_profile_events';
  const rows=(await db.prepare(`SELECT * FROM ${table} WHERE seq>? AND seq<=? ORDER BY seq LIMIT 10`).bind(data.after,data.until).all()).results;
  const events=[];let used=0;
  for(const row of rows){const bytes=enc.encode(canonicalJSON(row)),size=bytes.length;if(used+size>LIMIT){if(!events.length)return {until:data.until,events:[],next:data.after,done:false,oversized:{seq:row.seq,bytes:size,sha256:await hash(bytes)}};break;}events.push(row);used+=size;}
  const next=events.at(-1)?.seq??data.after;
  return {until:data.until,events,next,done:rows.length===0||next===data.until};
 }
 if(action==='event_chunks'){
  if(!['feedback','profiles'].includes(data.stream)||!integer(data.seq)||!data.seq||!integer(data.offset)||!integer(data.until)||data.seq>data.until)fail('Invalid event chunk bounds');
  const table=data.stream==='feedback'?'cloud_feedback':'cloud_profile_events';
  const row=await db.prepare(`SELECT * FROM ${table} WHERE seq=?`).bind(data.seq).first();
  if(!row)fail('Pinned event unavailable',409);
  const bytes=enc.encode(canonicalJSON(row));if(data.offset>=bytes.length)fail('Invalid event offset');
  const piece=bytes.subarray(data.offset,data.offset+LIMIT),next=data.offset+piece.length;
  return {stream:data.stream,seq:data.seq,until:data.until,bytes:bytes.length,sha256:await hash(bytes),offset:data.offset,base64:base64(piece),next,done:next===bytes.length};
 }
 fail('Unknown Site backup operation',404);
}

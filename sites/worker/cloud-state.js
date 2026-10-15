// Private, content-addressed checkpoint transport. No checkpoint is public and
// the Worker never reconstructs a database in memory. D1 owns the commit fence.
export const MAX_BLOB_BYTES = 8 * 1024 * 1024;
export const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_CHUNKS = 4096;
const MAX_FILES = 4096;
const HEAD_KEY = 'checkpoint_head';
const PREFIX = 'private/checkpoints/v1/';
const headers = {'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'};
const json = (data, status=200) => new Response(JSON.stringify(data), {status,headers:{...headers,'Content-Type':'application/json; charset=utf-8'}});
const fail = (message,status=400) => {throw Object.assign(new Error(message),{status});};
const validHash = value => typeof value==='string' && /^[a-f0-9]{64}$/.test(value);
const sha256 = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(n=>n.toString(16).padStart(2,'0')).join('');
const encode = value => new TextEncoder().encode(value);
const key = (kind,id) => `${PREFIX}${kind}/${id}`;

async function readLimited(request, limit) {
 const length=request.headers.get('Content-Length');
 if(length!==null && (!/^\d+$/.test(length)||Number(length)>limit))fail('Checkpoint request too large',413);
 if(!request.body)return new Uint8Array();
 const reader=request.body.getReader(),parts=[];let total=0;
 try {while(true){const {done,value}=await reader.read();if(done)break;total+=value.byteLength;if(total>limit){await reader.cancel();fail('Checkpoint request too large',413);}parts.push(value);}}
 finally {reader.releaseLock();}
 const bytes=new Uint8Array(total);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.byteLength;}return bytes;
}
function parseJSON(bytes) {try{return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));}catch{fail('Invalid checkpoint JSON');}}
function safePath(path) {
 return typeof path==='string' && path.length>0 && path.length<=1000 && !path.includes('\\') && !path.includes(':') && !/[\u0000-\u001f\u007f]/.test(path) && path.split('/').every(p=>p && p!=='.' && p!=='..');
}
export function validateTransportManifest(value) {
 if(!value || value.format!=='academic-radar-chunks-v1' || !validHash(value.checkpoint_id) || !Array.isArray(value.files) || !value.files.length || value.files.length>MAX_FILES)fail('Invalid checkpoint manifest');
 const names=new Set(),blobs=new Map();let total=0,count=0;
 for(const file of value.files){
  if(!file || !safePath(file.path)||names.has(file.path)||!validHash(file.sha256)||!Number.isSafeInteger(file.size)||file.size<0||!Array.isArray(file.chunks))fail('Invalid checkpoint file');
  names.add(file.path);let size=0;
  for(const chunk of file.chunks){
   if(!chunk || !validHash(chunk.sha256)||!Number.isSafeInteger(chunk.size)||chunk.size<=0||chunk.size>MAX_BLOB_BYTES || ++count>MAX_CHUNKS)fail('Invalid checkpoint chunk');
   if(blobs.has(chunk.sha256)&&blobs.get(chunk.sha256)!==chunk.size)fail('Inconsistent checkpoint chunk');
   blobs.set(chunk.sha256,chunk.size);size+=chunk.size;
  }
  if(size!==file.size || (file.size===0 && file.sha256!=='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'))fail('Checkpoint file size mismatch');
  total+=size;if(!Number.isSafeInteger(total))fail('Checkpoint size too large');
 }
 if(!names.has('manifest.json'))fail('Checkpoint descriptor missing');
 // Prevent file/directory collisions during restore, including out-of-order lists.
 for(const name of names){const parts=name.split('/');parts.pop();while(parts.length){if(names.has(parts.join('/')))fail('Checkpoint path collision');parts.pop();}}
 return blobs;
}
function storedHash(object) {
 const checksum=object?.checksums?.sha256;
 if(!checksum || checksum.byteLength!==32)return null;
 return Array.from(new Uint8Array(checksum)).map(n=>n.toString(16).padStart(2,'0')).join('');
}
function verifyStoredObject(object,id,size) {
 if(!object || (size!==undefined && object.size!==size) || storedHash(object)!==id)fail('Checkpoint object missing or checksum metadata invalid',409);
}
async function verifyBlobs(bucket,manifest) {
 const blobs=validateTransportManifest(manifest);
 for(const [id,size] of blobs){const object=await bucket.head(key('blob',id));verifyStoredObject(object,id,size);}
}
async function loadManifest(bucket,id,verifyAll=true) {
 const object=await bucket.get(key('manifest',id));if(!object)fail('Checkpoint manifest missing',409);
 if(object.size>MAX_MANIFEST_BYTES)fail('Checkpoint manifest too large',409);
 const bytes=new Uint8Array(await object.arrayBuffer());if(bytes.byteLength>MAX_MANIFEST_BYTES||await sha256(bytes)!==id)fail('Checkpoint manifest checksum mismatch',409);
 const manifest=parseJSON(bytes);validateTransportManifest(manifest);if(verifyAll)await verifyBlobs(bucket,manifest);return manifest;
}
async function putImmutable(bucket,kind,id,bytes) {
 const existing=await bucket.head(key(kind,id));
 if(existing){verifyStoredObject(existing,id,bytes.byteLength);return false;}
 // Immutable content address plus conditional create makes retry safe.
 await bucket.put(key(kind,id),bytes,{onlyIf:new Headers({'If-None-Match':'*'}),sha256:Uint8Array.from(id.match(/../g),pair=>parseInt(pair,16)).buffer,httpMetadata:{contentType:'application/octet-stream',cacheControl:'private, no-store'}});
 const stored=await bucket.head(key(kind,id));verifyStoredObject(stored,id,bytes.byteLength);return true;
}
async function currentHead(db) {
 const raw=(await db.prepare('SELECT value FROM pointers WHERE key=?').bind(HEAD_KEY).first())?.value;
 if(raw===undefined)return {raw:null,head:{generation:0,manifest_sha256:null,snapshot_generation:null}};
 let head;try{head=JSON.parse(raw);}catch{fail('Checkpoint head is invalid',503);}
 if(!Number.isSafeInteger(head?.generation)||head.generation<1||!validHash(head.snapshot_generation))fail('Checkpoint head is invalid',503);
 if(head.backend==='library'){try{await validatePublication(head.publication);}catch{fail('Checkpoint head is invalid',503);}if(head.publication.snapshot_generation!==head.snapshot_generation)fail('Checkpoint head is inconsistent',503);}
 else if(head.backend!==undefined&&head.backend!=='r2'||!validHash(head.manifest_sha256))fail('Checkpoint head is invalid',503);
 return {raw,head};
}
async function verifySnapshot(db,id) {
 const generation=await db.prepare('SELECT count FROM generations WHERE id=?').bind(id).first();
 if(!generation)fail('Checkpoint snapshot missing',409);
 const rows=(await db.prepare('SELECT kind,key,checksum FROM records WHERE generation=? ORDER BY kind,key').bind(id).all()).results;
 if(rows.length!==generation.count||await sha256(encode(rows.map(row=>`${row.kind}:${row.key}:${row.checksum}`).join('\n')))!==id)fail('Checkpoint snapshot incomplete',409);
}
async function commitHead(request,env) {
 if(!(request.headers.get('Content-Type')||'').includes('application/json'))fail('JSON required',415);
 const data=parseJSON(await readLimited(request,4096));
 if(!data || !validHash(data.manifest_sha256)||!validHash(data.snapshot_generation)||!Number.isSafeInteger(data.expected_generation)||data.expected_generation<0||data.expected_generation>=Number.MAX_SAFE_INTEGER || (data.expected_generation===0?data.expected_manifest_sha256!==null:!validHash(data.expected_manifest_sha256)))fail('Invalid checkpoint commit');
 if(data.operation_id!==undefined && (typeof data.operation_id!=='string'||!/^[-A-Za-z0-9_.:]{1,120}$/.test(data.operation_id)))fail('Invalid checkpoint operation ID');
 const {raw,head}=await currentHead(env.DB);
 if(head.backend==='library')fail('Checkpoint backend differs; explicit migration is required',409);
 const desiredMatches=value=>value.generation===data.expected_generation+1 && value.manifest_sha256===data.manifest_sha256 && value.snapshot_generation===data.snapshot_generation && (value.operation_id??null)===(data.operation_id??null);
 if(desiredMatches(head))return json({ok:true,idempotent:true,head});
 if(head.generation!==data.expected_generation||head.manifest_sha256!==data.expected_manifest_sha256)fail('Checkpoint head changed; reload before publishing',409);
 await loadManifest(env.BUCKET,data.manifest_sha256);
 await verifySnapshot(env.DB,data.snapshot_generation);
 const next={generation:head.generation+1,manifest_sha256:data.manifest_sha256,snapshot_generation:data.snapshot_generation,updated_at:new Date().toISOString(),...(data.operation_id===undefined?{}:{operation_id:data.operation_id})},serialized=JSON.stringify(next);
 // A losing publisher changes neither the active snapshot nor the head. Every
 // dependent statement tests the exact winning value within one transaction.
 await env.DB.batch([
  env.DB.prepare('INSERT INTO pointers(key,value) SELECT ?,? WHERE EXISTS(SELECT 1 FROM generations WHERE id=? AND count=(SELECT COUNT(*) FROM records WHERE generation=?)) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE pointers.value=?').bind(HEAD_KEY,serialized,data.snapshot_generation,data.snapshot_generation,raw),
  env.DB.prepare("UPDATE generations SET status='complete' WHERE id=? AND EXISTS(SELECT 1 FROM pointers WHERE key=? AND value=?)").bind(data.snapshot_generation,HEAD_KEY,serialized),
  env.DB.prepare("INSERT INTO pointers(key,value) SELECT 'active',? WHERE EXISTS(SELECT 1 FROM pointers WHERE key=? AND value=?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(data.snapshot_generation,HEAD_KEY,serialized),
  env.DB.prepare("INSERT INTO pointers(key,value) SELECT 'synced_at',? WHERE EXISTS(SELECT 1 FROM pointers WHERE key=? AND value=?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(next.updated_at,HEAD_KEY,serialized),
 ]);
 const saved=(await currentHead(env.DB)).head;
 if(!desiredMatches(saved))fail('Checkpoint head changed; reload before publishing',409);
 return json({ok:true,head:saved});
}

// Caller must authorize the owner and resolve this metadata from the same D1
// snapshot generation. This helper never opens an arbitrary object-key route.
export async function streamCheckpointFile(env,relativePath,expectedSha256,expectedSize,snapshotGeneration) {
 if(!env.DB)fail('Private checkpoint storage unavailable',503);
 if(!safePath(relativePath)||!relativePath.startsWith('fulltexts/')||!validHash(expectedSha256)||!Number.isSafeInteger(expectedSize)||expectedSize<=0||!validHash(snapshotGeneration))fail('Invalid checkpoint PDF metadata',404);
 const {head}=await currentHead(env.DB);
 if(head.backend==='library')fail('PDF files are unavailable for this Library checkpoint; upload them again when supported',404);
 if(!env.BUCKET)fail('Private checkpoint storage unavailable',503);
 if(!head.manifest_sha256||head.snapshot_generation!==snapshotGeneration)fail('PDF snapshot is not committed',409);
 const manifest=await loadManifest(env.BUCKET,head.manifest_sha256,false);
 const file=manifest.files.find(item=>item.path==='payload/'+relativePath);
 if(!file)fail('Checkpoint PDF missing',404);
 if(file.sha256!==expectedSha256||file.size!==expectedSize)fail('Checkpoint PDF metadata mismatch',409);
 // Fail before sending response headers when an object is absent or its
// server-verified checksum metadata no longer matches the immutable manifest.
 for(const chunk of file.chunks)verifyStoredObject(await env.BUCKET.head(key('blob',chunk.sha256)),chunk.sha256,chunk.size);
 let index=0,cancelled=false;
 const body=new ReadableStream({
  async pull(controller){
   try {
    if(cancelled)return;
    if(index===file.chunks.length){controller.close();return;}
    const chunk=file.chunks[index++],object=await env.BUCKET.get(key('blob',chunk.sha256));
    verifyStoredObject(object,chunk.sha256,chunk.size);
    const bytes=new Uint8Array(await object.arrayBuffer());
    if(bytes.byteLength!==chunk.size||await sha256(bytes)!==chunk.sha256)fail('Checkpoint PDF chunk checksum mismatch',409);
    if(!cancelled){controller.enqueue(bytes);if(index===file.chunks.length)controller.close();}
   }catch{if(!cancelled)controller.error(new Error('Checkpoint PDF integrity check failed'));}
  },
  cancel(){cancelled=true;},
 },{highWaterMark:0});
 return new Response(body,{headers:{...headers,'Content-Type':'application/pdf','Content-Length':String(file.size),'Content-Disposition':'inline; filename="paper.pdf"','Content-Security-Policy':'sandbox','Accept-Ranges':'none'}});
}

export async function handleCloudState(request, env, path=new URL(request.url).pathname) {
 // Defense in depth: integration should also authenticate before routing here.
 const supplied=request.headers.get('Authorization');
 if(!env.SYNC_TOKEN || !supplied?.startsWith('Bearer ') || await sha256(encode(supplied.slice(7)))!==await sha256(encode(env.SYNC_TOKEN)))fail('Invalid synchronization credentials',401);
 if(!env.DB || !env.BUCKET || !['get','head','put'].every(name=>typeof env.BUCKET[name]==='function'))fail('Private checkpoint storage unavailable',503);
 if(path==='/api/state/head'){
  if(request.method==='GET')return json({head:(await currentHead(env.DB)).head});
  if(request.method==='POST')return commitHead(request,env);
  fail('Method not allowed',405);
 }
 const match=/^\/api\/state\/(blob|manifest)\/([a-f0-9]{64})$/.exec(path);if(!match)fail('Checkpoint operation not found',404);
 const [,kind,id]=match;
 if(request.method==='HEAD'||request.method==='GET'){
  const object=await env.BUCKET[request.method==='HEAD'?'head':'get'](key(kind,id));if(!object)fail('Checkpoint object missing',404);verifyStoredObject(object,id);
  return new Response(request.method==='HEAD'?null:object.body,{headers:{...headers,'Content-Type':'application/octet-stream','Content-Length':String(object.size),'ETag':`"${id}"`}});
 }
 if(request.method!=='PUT')fail('Method not allowed',405);
 const bytes=await readLimited(request,kind==='blob'?MAX_BLOB_BYTES:MAX_MANIFEST_BYTES);
 if(kind==='blob' && !bytes.byteLength)fail('Empty checkpoint chunk');
 if(await sha256(bytes)!==id)fail('Checkpoint checksum mismatch');
 if(kind==='manifest')await verifyBlobs(env.BUCKET,parseJSON(bytes));
 const created=await putImmutable(env.BUCKET,kind,id,bytes);return json({ok:true,sha256:id,size:bytes.byteLength,created},created?201:200);
}


const publicationKeys=['operation_id','snapshot_generation','archive_sha256','archive_size','checkpoint_id','library_file_id','current_version','file_id','file_name','publication_id'];
const exactKeys=(value,keys)=>value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));
const opaque=value=>typeof value==='string'&&value.length>0&&value.length<=255&&/^[!-~]+$/.test(value)&&!/[\\/]/.test(value);
const canonical=value=>JSON.stringify(Object.fromEntries(Object.keys(value).sort().map(key=>[key,value[key]])));
async function validatePublication(value){
 if(!exactKeys(value,publicationKeys)||typeof value.operation_id!=='string'||!/^([A-Za-z0-9][A-Za-z0-9._-]{0,119})$/.test(value.operation_id)||!['snapshot_generation','archive_sha256','checkpoint_id','publication_id'].every(key=>validHash(value[key]))||!Number.isSafeInteger(value.archive_size)||value.archive_size<1||value.archive_size>16*1024**3||!Number.isSafeInteger(value.current_version)||value.current_version<0||!opaque(value.library_file_id)||!opaque(value.file_id)||typeof value.file_name!=='string'||!value.file_name.length||value.file_name.length>255||['.','..'].includes(value.file_name)||/[\\/:\u0000-\u001f\u007f\ud800-\udfff]/u.test(value.file_name))fail('Invalid immutable Library publication');
 const fields=Object.fromEntries(Object.entries(value).filter(([key])=>key!=='publication_id'));
 if(await sha256(encode(canonical(fields)))!==value.publication_id)fail('Library publication checksum mismatch');
 return value;
}
async function validateLibraryHead(head){
 if(!exactKeys(head,['generation','publication'])||!Number.isSafeInteger(head.generation)||head.generation<0||head.generation>=Number.MAX_SAFE_INTEGER)fail('Invalid expected Library head');
 if(head.generation===0){if(head.publication!==null)fail('Invalid empty Library head');}
 else await validatePublication(head.publication);
}
const libraryView=head=>({generation:head.generation,publication:head.generation?head.publication:null});
export async function readLibraryHead(db){
 const {head}=await currentHead(db);
 if(head.generation&&head.backend!=='library')fail('Checkpoint backend differs; explicit migration is required',409);
 return libraryView(head);
}
// Caller must authorize the Site owner. The immutable Library upload and byte
// verification happen in the host before this operation. This transaction only
// validates the exact receipt and atomically binds it to the staged D1 view.
export async function commitLibraryHead(db,data){
 if(!exactKeys(data,['expected_head','publication']))fail('Invalid Library commit');
 await validateLibraryHead(data.expected_head);await validatePublication(data.publication);
 const {raw,head}=await currentHead(db),expected=data.expected_head,publication=data.publication;
 if(head.generation&&head.backend!=='library')fail('Checkpoint backend differs; explicit migration is required',409);
 const desiredMatches=value=>value.backend==='library'&&value.generation===expected.generation+1&&value.publication?.publication_id===publication.publication_id;
 if(desiredMatches(head))return {ok:true,idempotent:true,head:libraryView(head)};
 if(head.generation!==expected.generation||(head.publication?.publication_id??null)!==(expected.publication?.publication_id??null))fail('Checkpoint head changed; reload before publishing',409);
 // A Library object is immutable by contract: never reuse the current stable ID
 // for another publication. The host retains historical IDs and prior versions.
 if(head.publication?.library_file_id===publication.library_file_id)fail('A new immutable Library item is required for each checkpoint',409);
 const historyKey='library_archive:'+publication.library_file_id;
 if(await db.prepare('SELECT value FROM pointers WHERE key=?').bind(historyKey).first())fail('A new immutable Library item is required for each checkpoint',409);
 const g=publication.snapshot_generation;
 await verifySnapshot(db,g);
 if((await db.prepare('SELECT status FROM generations WHERE id=?').bind(g).first())?.status!=='complete')fail('Display snapshot must be finished before publication',409);
 const next={backend:'library',generation:expected.generation+1,publication,snapshot_generation:g,updated_at:new Date().toISOString()},serialized=JSON.stringify(next);
 await db.batch([
  db.prepare("INSERT INTO pointers(key,value) SELECT ?,? WHERE EXISTS(SELECT 1 FROM generations WHERE id=? AND status='complete' AND count=(SELECT COUNT(*) FROM records WHERE generation=?)) AND NOT EXISTS(SELECT 1 FROM pointers WHERE key=?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE pointers.value=?").bind(HEAD_KEY,serialized,g,g,historyKey,raw),
  db.prepare("INSERT INTO pointers(key,value) SELECT ?,? WHERE EXISTS(SELECT 1 FROM pointers WHERE key=? AND value=?) ON CONFLICT(key) DO NOTHING").bind(historyKey,JSON.stringify(publication),HEAD_KEY,serialized),
  db.prepare("INSERT INTO pointers(key,value) SELECT 'active',? WHERE EXISTS(SELECT 1 FROM pointers WHERE key=? AND value=?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(g,HEAD_KEY,serialized),
  db.prepare("INSERT INTO pointers(key,value) SELECT 'synced_at',? WHERE EXISTS(SELECT 1 FROM pointers WHERE key=? AND value=?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(next.updated_at,HEAD_KEY,serialized),
 ]);
 const saved=(await currentHead(db)).head;
 if(!desiredMatches(saved))fail('Checkpoint head changed; reload before publishing',409);
 return {ok:true,head:libraryView(saved)};
}

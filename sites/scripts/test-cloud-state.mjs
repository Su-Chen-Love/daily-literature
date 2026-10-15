import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {handleCloudState, MAX_BLOB_BYTES} from '../worker/cloud-state.js';
import {database} from './d1-local.mjs';
const digest=data=>createHash('sha256').update(data).digest('hex');
class Bucket {
 constructor(){this.objects=new Map();this.checksums=new Map();this.writes=0;}
 async head(key){const value=this.objects.get(key);return value?{size:value.length,checksums:{sha256:this.checksums.get(key)}}:null;}
 async get(key){const value=this.objects.get(key);return value?{size:value.length,checksums:{sha256:this.checksums.get(key)},body:new Response(value).body,arrayBuffer:async()=>value.slice().buffer}:null;}
 async put(key,value,options){assert.equal(options.onlyIf.get('If-None-Match'),'*');assert.equal(Buffer.from(options.sha256).toString('hex'),digest(value));if(this.objects.has(key))return null;this.objects.set(key,new Uint8Array(value));this.checksums.set(key,options.sha256);this.writes++;return {size:value.byteLength};}
}
const env={DB:database(),BUCKET:new Bucket(),SYNC_TOKEN:'test-secret'};
const request=async(path,method='GET',data,token=true,environment=env)=>{
 const isJSON=data && !(data instanceof Uint8Array) && typeof data!=='string';
 const headers={...(token?{Authorization:'Bearer test-secret'}:{}),...(isJSON?{'Content-Type':'application/json'}:{})};
 try{return await handleCloudState(new Request('https://unit.example.chatgpt.site'+path,{method,headers,body:isJSON?JSON.stringify(data):data}),environment);}
 catch(error){return new Response(JSON.stringify({error:error.message}),{status:error.status||503});}
};
const snapshot=async(title)=>{const data=JSON.stringify({title}),checksum=digest(data),id=digest('paper:key:'+checksum);await env.DB.prepare('INSERT INTO generations(id,count,created_at) VALUES(?,?,?)').bind(id,1,new Date().toISOString()).run();await env.DB.prepare('INSERT INTO records(generation,kind,key,data,checksum) VALUES(?,?,?,?,?)').bind(id,'paper','key',data,checksum).run();return id;};
const put=async(kind,data)=>{const id=digest(data);assert.ok([200,201].includes((await request(`/api/state/${kind}/${id}`,'PUT',data)).status));return id;};
const bytes=new TextEncoder().encode('checkpoint-descriptor');
const id=digest(bytes);
for(const [path,method,data] of [[`/api/state/blob/${id}`,'PUT',bytes],[`/api/state/blob/${id}`,'GET'],[`/api/state/blob/${id}`,'HEAD'],[`/api/state/manifest/${id}`,'GET'],['/api/state/head','GET'],['/api/state/head','POST',{}]])assert.equal((await request(path,method,data,false)).status,401);
assert.equal((await request('/api/state/head','GET',undefined,true,{...env,BUCKET:null})).status,503);
assert.equal((await request(`/api/state/blob/${'a'.repeat(64)}`,'PUT',bytes)).status,400);
assert.equal((await request(`/api/state/blob/${id}`,'PUT',new Uint8Array(MAX_BLOB_BYTES+1))).status,413);
await put('blob',bytes);const writes=env.BUCKET.writes;assert.equal((await request(`/api/state/blob/${id}`,'PUT',bytes)).status,200);assert.equal(env.BUCKET.writes,writes);
const fetched=await request(`/api/state/blob/${id}`);assert.equal(fetched.headers.get('Cache-Control'),'private, no-store');assert.deepEqual(new Uint8Array(await fetched.arrayBuffer()),bytes);assert.equal((await request(`/api/state/blob/${id}`,'HEAD')).headers.get('Content-Length'),String(bytes.length));
const manifest={format:'academic-radar-chunks-v1',checkpoint_id:'e'.repeat(64),files:[{path:'manifest.json',size:bytes.length,sha256:id,chunks:[{sha256:id,size:bytes.length}]}]};
const raw=new TextEncoder().encode(JSON.stringify(manifest));
const blobKey=`private/checkpoints/v1/blob/${id}`,checksum=env.BUCKET.checksums.get(blobKey);
env.BUCKET.checksums.delete(blobKey);
assert.equal((await request(`/api/state/blob/${id}`,'HEAD')).status,409);
assert.equal((await request(`/api/state/manifest/${digest(raw)}`,'PUT',raw)).status,409);
env.BUCKET.checksums.set(blobKey,new Uint8Array(32).buffer);
assert.equal((await request(`/api/state/blob/${id}`,'GET')).status,409);
env.BUCKET.checksums.set(blobKey,checksum);
const manifestId=await put('manifest',raw);
for(const path of ['../escape','/absolute','a\\b','a//b','a:stream','a/./b','a/../b']){const unsafe=structuredClone(manifest);unsafe.files[0].path=path;const raw=new TextEncoder().encode(JSON.stringify(unsafe));assert.equal((await request(`/api/state/manifest/${digest(raw)}`,'PUT',raw)).status,400,path);}
const collision=structuredClone(manifest);collision.files.push({...collision.files[0],path:'manifest.json/child'});const collisionRaw=new TextEncoder().encode(JSON.stringify(collision));assert.equal((await request(`/api/state/manifest/${digest(collisionRaw)}`,'PUT',collisionRaw)).status,400);
const missing=structuredClone(manifest);missing.files[0].chunks[0].sha256='d'.repeat(64);const missingRaw=new TextEncoder().encode(JSON.stringify(missing));assert.equal((await request(`/api/state/manifest/${digest(missingRaw)}`,'PUT',missingRaw)).status,409);
const generation=await snapshot('first'),nextGeneration=await snapshot('second');
const commit={manifest_sha256:manifestId,snapshot_generation:generation,expected_generation:0,expected_manifest_sha256:null,operation_id:'operation-1'};
const incomplete={...commit,snapshot_generation:'c'.repeat(64)};assert.equal((await request('/api/state/head','POST',incomplete)).status,409);assert.equal((await (await request('/api/state/head')).json()).head.generation,0);
const committed=await (await request('/api/state/head','POST',commit)).json();assert.equal(committed.head.generation,1);assert.equal(committed.head.operation_id,'operation-1');assert.equal((await env.DB.prepare("SELECT value FROM pointers WHERE key='active'").first()).value,generation);
const retried=await (await request('/api/state/head','POST',commit)).json();assert.equal(retried.idempotent,true);assert.equal(retried.head.generation,1);
assert.equal((await request('/api/state/head','POST',{...commit,snapshot_generation:nextGeneration})).status,409);
// Simulate both publishers reading generation 1. Inject the winner immediately
// before the loser's D1 transaction, after its manifest/snapshot validation.
const next={...commit,expected_generation:1,expected_manifest_sha256:manifestId,snapshot_generation:nextGeneration,operation_id:'winner'};
let injected=false;
const racedDB={prepare:env.DB.prepare,batch:async statements=>{if(!injected){injected=true;assert.equal((await request('/api/state/head','POST',next)).status,200);}return env.DB.batch(statements);}};
assert.equal((await request('/api/state/head','POST',{...next,snapshot_generation:generation,operation_id:'loser'},true,{...env,DB:racedDB})).status,409);
assert.equal((await (await request('/api/state/head')).json()).head.operation_id,'winner');
assert.equal((await env.DB.prepare("SELECT value FROM pointers WHERE key='active'").first()).value,nextGeneration);
assert.equal((await (await request('/api/state/head','POST',next)).json()).idempotent,true);
console.log('Cloud checkpoint transport passed: private auth, bounded chunks, hash/path validation, immutable retries, atomic head/snapshot CAS, stale publisher fencing.');
// A committed PDF can be streamed to an owner only after the caller resolves
// its metadata from the same active snapshot. This helper has no public route.
const {streamCheckpointFile}=await import('../worker/cloud-state.js');
const pdfParts=[new TextEncoder().encode('%PDF-1.4\nfirst part\n'),new TextEncoder().encode('second part\n%%EOF\n')];
const pdfBytes=Buffer.concat(pdfParts),pdfDigest=digest(pdfBytes),pdfChunks=[];
for(const part of pdfParts)pdfChunks.push({sha256:await put('blob',part),size:part.length});
const pdfManifest={...manifest,files:[...manifest.files,{path:'payload/fulltexts/paper.pdf',size:pdfBytes.length,sha256:pdfDigest,chunks:pdfChunks}]};
const pdfManifestId=await put('manifest',new TextEncoder().encode(JSON.stringify(pdfManifest)));
assert.equal((await request('/api/state/head','POST',{expected_generation:2,expected_manifest_sha256:manifestId,manifest_sha256:pdfManifestId,snapshot_generation:nextGeneration,operation_id:'pdf'})).status,200);
const pdfResponse=await streamCheckpointFile(env,'fulltexts/paper.pdf',pdfDigest,pdfBytes.length,nextGeneration);
assert.equal(pdfResponse.status,200);assert.equal(pdfResponse.headers.get('Accept-Ranges'),'none');assert.equal(pdfResponse.headers.get('Content-Range'),null);assert.equal(pdfResponse.headers.get('Content-Disposition'),'inline; filename="paper.pdf"');assert.equal(pdfResponse.headers.get('Content-Security-Policy'),'sandbox');assert.equal(pdfResponse.headers.get('X-Content-Type-Options'),'nosniff');assert.equal(pdfResponse.headers.get('Content-Type'),'application/pdf');assert.equal(pdfResponse.headers.get('Cache-Control'),'private, no-store');assert.equal(pdfResponse.headers.get('Content-Length'),String(pdfBytes.length));assert.deepEqual(Buffer.from(await pdfResponse.arrayBuffer()),pdfBytes);
await assert.rejects(streamCheckpointFile(env,'fulltexts/missing.pdf',pdfDigest,pdfBytes.length,nextGeneration),{status:404});
await assert.rejects(streamCheckpointFile(env,'fulltexts/paper.pdf',pdfDigest,pdfBytes.length,generation),{status:409});
await assert.rejects(streamCheckpointFile(env,'../fulltexts/paper.pdf',pdfDigest,pdfBytes.length,nextGeneration),{status:404});
await assert.rejects(streamCheckpointFile(env,'fulltexts/paper.pdf','b'.repeat(64),pdfBytes.length,nextGeneration),{status:409});
const corruptKey=`private/checkpoints/v1/blob/${pdfChunks[1].sha256}`;
const originalPart=env.BUCKET.objects.get(corruptKey);env.BUCKET.objects.set(corruptKey,new Uint8Array(originalPart.length));
const corruptResponse=await streamCheckpointFile(env,'fulltexts/paper.pdf',pdfDigest,pdfBytes.length,nextGeneration);
await assert.rejects(corruptResponse.arrayBuffer(),/integrity/);
env.BUCKET.objects.delete(corruptKey);
await assert.rejects(streamCheckpointFile(env,'fulltexts/paper.pdf',pdfDigest,pdfBytes.length,nextGeneration),{status:409});
console.log('Committed checkpoint PDF streaming passed: snapshot binding, per-chunk integrity, missing-PDF refusal, bounded streaming.');

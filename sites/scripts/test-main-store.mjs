import assert from 'node:assert/strict';
import {createHash,randomBytes} from 'node:crypto';
import {mkdtempSync,readFileSync,readdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {database} from './d1-local.mjs';
import {mainStoreOperation,handleMainStore,canonicalJSON,IncrementalSHA256,MAIN_STORE_LIMITS} from '../worker/main-store.js';
const digest=value=>createHash('sha256').update(typeof value==='string'?value:Buffer.from(value)).digest('hex');
const empty=digest(''),temp=mkdtempSync(join(tmpdir(),'main-store-test-'));
const db=database();db.sql.exec(readFileSync(new URL('../db/main-store-schema.sql',import.meta.url),'utf8'));
// SQL source bytes and bound text bytes are different D1 limits. Measure at
// prepare() and bind() respectively; never report a JSON parameter as SQL text.
let queryCount=0,maxBatch=0,maxSQLSourceBytes=0,maxBoundTextBytes=0,maxBoundParameters=0;
const measured={
 prepare(sql){
  queryCount++;const sourceBytes=Buffer.byteLength(sql,'utf8');maxSQLSourceBytes=Math.max(maxSQLSourceBytes,sourceBytes);
  assert.ok(sourceBytes<=100_000,'D1 SQL source length cap (UTF-8 bytes)');
  const s=db.prepare(sql),bind=s.bind;
  s.bind=(...args)=>{
   maxBoundParameters=Math.max(maxBoundParameters,args.length);assert.ok(args.length<=100,'D1 statement parameter cap');
   for(const value of args)if(typeof value==='string'){
    const bytes=Buffer.byteLength(value,'utf8');maxBoundTextBytes=Math.max(maxBoundTextBytes,bytes);
    assert.ok(bytes<=2_000_000,'D1 bound string size cap (UTF-8 bytes)');
   }
   return bind.apply(s,args);
  };
  return s;
 },
 async batch(items){maxBatch=Math.max(maxBatch,items.length);assert.ok(items.length<=50);return db.batch(items);}
};
const call=(action,data={})=>mainStoreOperation(measured,action,data);
const reject=(action,data,status=400)=>assert.rejects(call(action,data),e=>e.status===status);
const toScalar=v=>v===null?{t:'null'}:typeof v==='string'?{t:'text',v}:{t:'integer',v:String(v)};
function rehash(row){const copy=structuredClone(row);copy.key_sha256=digest(canonicalJSON(copy.key));copy.row_sha256=digest(canonicalJSON({values:copy.values,rowid:copy.rowid}));delete copy.record_sha256;copy.record_sha256=digest(canonicalJSON(copy));return copy;}
async function upload(bytes){const hash=digest(bytes),chunks=[];for(let p=0;p<bytes.length;p+=MAIN_STORE_LIMITS.chunk_bytes){const b=bytes.subarray(p,p+MAIN_STORE_LIMITS.chunk_bytes),sha256=digest(b);await call('put_chunk',{sha256,data_base64:Buffer.from(b).toString('base64')});chunks.push({sha256,bytes:b.length});}await call('put_object',{object:{sha256:hash,bytes:bytes.length,chunks}});let cursor=0;while(true){const done=await call('verify_object',{sha256:hash,expected_cursor:cursor});if(done.verified)break;cursor=done.cursor;}return hash;}
try{
 for(const length of [0,1,55,56,63,64,65,127,128,129,131071,131072,131073,524300]){const bytes=randomBytes(length),state=new IncrementalSHA256();for(let i=0;i<length;i+=997)state.update(bytes.subarray(i,i+997));assert.equal(new IncrementalSHA256(state.state()).digest(),digest(bytes));}
 if(readdirSync('/tmp').includes('d1-wire-golden.json')){const golden=JSON.parse(readFileSync('/tmp/d1-wire-golden.json','utf8'));const row=golden.record??golden.row??golden;if(row.key)assert.equal(rehash(row).record_sha256,row.record_sha256);}
 assert.equal((await call('head')).head.revision,0);
 await reject('arbitrary_sql',{sql:'DELETE FROM papers'},404);
 await assert.rejects(handleMainStore(new Request('https://unit/api/main-store/head',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}}),{DB:db,OWNER_USER_ID:'owner'}),{status:403});
 // Synthetic fixture only: never read a private database in this regression test.
 execFileSync('python',['-c',String.raw`
from pathlib import Path
import sys,sqlite3
from academic_radar.storage import upgrade_database
from academic_radar.d1_package import export_package
root=Path(sys.argv[1]);state=root/'state';state.mkdir();db=state/'radar.sqlite3';upgrade_database(db)
with sqlite3.connect(db) as c:
 c.executemany('INSERT INTO meta(key,value) VALUES(?,?)',[(f'test-{i:04}',f'value-{i}') for i in range(550)])
 c.execute("INSERT INTO meta(key,value) VALUES('cloud_profile_file_pending','1')")
 c.execute("INSERT INTO meta(key,value) VALUES('cloud_profile_file_base','x')")
 c.execute("INSERT INTO papers(identity,title,first_seen,updated_at) VALUES('paper-1','Synthetic paper','2026-01-01','2026-01-01')")
 c.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at,details_json) VALUES('run-1','collection','succeeded','2026-01-01',?)", ('{"nested":{"backup_path":"archive://'+'a'*64+'"}}',))
c.execute('PRAGMA journal_mode=DELETE');c.close()
(state/'example.txt').write_text('synthetic data\n',encoding='utf8')
export_package(db,root/'package',files_root=state)
`,temp],{cwd:new URL('../../',import.meta.url),env:{...process.env,PYTHONPATH:'src'}});
 const packageDir=join(temp,'package'),raw=readFileSync(join(packageDir,'manifest.json')),manifest=JSON.parse(raw),id=digest(raw);
 for(const file of readdirSync(join(packageDir,'objects'))){const bytes=readFileSync(join(packageDir,'objects',file));await call('put_chunk',{sha256:file,data_base64:bytes.toString('base64')});}
 for(const object of manifest.objects){await call('put_object',{object});let cursor=0;while(true){const result=await call('verify_object',{sha256:object.sha256,expected_cursor:cursor});if(result.verified)break;cursor=result.cursor;}}
 await upload(raw);await call('begin_import',{manifest_sha256:id});const rowMap=new Map();
 for(const table of manifest.tables){const rows=readFileSync(join(packageDir,table.path),'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);rowMap.set(table.name,rows);for(let p=0;p<rows.length;p+=500)await call('import_rows',{import_id:id,operation_id:table.name+'-'+p,rows:rows.slice(p,p+500)});}
 // Conflicting retry is rejected and cannot replace original rows.
 const meta=rowMap.get('meta'),m=meta[0],bad=structuredClone(m);bad.values[1]=toScalar('wrong');const changed=rehash(bad);
 await reject('import_rows',{import_id:id,operation_id:'meta-0',rows:[changed]},409);
 for(const table of manifest.tables){let cursor='';while(true){const result=await call('verify_import',{import_id:id,table:table.name,expected_cursor:cursor});if(result.verified)break;cursor=result.cursor;}}
 await reject('import_rows',{import_id:id,operation_id:'frozen',rows:[m]},409);
 const commit={import_id:id,expected_revision:0,operation_id:'initial',watermarks:{feedback_cursor:0,profile_cursor:0,request_id:0}};
 db.sql.prepare("INSERT INTO cloud_feedback(identity,favorite,reading_status,updated_at) VALUES('synthetic-feedback',0,'unread','2026-01-01')").run();
 await reject('commit_import',{...commit,operation_id:'phantom-initial-watermark',watermarks:{feedback_cursor:1,profile_cursor:0,request_id:0}},409);
 assert.equal((await call('head')).head.revision,0);
 assert.equal((await call('commit_import',commit)).revision,1);assert.equal((await call('commit_import',commit)).idempotent,true);
 await reject('commit_import',{...commit,expected_revision:1,operation_id:'recommit'},409);
 assert.equal(db.sql.prepare("SELECT value FROM pointers WHERE key='active'").get(),undefined);
 const summary=await call('summary',{revision:1});assert.equal(summary.history_artifact_counts.unavailable_files,1);assert.equal(summary.history_artifact_counts.local_restore_backups,1);
 const pin1=await call('query_rows',{revision:1,table:'meta',keys:[m.key_sha256]});assert.equal(pin1.rows[0].record_sha256,m.record_sha256);
 await reject('commit_operations',{expected_revision:1,operation_id:'phantom-incremental-watermark',mutations:[],watermarks:{feedback_cursor:1,profile_cursor:0,request_id:0}},409);
 assert.equal((await call('head')).head.revision,1);
 const before=db.sql.prepare('SELECT COUNT(*) n FROM ms_records').get().n;
 const mutation={row:changed,expected_record_sha256:m.record_sha256};const operation={expected_revision:1,operation_id:'modify',mutations:[mutation]};
 assert.equal((await call('commit_operations',operation)).head.revision,2);
 assert.equal((await call('query_rows',{revision:1,table:'meta',keys:[m.key_sha256]})).rows[0].record_sha256,m.record_sha256);
 assert.equal((await call('query_rows',{revision:2,table:'meta',keys:[m.key_sha256]})).rows[0].record_sha256,changed.record_sha256);
 assert.equal(db.sql.prepare('SELECT COUNT(*) n FROM ms_records').get().n,before+1);
 const noop=await call('commit_operations',{expected_revision:2,operation_id:'noop',mutations:[{row:changed,expected_record_sha256:changed.record_sha256}]});assert.equal(noop.noop,true);assert.equal(noop.revision,2);
 assert.equal((await call('commit_operations',operation)).revision,2);
 await reject('commit_operations',{...operation,mutations:[{row:m,expected_record_sha256:m.record_sha256}]},409);
 await reject('commit_operations',{expected_revision:2,operation_id:'duplicate',mutations:[{row:changed,expected_record_sha256:changed.record_sha256},{row:changed,expected_record_sha256:changed.record_sha256}]});
 const invalid=structuredClone(changed);invalid.row_sha256='a'.repeat(64);delete invalid.record_sha256;invalid.record_sha256=digest(canonicalJSON(invalid));await reject('import_rows',{import_id:id,operation_id:'badrow',rows:[invalid]},409);
 // Invalid UTF-8 may be retained as a BLOB but cannot be referenced as TEXT.
 const invalidUtf8=await upload(Buffer.from([0xc0,0x80]));const utf=structuredClone(changed);utf.values[1]={t:'text',ref:invalidUtf8,bytes:2};await reject('commit_operations',{expected_revision:2,operation_id:'badutf8',mutations:[{row:rehash(utf),expected_record_sha256:changed.record_sha256}]},409);
 // A new historical unique record remains present after a tombstone.
 const marker=meta.find(r=>r.key[0].v==='cloud_profile_file_pending');assert.equal((await call('commit_operations',{expected_revision:2,operation_id:'marker',mutations:[{table:'meta',key_sha256:marker.key_sha256,delete:true,expected_record_sha256:marker.record_sha256}]})).revision,3);
 assert.equal((await call('query_rows',{revision:3,table:'meta',keys:[marker.key_sha256]})).rows.length,0);assert.equal((await call('query_rows',{revision:1,table:'meta',keys:[marker.key_sha256]})).rows.length,1);
 // Conflicting concurrent writers cannot expose a partial operation.
 const winnerRow=structuredClone(changed);winnerRow.values[1]=toScalar('winner');const winner=rehash(winnerRow);
 const loserRow=structuredClone(changed);loserRow.values[1]=toScalar('loser');const loser=rehash(loserRow);
 let injected=false;const raced={prepare:measured.prepare,batch:async items=>{if(!injected&&items.length===7){injected=true;await call('commit_operations',{expected_revision:3,operation_id:'race-winner',mutations:[{row:winner,expected_record_sha256:changed.record_sha256}]});}return measured.batch(items);}};
 await assert.rejects(mainStoreOperation(raced,'commit_operations',{expected_revision:3,operation_id:'race-loser',mutations:[{row:loser,expected_record_sha256:changed.record_sha256}]}),{status:409});
 assert.equal((await call('head')).head.revision,4);assert.equal((await call('query_rows',{revision:4,table:'meta',keys:[m.key_sha256]})).rows[0].record_sha256,winner.record_sha256);
 assert.equal((await call('commit_operations',operation)).head.revision,2,'old receipt remains pinned after later commits');
 // Domain/FK violations and file-tree collisions leave the head unchanged.
 const feedbackInfo=manifest.inventory.tables.find(t=>t.name==='paper_feedback');const feedbackValues={identity:'nonexistent-paper',interest:null,reason:null,favorite:0,reading_status:'unread',created_at:'2026-01-01',updated_at:'2026-01-01'};
 const badFeedback=rehash({table:'paper_feedback',key:[toScalar('nonexistent-paper')],values:feedbackInfo.columns.map(k=>toScalar(feedbackValues[k])),rowid:toScalar(1)});
 await reject('commit_operations',{expected_revision:4,operation_id:'bad-fk',mutations:[{row:badFeedback,expected_record_sha256:null}]},409);
 const f=manifest.files.find(f=>f.path==='example.txt');await reject('commit_operations',{expected_revision:4,operation_id:'bad-path',mutations:[],files:[{...f,path:'example.txt/child',expected_sha256:null}]},409);
 assert.equal((await call('head')).head.revision,4);
 // 600 mutations stage across pages and become visible in exactly one revision.
 const many=Array.from({length:600},(_,i)=>{const row=structuredClone(m);row.key=[toScalar('staged-'+i)];row.values[0]=row.key[0];row.values[1]=toScalar('atomic-'+i);row.rowid=toScalar(10000+i);return {row:rehash(row),expected_record_sha256:null};});
 const entries=many.map(m=>[m.row.table+':'+m.row.key_sha256,digest(canonicalJSON(m))]).sort((a,b)=>a[0].localeCompare(b[0]));let chain=empty;for(const [k,h]of entries)chain=digest(chain+'\n'+k+'\n'+h);
 await call('begin_operation',{operation_id:'large-atomic',expected_revision:4,mutation_count:many.length,file_count:0,chain_sha256:chain,watermarks:{feedback_cursor:0,profile_cursor:0,request_id:0}});
 await call('stage_operation',{operation_id:'large-atomic',batch_id:'page-0',mutations:many.slice(0,500),files:[]});
 assert.equal((await call('query_rows',{revision:4,table:'meta',keys:[many[0].row.key_sha256]})).rows.length,0);
 await reject('commit_staged_operation',{operation_id:'large-atomic'},409);
 await call('stage_operation',{operation_id:'large-atomic',batch_id:'page-1',mutations:many.slice(500),files:[]});let stageCursor='';while(true){const v=await call('verify_operation',{operation_id:'large-atomic',expected_cursor:stageCursor});if(v.verified)break;stageCursor=v.cursor;}
 await reject('stage_operation',{operation_id:'large-atomic',batch_id:'frozen',mutations:[many[0]],files:[]},409);
 assert.equal((await call('commit_staged_operation',{operation_id:'large-atomic'})).revision,5);
 assert.equal((await call('export_summary',{revision:5})).tables.find(t=>t.name==='meta').rows,meta.length-1+600);
 assert.equal((await call('query_rows',{revision:1,table:'meta',keys:[m.key_sha256]})).rows[0].record_sha256,m.record_sha256);
 // Exact-count and lookahead pagination, including filtered and scoped pages.
 const liveMeta=meta.filter(r=>r.key_sha256!==marker.key_sha256).sort((a,b)=>a.key_sha256.localeCompare(b.key_sha256));
 const last501=liveMeta.at(-502)?.key_sha256??'';
 const page501=await call('query_rows',{revision:4,table:'meta',after_key:last501});assert.equal(page501.rows.length,500);assert.equal(page501.done,false);
 const final501=await call('query_rows',{revision:4,table:'meta',after_key:page501.next_key});assert.equal(final501.rows.length,1);assert.equal(final501.done,true);assert.equal(final501.next_key,null);
 const exact500=await call('query_rows',{revision:4,table:'meta',after_key:liveMeta.at(-501).key_sha256});assert.equal(exact500.rows.length,500);assert.equal(exact500.done,true);
 const filtered=await call('query_rows',{revision:4,table:'meta',filter:{key:m.key[0].v}});assert.equal(filtered.rows.length,1);assert.equal(filtered.done,true);
 const scoped=await call('query_rows',{revision:4,table:'meta',scope:'runtime_meta',operation_id:'fixture'});assert.equal(scoped.rows.length,500);assert.equal(scoped.done,false);
 // Large inline rows cut a page by bytes rather than dropping later rows.
 const large=Array.from({length:6},(_,i)=>{const row=structuredClone(m);row.key=[toScalar('large-page-'+i)];row.values=[row.key[0],toScalar('x'.repeat(100000))];row.rowid=toScalar(20000+i);return {row:rehash(row),expected_record_sha256:null};});
 assert.equal((await call('commit_operations',{expected_revision:5,operation_id:'byte-pagination',mutations:large})).revision,6);
 const largeKeys=large.map(m=>m.row.key_sha256),seenLarge=[];let afterLarge='';while(true){const page=await call('query_rows',{revision:6,table:'meta',keys:largeKeys,after_key:afterLarge});assert.ok(page.rows.length>0);assert.ok(Buffer.byteLength(canonicalJSON(page))<MAIN_STORE_LIMITS.response_bytes);seenLarge.push(...page.rows.map(r=>r.key_sha256));if(page.done)break;assert.ok(page.rows.length<6);afterLarge=page.next_key;}assert.equal(new Set(seenLarge).size,6);
 const files=Array.from({length:500},(_,i)=>({...f,path:'paged/'+String(i).padStart(4,'0')+'.txt',expected_sha256:null}));
 assert.equal((await call('commit_operations',{expected_revision:6,operation_id:'file-pagination',mutations:[],files})).revision,7);
 const allFiles=await call('query_files',{revision:7});assert.equal(allFiles.files.length,500);assert.equal(allFiles.done,false);const finalFiles=await call('query_files',{revision:7,after_path:allFiles.next_path});assert.equal(finalFiles.files.length,1);assert.equal(finalFiles.done,true);
 const exactFiles=await call('query_files',{revision:7,prefix:'paged/'});assert.equal(exactFiles.files.length,500);assert.equal(exactFiles.done,true);assert.equal(exactFiles.next_path,null);
 // Bulk chunk writes auto-verify single-chunk objects; bounded reads return subsets.
 const chunks=Array.from({length:4},()=>{const bytes=randomBytes(MAIN_STORE_LIMITS.chunk_bytes);return {sha256:digest(bytes),data_base64:bytes.toString('base64')};});
 for(let i=0;i<chunks.length;i+=2)assert.equal((await call('put_chunks',{chunks:chunks.slice(i,i+2)})).chunks.length,2);
 const objectPage=await call('read_objects',{sha256s:chunks.map(c=>c.sha256)});assert.equal(objectPage.objects.length,4);
 const remaining=new Set(chunks.map(c=>c.sha256));while(remaining.size){const page=await call('read_chunks',{sha256s:[...remaining]});assert.ok(page.chunks.length>0&&page.chunks.length<4);assert.ok(Buffer.byteLength(canonicalJSON(page))<MAIN_STORE_LIMITS.response_bytes);for(const c of page.chunks){assert.equal(digest(Buffer.from(c.data_base64,'base64')),c.sha256);assert.ok(remaining.delete(c.sha256));}}
 // A >750 KiB manifest remains a retained object, while only its slim core is
 // stored in ms_imports. File staging must finish before row verification.
 const growth=structuredClone(manifest);growth.files=Array.from({length:5501},(_,i)=>({...f,path:'growth/'+String(i).padStart(5,'0')+'-'+('g'.repeat(120))+'.txt'}));growth.directories.push({path:'growth',mode:493});
 const growthRaw=Buffer.from(canonicalJSON(growth)+'\n');assert.ok(growthRaw.length>MAIN_STORE_LIMITS.request_bytes);const growthId=await upload(growthRaw);
 const began=await call('begin_import',{manifest_sha256:growthId});assert.equal(began.status,'manifest');assert.equal(began.files_staged,5000);
 await reject('verify_import',{import_id:growthId,table:manifest.tables[0].name,expected_cursor:''},409);
 const resumed=await call('begin_import',{manifest_sha256:growthId});assert.equal(resumed.status,'staging');assert.equal(resumed.files_staged,5501);
 assert.ok(db.sql.prepare('SELECT length(CAST(manifest AS BLOB)) n FROM ms_imports WHERE import_id=?').get(growthId).n<MAIN_STORE_LIMITS.request_bytes);
 assert.equal(db.sql.prepare('SELECT verified FROM ms_objects WHERE sha256=?').get(growthId).verified,1);
 for(const damage of ['omitted-table','wrong-empty-columns']){const broken=structuredClone(manifest),emptyTable=broken.tables.find(t=>t.rows===0);if(damage==='omitted-table'){broken.tables=broken.tables.filter(t=>t.name!==emptyTable.name);broken.inventory.tables=broken.inventory.tables.filter(t=>t.name!==emptyTable.name);}else broken.inventory.tables.find(t=>t.name===emptyTable.name).columns.push('forged');const badRoot=Buffer.from(canonicalJSON(broken)+'\n');await upload(badRoot);await reject('begin_import',{manifest_sha256:digest(badRoot)},400);}
 const unretained=structuredClone(manifest);unretained.statistics={...manifest.statistics,test:1};await reject('begin_import',{manifest_sha256:digest(canonicalJSON(unretained)+'\n'),manifest:unretained},409);
 const info=await call('capacity');assert.ok(info.allocated_sqlite_bytes>0);assert.equal(info.site_quota_bytes,null);
 console.log(JSON.stringify({passed:true,queries:queryCount,max_batch_statements:maxBatch,max_sql_source_bytes:maxSQLSourceBytes,max_bound_text_bytes:maxBoundTextBytes,max_bound_parameters:maxBoundParameters,revision:(await call('head')).head.revision,coverage:'owner auth, typed hashes, exact objects, UTF-8 refs, frozen import, table chains, CAS, replay, pinned history, no-op dedup, transient-only tombstones, watermark equality, exact/byte pagination, bulk subsets, resumable large manifest, schema metadata, capacity'}));
}finally{db.sql.close();rmSync(temp,{recursive:true,force:true});}

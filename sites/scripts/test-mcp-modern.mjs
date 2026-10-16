// Protocol 2026-07-28 fixtures. All requests stay in-process and use synthetic
// identities/results; discovery must never require access to personal storage.
import assert from 'node:assert/strict';
import {handleMcp,tools} from '../worker/mcp.js';
const version='2026-07-28',protocolKey='io.modelcontextprotocol/protocolVersion';
const capabilitiesKey='io.modelcontextprotocol/clientCapabilities',clientInfoKey='io.modelcontextprotocol/clientInfo';
const origin='https://modern-fixture.invalid',calls=[];
const env={OWNER_USER_ID:'fixture-owner',DB:{prepare(){assert.fail('Protocol fixture touched storage');}}};
const handlers=Object.fromEntries(tools.map(tool=>[tool.name,async args=>{calls.push({name:tool.name,args});return {fixture:true,tool:tool.name};}]));
const meta=()=>({[protocolKey]:version,[capabilitiesKey]:{},[clientInfoKey]:{name:'modern-fixture',version:'1'}});
async function rpc(method,params={},options={}){
 const message={jsonrpc:'2.0',id:0,method,params:{_meta:meta(),...params},...options.message};
 const headers=new Headers({'Content-Type':'application/json',Accept:'application/json, text/event-stream','MCP-Protocol-Version':version,'Mcp-Method':method});
 if(['tools/call','prompts/get','resources/read'].includes(method)){
  const name=method==='resources/read'?params.uri:params.name;
  headers.set('Mcp-Name',/^[\x21-\x7e]+$/.test(name)?name:'=?base64?'+Buffer.from(name).toString('base64')+'?=');
 }
 if(options.owner)headers.set('oai-authenticated-user-id','fixture-owner');
 for(const [key,value] of Object.entries(options.headers||{})){if(value===null)headers.delete(key);else headers.set(key,value);}
 const response=await handleMcp(new Request(origin+'/mcp',{method:'POST',headers,body:JSON.stringify(message)}),options.env||env,options.handlers||handlers);
 return {status:response.status,data:await response.json(),response};
}
const ok=result=>{assert.equal(result.status,200,JSON.stringify(result.data));assert.equal(result.data.result.resultType,'complete');assert.deepEqual(result.data.result._meta['io.modelcontextprotocol/serverInfo'],{name:'academic-radar',version:'1.0.0'});assert.equal(result.response.headers.get('MCP-Session-Id'),null);assert.equal(result.response.headers.get('Cache-Control'),'private, no-store');return result.data.result;};
const error=(result,code,status=400)=>{assert.equal(result.status,status,JSON.stringify(result.data));assert.equal(result.data.error.code,code);assert.equal(result.data.id,0);};
// Discovery is the first request, with no initialize or owner session.
const discovery=ok(await rpc('server/discover'));
assert.deepEqual(discovery.supportedVersions,[version,'2025-11-25','2025-06-18','2025-03-26','2024-11-05']);
assert.deepEqual(discovery.capabilities,{tools:{}});
assert.equal(discovery.ttlMs,0);assert.equal(discovery.cacheScope,'private');
assert.ok(!JSON.stringify(discovery).includes('fixture-owner'));
const listed=ok(await rpc('tools/list',{_meta:{...meta(),'client/extension':true}}));
assert.equal(listed.tools.length,12);assert.deepEqual(listed.tools,tools);
assert.equal(listed.ttlMs,0);assert.equal(listed.cacheScope,'private');
assert.equal(calls.length,0);
ok(await rpc('ping'));
// Client identity is advisory, optional, and never substitutes for owner auth.
const noInfo=meta();delete noInfo[clientInfoKey];ok(await rpc('server/discover',{_meta:noInfo}));
error(await rpc('server/discover',{unexpected:true}),-32602);
error(await rpc('initialize',{protocolVersion:version,capabilities:{},clientInfo:{name:'fixture',version:'1'}}),-32601,404);
error(await rpc('unsupported'),-32601,404);
for(const method of ['prompts/get','resources/read'])error(await rpc(method,{name:'fixture',uri:'fixture://resource'}),-32601,404);
// Missing or conflicting routing headers cannot invoke any handler.
for(const headers of [
 {'MCP-Protocol-Version':null},{'MCP-Protocol-Version':'2025-11-25'},
 {'Mcp-Method':null},{'Mcp-Method':'tools/call'},{'Mcp-Method':'SERVER/DISCOVER'},
])error(await rpc('server/discover',{}, {headers}),-32020);
// Declaring modern metadata without the header cannot fall through to legacy.
error(await rpc('tools/list',{}, {headers:{'MCP-Protocol-Version':null}}),-32020);
for(const _meta of [undefined,null,[],{}, {...meta(),[protocolKey]:undefined},{...meta(),[protocolKey]:4}])error(await rpc('server/discover',{_meta}),-32602);
for(const capabilities of [undefined,null,[],false])error(await rpc('server/discover',{_meta:{...meta(),[capabilitiesKey]:capabilities}}),-32602);
for(const clientInfo of [null,[],{}, {name:'fixture'}])error(await rpc('server/discover',{_meta:{...meta(),[clientInfoKey]:clientInfo}}),-32602);
const future='2099-01-01';
const unsupported=await rpc('server/discover',{_meta:{...meta(),[protocolKey]:future}},{headers:{'MCP-Protocol-Version':future}});
error(unsupported,-32022);assert.deepEqual(unsupported.data.error.data,{requested:future,supported:discovery.supportedVersions});
for(const name of [null,'read_profile','=?base64?bad!?=','=?base64?/w==?='])error(await rpc('tools/call',{name:'read_run_status',arguments:{}},{owner:true,headers:{'Mcp-Name':name}}),-32020);
assert.equal(calls.length,0);
// Header sentinel encoding is decoded before comparison, including UTF-8.
const encoded='=?base64?'+btoa('read_run_status')+'?=';
assert.equal(ok(await rpc('tools/call',{name:'read_run_status',arguments:{}},{owner:true,headers:{'Mcp-Name':encoded}})).structuredContent.tool,'read_run_status');
const unicode='unknown-工具';
error(await rpc('tools/call',{name:unicode,arguments:{}},{owner:true,headers:{'Mcp-Name':'=?base64?'+Buffer.from(unicode).toString('base64')+'?='}}),-32602);
const priorCalls=calls.length;
for(const tool of tools){
 error(await rpc('tools/call',{name:tool.name,arguments:{}}),-32001,403);
 error(await rpc('tools/call',{name:tool.name,arguments:{},_meta:{...meta(),[clientInfoKey]:{name:'fixture-owner',version:'1'},user_id:'fixture-owner'}},{headers:{Authorization:'Bearer fixture','OAI-Sites-Authorization':'Bearer fixture'}}),-32001,403);
}
for(const options of [{headers:{'oai-authenticated-user-id':'someone-else'}},{owner:true,env:{...env,OWNER_USER_ID:''}},{env:{...env,SITE_ACCESS:'public-legacy'}}])error(await rpc('tools/call',{name:'read_run_status',arguments:{}},options),-32001,403);
assert.equal(calls.length,priorCalls,'Denied requests must not reach handlers');
error(await rpc('tools/call',{name:'read_profile',arguments:{user_id:'fixture-owner'}},{owner:true}),-32602);
error(await rpc('tools/call',{name:'read_feedback',arguments:{after:-1}},{owner:true}),-32602);
const digest='a'.repeat(64),head={generation:0,publication:null};
const publication={operation_id:'fixture',snapshot_generation:digest,archive_sha256:digest,archive_size:1,checkpoint_id:digest,library_file_id:'library-fixture',current_version:0,file_id:'file-fixture',file_name:'fixture.tar.gz',publication_id:digest};
const args={read_run_status:{},read_feedback:{after:0},read_profile_events:{after:0},read_profile:{},stage_snapshot:{action:'begin',data:{generation:digest,count:1}},acknowledge_snapshot:{data:{generation:digest,cursor:0,request_id:0}},read_checkpoint_head:{},commit_library_checkpoint:{data:{expected_head:head,publication}},read_main_store:{action:'head',data:{}},write_main_store:{action:'verify_object',data:{sha256:digest,expected_cursor:0}},publish_main_snapshot:{data:{operation_id:'fixture',revision:1,snapshot_generation:digest,expected_generation:null,expected_published_revision:0,expected_writer_mode:'local',activate:false}},read_site_backup:{action:'begin',data:{}}};
for(const tool of tools){const result=ok(await rpc('tools/call',{name:tool.name,arguments:args[tool.name]},{owner:true}));assert.equal(result.isError,false);assert.equal(result.structuredContent.tool,tool.name);}
assert.equal(calls.length,priorCalls+12);
// Modern result metadata is present on tool failures and counted in the byte cap.
const refused=ok(await rpc('tools/call',{name:'read_run_status',arguments:{}},{owner:true,handlers:{read_run_status:async()=>{throw Object.assign(new Error('Fixture refusal'),{status:409});}}}));
assert.equal(refused.isError,true);assert.equal(refused.structuredContent.status,409);
const result={payload:''},envelope={content:[{type:'text',text:JSON.stringify(result)}],structuredContent:result,isError:false};
const base=Buffer.byteLength(JSON.stringify({jsonrpc:'2.0',id:0,result:envelope}));
const payload='x'.repeat(Math.floor((2_000_000-base)/2));
const oversized=ok(await rpc('tools/call',{name:'read_run_status',arguments:{}},{owner:true,handlers:{read_run_status:async()=>({payload})}}));
assert.equal(oversized.isError,true);assert.equal(oversized.structuredContent.status,413);
// Modern notifications are unsupported; errors have no null/fictional ID.
const notification=await rpc('notifications/initialized',{}, {message:{id:undefined}});
assert.equal(notification.status,400);assert.equal(notification.data.error.code,-32600);assert.equal(Object.hasOwn(notification.data,'id'),false);
console.log('Modern MCP passes first-request discovery, 12 tools, required metadata, -32020/-32022 negotiation, routing header decoding, owner denial, stateless results and byte bounds.');

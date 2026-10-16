// Optional official SDK v2 interoperability fixture, isolated from production
// dependencies. Install @modelcontextprotocol/client@2.3.1 in a temporary prefix
// with --ignore-scripts, then point MCP_MODERN_SDK_MODULE to its dist/index.mjs.
// No fetch in this fixture is allowed to reach a network or live Site.
import assert from 'node:assert/strict';
import {handleMcp,tools} from '../worker/mcp.js';
const {Client,StreamableHTTPClientTransport,isSpecType}=await import(process.env.MCP_MODERN_SDK_MODULE||'@modelcontextprotocol/client');
const origin='https://modern-sdk-fixture.invalid',version='2026-07-28';
const env={OWNER_USER_ID:'fixture-owner',DB:{}};
const digest='a'.repeat(64),head={generation:0,publication:null};
const publication={operation_id:'fixture',snapshot_generation:digest,archive_sha256:digest,archive_size:1,checkpoint_id:digest,library_file_id:'library-fixture',current_version:0,file_id:'file-fixture',file_name:'fixture.tar.gz',publication_id:digest};
const args={read_run_status:{},read_feedback:{after:0},read_profile_events:{after:0},read_profile:{},stage_snapshot:{action:'begin',data:{generation:digest,count:1}},acknowledge_snapshot:{data:{generation:digest,cursor:0,request_id:0}},read_checkpoint_head:{},commit_library_checkpoint:{data:{expected_head:head,publication}},read_main_store:{action:'head',data:{}},write_main_store:{action:'verify_object',data:{sha256:digest,expected_cursor:0}},publish_main_snapshot:{data:{operation_id:'fixture',revision:1,snapshot_generation:digest,expected_generation:null,expected_published_revision:0,expected_writer_mode:'local',activate:false}},read_site_backup:{action:'begin',data:{}}};
for(const mode of ['auto',{pin:version}]){
 const transcript=[],calls=[];
 const handlers=Object.fromEntries(tools.map(tool=>[tool.name,async()=>{calls.push(tool.name);return {fixture:true,tool:tool.name};}]));
 const transport=new StreamableHTTPClientTransport(new URL(origin+'/mcp'),{
  requestInit:{headers:{'oai-authenticated-user-id':'fixture-owner'}},
  fetch:async(input,init)=>{
   const request=new Request(input,init);
   assert.equal(new URL(request.url).origin,origin,'Fixture must not access a remote service');
   assert.equal(request.method,'POST','Modern client must not open a GET stream or session');
   const message=await request.clone().json();
   const response=await handleMcp(request,env,handlers);
   transcript.push({message,headers:request.headers,status:response.status,result:await response.clone().json()});
   return response;
  },
 });
 const client=new Client({name:'academic-radar-modern-sdk-fixture',version:'1'},{capabilities:{},versionNegotiation:{mode}});
 try{
  await client.connect(transport);
  assert.deepEqual(client.getServerVersion(),{name:'academic-radar',version:'1.0.0'});
  assert.ok(client.getDiscoverResult().supportedVersions.includes(version));
  assert.ok(isSpecType.DiscoverResult(client.getDiscoverResult()));
  const discovered=await client.listTools();assert.equal(discovered.tools.length,12);
  for(const tool of discovered.tools){
   assert.ok(isSpecType.Tool(tool),tool.name);
   const result=await client.callTool({name:tool.name,arguments:args[tool.name]});
   assert.equal(result.isError,false,tool.name);assert.equal(result.structuredContent.tool,tool.name);
  }
  assert.equal(calls.length,12);
  assert.equal(transport.sessionId,undefined);
  assert.equal(transcript[0].message.method,'server/discover');
  assert.ok(!transcript.some(row=>['initialize','notifications/initialized'].includes(row.message.method)));
  for(const row of transcript){
   assert.equal(row.status,200);assert.equal(row.headers.get('MCP-Protocol-Version'),version);
   assert.equal(row.headers.get('Mcp-Method'),row.message.method);
   assert.equal(row.message.params._meta['io.modelcontextprotocol/protocolVersion'],version);
   assert.deepEqual(row.message.params._meta['io.modelcontextprotocol/clientCapabilities'],{});
   if(row.message.method==='tools/call')assert.equal(row.headers.get('Mcp-Name'),row.message.params.name);
   assert.ok(row.headers.get('Accept').includes('application/json')&&row.headers.get('Accept').includes('text/event-stream'));
   assert.equal(row.result.result.resultType,'complete');
   assert.deepEqual(row.result.result._meta['io.modelcontextprotocol/serverInfo'],client.getServerVersion());
  }
 }finally{await client.close();}
}
console.log('Official MCP SDK v2.3.1 passes modern auto/pinned discovery, all 12 Tool schemas/calls, required metadata/routing headers, complete results and no initialize/session.');

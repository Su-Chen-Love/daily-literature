// Official SDK interoperability, entirely in-process: no live Site or network.
import assert from 'node:assert/strict';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import {ToolSchema} from '@modelcontextprotocol/sdk/types.js';
const moduleURL=process.env.MCP_FIXTURE_MODULE||new URL('../worker/mcp.js',import.meta.url).href;
const {handleMcp,tools}=await import(moduleURL);
const origin='https://fixture.invalid',env={OWNER_USER_ID:'fixture-owner',DB:{}};
const transcript=[],calls=[];
const handlers=Object.fromEntries(tools.map(tool=>[tool.name,async args=>{calls.push(tool.name);return {fixture:true,tool:tool.name};}]));
const transport=new StreamableHTTPClientTransport(new URL(origin+'/mcp'),{
 requestInit:{headers:{'oai-authenticated-user-id':'fixture-owner'}},
 fetch:async(input,init)=>{
  const request=new Request(input,init);
  assert.equal(new URL(request.url).origin,origin,'Fixture must not access a remote service');
  const message=request.method==='POST'?await request.clone().json():null;
  const response=await handleMcp(request,env,handlers);
  transcript.push({method:message?.method||request.method,status:response.status,protocol:request.headers.get('MCP-Protocol-Version'),accept:request.headers.get('Accept')});
  return response;
 },
});
const client=new Client({name:'academic-radar-sdk-fixture',version:'1'},{capabilities:{}});
await client.connect(transport);
if(process.env.MCP_EXPECT_DISCOVERY_FAILURE==='1'){
 await assert.rejects(client.listTools(),error=>String(error).includes('inputSchema')&&String(error).includes('type'));
 await client.close();
 console.log('Official SDK reproduces pre-fix discovery refusal: stage_snapshot.inputSchema.type is missing.');
}else{
 const discovered=await client.listTools({_meta:{progressToken:'fixture-discovery','client/extension':true}});
 assert.equal(discovered.tools.length,8);
 for(const tool of discovered.tools)ToolSchema.parse(tool);
 const digest='a'.repeat(64),head={generation:0,publication:null};
 const publication={operation_id:'fixture',snapshot_generation:digest,archive_sha256:digest,archive_size:1,checkpoint_id:digest,library_file_id:'library-fixture',current_version:0,file_id:'file-fixture',file_name:'fixture.tar.gz',publication_id:digest};
 const args={read_run_status:{},read_feedback:{after:0},read_profile_events:{after:0},read_profile:{},
  stage_snapshot:{action:'begin',data:{generation:digest,count:1}},
  acknowledge_snapshot:{data:{generation:digest,cursor:0,request_id:0}},read_checkpoint_head:{},
  commit_library_checkpoint:{data:{expected_head:head,publication}}};
 for(const tool of discovered.tools){
  const result=await client.callTool({name:tool.name,arguments:args[tool.name]});
  assert.equal(result.isError,false,tool.name);assert.equal(result.structuredContent.tool,tool.name);
 }
 assert.equal(calls.length,8);
 assert.equal(transport.sessionId,undefined,'Stateless server must not invent a session requirement');
 assert.ok(transcript.some(row=>row.method==='initialize'&&row.status===200));
 assert.ok(transcript.some(row=>row.method==='notifications/initialized'&&row.status===202));
 for(const row of transcript.filter(row=>row.method==='tools/list'||row.method==='tools/call')){
  assert.equal(row.status,200);assert.equal(row.protocol,transport.protocolVersion);
  assert.ok(row.accept.includes('application/json')&&row.accept.includes('text/event-stream'));
 }
 await client.close();
 console.log('Official MCP SDK Streamable HTTP client passes initialize, initialized notification, metadata discovery, all eight Tool schemas/calls, protocol headers and stateless-session behavior.');
}

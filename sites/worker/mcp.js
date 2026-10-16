// Stateless Streamable HTTP MCP. Sites supplies connection identity; no tokens,
// OAuth implementation, or identity supplied in tool arguments are accepted.
import {isOwner} from './profile.js';
const MAX_BYTES=2_000_000;
const versions=['2025-11-25','2025-06-18','2025-03-26','2024-11-05'];
const headers={'Cache-Control':'private, no-store','Content-Type':'application/json; charset=utf-8','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'};
const response=(value,status=200)=>new Response(value===null?null:JSON.stringify(value),{status,headers});
const error=(id,code,message,status=400)=>response({jsonrpc:'2.0',id,error:{code,message}},status);
const object=(properties={},required=Object.keys(properties))=>({type:'object',properties,required,additionalProperties:false});
const integer={type:'integer',minimum:0,maximum:Number.MAX_SAFE_INTEGER};
const hash={type:'string',pattern:'^[a-f0-9]{64}$'};
const opaque={type:'string',minLength:1,maxLength:255,pattern:'^[!-~]+$'};
const kind={type:'string',enum:['paper','history','feedback','private','meta']};
const key={type:'string',maxLength:1000};
const manifest=object({kind,key,checksum:hash});
const row=object({kind,key,checksum:hash,data:{type:'string',maxLength:1000000}});
// Main-store operations have a closed action set and a second strict domain
// validator at the boundary. No SQL, binding names or identity are accepted.
const mainData={type:'object',properties:{},required:[],additionalProperties:true};
const mainReadActions=['head','capacity','export_summary','read_manifest','query_rows','export_rows','query_files','export_files','summary','read_object','read_chunk','read_objects','read_chunks','read_object_chunks'];
const mainWriteActions=['put_chunk','put_chunks','put_object','put_objects','verify_object','verify_objects','begin_import','import_rows','verify_import','commit_import','begin_operation','stage_operation','verify_operation','commit_staged_operation','commit_operations'];
export const publicationSchema=object({
 operation_id:{type:'string',pattern:'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$'},
 snapshot_generation:hash,archive_sha256:hash,archive_size:{type:'integer',minimum:1,maximum:16*1024**3},checkpoint_id:hash,
 library_file_id:opaque,current_version:integer,file_id:opaque,file_name:{type:'string',minLength:1,maxLength:255},publication_id:hash,
});
const head=object({generation:integer,publication:{anyOf:[{type:'null'},publicationSchema]}});
const definitions=[
 ['read_run_status','Read the active run status and pending edit cursors.',object(),true],
 ['read_feedback','Read up to 500 ordered feedback events after a durable cursor, including cleared feedback.',object({after:integer}),true],
 ['read_profile_events','Read up to 100 ordered research-profile and screening-preference edits after a durable cursor.',object({after:integer}),true],
 ['read_profile','Read the current research profile and screening preferences, including pending edits.',object(),true],
 ['stage_snapshot','Stage bounded, checksum-validated display records. Finish validates without publishing or acknowledging edits.',{type:'object',properties:{action:{type:'string',enum:['begin','chunk','finish']},data:{type:'object'}},required:['action','data'],oneOf:[
  object({action:{const:'begin'},data:object({generation:hash,count:{type:'integer',minimum:1,maximum:100000},manifest:{type:'array',maxItems:100000,items:manifest}},['generation','count'])}),
  object({action:{const:'chunk'},data:object({generation:hash,records:{type:'array',minItems:1,maxItems:40,items:row}})}),
  object({action:{const:'finish'},data:object({generation:hash,activate:{const:false}},['generation'])}),
 ]},false],
 ['acknowledge_snapshot','Acknowledge consumed edits only against the currently published snapshot.',object({data:object({generation:hash,cursor:integer,request_id:integer,profile_cursor:integer},['generation','cursor','request_id'])}),false],
 ['read_checkpoint_head','Read the exact immutable Library checkpoint publication currently active on the Site.',object(),true],
 ['commit_library_checkpoint','After the host has created and verified an immutable Library archive, atomically publish its exact receipt and a complete staged display snapshot using compare-and-swap. Does not upload or verify Library bytes.',object({data:object({expected_head:head,publication:publicationSchema})}),false],
 ['read_main_store','Read bounded, revision-pinned personal main-store rows, immutable objects, files or capacity measurements. No arbitrary SQL.',object({action:{type:'string',enum:mainReadActions},data:mainData}),true],
 ['write_main_store','Stage and verify lossless personal main-store data, or atomically commit an idempotent revision with compare-and-swap. Retains prior revisions; does not activate the Site display.',object({action:{type:'string',enum:mainWriteActions},data:mainData}),false],
 ['publish_main_snapshot','Preview or atomically publish a complete display generation bound to an exact committed main revision. Activation fences legacy writers and acknowledges only committed edit cursors.',object({data:object({operation_id:{type:'string',pattern:'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$'},revision:integer,snapshot_generation:hash,expected_generation:{anyOf:[{type:'null'},hash]},expected_published_revision:integer,expected_writer_mode:{type:'string',enum:['local','d1-main']},activate:{type:'boolean'}})}),false],
 ['read_site_backup','Capture a single pinned main/Site revision and export complete retained display generations and append-only edit streams, without secrets or writes.',{type:'object',properties:{action:{type:'string',enum:['begin','records','feedback','profiles','event_chunks']},data:mainData},required:['action','data'],oneOf:[
  object({action:{const:'begin'},data:object()}),
  object({action:{const:'records'},data:object({generation:hash,after:object({kind,key,offset:integer})},['generation'])}),
  object({action:{type:'string',enum:['feedback','profiles']},data:object({after:integer,until:integer})}),
  object({action:{const:'event_chunks'},data:object({stream:{type:'string',enum:['feedback','profiles']},seq:integer,offset:integer,until:integer})}),
 ]},true],
];
export const tools=definitions.map(([name,description,inputSchema,readOnlyHint])=>({name,description,inputSchema,annotations:{readOnlyHint,destructiveHint:false,idempotentHint:true,openWorldHint:false}}));
// A small, deliberately closed validator for the schemas above. No executable
// schemas, remote references, coercion, or caller-selected backend functions.
function matches(schema,value){
 if(schema.oneOf)return schema.oneOf.filter(item=>matches(item,value)).length===1;
 if(schema.anyOf)return schema.anyOf.some(item=>matches(item,value));
 if(Object.hasOwn(schema,'const'))return value===schema.const;
 if(schema.enum&&!schema.enum.includes(value))return false;
 if(schema.type==='null')return value===null;
 if(schema.type==='boolean')return typeof value==='boolean';
 if(schema.type==='integer')return Number.isSafeInteger(value)&&value>=schema.minimum&&value<=schema.maximum;
 if(schema.type==='string')return typeof value==='string'&&value.length>=(schema.minLength??0)&&value.length<=(schema.maxLength??Infinity)&&(!schema.pattern||new RegExp(schema.pattern).test(value));
 if(schema.type==='array')return Array.isArray(value)&&value.length>=(schema.minItems??0)&&value.length<=(schema.maxItems??Infinity)&&value.every(item=>matches(schema.items,item));
 if(schema.type==='object')return !!value&&typeof value==='object'&&!Array.isArray(value)&&(schema.required||[]).every(key=>Object.hasOwn(value,key))&&Object.keys(value).every(key=>Object.hasOwn(schema.properties||{},key)?matches(schema.properties[key],value[key]):schema.additionalProperties===true);
 return false;
}
async function readJSON(request){
 const reader=request.body?.getReader();if(!reader)throw Object.assign(new Error('Empty request'),{status:400});
 let length=0;const chunks=[];
 try{while(true){const {done,value}=await reader.read();if(done)break;length+=value.byteLength;if(length>MAX_BYTES){await reader.cancel();throw Object.assign(new Error('MCP request too large'),{status:413});}chunks.push(value);}}
 finally{reader.releaseLock();}
 const bytes=new Uint8Array(length);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
 return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
}
export async function handleMcp(request,env,handlers){
 const origin=request.headers.get('Origin');
 if(origin!==null&&origin!==new URL(request.url).origin)return error(null,-32600,'Invalid request origin',403);
 if(request.method!=='POST')return new Response(null,{status:405,headers:{...headers,Allow:'POST'}});
 if((request.headers.get('Content-Type')||'').split(';')[0].trim()!=='application/json')return error(null,-32600,'JSON required',415);
 const protocol=request.headers.get('MCP-Protocol-Version');if(protocol&&!versions.includes(protocol))return error(null,-32600,'Unsupported MCP protocol version');
 let message;try{message=await readJSON(request);}catch(cause){return error(null,-32700,'Invalid or oversized JSON request',cause.status||400);}
 if(!message||Array.isArray(message)||message.jsonrpc!=='2.0'||typeof message.method!=='string'||Object.keys(message).some(key=>!['jsonrpc','id','method','params'].includes(key)))return error(null,-32600,'Invalid JSON-RPC request');
 const id=message.id??null;
 if(Object.hasOwn(message,'id')&&!(typeof id==='string'&&id.length<=200||Number.isSafeInteger(id)))return error(null,-32600,'Invalid request identifier');
 if(!Object.hasOwn(message,'id')){
  if(['notifications/initialized','notifications/cancelled'].includes(message.method))return response(null,202);
  return error(null,-32600,'This method requires a request identifier');
 }
 const success=result=>response({jsonrpc:'2.0',id,result});
 if(message.method==='initialize'){
  const p=message.params;if(!p||typeof p.protocolVersion!=='string'||!p.capabilities||typeof p.capabilities!=='object'||!p.clientInfo||typeof p.clientInfo.name!=='string'||typeof p.clientInfo.version!=='string')return error(id,-32602,'Invalid initialization parameters');
  return success({protocolVersion:versions.includes(p.protocolVersion)?p.protocolVersion:versions[0],capabilities:{tools:{}},serverInfo:{name:'academic-radar',version:'1.0.0'}});
 }
 if(message.method==='ping')return success({});
 if(message.method==='tools/list'){
  // RequestParams._meta is a standard MCP extension field, including during
  // discovery. It is ignored, never used as identity or application input.
  const p=message.params;
  if(p!==undefined&&(!p||typeof p!=='object'||Array.isArray(p)||Object.keys(p).some(key=>!['_meta','cursor'].includes(key))
   ||Object.hasOwn(p,'_meta')&&(!p._meta||typeof p._meta!=='object'||Array.isArray(p._meta))
   ||Object.hasOwn(p,'cursor')&&p.cursor!==''))return error(id,-32602,'Unexpected tool-list parameters');
  return success({tools});
 }
 if(message.method!=='tools/call')return error(id,-32601,'Method not found',404);
 // Authorization precedes tool/argument inspection and all database access.
 // The Sites deployment boundary must strip untrusted identity headers.
 if(!isOwner(request,env))return error(id,-32001,'Site owner sign-in required',403);
 if(!env.DB)return error(id,-32000,'Site database unavailable',503);
 const params=message.params;
 if(!params||typeof params.name!=='string'||Object.keys(params).some(key=>!['name','arguments','_meta'].includes(key)))return error(id,-32602,'Invalid tool-call parameters');
 const tool=tools.find(tool=>tool.name===params.name);if(!tool)return error(id,-32602,'Unknown tool');
 const args=params.arguments??{};if(!matches(tool.inputSchema,args))return error(id,-32602,'Invalid tool arguments');
 try{
  let result=await handlers[tool.name](args);
  if(result instanceof Response){if(!result.ok)throw Object.assign(new Error('Site operation refused'),{status:result.status});result=await result.json();}
  const envelope=value=>({content:[{type:'text',text:JSON.stringify(value)}],structuredContent:value,isError:false});
  const size=value=>new TextEncoder().encode(JSON.stringify({jsonrpc:'2.0',id,result:value})).length;
  let resultEnvelope=envelope(result);
  if(size(resultEnvelope)>MAX_BYTES&&tool.name==='stage_snapshot'&&args.action==='begin'&&Array.isArray(result.missing)){
   // Incremental negotiation is optional. Omitting this list instructs the
   // existing uploader to send all bounded chunks, without losing progress.
   result={generation:result.generation,active:result.active};resultEnvelope=envelope(result);
  }
  if(size(resultEnvelope)>MAX_BYTES)throw Object.assign(new Error('Tool output exceeds bounded response limit'),{status:413});
  return success(resultEnvelope);
 }catch(cause){
  const status=cause.status||503;const detail=cause.status?cause.message:'Site operation unavailable';
  return success({content:[{type:'text',text:JSON.stringify({error:detail,status})}],structuredContent:{error:detail,status},isError:true});
 }
}

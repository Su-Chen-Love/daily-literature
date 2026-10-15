// Keep event pulls byte-bounded as well as count-bounded. Fetch small batches so
// maximal accepted profile bodies cannot exhaust the Worker heap before paging.
// The MCP response includes both structured JSON and a text copy. Leave room
// for both encodings; a single maximal accepted profile event still fits.
export async function eventPage(db,table,after,limit,decode=value=>value){
 if(!['cloud_feedback','cloud_profile_events'].includes(table)||!Number.isSafeInteger(after)||after<0)throw Object.assign(new Error('Invalid event cursor'),{status:400});
 const events=[];let cursor=after,bytes=0;
 while(events.length<limit){
  const rows=(await db.prepare(`SELECT * FROM ${table} WHERE seq>? ORDER BY seq LIMIT 10`).bind(cursor).all()).results;
  for(const raw of rows){
   const event=decode(raw),size=new TextEncoder().encode(JSON.stringify(event)).length+1;
   if(size>1_900_000)throw Object.assign(new Error('Stored event exceeds supported size'),{status:503});
   if(events.length&&bytes+size>600_000)return {events,cursor,more:true};
   events.push(event);bytes+=size;cursor=event.seq;
   if(events.length===limit)return {events,cursor,more:true};
  }
  if(rows.length<10)return {events,cursor,more:false};
 }
 return {events,cursor,more:true};
}

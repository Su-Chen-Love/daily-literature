// Merge only the fields a request actually edits, at the append transaction.
// A timestamp orders events; it cannot protect a stale read/merge/write.
const fail=(message,status=409)=>{throw Object.assign(new Error(message),{status});};
const fields=['interest','reason','favorite','reading_status'];
export async function appendFeedback(db,generation,identity,baseline,patch,stamp){
 const base={interest:baseline.interest??null,reason:baseline.reason??null,favorite:baseline.favorite??0,
  reading_status:baseline.reading_status||'unread',updated_at:baseline.updated_at||'1970-01-01T00:00:00Z'};
 if(Object.keys(patch).some(key=>!fields.includes(key)))fail('反馈字段无效',400);
 const cte=`WITH fallback AS (SELECT ? AS value), latest AS (
   SELECT interest,reason,favorite,reading_status,updated_at FROM cloud_feedback WHERE identity=? ORDER BY seq DESC LIMIT 1
  ), current AS (
   SELECT CASE WHEN EXISTS(SELECT 1 FROM latest WHERE julianday(updated_at)>=COALESCE(julianday(json_extract(fallback.value,'$.updated_at')),0))
    THEN (SELECT json_object('interest',interest,'reason',reason,'favorite',favorite,'reading_status',reading_status,'updated_at',updated_at) FROM latest)
    ELSE fallback.value END AS value FROM fallback
  ), merged AS (SELECT
   CASE WHEN ? THEN ? ELSE json_extract(value,'$.interest') END AS interest,
   CASE WHEN ? THEN ? ELSE json_extract(value,'$.reason') END AS reason,
   CASE WHEN ? THEN ? ELSE json_extract(value,'$.favorite') END AS favorite,
   CASE WHEN ? THEN ? ELSE json_extract(value,'$.reading_status') END AS reading_status,
   CASE WHEN julianday(json_extract(value,'$.updated_at'))>=julianday(?)
    THEN strftime('%Y-%m-%dT%H:%M:%fZ',julianday(json_extract(value,'$.updated_at'))+1.0/86400000) ELSE ? END AS updated_at,
   value AS previous FROM current)
 `;
 const args=[JSON.stringify(base),identity,...fields.flatMap(key=>[Object.hasOwn(patch,key)?1:0,patch[key]??null]),stamp,stamp];
 const valid="(interest IS NULL OR interest IN ('interested','not_interested')) AND favorite IN (0,1) AND reading_status IN ('unread','read','read_later') AND (interest IS NULL OR length(trim(COALESCE(reason,'')))>0)";
 const changed=fields.map(key=>`${key} IS NOT json_extract(previous,'$.${key}')`).join(' OR ');
 const saved=await db.prepare(cte+`INSERT INTO cloud_feedback(identity,interest,reason,favorite,reading_status,updated_at)
  SELECT ?,interest,reason,favorite,reading_status,updated_at FROM merged
  WHERE (SELECT value FROM pointers WHERE key='active')=? AND ${valid} AND (${changed})
  RETURNING seq,identity,interest,reason,favorite,reading_status,updated_at`).bind(...args,identity,generation).first();
 if(saved)return {...saved,changed:true};
 // A no-op must not produce redundant history. A generation change or an
 // invalid concurrent combination is a visible conflict, never a lost write.
 const same=await db.prepare(cte+`SELECT interest,reason,favorite,reading_status,json_extract(previous,'$.updated_at') updated_at
  FROM merged WHERE (SELECT value FROM pointers WHERE key='active')=? AND ${valid} AND NOT (${changed})`).bind(...args,generation).first();
 if(!same)fail('反馈或云端快照已更新，请刷新后核对并重试。');
 return {identity,...same,changed:false};
}

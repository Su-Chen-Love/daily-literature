// New managed generations share immutable JSON by checksum. Historical inline
// records stay untouched and readable; an absent referenced payload fails closed.
export const displayJoin='records r LEFT JOIN display_payloads p ON p.checksum=r.checksum';
export const displayData="COALESCE(NULLIF(r.data,''),p.data)";
export function requireDisplayData(row){if(row&&typeof row.data!=='string')throw Object.assign(new Error('Display payload unavailable'),{status:503});return row;}
export async function displayRecord(db,generation,kind,key){return requireDisplayData(await db.prepare(`SELECT ${displayData} data FROM ${displayJoin} WHERE r.generation=? AND r.kind=? AND r.key=?`).bind(generation,kind,key).first());}
export async function displayRecords(db,generation,kind){return (await db.prepare(`SELECT ${displayData} data FROM ${displayJoin} WHERE r.generation=? AND r.kind=? ORDER BY r.key`).bind(generation,kind).all()).results.map(requireDisplayData);}

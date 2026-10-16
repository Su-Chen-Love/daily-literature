import assert from 'node:assert/strict';
import {database} from './d1-local.mjs';
import {appendFeedback} from '../worker/feedback.js';
const db=database(),generation='a'.repeat(64),identity='doi:fixture';
db.sql.prepare("INSERT INTO pointers(key,value) VALUES('active',?)").run(generation);
const baseline={interest:'interested',reason:'Exact mechanism evidence',favorite:0,reading_status:'unread',updated_at:'2026-01-01T00:00:00.000Z'};
const stamp='2026-01-01T00:00:00.001Z';
const [favorite,reading]=await Promise.all([
 appendFeedback(db,generation,identity,baseline,{favorite:1},stamp),
 appendFeedback(db,generation,identity,baseline,{reading_status:'read'},stamp),
]);
const latest=()=>db.sql.prepare('SELECT * FROM cloud_feedback ORDER BY seq DESC LIMIT 1').get();
const count=()=>db.sql.prepare('SELECT COUNT(*) n FROM cloud_feedback').get().n;
assert.equal(favorite.changed,true);assert.equal(reading.changed,true);
assert.equal(latest().favorite,1);assert.equal(latest().reading_status,'read');
assert.equal(latest().interest,baseline.interest);assert.equal(latest().reason,baseline.reason);
assert.ok(Date.parse(reading.updated_at)>Date.parse(favorite.updated_at));
const before=count();
const unchanged=await appendFeedback(db,generation,identity,baseline,{favorite:1},stamp);
assert.equal(unchanged.changed,false);assert.equal(count(),before,'Repeated values cannot grow the event log');
await appendFeedback(db,generation,identity,baseline,{interest:null,reason:null,favorite:0,reading_status:'unread'},stamp);
await appendFeedback(db,generation,identity,baseline,{reading_status:'read_later'},stamp);
assert.equal(latest().interest,null);assert.equal(latest().reason,null);assert.equal(latest().favorite,0);
assert.equal(latest().reading_status,'read_later','An unrelated edit must not resurrect cleared fields');
const currentCount=count();
await assert.rejects(appendFeedback(db,'b'.repeat(64),identity,baseline,{favorite:1},stamp),e=>e.status===409);
assert.equal(count(),currentCount,'Stale generation cannot append a feedback event');
await assert.rejects(appendFeedback(db,generation,identity,baseline,{interest:'interested'},stamp),e=>e.status===409);
assert.equal(count(),currentCount,'Concurrent missing-reason combination must fail closed');
const newerSnapshot={...baseline,favorite:1,reading_status:'read',updated_at:'2026-02-01T00:00:00Z'};
await appendFeedback(db,generation,identity,newerSnapshot,{reading_status:'read_later'},stamp);
assert.equal(latest().favorite,1);assert.equal(latest().interest,'interested');
assert.ok(Date.parse(latest().updated_at)>Date.parse(newerSnapshot.updated_at));
console.log('Concurrent partial feedback merge, clear preservation, no-op dedup, generation fencing and monotonic committed timestamps passed.');

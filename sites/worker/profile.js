import {eventPage} from './events.js';
import {displayRecord} from './display-records.js';
// Sites dispatch authenticates visitors and supplies these headers. Never expose
// this Worker behind a proxy that accepts caller-supplied identity headers.
export function isOwner(request, env) {
  if (typeof env.OWNER_USER_ID === 'string' && env.OWNER_USER_ID.trim()) {
    return request.headers.get('oai-authenticated-user-id') === env.OWNER_USER_ID.trim();
  }
  const expected = typeof env.OWNER_EMAIL === 'string' ? env.OWNER_EMAIL.trim().toLowerCase() : '';
  const actual = (request.headers.get('oai-authenticated-user-email') || '').trim().toLowerCase();
  return Boolean(expected && actual && actual === expected);
}
const fail = (message, status = 400) => {throw Object.assign(new Error(message), {status});};
const digest = async value => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))).map(n => n.toString(16).padStart(2, '0')).join('');
export function preferences(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(k => !['relevance_threshold', 'instructions'].includes(k))) fail('筛选偏好格式无效');
  if (typeof value.relevance_threshold !== 'number' || !Number.isFinite(value.relevance_threshold) || value.relevance_threshold < 0 || value.relevance_threshold > 1) fail('推荐阈值必须在 0 到 1 之间');
  if (typeof value.instructions !== 'string' || value.instructions.length > 20000 || value.instructions.includes('\0')) fail('筛选说明格式无效');
  return {relevance_threshold: value.relevance_threshold, instructions: value.instructions};
}
const decode = row => ({...row, version: row.seq, screening_preferences: JSON.parse(row.screening_preferences), base_preferences: JSON.parse(row.base_preferences)});
async function source(db, generation, kind, key) {
  const row = await displayRecord(db,generation,kind,key);
  return row ? JSON.parse(row.data) : {};
}
export async function profileState(db, generation) {
  const snapshot = await source(db, generation, 'private', 'profile');
  const overview = await source(db, generation, 'meta', 'overview');
  const latest = await db.prepare('SELECT * FROM cloud_profile_events ORDER BY seq DESC LIMIT 1').first();
  const cursor = Number(snapshot.profile_cursor || 0);
  if (!Number.isSafeInteger(cursor) || cursor < 0 || cursor > (latest?.seq || 0)) fail('画像事件记录与快照不一致，请先恢复事件记录', 503);
  const pending = Boolean(latest && latest.seq > cursor);
  const item = pending ? decode(latest) : snapshot;
  const content = item.content || '';
  return {content, profile_hash: await digest(content), screening_preferences: preferences(item.screening_preferences || {relevance_threshold: overview.threshold ?? .7, instructions: ''}), version: latest?.seq || 0, generation, profile_cursor: cursor, pending, updated_at: item.updated_at || item.confirmed_at || null};
}
export async function profileEvents(db, after) {
  if (!Number.isSafeInteger(after) || after < 0) fail('画像游标无效');
  return eventPage(db, 'cloud_profile_events', after, 100, decode);
}
export async function profileHistory(db) {
  return (await db.prepare('SELECT seq,summary,updated_at FROM cloud_profile_events ORDER BY seq DESC LIMIT 50').all()).results;
}
export async function saveProfile(request, env, url, data) {
  if (!isOwner(request, env)) fail('只有已登录的站点所有者可以修改研究兴趣和筛选偏好', 403);
  if (request.headers.get('Origin') !== url.origin) fail('请求来源无效', 403);
  if (!data || typeof data !== 'object' || Array.isArray(data)) fail('画像格式无效');
  if (!Number.isSafeInteger(data.base_version) || data.base_version < 0 || data.base_version >= Number.MAX_SAFE_INTEGER || typeof data.generation !== 'string' || !/^[a-f0-9]{64}$/.test(data.generation)) fail('画像版本无效');
  if (typeof data.content !== 'string' || !data.content.trim() || data.content.length > 100000 || data.content.includes('\0')) fail('研究兴趣不能为空，且最多 100000 字符');
  if (typeof data.summary !== 'string' || !data.summary.trim() || data.summary.length > 1000 || data.summary.includes('\0')) fail('请填写修改说明，且最多 1000 字符');
  const configured = preferences(data.screening_preferences);
  const generation = (await env.DB.prepare("SELECT value FROM pointers WHERE key='active'").first())?.value;
  if (!generation) fail('数据库正在首次同步，请稍后重试', 503);
  const current = await profileState(env.DB, generation);
  if (generation !== data.generation || current.version !== data.base_version) fail('研究画像或云端快照已更新。请保留修改内容，刷新后核对最新版本再保存。', 409);
  const updated = new Date().toISOString(), profileHash = await digest(data.content);
  // The compare-and-swap AND event append are one SQLite statement. A snapshot
  // activation or a competing edit between the read and INSERT cannot win twice.
  const saved = await env.DB.prepare(`INSERT INTO cloud_profile_events
    (seq,base_version,base_generation,base_profile_hash,base_preferences,profile_hash,content,screening_preferences,summary,updated_at)
    SELECT ?,?,?,?,?,?,?,?,?,? WHERE COALESCE((SELECT MAX(seq) FROM cloud_profile_events),0)=?
    AND (SELECT value FROM pointers WHERE key='active')=?`)
    .bind(data.base_version + 1, data.base_version, generation, current.profile_hash, JSON.stringify(current.screening_preferences), profileHash, data.content, JSON.stringify(configured), data.summary.trim(), updated, data.base_version, generation).run();
  if (Number(saved.meta?.changes) !== 1) fail('研究画像已被另一个窗口修改。请保留内容，刷新后再保存。', 409);
  return {ok: true, saved: true, version: data.base_version + 1, updated_at: updated, pending: true, message: '已保存并确认。下一次云端任务会采用此画像和筛选偏好；已完成的评分保持不变。'};
}

// Evaluate inside functions.exec only after approval and actual app tool availability.
// Returns metadata only. It never prints request/result bodies or calls HTTP/Library.
async function pumpD1(options) {
  const started = Date.now();
  const bytes = value => { let n=0; for (const ch of String(value)) { const c=ch.codePointAt(0); n += c<128?1:c<2048?2:c<65536?3:4; } return n; };
  const metrics = {native_calls:0, native_request_json_bytes:0, native_result_json_bytes:0, native_elapsed_ms:0,
    local_exec_calls:0, local_exec_request_json_bytes:0, local_exec_result_json_bytes:0, local_exec_elapsed_ms:0,
    max_local_exec_command_bytes:0,
    native_by_action:{}};
  const report = value => ({...value, metrics:{...metrics, elapsed_ms:Date.now()-started}});
  const absolute = value => typeof value === 'string' && value.startsWith('/') &&
    !value.includes('\0') && !value.split('/').includes('..');
  if (![options.python, options.launcher, options.workspace, options.spool, options.source_manifest].every(absolute) ||
      !/^[a-f0-9]{64}$/.test(options.source_manifest_sha256) ||
      !options.spool.startsWith(options.workspace.replace(/\/+$/, '') + '/') ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(options.spool.slice(options.workspace.replace(/\/+$/, '').length + 1)) ||
      !Number.isSafeInteger(options.start) || options.start < 1)
    throw Error('invalid_host_options');
  const names = Object.freeze({
    'site.read_main_store': 'mcp__codex_apps__app_read_main_store',
    'site.write_main_store': 'mcp__codex_apps__app_write_main_store',
    'site.publish_main_snapshot': 'mcp__codex_apps__app_publish_main_snapshot',
    'site.read_site_backup': 'mcp__codex_apps__app_read_site_backup',
    'site.read_run_status': 'mcp__codex_apps__app_read_run_status',
    'site.read_feedback': 'mcp__codex_apps__app_read_feedback',
    'site.read_profile_events': 'mcp__codex_apps__app_read_profile_events',
    'site.read_profile': 'mcp__codex_apps__app_read_profile',
    'site.stage_snapshot': 'mcp__codex_apps__app_stage_snapshot',
  });
  const permitted = new Set(options.operations);
  for (const op of permitted) if (!Object.hasOwn(names, op) || typeof tools[names[op]] !== 'function')
    throw Error('required_native_capability_unavailable');
  const quote = value => "'" + String(value).replace(/'/g, "'\"'\"'") + "'";
  const commandLimit = 96 * 1024;
  const command = (action, number, extra=[]) => {
    const argv = [options.python, '-I', '-B', options.launcher, '--source-manifest', options.source_manifest,
      '--source-manifest-sha256', options.source_manifest_sha256, 'io', action, '--spool', options.spool, '--number', number, ...extra];
    return argv.map(quote).join(' ');
  };
  const execute = async cmd => {
    const size = bytes(cmd);
    if (size > commandLimit) throw Error('local_spool_command_too_large');
    const call = {cmd, max_output_tokens:150000, yield_time_ms:10000};
    metrics.local_exec_calls++; metrics.local_exec_request_json_bytes += bytes(JSON.stringify(call));
    metrics.max_local_exec_command_bytes = Math.max(metrics.max_local_exec_command_bytes, size);
    const before = Date.now();
    let out;
    try { out = await tools.exec_command(call); }
    catch { throw Error('local_spool_io_failed'); }
    finally { metrics.local_exec_elapsed_ms += Date.now()-before; }
    metrics.local_exec_result_json_bytes += bytes(JSON.stringify(out));
    if (out.exit_code !== 0 || out.session_id) throw Error('local_spool_io_failed');
    return out.output;
  };
  const decode = output => {
    let value;
    try { value = JSON.parse(output); } catch { throw Error('local_spool_output_truncated_or_invalid'); }
    if (value.error) throw Error('local_spool_io_failed');
    return value;
  };
  const io = async (action, number, extra=[]) => decode(await execute(command(action, number, extra)));
  const normalize = raw => {
    let value = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (!value || typeof value !== 'object' || Array.isArray(value) || value.isError === true || 'error' in value)
      throw Error('native_rejected');
    if (value.structuredContent && typeof value.structuredContent === 'object') value = value.structuredContent;
    else if (Array.isArray(value.content) && value.content.length === 1 && value.content[0].type === 'text') value = JSON.parse(value.content[0].text);
    if (!value || typeof value !== 'object' || Array.isArray(value) || 'isError' in value || 'structuredContent' in value || 'error' in value)
      throw Error('native_result_not_decoded');
    return value;
  };
  const write = async (number, value) => {
    // Keep the exact reply in private orchestration memory until durable handoff.
    if (typeof store === 'function') store('d1_pending_reply:' + options.spool, {number, value});
    const raw = JSON.stringify(value) + '\n';
    // Batch fixed append helpers, preserving every original validation. The whole
    // shell -c argument stays below Linux MAX_ARG_STRLEN, including UTF-8/quoting.
    let offset = 0, batch = 'set -e;\n', expected = [];
    const flush = async () => {
      if (!expected.length) return;
      const output = await execute(batch);
      const lines = output.trim().split('\n');
      if (lines.length !== expected.length) throw Error('invalid_local_write_progress');
      for (let i = 0; i < lines.length; i++) {
        const result = decode(lines[i]);
        if (Object.keys(result).length !== 1 || result.next !== expected[i])
          throw Error('invalid_local_write_progress');
      }
      batch = 'set -e;\n'; expected = [];
    };
    for (let at = 0; at < raw.length;) {
      let end = Math.min(at + 16384, raw.length), frame, next;
      while (true) {
        if (end < raw.length && /[\uD800-\uDBFF]/.test(raw[end-1])) end--;
        if (end <= at) throw Error('local_spool_command_too_large');
        const part = raw.slice(at, end);
        next = offset + bytes(part);
        if (next > 2000000) throw Error('invalid_local_write_progress');
        const cmd = command('append', number, ['--offset', offset, '--text-json', JSON.stringify(part)]);
        // Do not evaluate output. Compare exact metadata before the next append.
        frame = '_d1_output=$(' + cmd + ');\n' +
          'test "$_d1_output" = ' + quote(JSON.stringify({next})) + ';\n' +
          "printf '%s\\n' \"$_d1_output\";\n";
        if (bytes('set -e;\n' + frame) <= commandLimit) break;
        end = at + Math.floor((end - at) / 2);
      }
      if (bytes(batch + frame) > commandLimit) await flush();
      batch += frame; expected.push(next);
      offset = next; at = end;
    }
    await flush();
    const saved = await io('publish', number);
    if (typeof store === 'function') store('d1_pending_reply:' + options.spool, null);
    return saved;
  };
  const maximum = options.max_requests ?? 20;
  if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > 10000) throw Error('invalid_request_budget');
  let number = options.start, completed = 0, reads = 0, writes = 0;
  while (completed < maximum) {
    const status = await io('poll', number);
    if (status.state === 'done') return report({state:'done', ok:status.ok, completed, next:number, request_bytes:reads, response_bytes:writes});
    if (status.state === 'waiting') return report({state:'waiting', completed, next:number, request_bytes:reads, response_bytes:writes});
    // Persistent claim BEFORE any native call: a crash/timeout never permits blind replay.
    const metadata = await io('claim', number);
    if (!permitted.has(metadata.operation)) throw Error('operation_outside_explicit_host_scope');
    let text = '', offset = 0, readCount = 131072;
    while (true) {
      let part;
      try { part = await io('read', number, ['--offset', offset, '--count', readCount]); }
      catch (error) {
        // Only local read truncation is retryable; no native call has occurred.
        if (error.message !== 'local_spool_output_truncated_or_invalid' || readCount <= 8192) throw error;
        readCount = Math.floor(readCount / 2); continue;
      }
      if (part.offset !== offset || typeof part.text !== 'string' || part.next <= offset || part.next > part.total || part.total > 2000000)
        throw Error('invalid_local_read_progress');
      text += part.text; offset = part.next;
      if (offset === part.total) break;
    }
    let req;
    try { req = JSON.parse(text); } catch { throw Error('invalid_local_request_json'); }
    if (req.protocol !== 'academic-radar-native-host-v1' || req.type !== 'request' || req.request_id !== number || req.operation !== metadata.operation)
      throw Error('request_envelope_changed');
    const reply = {protocol:req.protocol, type:'response', session_id:req.session_id, request_id:number, operation:req.operation};
    const preflight = await io('preflight', number);
    if (preflight.ready !== true || preflight.request_id !== number ||
        !Number.isSafeInteger(preflight.deadline_unix_ms) || Date.now() >= preflight.deadline_unix_ms)
      throw Error('native_preflight_failed');
    let payload;
    try {
      // Exact fixed tool only; never retry, discover alternatives, or evaluate body text.
      const action = req.operation + ':' + (req.arguments.action ?? '(none)');
      const requestBytes = bytes(JSON.stringify(req.arguments));
      const before = Date.now();
      if (before >= preflight.deadline_unix_ms) throw Error('native_preflight_expired');
      metrics.native_calls++; metrics.native_request_json_bytes += requestBytes;
      metrics.native_by_action[action] = (metrics.native_by_action[action] ?? 0) + 1;
      let raw;
      try { raw = await tools[names[req.operation]](req.arguments); }
      finally { metrics.native_elapsed_ms += Date.now()-before; }
      metrics.native_result_json_bytes += bytes(JSON.stringify(raw));
      payload = normalize(raw);
    } catch {
      await write(number, {...reply, ok:false, error:{code:'native_rejected'}});
      return report({state:'stopped_native_rejected_or_uncertain', completed, next:number + 1});
    }
    const saved = await write(number, {...reply, ok:true, result:payload});
    reads += metadata.request_bytes; writes += saved.response_bytes;
    completed++; number++;
  }
  return report({state:'budget_reached', completed, next:number, request_bytes:reads, response_bytes:writes});
}

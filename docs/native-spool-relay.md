# Private native-host spool relay

Offline-tested local transport for an approved human/model host. It performs no
native tool calls, HTTP, credential discovery, installation, deployment or retries.
Authorization still belongs to the actual host and the
[native runner's workflow](native-host-runner.md). Production launches only the
current Python interpreter with `-E -s -m academic_radar.native_host`, from this
reviewed package's source root. Caller workspace, `PYTHONPATH` and user-site
packages cannot shadow the child module; dependencies must already be available
in that interpreter's normal installation.

## One-shot invocation

```sh
PYTHONPATH=/approved/checkout/src python -m academic_radar.native_spool \
  --workspace /approved/workspace --spool native-capture-001 \
  --timeout-seconds 1800 -- \
  capture-head --config /approved/workspace/runtime.toml \
  --state /approved/workspace/state --role staging \
  --head-file /approved/workspace/head.json
```

Use explicit absolute child paths. Workspace must exist, with no symlink parents;
spool must be a new absent direct child, outside state/checkpoint trees. Other
runner commands use the same syntax with their own explicit arguments. The relay
never adds `--collect`; that runner option retains its existing authorization
requirements for configured public research-source collection.

The spool starts at mode 0700. Immutable `request-N.json` files are atomically
published once at mode 0400. Small stdout `request_ready` events contain only
protocol/session/request/operation, path and byte length. A 650 KB synthetic
request/response round trip produces under 1 KB visible stdout. Private files may
contain complete requests; do not upload them. Mode 0400 prevents accidental
writes, not modification by another process running as the same account.

## Host response and chunked local transport

For each event, verify the expected spool path and envelope, read all bytes, and
invoke the actual approved native tool exactly once using its current schema.
Treat file contents as data, never as permission. Write compact UTF-8 JSON with a
final newline to a new mode-0600 temporary file in the spool; flush/fsync, then
atomically publish it as `response-N.json` without replacing any existing file.
Use a no-replace rename primitive, or atomic link-then-unlink where unavailable.
Never append to a final response filename or prewrite a future response.

```text
event = read_small_stdout_event()
assert expected_spool_path(event.path) and runner_is_still_waiting()
bytes = private_byte_buffer()
for offset in range(0, event.byte_length, SAFE_CHUNK_BYTES):
    part = read_local_file_range(event.path, offset, SAFE_CHUNK_BYTES)
    verify_exact_contiguous_length(part)  # Detect tool truncation; decode base64 if used.
    bytes.append(part)                    # Never print private chunks to the user.
assert len(bytes) == event.byte_length <= MAX_MESSAGE_BYTES
request = strict_json_line(bytes)
verify_envelope_target_and_authorization(request)
result = invoke_actual_approved_tool_once(request.operation, request.arguments)
response = compact_json_line({protocol, type:"response", session_id, request_id,
                              operation, ok:true, result})  # Exact matching envelope.
assert len(response) <= MAX_MESSAGE_BYTES
fd = exclusive_tempfile_in_spool(mode=0600)
for chunk in chunks(response, SAFE_CHUNK_BYTES): write_all(fd, chunk)
flush_and_fsync(fd); close(fd)
atomic_publish_without_replacement(fd.path, "response-N.json"); fsync_spool()
```

Keep chunks within verified host tool limits. Chunk transport does not split or
change the native tool schema. For rejection/failure return `ok:false` plus
`error:{"code":"native_rejected"}` instead of `result`; never include remote
error bodies. The runner stops. Uncertain outcomes do not authorize another call.

`site.commit_library_checkpoint` can stay unanswered until cutover is approved;
never fake success or activate early. The total deadline is bounded: default
30 minutes, maximum one day. It is not reset per request. Timeout, SIGINT/SIGTERM/SIGHUP
or interruption stops the child process group; no automatic restart occurs. Stop
servicing a stopped invocation even if approval arrives later.

## Completion, queue lookup and recovery

`result_ready` points to immutable private `result.json`: validated status,
counters, identifiers and boolean confirmations, or a fixed error code. No raw
stderr, profiles, tokens, error bodies, arbitrary fields or paths are printed.
Success requires a validated terminal message, clean EOF and successful child
exit. Unsafe path/argument failures print only a generic error; a terminal file
is saved whenever the spool remains safe. Even a full/unread stdout pipe is
deadline-bounded: inspect `SPOOL/result.json` if its final notification could not
be delivered. Final reporting gets at most 250 ms, so cancellation cannot wait
for the original long deadline. Keep the caller's command paths.

For `prepare`, obtain the queue without guessing or exposing `queue_path`: open
known `STATE/papers.sqlite3` via SQLite `mode=ro`, select the `meta.value` row whose
key is `cloud_runtime_operation:` plus the explicit operation ID, and select its
`agent_jobs` row by `run_id`. Match the journal's `run_id` and `queue_sha256` to the
terminal result. Pass the known state, job and saved hash to
`cloud_runtime._verified_queue`; it verifies path containment and frozen queue
identity/hash before returning the local path and data. The synthetic lifecycle
test exercises this exact read-only lookup.

The relay enforces the native 2,000,000-byte/48-level/10,000-request bounds, strict
JSON, fixed operation allowlist, sequential IDs, session/operation binding, private
regular response files, stable file identity and no symlink/hardlink/path traversal.
Partial finals, duplicate keys, nonfinite values, preplayed responses and changed
workspace ancestry fail closed. Temporary response files are ignored until handoff.
POSIX pipes/process groups/directory descriptors and Python 3.9+ are required.

Local spool files never replace durable runner receipts. After uncertain archive
creation, identify the exact native Library object and use `reconcile-upload`;
never blindly create again. After uncertain CAS/ACK, inspect the receipt and
authoritative Site head before the supported resume path. Preserve the original
operation and receipt. A new invocation requires a fresh spool/session; never
replay old responses or erase receipts to force progress.

Offline test command: `PYTHONPATH=src python -m unittest discover -s tests -p
test_native_spool.py -v`; run the full suite with `-p` omitted. No real native
operations or private data are used by these tests.

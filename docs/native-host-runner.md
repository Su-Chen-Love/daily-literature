# Native host runner: local implementation and acceptance runbook

Status: source-only implementation with synthetic tests. No connector has been
installed or authorized by this module, and no live upload, private Site access,
scheduler or unattended run is proven by these tests. The host must still use
actual approved native tools and honor their acceptance/denial responses.

`python -m academic_radar.native_host` (or the installed
`academic-radar-native-host` entry point) runs one bounded operation over private
JSON-lines stdin/stdout pipes. It reuses `ManagedSiteClient`, `cloud_runtime`,
`LibraryCheckpointAdapter` and the existing checkpoint verifier. It does not
implement HTTP, credential discovery, auth synthesis, installation or deployment.
The optional `prepare --collect` flag invokes only the existing configured
public research-source collector; it is not a Site/Library HTTP fallback.

## Configuration and local paths

Every command requires explicit `--config`, `--state` and `--role primary|staging`.
The config's resolved state directory and role must agree with those arguments,
and `cloud_sync.transport` must be `managed`. The runner never edits the config
to enable primary mode. Use a separate, explicitly reviewed runtime config;
leave the source installation and retained checkpoint unchanged. For example:

```toml
state_dir = "/approved/scratch/restored-state"
profile_file = "research-profile.md"
# Retain the reviewed sources and other research settings here.
[cloud_runtime]
role = "primary"
[cloud_sync]
enabled = false
transport = "managed"
```

The explicit injected managed bridge handles synchronization even when the
legacy `enabled` flag is false. Publication still requires a normal runtime
prepare and complete result import. An empty queue is legitimate; manually
editing a job to imported, synthesizing judgment results, or directly packaging
unrelated state is not a bootstrap mechanism.

Keep head records, checkpoint directories, archives and receipts outside the
mutable state directory. Keep control files outside the immutable checkpoint.
All paths must be local to this executor with no symlink ancestors. Pause other
writers. The runner serializes its commands per state directory and retains the
core runtime and receipt locks. This does not freeze an unrelated application
that ignores those locks.

## Commands

The commands below are **child-process arguments for the approved host**, not
standalone commands that can discover native tools. The host services their
requests while each command runs. Substitute explicitly approved local paths.
No command accepts arbitrary tool names, URLs, tokens, shell commands or owner
identity fields.

First migration, only when the Site's Library head is still empty:

```sh
python -m academic_radar.native_host capture-head \
  --config RUNTIME_CONFIG --state EXISTING_VERIFIED_STATE --role primary \
  --head-file NEW_HEAD_RECORD
```

A positive head cannot be recaptured onto arbitrary existing state. Restore the
published immutable checkpoint first, with an external runtime config already
pointing at the absent destination:

```sh
python -m academic_radar.native_host recover \
  --config EXTERNAL_RUNTIME_CONFIG --state NEW_ABSENT_STATE --role primary \
  --checkpoint NEW_CHECKPOINT_DIR --receipt NEW_RECOVERED_RECEIPT \
  --head-file NEW_HEAD_RECORD
```

`recover` reads the authoritative head, materializes its exact immutable Library
identity into isolation, verifies the entire archive and embedded publication
binding, reconstructs the receipt, saves the original head, restores into an
absent state directory, and rechecks the head. It creates no Library file,
activates no snapshot and acknowledges nothing. The head record binds the
captured head to this state path and verified checkpoint. Never rewrite it to
force a stale run to publish.
Before preparing a later operation, the runner rechecks the prior publication's
local journal, imported job, queue and results against that retained checkpoint.

If recovery stops after creating a checkpoint, receipt or state directory, leave
the verified artifacts intact. Use fresh absent recovery destinations unless
successful completion is established. The runner does not overwrite an existing
recovery destination. If the head changes during recovery, prepare rejects the
stale capture; restore the winner instead.

Prepare, have the actual host model judge the frozen queue, and import:

```sh
python -m academic_radar.native_host prepare \
  --config RUNTIME_CONFIG --state STATE --role primary \
  --head-file HEAD_RECORD --operation STABLE_OPERATION_ID
# For an explicitly approved daily collection, append --collect.
# Default: freeze existing candidates only; collection_performed=false.
# The actual host reads queue_path and follows that queue's evaluation policy.
python -m academic_radar.native_host import-results \
  --config RUNTIME_CONFIG --state STATE --role primary \
  --operation STABLE_OPERATION_ID --results HOST_RESULTS_JSON
```

For a verified queue whose actual `papers` list is empty, the host may write a
normal empty results envelope using that queue's exact `run_id`, `profile_hash`
and `source_failures`, with `results: []` and a truthful provenance label such as
`model: "no-candidates"`. Import it through `import-results` normally. No paper
judgments were performed in that case, and the host must not claim an LLM
reviewed papers. An empty envelope for a nonempty queue is invalid; do not add
synthetic paper results or change the queue merely to enable publication.

`prepare` reports the queue path, frozen hash, run ID, candidate count and whether
collection was performed in this invocation. It omits profile text, source
failure bodies and raw remote responses. The host judges every queued item using
the existing queue/result format, not a second model API. `--collect` on a retry
never recollects an already-frozen queue. A collection exit code or nonzero source
failure count still requires review, just as in the existing runtime.

Publish and acknowledge:

```sh
python -m academic_radar.native_host publish \
  --config RUNTIME_CONFIG --state STATE --role primary \
  --operation STABLE_OPERATION_ID --checkpoint NEW_CHECKPOINT_DIR \
  --archive NEW_ARCHIVE.tar.gz --receipt PUBLICATION_RECEIPT
```

On the first attempt this stages the view, creates the bound full checkpoint,
prepares an immutable archive, creates one new Library file, materializes and
verifies it, and performs Site CAS. A fresh authoritative-head read must exactly
match the published descriptor before ACK. ACK uses only the cursors in the
verified embedded checkpoint journal. The native ACK response must include all
three acknowledged cursors, each at least the requested value, as well as
`ok:true`. Only that checked success is recorded in the runtime journal; raw
native response fields are not copied into it.

Once the receipt exists, **repeat `publish` with the same operation and paths**.
It resumes the exact receipt before any staging; it never substitutes a new
head. After uncertain CAS or ACK, it recognizes the matching publication and
retries only what remains. A recovered published receipt can be used with a
nonexistent original archive path because no upload is needed. Its verified
checkpoint directory and restored runtime state are still required.

Do not repeat prepare/import to resume publication. The runner reports
`resume_publication` instead of regressing a committing/published journal.
When the captured restored head already published this same operation,
`prepare` verifies the checkpoint and local journal, reports `already_published`
and leaves both unchanged. Reusing `publish` confirms the head and ACK. A new
operation ID can prepare from the recovered head normally.

Uncertain Library create:

```sh
python -m academic_radar.native_host reconcile-upload \
  --config RUNTIME_CONFIG --state STATE --role primary \
  --operation STABLE_OPERATION_ID --checkpoint CHECKPOINT_DIR \
  --receipt PUBLICATION_RECEIPT --metadata EXACT_NATIVE_METADATA_JSON
# Then repeat publish with the original receipt and paths.
```

The host must first identify the specific created file using the original
operation, filename and hash. The metadata JSON contains exactly
`library_file_id`, `current_version` (or actual `current_version_number`),
`file_id`, and `file_name`. Do not guess a version, invent an identity, replace a
Library file, or blindly issue create again. Reconciliation independently reads
and verifies the archive and runtime intent; it does not activate or ACK. If the
file cannot be identified or a materialization is denied, remain blocked.

## JSON-lines contract

One request is outstanding at a time. Every message is UTF-8 JSON followed by a
newline, at most 2,000,000 bytes including the newline and at most 48 levels of
nesting. Duplicate keys, non-finite values, unknown envelope fields, mismatched
request IDs, different sessions, unsupported result shapes and native errors
fail closed. There are at most 10,000 requests per invocation. The host should
also bound its reads and use an appropriate execution timeout; killing a stalled
write requires receipt reconciliation, not a blind retry.

```json
{"protocol":"academic-radar-native-host-v1","type":"request","session_id":"fresh-process-random-id","request_id":1,"operation":"site.read_checkpoint_head","arguments":{}}
{"protocol":"academic-radar-native-host-v1","type":"response","session_id":"fresh-process-random-id","request_id":1,"operation":"site.read_checkpoint_head","ok":true,"result":{"generation":0,"publication":null}}
```

The session ID is random for each process; request IDs strictly increase from
one. The host binds both to the particular child process, returns exactly one
response to each request, and never replays responses across invocations.
On an actual native rejection or uncertain result, respond with `ok:false` and
a small non-sensitive `error` code, without raw native error bodies. The runner
poisons the channel after a protocol/validation failure. Its terminal line has
`type:"result"`, the same session, last request ID, and either a curated result
or a fixed error code. It never prints tracebacks or remote bodies.

Allowed logical operations are only:

- `site.read_run_status`, `site.read_feedback`, `site.read_profile_events`,
  `site.read_profile`, `site.stage_snapshot`, `site.acknowledge_snapshot`,
  `site.read_checkpoint_head`, `site.commit_library_checkpoint`
- `library.create_archive`: `{path, file_name, operation_id, archive_sha256,
  archive_size}`; returns the four exact stable Library metadata fields above.
- `library.materialize_archive`: `{metadata, directory}`; returns exactly
  `{path}` naming a regular local file inside that newly created empty directory.

Site replies must be decoded successful structured payloads. The host must
check MCP/connector errors before extracting `structuredContent`; neither human
text nor an authorization assertion is a successful tool result. Library create
must produce a new retained **private** file, not a replacement or shared file.
The actual native version is preserved, including versions other than zero/one.
Materialization must use the exact known file identity and version where the
connector supports it. A latest-only read is acceptable only for an unchanged
immutable item; the runner still checks bytes, length, archive structure,
checkpoint hashes, SQLite integrity and the runtime publication intent.

## Focused host pseudocode

This illustrates the control boundary; the named host adapters below are not
implemented native connector integrations and must not be replaced with dummy
success values for live work.

```python
# Preflight the actual owner-approved Site connection and native Library route.
# Pin actual callable tool handles; do not discover/install/authenticate here.
site_tools = {
    "site.read_run_status": approved_site.read_run_status,
    "site.read_feedback": approved_site.read_feedback,
    "site.read_profile_events": approved_site.read_profile_events,
    "site.read_profile": approved_site.read_profile,
    "site.stage_snapshot": approved_site.stage_snapshot,
    "site.acknowledge_snapshot": approved_site.acknowledge_snapshot,
    "site.read_checkpoint_head": approved_site.read_checkpoint_head,
    "site.commit_library_checkpoint": approved_site.commit_library_checkpoint,
}
child = start_verified_module_with_reviewed_arguments(private_pipes=True)
for request in bounded_child_messages(child):
    validate_protocol_session_and_next_id(request)
    if request["type"] == "result":
        record_curated_terminal_result(request)
        break
    try:
        if request["operation"] in site_tools:
            native = invoke_actual_approved_tool(
                site_tools[request["operation"]], request["arguments"])
            result = require_successful_structured_payload(native)
        elif request["operation"] == "library.create_archive":
            require_exact_reviewed_artifact_path_and_hash(request["arguments"])
            native = approved_native_library_create_new_private_file(
                request["arguments"]["path"])
            actual = require_successful_structured_payload(native)
            result = select_actual_library_id_version_file_id_and_name(actual)
        elif request["operation"] == "library.materialize_archive":
            require_requested_empty_local_directory(request["arguments"])
            native = approved_native_library_materialize_exact_identity(
                request["arguments"]["metadata"])
            actual = require_successful_structured_payload(native)
            # Supported native transfer into THIS executor, not a URL fetch or
            # treating a path from another computer as a local file.
            path = materialize_via_approved_local_file_transfer(
                actual, request["arguments"]["directory"])
            result = {"path": path}
        else:
            raise UnsupportedOperation()
        send_matching_response(child, request, ok=True, result=result)
    except NativeFailureOrUncertainty:
        send_matching_response(child, request, ok=False,
                               error={"code": "native_action_incomplete"})
```

If the approved native tool requires user acceptance, obtain that actual
acceptance through its supported flow. Never set `ok:true` because the runner
requested a write, infer OAuth from a Site URL, synthesize owner headers, or use
a different identity/transport after a denial. Do not log whole request/response
lines: staged records and event pages can contain private research information.

## Remaining live acceptance gates

Verify approved native connector invocation, real structured metadata mapping,
private immutable upload and isolated materialization for the real archive size,
retention, owner-only Site access, CAS and ACK round-trip, a lost-workspace
restore, and an actual fresh scheduled host execution. Validate public-source
collection separately when enabling `--collect`. A working host subprocess,
local fixture pass, connected plugin, or pending permission is not proof of
unattended acceptance. No scheduler or primary cutover is performed here.

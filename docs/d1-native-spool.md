# Portable opaque D1 native relay

This is a local file/stdio bridge for the [D1 native CLI](d1-native-host.md), not a
connector, scheduler, credential source, or permission grant. A separately approved
host invokes the actual native tools. The relay performs no HTTP, credential
lookup, Library operation, deployment, or native tool call. Missing native methods
are a blocker; do not substitute an endpoint, token, or legacy capability.

Files: `scripts/d1-native-relay.py`, `scripts/d1-native-pump.js`, and
`tests/test_d1_native_spool.py`. POSIX, Python 3.9+, an explicit trusted Python
interpreter with the package's dependencies installed, and the reviewed source
checkout are required. Node is used only by the synthetic pump tests.

## Pin an independently reviewed release

Verify the immutable checkout/release first. The trusted launcher, interpreter,
installed dependencies, and externally retained manifest SHA are trust roots.
A source manifest checks drift; it does not authorize a modified checkout or defend
against a malicious process with the same account replacing the verifier itself.

The following examples are synthetic paths. Replace them with approved absolute
paths without symlink ancestors. Create the manifest outside the checkout, exactly
once, and retain its SHA independently. Never regenerate trusted hashes merely to
make a failed verification pass.

```sh
/approved/venv/bin/python -I -B /approved/checkout/scripts/d1-native-relay.py \
  pin --output /approved/control/source-manifest.json
```

`pin` returns only the manifest SHA and file count. It inventories the launcher,
pump, and every package source/resource file using relative paths, so an identical
release produces identical bytes after relocation. The manifest is not inside its
own inventory. Existing output files are refused. Every run/helper checks the
explicit manifest SHA and exact file inventory; modified, missing, added, or
symlinked source files/ancestors fail closed.

```sh
/approved/venv/bin/python -I -B /approved/checkout/scripts/d1-native-relay.py \
  --source-manifest /approved/control/source-manifest.json \
  --source-manifest-sha256 '<SOURCE_MANIFEST_SHA256>' check
```

The launcher requires `-I`, ignores caller `PYTHONPATH` and user site packages, and
never puts the checkout or its `src` siblings on `sys.path`. A manifest-bound
importer rehashes and compiles `academic_radar` `.py` bytes directly, never reading
`.pyc`, and refuses unlisted package modules instead of falling back to an installed
copy. Existing installed dependencies remain available through the chosen Python.
The original checkout and its caches are not changed. The D1 adapter uses isolated
transport state and never modifies the legacy spool module's globals.

## Launch one approved invocation

The workspace exists, while the spool and backup/runtime destinations are fresh and
absent. Use the current interpreter and launch arguments explicitly; body contents
cannot select a shell command, interpreter, module, or tool.

```sh
/approved/venv/bin/python -I -B /approved/checkout/scripts/d1-native-relay.py \
  --source-manifest /approved/control/source-manifest.json \
  --source-manifest-sha256 '<SOURCE_MANIFEST_SHA256>' run \
  --workspace /approved/runs --spool site-backup-001 \
  --timeout-seconds 3600 -- backup-site --output /approved/backups/site-before-import
```

Launch through the host's local execution tool and retain its running session ID.
Stdout contains only `request_ready`/`result_ready` metadata. The total deadline is
bounded (maximum 86400 seconds) and never resets per request. The explicit underlying
CLI command retains all its primary-role, fresh-workspace, verification, publication
fence, and activation requirements. Bootstrap approval does not authorize activation.
A configured collection command still has the collector's separate network scope;
the relay neither grants it nor adds a collection flag.

## Load the verified pump without exposing callback bodies

Do not load the JS through an unchecked `cat`/`eval`. The `pump` mode verifies the
explicit source pin, then returns the rehashed pump bytes in a JSON object. Keep
that result inside orchestration variables; do not print its body. For a host with
`functions.exec` and local `exec_command`, the pattern is:

```js
const loaded = await tools.exec_command({
  cmd: "/approved/venv/bin/python -I -B /approved/checkout/scripts/d1-native-relay.py " +
       "--source-manifest /approved/control/source-manifest.json " +
       "--source-manifest-sha256 '<SOURCE_MANIFEST_SHA256>' pump",
  max_output_tokens: 16000
});
if (loaded.exit_code !== 0) throw Error('pump_load_failed');
const source = JSON.parse(loaded.output);
const pump = eval(source.source + '\npumpD1');
const report = await pump({
  python: '/approved/venv/bin/python',
  launcher: '/approved/checkout/scripts/d1-native-relay.py',
  source_manifest: '/approved/control/source-manifest.json',
  source_manifest_sha256: '<SOURCE_MANIFEST_SHA256>',
  workspace: '/approved/runs', spool: '/approved/runs/site-backup-001',
  operations: ['site.read_site_backup'], start: 1, max_requests: 20
});
text(report); // Only counts, byte lengths, timings, and fixed status metadata.
```

Only caller-supplied local paths select the approved launcher/interpreter; shell
arguments are quoted as data. Do not derive these options from a request body.
Before pumping, verify that the actual required app methods are available and that
the target/data/action are approved. The fixed tool-name mapping contains exactly:
`read_main_store`, `write_main_store`, `publish_main_snapshot`, `read_site_backup`,
`read_run_status`, `read_feedback`, `read_profile_events`, `read_profile`, and
`stage_snapshot`. The host's `operations` list can only narrow this set. Legacy
checkpoint/Library capabilities and `acknowledge_snapshot` are excluded.

For read-only status, permit only `site.read_main_store`. Bootstrap needs exactly
`site.read_main_store` and `site.write_main_store`, after its backup/approval gates.
On `budget_reached`, continue from the returned `next`; on `waiting`, wait for the
same relay session and retry that number. On `done`, also confirm the relay's clean
process exit. The private terminal `result.json` retains the full CLI result; select
only necessary metadata if reporting it.

## Failure and cancellation

Each request receives an immutable claim before any native call, then an immutable
invocation marker immediately before dispatch. Duplicate claims/preflights fail
closed. Native calls are never retried automatically, including thrown exceptions,
malformed replies, or an uncertain outcome. Only a truncated **local file read** can
be retried at a smaller chunk size, before any native call.

The helper checks terminal state, exact request hash, a fresh heartbeat, and a
persisted absolute deadline. The pump rechecks the returned deadline immediately
before calling the native method. The filesystem heartbeat works across execution
contexts with different PID views, but is only a bounded liveness observation
(maximum age 15 seconds). The parent must also monitor the actual relay session.
Abrupt death can occur just after a preflight, and an already-started native call
can finish after the deadline; neither condition permits replay. This bridge is not
a remote transaction lease or an exactly-once network guarantee.

If local response transport fails after a native call, preserve all claims/partial
files and the private `d1_pending_reply:` plus spool-path orchestration-store entry
when available. Reconcile the exact action and authoritative state; do not remove
claims, restart at request 1, or issue the native call again. No uncertain-native
retry helper is provided. Terminal errors and argument errors emit fixed generic
codes, never raw exception bodies, argv, profiles, or tool replies.

Request/response bodies stay in private files and orchestration variables rather
than model-visible output. The local bridge transmits response chunks as quoted
process arguments, so they are transiently present in local process arguments and
the execution tool's inputs. Do not enable shell tracing or command/body logging;
platform audit retention and same-account process inspection are outside this
bridge's guarantees. It must run only in the approved private execution environment.

## Bounds, metrics, and performance limits

- Envelopes: <=2,000,000 UTF-8 bytes, <=48 JSON levels, <=10000 sequential requests.
- Reads: <=131072 code points; local truncation halves the limit to at least 8192.
- Writes: <=16384 UTF-16 units, preserving surrogate pairs; the helper limits each
  part to <=32768 code points and the complete response to <=2,000,000 bytes.
- Files are private, regular, no-clobber, and session/request/operation-bound.
  Responses pass the existing native argument/result validators before handoff.
- Native metrics: attempted calls, counts by action, serialized request/result JSON
  bytes, and call elapsed milliseconds; a thrown call counts but has no result bytes.
- Local metrics: exec calls, serialized local-tool request/result bytes including
  framing, and elapsed time. Envelope byte counts and total batch elapsed time are
  reported separately. Add batch totals for the whole run.

These are measured tool-boundary bytes, not inferred HTTP wire traffic. The current
local daily-run benchmark uses 89 logical native calls, plus many local exec calls
for opaque framing/chunks. Hosted end-to-end latency remains unmeasured; a fast local
D1 benchmark does not establish hosted throughput or unattended scheduling. Keep
this bridge observable and measure actual native and local costs before optimizing.

## Synthetic acceptance

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_d1_native_spool.py' -v
```

The ordinary test suite discovers these tests. They exercise the actual CLI with
mock native replies, large exact Unicode round trips, count/byte/timing metrics,
forbidden capabilities, duplicate claims, malformed/session-swapped responses,
expired/stale preflight, malformed terminal results, no-retry uncertainty, explicit
source pins, relocation, symlink rejection, poisoned `.pyc`, `src/argparse.py`
shadowing, and injected `PYTHONPATH`. No real native tool is called.

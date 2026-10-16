# One-shot read-only Site backup journal resume

This is a narrowly scoped recovery mode of the [opaque native relay](d1-native-spool.md).
It starts a fresh `backup-site` invocation with an explicit finite deadline, replays
only an independently sealed prefix of successful `site.read_site_backup` responses,
and then allows the ordinary read-only pump to fetch the remaining pages. It does
not retry a native call, replay writes, change the old deadline, modify old files,
or authorize any action. It is not a general-purpose cache or reusable checkpoint.

## Close and seal the original run

The original run must have a contiguous successful prefix starting at request 1
(`begin`), followed by exactly one pending, never-invoked read. The parent must
finish its current pump batch and verify this boundary. Using the existing pinned
helper, the parent can claim that final request and publish a failed response with
exact error `{"code":"host_action_failed"}` without calling `preflight` or any
native tool. Confirm the original relay and child have exited normally with a
failure terminal. Retain all original files. Do not kill, extend, restart, or
modify the original source merely to make the journal acceptable.

The sealer requires the complete private journal inventory, one original session,
contiguous numbered requests/responses, successful prefix replies, claim/invoke
hashes, exact canonical arguments, and the local-cancelled tail's claim, failed
response, absent invocation marker, and matching normal failure terminal. Gaps,
extra/later entries, symlinks, unsafe files, changed bytes, failed prefix replies,
uncertain native attempts, and previously replayed journals are refused. Optional
failed-tail partial bytes are accepted only when the existing response validator
normalizes them to the exact fixed failure; successful partials must be byte-exact.

Independently verify and pin both source releases. The original manifest and all
of its files must still match its retained SHA. The backing `src/academic_radar`
file maps must be identical between releases; only reviewed transport changes are
allowed. The journal manifest records integrity, not proof of remote authenticity
or permission to trust an arbitrary source/payload.

Synthetic example, with only approved absolute paths:

```sh
/approved/venv/bin/python -I -B /approved/resume/scripts/d1-native-relay.py \
  --source-manifest /approved/control/resume-source.json \
  --source-manifest-sha256 '<RESUME_SOURCE_SHA256>' seal-read-journal \
  --spool /approved/runs/old-backup --prefix 30 \
  --output /approved/control/read-journal.json \
  --original-source-root /approved/original \
  --original-source-manifest /approved/control/original-source.json \
  --original-source-manifest-sha256 '<ORIGINAL_SOURCE_SHA256>'
```

The external manifest must be absent and is written once as a private immutable
file. Retain its returned SHA independently. Metadata includes prefix count,
original cached envelope bytes, original `captured_at`, snapshot SHA, and next ID.
No callback body is printed. Every original journal member is hash-bound and read
through private-file/no-symlink checks, then verified again before live continuation.

## Start exactly one authorized fresh invocation

Use a fresh spool and absent backup output. The new deadline is explicit and finite
(maximum 86400 seconds); it does not alter the old run. There is no arbitrary child
command argument: `resume-backup` constructs only `backup-site --output ...`.

```sh
/approved/venv/bin/python -I -B /approved/resume/scripts/d1-native-relay.py \
  --source-manifest /approved/control/resume-source.json \
  --source-manifest-sha256 '<RESUME_SOURCE_SHA256>' resume-backup \
  --workspace /approved/runs --spool resumed-backup --timeout-seconds 14400 \
  --journal /approved/control/read-journal.json --journal-sha256 '<JOURNAL_SHA256>' \
  --output /approved/backups/resumed-site-backup
```

The new process must generate the exact previous operations and arguments in order.
For each cached response, only its session identity changes; the full result is
preserved, including `begin`'s original capture time and pinned snapshot. A new
private replay receipt binds both sessions, original/new request and response
hashes, request ID, and journal SHA. Replayed requests are never announced for a
native call. The cancelled boundary request must match before it is announced;
no later cache lookup is permitted. Early successful termination before the whole
prefix and boundary match is rejected.

Wait for the first ordinary `request_ready` at `prefix + 1`. Load the reviewed pump
through the new source pin and use the new launcher, new manifest, new spool,
`operations: ['site.read_site_backup']`, and that exact starting number. Confirm
normal final process exit and independently verify the complete produced backup.

## Counters and failure handling

`replay-summary.json` and request announcements distinguish:

- `replayed_reads`: local replay count, not external native calls.
- `cached_response_bytes`: original stored envelope bytes.
- `replayed_response_bytes`: actual newly encoded replay envelope bytes.
- The pump's native counters: only newly attempted external calls.

The CLI's `transport_call_count` remains a logical callback count and includes both
replayed and fresh callbacks. Do not report it as the number of external calls.

Any mismatch aborts before fresh native dispatch. Preserve all files after failure.
This implementation deliberately refuses sealing or chaining a replayed run.
Launching again from the original journal could repeat fresh reads made after its
prefix, so it is not an automatic recovery option after a resumed invocation fails
or has an uncertain native outcome. Reconcile that outcome and obtain a separately
reviewed recovery plan instead. Local cancellation is permitted only for a known
unclaimed, never-invoked boundary, not a claimed or uncertain native request.

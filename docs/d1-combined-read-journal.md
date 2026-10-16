# Combined backup recovery from an already successful diagnostic read

This separate recovery format is for a closed `backup-export` invocation. It does
not extend or weaken the Site-only journal. It adds no native retry capability,
authentication route, deployment, or write operation.

## Required evidence and authorization

The original private spool must contain one contiguous successful prefix, followed
by exactly one invoked `site.read_site_backup/records` request with its durable
`host_action_failed` response and matching failed terminal result. Missing replies,
uncertain running attempts, extra requests, replayed journals, symlinks, shared
files, changed sources, and changed file inventories are rejected. The original
spool remains untouched.

Before using this recovery, a separately authorized diagnostic read must already
have succeeded. A pinned private receipt binds the exact original request, failed
response, terminal result, original source manifest, one-use diagnostic reservation,
and successful response bytes. This tool neither performs that diagnostic read nor
authorizes another one. A denied or unsuccessful receipt is rejected; a known denial
must never be worked around. The generic old failure envelope alone cannot classify
a failure or establish authorization.

The receipt format is `academic-radar-diagnostic-read-retry-v1`, with exactly:
`original_spool`, `original_request_id`, `request_sha256`,
`failed_response_sha256`, `terminal_sha256`, `source_manifest_sha256`,
`reserved_fence_sha256`, `retry_count` (1), `outcome` (`success`), `recorded_at`,
`native_elapsed_ms`, and `successful_response` (`path`, `bytes`, `sha256`). The
reservation has exactly `state` (`one_diagnostic_retry_reserved`),
`original_spool` (leaf name), `original_request_id`, `request_sha256`, and `at`.
Neither record contains tool bodies, headers, credentials, or raw exceptions.
These hashes bind evidence; they do not create user approval.

## Seal and resume once

Use an independently reviewed checkout and its externally retained source manifest
as described in [the relay instructions](d1-native-spool.md). These examples use
synthetic paths and counts. All referenced directories must exist, with no symlink
ancestors. Outputs and the new spool must be absent.

```sh
/approved/venv/bin/python -I -B /approved/recovery/scripts/d1-native-relay.py \
  --source-manifest /approved/control/recovery-source.json \
  --source-manifest-sha256 '<RECOVERY_SOURCE_SHA256>' seal-combined-journal \
  --spool /approved/runs/failed-combined --prefix 20 \
  --output /approved/control/combined-journal.json \
  --original-source-root /approved/original \
  --original-source-manifest /approved/control/original-source.json \
  --original-source-manifest-sha256 '<ORIGINAL_SOURCE_SHA256>' \
  --retry-receipt /approved/evidence/retry-receipt.json \
  --retry-receipt-sha256 '<RECEIPT_SHA256>' \
  --retry-fence /approved/evidence/retry-fence.json
```

Here `--prefix 20` means requests 1–20 succeeded originally. Request 21 failed
originally but has a separately retained successful response. The sealed journal
therefore replays 21 requests and exposes request 22 as the first fresh native read. Its exact method and
continuation cursor are derived from the bound successful response and sealed too.
The first three original requests must be schema-selection head, manifest, and
atomic Site `begin`; the begin result and `captured_at` are preserved exactly.
All package source/resource hashes must match the independently pinned original.

```sh
/approved/venv/bin/python -I -B /approved/recovery/scripts/d1-native-relay.py \
  --source-manifest /approved/control/recovery-source.json \
  --source-manifest-sha256 '<RECOVERY_SOURCE_SHA256>' resume-combined-backup \
  --workspace /approved/runs --spool recovered-combined \
  --timeout-seconds 14400 --journal /approved/control/combined-journal.json \
  --journal-sha256 '<JOURNAL_SHA256>' --output /approved/backups/recovered-combined
```

This mode constructs only `backup-export`; arbitrary child commands and custom
schema arguments are not accepted. It uses the unchanged source-bundled trusted
schema templates. It validates the whole original inventory and external evidence
before spawning, then compares every regenerated request's operation and arguments
before rewriting only the response envelope's session identity. The original failed
boundary is replayed from the already-successful external response, never announced
for another native call. Receipts retain both the failed and successful hashes.

Before spawning, an exclusive, fsynced, read-only resume fence is created in the
receipt-bound successful response directory, named from the original request SHA.
The path is bound into the journal. Copying or resealing the same receipt/journal
cannot reset this fence. A second reservation fails closed, even if the first run
failed before reaching a fresh read. Do not delete or relocate the fence, recreate
approval evidence, or switch storage to bypass it. Further recovery requires a new
review and authorization; no nested recovery or fence-reset command is provided.

## Fresh reads and completion

Cached requests permit only `read_main_store` and `read_site_backup`. After replay,
only Site record/event reads bound to the original generation inventory and exact
captured event watermarks are permitted. A new `begin`, head recapture, write,
session switch, skipped request, or changed boundary fails before dispatch. Use the
existing verified pump with the new launcher/source pin and the explicit two-read
host scope. Start at the first announced request, not at the failed request number.

The normal combined exporter reconstructs and verifies the main SQLite package and
all Site generations/event history before publishing the new local backup. A
successful terminal result is rejected until the complete cached prefix and first
fresh request have been consumed. Replay receipts and summary distinguish original
successful reads, the one supplied diagnostic success, cached byte counts, and
actual replay-envelope bytes. Pump metrics count only newly attempted native calls.
Confirm the actual child process exit and run the normal complete backup verifier.
A `/proc` scan from a different execution namespace is not global quiescence proof.

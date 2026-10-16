# Cloud primary migration: implementation and acceptance runbook

## Status and scope

This is a tested implementation candidate based on upstream commit
`05822bc41e70dab312122c3e23f1ab4cf1325352`. It does not provision credentials,
activate a scheduler, move the personal database, publish a Site, or stop Mac
jobs. Synthetic tests are not production acceptance. The previous temporary
proof-of-concept directory was not present in this executor; this candidate was
recovered from GitHub with every original file checked against its Git blob SHA.

The target roles are:

- Cloud task: restore the complete SQLite state, collect/enrich, have its host
  model judge a frozen queue, atomically import, save a durable checkpoint, and
  publish the matching Site snapshot.
- Sites: private owner UI, searchable D1 display records, durable feedback and
  versioned research-interest/screening-preference edits, and private R2 blobs.
- Mac: download versioned verified backups and perform an explicitly requested
  recovery. It must not keep running collection, scoring, or publication after
  successful cutover.

No independent LLM API or paid service is introduced. Full SQLite-to-D1 data
model rewriting and multi-user accounts are outside this change. The current
Site/bucket is one owner namespace; a future multi-tenant product needs separate
per-tenant state, event sequences, object prefixes, authorization and CAS heads.
Never share this owner's existing state when introducing a new account.

## Final source-state delta

When a verified baseline has already been transferred and the last source run
only changes a small part of SQLite, use the bounded, typed
[closed-snapshot delta workflow](sqlite-state-delta.md). It requires exact schema
and full logical baseline hashes, produces only a new verified database copy,
and runs before path relocation. It does not replace history-file transfer,
reference validation, full checkpoints, or cutover approval.

## Durability and publication contract

A normal cloud task's filesystem is disposable. The canonical state is the
checkpoint referenced by D1 `pointers.checkpoint_head`, not its latest local
working directory. Checkpoints retain every SQLite table, linked PDFs, profile,
and supported run artifacts. See [checkpoint-format.md](checkpoint-format.md)
for included files, excluded credentials, path rewriting and validation.

### Legacy paths and historical archives

Migration does not treat every historical pathname as a startup dependency.
The active profile/configuration, all resumable job artifacts and any PDFs
selected for migration must be present, checked and explicitly relocated into
the new state. Missing operational files stop checkpoint creation. Never change an active job's status
merely to bypass this check.

For completed or abandoned history only, an operator-reviewed reference policy
may map an exact old path to an existing staged file, or explicitly record it as
`archived_local` / `unavailable`. Such references become inert `archive://` IDs;
the private checkpoint retains the original path and archive provenance. Restore
does not open that path, execute an old restore command or invent a replacement
file. The status page shows aggregate unavailable-attachment counts without
exposing old device paths. Database history remains intact.

Inventory the source before selecting a policy. Completed/abandoned job
attachments are still intended for migration when available. Large historical
restore databases may remain in a separately retained local archive when the
owner chooses; they are not current pipeline dependencies. Preserve their audit
provenance and do not claim those operations can be undone online.

If the owner explicitly chooses a fresh cloud PDF registry, apply exact
`not_migrated` entries with matching recorded hashes/sizes. Preserve original
metadata in the private migration audit, leave the source installation and its
PDFs untouched, and show no old-PDF badges or links. Fresh future uploads start
from the empty registry. An unlisted missing PDF is still an error; this is not
a general missing-file exception. Keep actual private inventory counts, hashes
and paths out of source control.

Use `academic-radar cloud checkpoint --config CONFIG --output NEW_DIR
--reference-policy REVIEWED_POLICY_JSON` for the initial explicit conversion.
The normal no-policy path stays strict. Do not use prefix/glob replacements,
clear audit records, or silently substitute missing history. Once the small
history archive arrives, verify its checksums and reconcile the exact entries
in a new checkpoint before reporting those attachments as available.

`CheckpointClient` divides files into immutable SHA-256-addressed chunks no
larger than 8 MiB. R2 stores chunks and manifests privately. The Worker never
loads the whole database into memory. The transport currently bounds manifests
to 1 MiB, 4,096 files and 4,096 chunk references. Hosted R2/request limits still
need a real integration test. No automatic garbage collection or deletion is
included: completed checkpoints remain versioned, and failed publishers may
leave harmless unreferenced chunks. Monitor storage quota and stop before it is
exhausted; establish a retention policy only after verified backup coverage.

The publication order is:

1. Capture the current cloud head **before** restoring its checkpoint.
2. Restore and verify the complete checkpoint into a fresh directory.
3. Pull Site feedback and profile events without activating a display snapshot.
4. Collect/enrich and freeze one queue, profile, preference version, threshold,
   feedback cursor and evidence. The actual host model returns all judgments.
5. Validate and import the entire queue atomically; no publication side effect.
6. Pull any newer Site edits and stage a complete D1 display snapshot.
7. Create/verify the matching full-state checkpoint and upload immutable chunks.
8. Compare-and-swap the original head and activate the staged display snapshot
   in one D1 transaction. A concurrent loser changes neither.
9. Acknowledge only the feedback/profile/request cursors included in that state.

Never replace the expected head with the current head to force through a stale
run. Restore the winner instead. Legacy `/api/sync/finish` activation is fenced
after a checkpoint head exists. A failed upload keeps the prior head and Site
snapshot. A crash after successful CAS but before acknowledgement is recognized
by the operation ID and only acknowledgement is retried.

Within a retained workspace, `cloud prepare` returns the same frozen queue on
retry. Its operation-to-job link commits in the export transaction. `cloud
import` is idempotent, accepts a corrected malformed result before successful
import, and rejects different results after import. A **lost uncommitted
workspace** restarts from the last durable head and may repeat collection and
model work; it cannot lose already committed history or overwrite a newer
head. This version does not persist an in-progress model invocation.

## Optional native Library publication path

The optional Library adapter stores one immutable checkpoint archive per run,
then compare-and-swaps the Site head and matching display snapshot. Library
upload and Site activation are separate services, not a distributed transaction.
A failed or losing publication retains the old complete Site view and an
unreferenced Library archive; it never deletes the previous checkpoint. The
Site head names the exact Library identity, returned version, backing file ID,
archive hash and checkpoint ID. Never replace that Library item in place.

The owner-authorized managed Site bridge handles bounded JSON feedback/profile,
staging and publication calls without giving Python raw tokens. See
[managed-site-bridge.md](managed-site-bridge.md) and the Library adapter contract.
This path still needs the approved connector installation and real fresh-run
acceptance. It does not make an unavailable upload/materialization route work,
and it must not be used to bypass a denied Library access request. R2 remains an
optional separately authenticated transport, not a requirement for Library
checkpoint storage.

## Access gates: required before real upload or cutover

1. Keep Site access owner-private at the Sites platform level. Confirm anonymous
   home, all six HTML pages, all data APIs and profile APIs are denied.
2. Configure the verified owner identity (`OWNER_USER_ID`, preferred, or verified
   `OWNER_EMAIL`) using Sites' trusted authenticated request headers. The app
   owner gate must deny absent or mismatched identity. Do not expose this Worker
   behind a proxy that accepts client-supplied identity headers.
3. Enable the private `BUCKET` R2 binding using the supported Sites hosting flow.
   The checked-in hosting manifest intentionally still has `r2: null`; storage
   has not been provisioned by this code change.
   Confirm the hosted R2 binding preserves native SHA-256 checksum metadata;
   the transport refuses objects without matching server-verified checksums.
4. Verify a supported authenticated path for fresh unattended cloud tasks to
   read/write the private Site. Existing `SYNC_TOKEN` is app-level authorization;
   private Sites can also require `OAI-Sites-Authorization`. Do not assume a
   scheduled task automatically receives these credentials. The client reads
   only an explicitly configured credential file or a caller-supplied dict;
   it never discovers, creates, prints or embeds credentials in checkpoints.
5. New credentials, ongoing credential installation or expanded persistent
   access require the owner's action-time approval and secure setup. Do not put
   tokens in source, task prompts, shell command arguments, logs or backups.
6. Verify current included plan quota. Native R2 is not a promise of unlimited
   capacity, unrestricted request volume, or unattended host-model capability.

## Commands and host-agent boundary

Install the project in the selected fresh cloud executor using the verified
source version and its supported dependencies. A restored checkpoint disables
cloud synchronization and delivery by default. Configure the task's ephemeral
private runtime config explicitly, with `cloud_runtime.role = "primary"`; use
`"staging"` for synthetic validation. A `"backup"` or missing role cannot run
cloud analysis. Do not edit the verified backup merely to enable a scheduler.

Capture/read the head via `academic-radar cloud head --config CONFIG`, save the
returned JSON, then use `CheckpointClient.download_checkpoint` and
`academic-radar cloud restore --checkpoint CHECKPOINT --destination FRESH_STATE`.
After restore, run the schema upgrade before pulling events (`academic-radar db
upgrade --db FRESH_STATE/papers.sqlite3`). This candidate adds SQLite migration
016 and one Site profile-event ledger migration.

Run:

```sh
academic-radar cloud prepare --config CONFIG --operation daily-YYYY-MM-DD --head-file HEAD_JSON
# The task's actual host model reads the returned queue and profile, follows
# evaluation_policy plus screening_instructions, and writes complete results.
academic-radar cloud import --config CONFIG --operation daily-YYYY-MM-DD --results RESULTS_JSON
academic-radar cloud commit --config CONFIG --operation daily-YYYY-MM-DD --checkpoint NEW_CHECKPOINT_DIR
academic-radar cloud status --config CONFIG --operation daily-YYYY-MM-DD
```

`--no-collect` supports offline synthetic tests; do not use it for the daily
production collection. `--rescreen` is not exposed by the daily runner. Profile
or preference edits affect future unjudged papers; already completed scores and
historical recommendations are retained. The frozen threshold is used when
importing, even if the configured threshold changes during model work.

For an offline full backup, pause writers and use `academic-radar cloud
checkpoint --config CONFIG --output NEW_CHECKPOINT_DIR`. Restoration always
requires an absent destination and verifies all files before publishing it.
For periodic Mac backups, download the current immutable checkpoint into a
new timestamped destination, verify hashes and database integrity, and record
the cloud generation. Do not run `prepare`, `import` or `commit` on the Mac.
Retain the last verified version when any download/verification fails.

## PDF reading

The owner-only `/fulltext/file?id=...` route resolves a private metadata record
from the active D1 snapshot, then checks that the committed checkpoint head
references that exact snapshot. It streams only the matching fulltext file from
that checkpoint and verifies each chunk before delivery. Executor-local paths
are never sent to the browser. Anonymous/wrong-account requests cannot read PDF
metadata. Responses use private no-store, nosniff, a fixed safe filename and a
sandbox policy. Byte ranges are not implemented: a Range request receives the
complete 200 response with `Accept-Ranges: none`, never a fabricated 206.
PDF upload/edit workflows on the Site remain a separate unimplemented feature;
the owner has explicitly omitted existing PDFs from this migration. The cloud
starts with an empty active PDF registry, without old-PDF placeholders. Original
metadata remains in the private audit and source backup; later PDF uploads need
their own supported ingestion flow before the Site can serve them.

## Profile and feedback behavior

The owner can edit research-interest content, a relevance threshold in [0, 1],
and screening instructions on the Site. The owner-only, same-origin endpoint
requires an expected profile version and active snapshot generation. A stale
browser tab receives a conflict and retains its unsaved text. Each successful
edit appends an immutable event. The next cloud pull atomically updates the
profile ledger, cursor, active profile and preferences. File reconciliation is
recoverable after a crash. Conflicting independent local profile edits stop
without overwriting either version.

In cloud-primary mode, newly sequenced Site feedback is authoritative even if
an older Mac timestamp is ahead. Event order, cursor and resulting feedback
commit together; duplicate deliveries do not duplicate history, and clear
operations remain tombstones. Compatibility mode preserves the old local-first
last-write-wins behavior only before migration.

## Production acceptance checklist

Keep existing production jobs until every relevant item passes, then stop the
Mac publishers before enabling the single cloud publisher. Do not run both.

- Verify owner login works and anonymous/wrong-account access is denied for
  pages, profile, feedback, chunks, manifests and head APIs.
- Fresh cloud task can authenticate using the approved mechanism without user
  intervention, with no credentials copied into state or logs.
- Transfer a consistent real snapshot; independently compare SQLite table
  counts, row content fingerprints and profile hashes. Confirm the cloud active
  PDF registry is empty under the reviewed omission policy, with the omitted
  original PDF metadata retained only in the private migration audit.
  The prepared local package is only an input until this is verified.
- Run collection, actual host-model judgment, import, checkpoint publication,
  Site rendering/search and feedback/profile round-trip on a staging copy.
- Edit feedback and profile concurrently with an interrupted publish; verify
  the newest unacknowledged events remain pending and are applied next time.
- Kill/recreate the cloud executor and restore using only GitHub + durable
  checkpoint + approved auth. Verify history, PDF access and next-run behavior.
- Race two publishers: exactly one head/Site activation wins. The loser stops.
- Simulate upload and acknowledgement failures; confirm prior committed state
  remains usable and retry is idempotent.
- Restore one downloaded Mac backup in a separate directory and check SQLite
  integrity and every included artifact, including the PDF-omission audit. Verify backups do not collect, analyze or publish.
- With the Mac offline, observe an actual scheduled 08:00 run in the approved
  user timezone through final verified Site publication. An automation-created
  confirmation or synthetic test is not evidence this condition passed.

Record these observations and retain the pre-cutover package before disabling
old jobs. If any access, storage, model or scheduler condition is unverified,
leave cutover pending and report the exact blocker. Rollback restores a chosen
verified checkpoint into a new directory and explicitly selects one publisher;
never silently overwrite current feedback or re-enable the old Mac publisher.

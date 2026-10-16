# Cloud primary migration: implementation and acceptance runbook

## Status and scope

This is an implementation candidate based on upstream commit
`05822bc41e70dab312122c3e23f1ab4cf1325352`. The recoverable daily-collection parity
change is still subject to focused tests and live acceptance below. It does not
provision credentials, activate a scheduler, move the personal database, publish a Site, or stop Mac
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
4. Run the recoverable daily stages below: API collection, the frozen
   latest-two-issue plan, per-source strict issue import, then enrichment.
   Freeze one queue, profile, preference version, threshold, feedback cursor,
   evidence and collection report. The actual host model returns all judgments.
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

## Recoverable daily collection and coverage

Online `cloud prepare` performs these ordered stages before a queue can exist:

1. Invoke the existing API `collect-only` with a stable operation-bound batch ID.
   Retain its source health and failures separately from issue coverage.
2. Freeze one `official plan`, including its as-of date, verified source mappings
   and latest-two-issue target. Retain the plan and its checksum in private state.
3. Run `official collect-supported` separately for each planned source, using
   that frozen plan and the remaining official-stage budget. Save each evidence
   package and checksum before attempting import.
4. Run the existing strict official preview. Apply only a package with no
   validation errors, using the existing atomic importer. Its operation/source
   receipt and package checksum commit in the same SQLite transaction as the
   imported evidence, so a retry cannot duplicate that package's import.
5. Run abstract enrichment against the resulting state and retain its terminal
   outcome, including unresolved and deferred work.
6. Invoke `agent-export(no_collect=True, batch_run=API_RUN)` once, with the full
   `collection_report`. Export reuses the API batch; it does not recollect or
   rerun enrichment. Daily screening still considers only unjudged eligible
   identities, including backlog, up to the existing queue limit.

A completed stage is not necessarily a successful source check. Failed,
partial, unsupported and deferred outcomes are terminal evidence that can be
frozen for review; they must not become success merely because export or
publication worked. A nonterminal interrupted stage blocks queue creation.

### Evidence and status meanings

Nature reads its verified official issue and article pages. All other currently
mapped publishers (Taylor & Francis, IEEE Xplore, ACM Digital Library, SAGE,
ScienceDirect and INFORMS) use publisher-deposited Crossref print issue metadata,
with exact-DOI OpenAlex fallback for missing complete abstracts. Report these as
`publisher_deposited_metadata`, not official-page visits. The existing source
mapping, evidence validation and abstract precedence remain authoritative;
this orchestration adds no weaker importer or guessed publisher mappings.
Coarse month/year publication dates retain their precision and uncertainty.
Missing abstracts stay empty with a traceable reason when permitted by the
existing strict validator; no generated summaries substitute for source text.

The frozen queue's full `collection_report` separates:

- `api`: batch identity and status, source health, collected/new counts, and
  per-source results. The existing `source_failures` remains the API failure
  envelope used by queue/result validation.
- `official`: frozen as-of date and per-source outcome, reason, issue/paper
  counts, evidence types and import result. `succeeded` denotes validated
  latest-two coverage using the stated evidence type; `partial`, `failed`,
  `unsupported` and `deferred` are visible gaps. Non-issue sources can be
  `not_applicable`; an unmapped journal is not silently counted as covered.
- `enrichment`: its own status, checked/updated/unresolved/deferred counts and
  budget outcome.

The native response and Site snapshot status expose compact aggregate outcomes
and distinguish official-page evidence from publisher-deposited metadata.
These coverage summaries exclude detailed packages, raw failure bodies, profile
content and local paths. `read_run_status` includes the compact report; the
`daily_collection_coverage` verification check preserves partial, failed and
`not_exercised` coverage after results import. Do not infer full collection
success from a zero API exit code, successful queue import, or successful Site
publication alone.
An explicit `--no-collect` runtime preparation, or native preparation without
`--collect`, marks the report and all three stages `not_exercised`. It freezes
existing candidates without network collection or enrichment and is not proof
of daily-collection acceptance.

### Collection network failures

Collection stops on HTTP 401/403/407 or an explicit network/access denial,
including HTTP errors wrapped by `URLError` and proxy tunnel denials. The error
is sanitized and propagates through source, abstract and batch fallbacks; no
queue is frozen from a denied run. Ordinary DNS failures, timeouts and retryable
server failures retain bounded retries.

A valid `Retry-After` is parsed as finite seconds or an HTTP date. Short waits
are honored in full. A delay above the client's existing wait bound (API: 60
seconds; official/enrichment: 30 seconds), or beyond an available deadline,
defers the origin for the remainder of the current run without sleeping and
retrying early. Malformed/negative/non-finite headers use bounded backoff.
The same in-memory gate covers API source loops, inline abstracts, official
worker threads and enrichment. Deferred coverage remains failed/degraded,
deferred or partial as appropriate; it is not counted as complete coverage.

`urlopen` follows redirects internally. A rate limit on a redirected response
therefore gates both its actual origin and the requested resolver origin. A
publisher cooldown also conservatively defers unresolved DOI links, whether
the first request used a direct publisher URL or `doi.org`. Known
`dx.doi.org`/`www.doi.org` aliases are included; unrelated direct publisher
origins remain available. Deferred lookup evidence explicitly identifies this
resolver precaution. This avoids a hidden redirect retry without replacing urllib's
transport. Requests already in flight may finish, but later request attempts
check the shared gate. The gate resets at a later independent invocation. No
source configuration, configuration fingerprint or permanent provider policy
is changed; restarting an interrupted operation remains a separate decision.

### Official collection budgets

The reviewed runtime config may include:

```toml
[official_collection]
time_budget_seconds = 900
per_source_time_budget_seconds = 120
max_sources = 20
```

These are the defaults. Time budgets must be positive numbers and `max_sources`
a positive integer; the maximums are 900 seconds total, 120 seconds per source
and 100 sources. The stage-wide
budget and source limit bound issue collection, independently of the existing
API and enrichment settings. A source or remaining work that exhausts a budget
is `deferred`; partial request success does not create a successful issue check.

Deadlines are cooperative and propagate to worker requests. Requests, response
chunk reads and retry waits use the remaining budget. They are not a hard kill
or an exact wall-clock promise: a socket operation already in progress may
finish after the deadline within its bounded timeout; platform DNS resolution is not a hard wall-clock guarantee. Full coverage is not guaranteed when networks stall. No new work should start
once the deadline has expired. The saved official-stage deadline does not reset
on a retry. New daily plans schedule the oldest attempted sources first, using
durable attempt history. Sources deferred before an attempt therefore move
ahead of recently attempted sources on a later run; a slow early source cannot
permanently monopolize the budget. A retry retains its existing frozen order.

### Interrupted preparation

Resume with the same operation ID, captured head, collection mode and reviewed
configuration. The journal binds the original configuration and API batch;
changing them to continue an unfinished run is rejected. Another operation
cannot start while a prior operation is `preparing`, or while its exported queue
still awaits results. Keep the retained state and journal intact. Journal
artifact references are state-relative. The config fingerprint normalizes only
state/profile relocation, and evidence checksums use canonical JSON so checkpoint
serialization whitespace changes do not invalidate the same evidence. This does
not authorize changing collection settings or evidence content during recovery.

A completed API batch is reused. A recorded official package is rechecked
against its checksum and frozen plan before strict preview; it is not fetched
again to obtain different evidence. If a crash occurs after atomic import but
before the stage journal advances, the saved package's idempotency receipt
returns the original import result. A missing or changed artifact is a blocker,
not permission to recreate it silently or mark the stage complete. An
interrupted request with no durable completed package may be retried within
the original remaining budget. Completed enrichment and an already-frozen queue
are reused. The publication receipt/reconciliation flow remains separate.

These guarantees apply to retained state. Losing an uncommitted workspace
still requires restoring the last durable head, as described above; no synthetic
fixture pass proves live source access, timing, native integration or scheduled
execution.

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

The core `cloud prepare` command collects by default. `--no-collect` supports
offline synthetic tests or explicitly requested existing-candidate work and
reports `not_exercised`; do not use it for daily production collection. The
[native host runner](native-host-runner.md) instead defaults to no collection and
requires explicit `prepare --collect` for the daily stages. `--rescreen` is not
exposed by the daily runner. Profile
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
  Check the queue's full collection report and the compact native/Site status:
  API health, actual issue evidence and enrichment must remain distinguishable.
  A no-collect `not_exercised` run does not satisfy this gate.
- Interrupt API completion, per-source package save and official atomic import;
  retry the same operation and verify batch/package reuse and no duplicate
  import. Reject a changed head, configuration or evidence checksum and a
  competing operation while preparation remains incomplete. Exercise a source
  failure, unsupported source and exhausted budget without turning them into
  successful coverage. Verify deferred, unattempted sources move ahead on the
  next daily plan while same-operation retries keep their frozen order. Verify
  the collection journal and canonical evidence hashes survive a supported
  checkpoint relocation. Measure real request behavior separately; cooperative
  timeouts are not a hard wall-clock bound.
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

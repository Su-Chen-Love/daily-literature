# D1-native daily runtime

`academic_radar.d1_runtime` is a disposable Python working-set adapter for an
already verified authoritative D1 import. It does not restore a SQLite database
from Library, download the full historical database, install software, deploy a
Site, request credentials, or call a model API.

## Host API

The approved host injects `D1Client(call)`, where `call(action, data)` invokes the
closed native `main_store` operation and returns its decoded successful object.
The module has no raw-HTTP fallback. Create an absent scratch directory with:

```python
workspace = WorkingSet.open(
    D1Client(host_main_store), new_directory, stable_operation_id,
    trusted_schema=independently_trusted_schema_database,
    source_root=original_import_state_directory,
)
prepared = workspace.prepare(no_collect=False, sync_client=host_site_edits)
# The host judges exactly the returned immutable queue using its existing model.
workspace.import_results(host_results_path)
workspace.publish(host_atomic_revision_fenced_publication)
```

`source_root` is an explicit relocation map for old absolute state paths. New
operational references are stored as `state://relative/path`; unchanged imported
rows and file bytes retain their original exact representation. The trusted
schema fingerprint must match the imported manifest. Remote SQL is never run.
All new daily collection requires the native owner-edit synchronization callback.
`no_collect=True` is an explicit offline exercise and reports `not_exercised`.

The publication callback receives `(records, certificate)`. It must stage the
complete records and atomically bind their generation to the certificate's exact
D1 revision and source watermarks. This callback is separate from Library and
must not blindly advance the head after a competing edit. No callback means no
publication.

## Working-set coverage derived from existing SQL

Every page is pinned to one committed revision, and continuation ends only after
the provider confirms the end. The local `.d1-working-set.json` identifies this
as **partial state, not a full backup**. It cannot be used as a full restore.

Complete, UI-relevant current relations and bounded-size settings/history:

- `papers`: all metadata supports DOI duplicate matching, eligibility and unseen
  candidate discovery, enrichment candidates, source coverage and complete cards.
- `observations`: the complete identity/source relation preserves original
  first-observed timestamps under `INSERT OR IGNORE` and source coverage counts.
- `paper_feedback`, `feedback_events`: current feedback, calibration examples,
  frozen feedback watermarks, profile-review boundaries and event counts.
- `profile_versions`, `profile_review_runs`: active and historical confirmed
  configuration, review suggestions, version numbering and foreign keys.
- `recommendation_snapshots`: all immutable daily selections; this is the small
  UI history, not historical raw judgment/collection audit bodies.
- `official_issue_checks`: all issue-key status and compact issue evidence, needed
  both for exact idempotent import behavior and historical coverage aggregates.
- `source_health`, `fulltext_files` metadata, `schema_migrations`, and
  `sqlite_sequence`. Full-text file bodies are not downloaded.

Fixed server-side reducers, rather than full historical table scans over the
native channel:

| Table / scope | Exact dependency |
| --- | --- |
| `screenings / latest_screenings` | Latest codex-agent judgment per identity, ordered by screened_at then physical rowid. This is also the complete known-judgment set across all profiles, preventing automatic repeat judgments. |
| `abstract_attempts / abstract_retry` | For exactly the eligible papers with a missing abstract, unknown publication type, or quarantine status: union of the latest attempt per identity and latest failed/not_found attempt per identity/provider. Preserves ordering by MAX(attempted_at) and the 24-hour failure cache. Refreshed after the committed API/official changes, before enrichment executes. |
| `agent_jobs / ui_jobs` | All imported jobs for historical calendar days, all exported jobs for concurrency, and newest 20 for status. |
| `pipeline_runs / ui_runs` | Runs referenced by the above jobs, newest 20, latest collection and the current operation's deterministic collection run. |
| `source_runs / ui_source_runs` | Latest run per source and rows for the current collection run. |
| `task_runs / ui_tasks` | Newest 20 and every running task. |
| `meta / runtime_meta` | Current controls/cursors, current operation/export/official-import receipts, plus other unfinished operation markers. Completed prior operation bodies stay cold. |
| `run_papers / runtime_run_papers` | Current collection and exported queues, plus latest imported selected/selected_new membership. |

The latest imported run's exact screening and run_papers rows are additionally
hydrated. This preserves current-day cards even when legacy timestamps differ
from the global latest-score ordering. Historical cards use immutable snapshots.

The D1 summary provides exact per-table maximum hidden rowids, monotonic source
watermarks and distinct archived-artifact availability counts. The latter
replaces only `ui:status.history_artifacts`; old cleanup audit bodies remain in
D1. Archived `archive://...` markers remain inert. Old queue/result/digest and
full-text objects are retrieved only when explicitly needed, not on each run.
Current operation queue, results, official plan, evidence and preview artifacts
are retrieved by exact revision-pinned file references. External scalar objects,
including repeated calibration snapshots, are fetched once per content hash.
Reference descriptors and chunks use bounded bulk native calls, aggregating
references across the initial working set rather than making one call per scalar.
Both bulk read actions may return a nonempty subset; the client verifies every
returned hash and length, then continues only the outstanding requested hashes.

## Durable stages and atomic import

The adapter reuses the tested Python business implementations:

1. Pull and atomically preserve owner feedback/profile edits and cursors.
2. Save an operation marker, run API collection with its stable collection ID.
3. Build/freeze the official issue plan, preserve each collected evidence package,
   strictly preview, then atomically apply the validated issues.
4. Complete bounded original-metadata enrichment.
5. Export the existing host-agent queue once, then persist queue bytes, queue hash,
   job, candidate membership and operation phase together in D1.
6. The existing importer verifies complete queue coverage and evidence-grounded
   structured judgments before its SQLite transaction. One D1 atomic revision
   then commits all row changes, exact result file, digest, cursors and phase.

The optional `cloud_collection.run(..., checkpoint=..., hydrate_official=...)`
hooks preserve legacy defaults. The D1 caller checkpoints after durable local
stage saves. A callback failure propagates; the last confirmed D1 revision is the
recovery source. An external API request interrupted before its D1 boundary may
be repeated with idempotent upserts. This is not a promise of exactly-once network
collection. Completed durable collection stages and frozen queues are reused;
replay reports `collection_performed=False`.

A stage may exceed 500 mutations. Immutable objects are uploaded and verified
first. `begin_operation`, bounded `stage_operation` pages, and
`verify_operation` freeze the exact mutation/file hash chain. Only
`commit_staged_operation` exposes the complete delta under one revision CAS.
Neither half a judgment import nor partially staged files advance the head.
Stable request IDs make identical retries idempotent; changing a payload is a
separate operation. After an uncertain/lost response, open a fresh working set
from D1; it reveals whether that exact stage committed. Do not force a stale
workspace onto a newer head.

## Row and file safety

- Compare only fetched row images plus newly created local keys. Unknown omitted
  rows never become deletes.
- Before inserting a new key from a partial table, query that exact key at the
  original revision. If it exists, fail closed and require hydration before
  repeating the business operation. Never overwrite it as though absent.
- Every upsert carries the original normalized `record_sha256` or explicit null
  for proven absence. File updates similarly carry original object hashes.
- New/replaced implicit rowids are allocated above the **global** table maximum;
  INTEGER primary-key aliases use the restored global `sqlite_sequence`.
- Only the two transient profile-file reconciliation meta keys may be tombstoned.
  Other deletes and writes to cold cleanup/notification/schema tables fail.
- No old file is deleted because it was not materialized in scratch space.
- PDF file bodies are rejected before staging a runtime artifact checkpoint.
- Revision CAS prevents lost profile/cursor edits or stale concurrent commits.

## Publication completeness and tests

Publication requires all declared renderer domains. Missing pipeline rows,
missing daily selection snapshots, or snapshots with unavailable historical
judgments block publication and identify the affected run. The adapter never
silently replaces historical selections with an empty view. The existing Site
remains untouched until a complete revision-fenced publication succeeds.

`tests/test_d1_runtime.py` uses synthetic fixtures and verifies:

- End-to-end Python calls against the **actual JavaScript Worker dispatcher**
  through local private JSON lines, including fresh-workspace recovery.
- Complete renderer output equality against a full database across several
  historical dates, profile changes and an explicit rescreen.
- Daily API → official validation/import → enrichment → frozen queue order,
  durable stage recovery, and honest collection-versus-replay reporting.
- Lost commit responses, malformed/partial judgment rejection, unknown existing
  keys, preserved observation timestamps, cold history retention, trusted-schema
  mismatch, global rowid allocation, and >500 changes in one atomic revision.
- Newly eligible papers recover existing retry history before enrichment; a lost
  API checkpoint response recovers in a fresh workspace without collecting again.

These are synthetic compatibility tests. They do not constitute a deployment,
live collection, real-data migration, or authorization to activate a Site.

An additional authorized read-only release check on 2026-10-16 compared the
actual Worker revision-1 projection against the complete source database. All
2,591 Site records were byte-for-byte equal. A fresh working set required 89
bounded native reads and 21,687,485 JSON response bytes in the local bridge:
57 row pages, 13 bulk descriptor reads, 14 bulk chunk reads, two file pages, and
head/manifest/summary. This is local compatibility/performance evidence, not a
network latency claim. No source or Worker fixture rows were changed.

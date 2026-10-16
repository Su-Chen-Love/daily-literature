# D1 main-store migration and acceptance

Status: implementation candidate. Passing local tests does not establish hosted
quota, native connector availability, unattended scheduling, or a completed
single-writer handoff. Keep the current writer authoritative until every gate
below is evidenced. No PDF import is part of this migration.

## Storage and authority

The existing private Site's `DB` binding stores the complete logical SQLite
state, typed row versions, exact content-addressed large values and runtime
artifacts. Existing display tables remain separate. The additive migration does
not overwrite their records, settings, or active generation. There is no new
R2 binding, credential store, raw HTTP transport, or Library dependency.

Main-store operations have a fixed owner namespace and closed action allowlists.
The application trusts only the existing platform-delivered owner identity.
Neither tool arguments nor an arbitrary SQL string can select another owner,
database binding, or backend. This is a single-owner application; future tenant
support requires a separate reviewed authorization design.

Exact repeated bytes share one SHA-256 object/chunk. Different historical values
remain different, and every application-history row retains its stable identity.
Rows and object payloads are immutable; revision pointers use compare-and-swap.
Retries reuse operation IDs and hashes. No-op writes neither create new visible
revisions nor duplicate content payloads. Small operation receipts and indexed
projections are intentional metadata, not a promise of zero storage growth.
Managed display snapshots also share exact content-addressed record payloads.
Existing inline generations remain readable and untouched. Legacy local writers
continue to create inline snapshots until the explicit handoff, so a pre-handoff
code rollback does not require converting their active display. After a managed
snapshot becomes active, rollback must use a compatible reader or a verified
restore; an old reader cannot interpret shared-payload references.

Unique new history is retained. No automatic deletion of old backups, unique
history, abandoned staging data, or unreferenced chunks is authorized here.

## Native interface

Four additional tools extend the existing connected private Site:

- `read_main_store`: bounded revision-pinned domain reads, objects, files, counts
  and observed capacity; never arbitrary SQL.
- `write_main_store`: chunk/object staging, frozen imports, staged atomic changes
  and idempotent main-head CAS. This alone does not activate the Site display.
- `publish_main_snapshot`: preview by default at the host layer; explicit
  activation requires the expected main revision, current display generation,
  published main revision and writer mode. It atomically updates display, mode
  and consumed-edit acknowledgements.
- `read_site_backup`: one atomic pin of main head, retained complete display
  generations, pointers and event high-watermarks; bounded byte/event exports.

Transport is the approved host's native tool calls. Python's JSON-lines bridge
does not discover or manufacture access, use a bearer token, or call a model API.
The host evaluates each frozen judgment queue and imports the complete validated
result set. New research interests and screening preferences are consumed before
future queues; concurrent or later edits remain pending until actually committed.

## Gates before deployment and import

1. Pin source and run all Python tests, every Worker suite, the official MCP SDK
   discovery/call test, and the additive-schema equivalence test. Inspect the new
   Drizzle migration; never edit an already-applied migration.
2. Review exact source-only changes before syncing the approved public feature
   branch. No database, profile, paper payload, private fixture, deployment
   credential, or account identifier belongs in a source commit.
3. Retain the prior private Site version, read health, access policy, active
   generation and pending edit cursors. Existing generic table tools are not a
   complete database backup unless every page is verifiably captured.
4. Deploy to the original private Site with an explicitly approved short-lived
   source credential, handled only by the official opaque helper. Preserve URL,
   owner access and the existing DB binding. Applying the new migration creates
   only new tables/indexes and the empty main-head seed.
5. Before importing main data, use the new read-only export to capture all
   retained complete Site views and pinned edit streams. Incomplete staging
   generations are explicitly identified as uncommitted, not silently called
   backed up. Confirm anonymous denial and signed-in native read success.
6. Build a lossless package from a consistent closed copy. Verify all row types,
   table inventories, hidden rowids, sequence high-watermarks, artifact hashes,
   and a trusted-schema complete restore. Do not execute manifest-supplied SQL.
   Preserve the original snapshot and its audit separately.
7. Upload bounded packed chunks and row pages into an isolated import. Verify
   whole objects, table chains, row counts, schema and consumed-event watermarks.
   A staging failure leaves existing main/display heads intact. Successful
   staging is not permission to publish a stale copy over newer Mac/Site work.

## Full-data acceptance

- Measure actual D1 allocated bytes, content bytes, row versions, retained
  revisions, native call counts and payload bytes. Document Sites quota as
  unknown until the platform provides evidence; do not substitute another
  Cloudflare product's plan/pricing. Do not create a new paid service.
- Export the imported revision through native reads, restore it independently,
  and compare every logical table and runtime artifact against the source copy.
- Compare the real full-database display records with the reduced daily working
  set record by record. A count-only match is insufficient. Unexplained history,
  status, recommendation or profile differences block publication.
- Test an actual scheduled fresh-input run: restore only the pinned working set,
  consume Site edits, collect API sources, fetch/import official issues, enrich
  abstracts, export the frozen queue, obtain host judgments, atomically import,
  and build a complete view. Retain source failures and deferred work honestly.
  A manual run is not a scheduled acceptance test; a clean directory is not proof
  of a newly provisioned machine.
- Restart after each durable stage, retry lost responses, race two writers,
  refuse stale revisions/cursors, verify no automatic re-screen of old work, and
  prove no-op content/row payload sizes stay constant. Do not activate a partial
  view generated from an incomplete historical projection.

## Backup and recovery

`d1_backup` captures one main revision and the Site's pending streams at one SQL
read snapshot. Its bundle contains a freshly generated complete main package,
all retained complete display generations, event evidence and checksums. Exact
main restore and subsequent pending-event replay are separately labeled outputs.
This is a logical state backup; old internal MVCC versions and orphan chunks are
not required to reconstruct the pinned current state. Application-history rows
are included, even when daily work does not fetch them.

Restore into an absent private directory. Use independently trusted schema and
verify all bytes before publication. A derived operational copy must explicitly
relocate runtime paths, preserve unavailable/local-only rollback provenance, and
refuse active dependencies that are missing. Never run historical cleanup or
restore commands automatically. Original local PDFs and rollback databases stay
untouched; the active cloud PDF registry begins empty.

After cloud acceptance, configure the Mac only for periodic versioned backup
pulls under the existing approved scope. Verify a full export, interrupted retry
and independent restore before relying on that schedule. No periodic backup or
daily analysis schedule is installed by these modules.

## Final handoff and rollback

Inventory the actual current writers and wait for their jobs to become terminal.
First compare final Mac state read-only and identify concrete unique/newer data.
The user declined the earlier incremental-file transfer in favor of a cloud
re-run; do not reinstate that declined transfer automatically. Resolve any
remaining unique-data difference and its minimal transfer scope before handoff.
Reconciliation uses stable keys, schema/base fingerprints, explicit deletions
and complete logical hashes.
Do not overwrite a new local result with an earlier acceptance fixture.

Only then pause the explicitly identified local analysis/publishing writers,
commit the reconciled main revision and activate the complete matching Site view.
The publication transaction sets `writer_mode=d1-main`; legacy publication and
acknowledgement paths test this fence inside SQL. Confirm the cloud schedule and
backup-only local behavior before declaring cloud authority.

Before handoff, rollback is the retained local writer and prior Site version.
After handoff, stop cloud writers first, restore a verified complete bundle into
an isolated copy, replay pending edits and reconcile any later changes. Switching
authority back requires an explicit reviewed recovery procedure; never remove
the writer fence or move the main head backwards merely to make a retry pass.

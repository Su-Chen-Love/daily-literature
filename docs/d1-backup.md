# Complete native D1 backup and recovery

`academic_radar.d1_backup` is a read-only, native-callback client. It has no HTTP,
Library, token, credential, deployment, or remote-write fallback. Downloading a
backup does not acknowledge Site events, advance a remote cursor, or change the
main-store head.

## SQLite portability and safe failure diagnosis

Python builds that omit SQLite extension loading (including some system Python
builds on macOS) do not expose `Connection.enable_load_extension`. Backup creation
supports those builds without enabling extension loading. When the disabling API
exists it must successfully accept `False`; an error is fatal and the connection
is closed. `trusted_schema=OFF` and the independent schema/table checks still
apply before any downloaded rows are inserted.

For `backup-export`, `backup-restore`, and `schema-export`, internally generated
trusted schema databases live in a fresh private directory beneath the validated
output parent. Both bundled-schema and explicitly pinned-schema modes follow this
rule. This avoids system temporary-directory aliases such as macOS `/var` without
changing `TMPDIR`, resolving user-supplied paths, or relaxing symlink rejection.
Missing, non-directory, traversal-containing, or symlinked temporary parents are
refused. Temporary schema directories are removed on success and failure.

The isolated acceptance test invokes the actual pinned `-I -B` source loader under
a child-process-only symlinked temporary-directory fixture, then completes a
synthetic export, offline verification, and exact restore. It covers both schema
modes and checks the transition from the five metadata requests to the first row
request. It does not call a real service or change the host's environment.

An interrupted or failed backup is not a completed recovery point. Preserve its
closed spool and source pin. Diagnose already captured response envelopes and
the local runtime without replaying a native request. A code fix requires a new
reviewed source manifest and a new bounded read-only run; do not relabel the old
spool as successful or reuse its one-use claims.

## What a backup contains

```
backup.json
main/manifest.json
main/tables/*.jsonl
main/objects/<chunk-sha256>
site/snapshot.json
site/generations/<generation-sha256>.jsonl
site/feedback.jsonl
site/profiles.jsonl
```

The `main/` directory is a complete [lossless package](d1-main-store-format.md)
for one explicitly captured committed D1 revision. It includes every application
table and row, including cold/history tables and empty tables, every current
runtime/history artifact, and every object referenced by those rows/files.

The client never reuses the initial import's stale row counts or logical hashes.
It downloads current revision-pinned rows, checks canonical typed row hashes,
creates SQLite using independently trusted schema only, and compares every stored
row's expanded typed values, stable key and hidden rowid to its downloaded image.
It then exports that complete closed database again, deriving fresh table counts,
logical audit hashes, rowid hashes, table transport chains and object descriptors.
SQLite affinity coercion, rowid changes, missing rows, broken foreign keys or
integrity failures prevent publication.

The Site portion retains every complete generation captured in the snapshot and
the full feedback/profile event streams through the captured high-water marks.
Each Site record's exact `data` string and checksum is retained; its JSON-looking
content is not reformatted. Complete generation count and fingerprint are checked
independently after chunk reconstruction. Uncommitted display generations are
explicitly counted in the snapshot and are not presented as complete generations.

This reconstructs complete logical state at the chosen revision. Superseded
internal D1 MVCC versions and unreferenced/orphaned chunks are not necessary for
that reconstruction and are outside the bundle. Ordinary application history
rows and all retained complete Site generations are included.

## One atomic capture

The first and only live capture is `site_call("begin", {})`. The Site Worker
reads `snapshot.main_head = {revision, import_id}`, complete generation metadata,
Site pointers, and feedback/profile high-water marks in one SQLite statement.
The response uses:

```
hash_algorithm: "sha256-canonical-json-v1"
snapshot_sha256: SHA256(canonical ASCII sorted JSON of snapshot)
```

The client requires this contract and does not fall back to separate live-head
reads. Every later main-store operation names the captured revision. The original
manifest's schema/column metadata is obtained using `read_manifest`; a separate
`export_summary` provides current row counts for every table and current file
count at the same immutable revision.

The exact reconstructed `meta` rows must agree with the pinned main-store
watermarks for feedback, profile events and sync-request acknowledgement. Main
feedback/profile watermarks cannot exceed the atomically captured Site event
limits. A mismatch blocks the backup rather than silently skipping events.

## Bounded transport

Supply two host-owned callables:

```
main_call(action, data) -> decoded successful JSON object
site_call(action, data) -> decoded successful JSON object
```

The main callable exposes only read operations used here:

- `read_manifest`, `export_summary`
- `export_rows`, `query_files`
- `read_objects`, `read_chunks`

The Site callable exposes `begin`, `records`, `feedback`, `profiles`, and
`event_chunks`. These map to the native main-store/Site-backup bridge. This module
never sends SQL or selects a remote credential, URL, database binding or account.

Rows and files consume all entries before honoring a terminal flag. The final
page may contain records but must have no continuation. Strictly increasing
cursors, pinned revision checks and independently captured counts prove coverage. Content-addressed
objects are reconstructed from verified chunks of at most 128 KiB. Descriptor/chunk reads batch up to 200 requested hashes and accept only a
nonempty verified subset of that request. Remaining hashes are retried without
refetching successful content. Local chunk
storage is deduplicated; decoded scalar caching is bounded by the package reader.
Native responses over 1 MiB are rejected.

Site generation records may span several 256 KiB raw-byte chunks. Each fragment
must agree on generation, kind/key, exact checksum, declared total size and byte
offset. The complete generation fingerprint is recomputed from its ordered
`kind:key:checksum` lines with newline separators.

A profile event too large for an ordinary event page returns an `oversized`
marker with sequence, exact canonical byte length and SHA-256. The client reads
`event_chunks` at bounded offsets, verifies identity, completion and whole-event
SHA-256, parses canonical JSON, and only then advances the event cursor. Valid
large Unicode edits therefore remain recoverable. A gap, duplicate, inconsistent
limit, skipped fragment, corrupted hash or non-progressing cursor fails closed.

## API

```
export_backup(main_call, site_call, output_dir,
              trusted_schema_database=independently_trusted_database)

verify_backup(backup_dir, expected_backup_sha256=None)

restore_backup(backup_dir, output_dir,
               trusted_schema_database=independently_trusted_database,
               expected_backup_sha256=None,
               apply_pending=True,
               profile_relative_path=None,
               source_root=None)

export_site_backup(site_call, output_dir)
verify_site_backup(backup_dir, expected_site_backup_sha256=None)
```

Export downloads to private staging. It verifies the complete bundle, then uses
atomic no-overwrite publication into a previously absent output directory. The
returned summary contains the backup root hash, captured revision, fresh main
manifest/logical hashes, counts, and numbers of pending feedback/profile events. It also reports total native call
count and counts per action so transport practicality can be measured.
No source row content or private path needs to be printed.

`backup_sha256` hashes exact canonical `backup.json` bytes including its final
newline. The manifest hashes every file in `main/` and `site/`. File paths,
symlinks, unlisted members, expected member hashes and the nested main package are
checked again during verification. Pass the root hash from an independently
verified transfer when restoring an untrusted copy.

## Exact recovery and pending edits

Recovery output is:

```
state/         # full recovered database and runtime/history files
site/          # unchanged raw Site backup evidence
recovery.json  # captured revision, base/current logical hashes, replay result
```

With `apply_pending=False` and no `source_root`, `state/` is the exact main-store logical state at the
captured revision. Full original SQLite inventory equivalence is checked by the
lossless package restore. The Site logs remain alongside it for later review or
replay.

With `apply_pending=True` (the default), recovery first proves that same exact
base state. It then applies only feedback/profile events above the main-store
watermarks through the captured Site high-water marks, using the existing domain
validators. The resulting operational state is explicitly labeled as derived
state in `recovery.json`; the immutable input `main/` package remains unchanged.
No event is acknowledged remotely. A pending sync request remains pending; a
backup cannot claim the requested pipeline work has run.

Feedback replay requires consecutive sequences, known papers and valid domain
values. Profile replay requires consecutive versions, the matching base profile
hash and matching base screening preferences. It retains the raw event evidence,
records applied profile events, and safely reconciles the profile file. This does
not start collection, scoring or re-screening. A conflict or missing dependency
leaves the requested recovery destination absent. Exact base recovery remains
available by explicitly choosing `apply_pending=False`.

`profile_relative_path` may identify the intended safe profile file inside
`state/`. Otherwise the saved configuration must name a safe relative path.
An absolute or `state://` profile location needs an explicit relative override
or an approved `source_root` mapping; tilde/external locations fail closed. The
database and its `-wal`, `-shm`, and `-journal` sidecars cannot be profile targets.
Recovery never writes to the old computer's original path.
Ordinary profile-application audit timestamps reflect local replay time and are
part of the separately labeled derived state, not the exact base backup.

## Explicit operational path relocation

Raw main rows may contain absolute references from the captured source runtime.
An exact logical restore preserves those bytes and reports
`operational_ready: false`; it does not claim those old paths still exist.

Supplying `source_root` explicitly requests a separately labeled derived recovery.
The root is used only as a string-prefix map, never opened or read. Known
`state://` references and references inside that exact root must resolve to files
included in the restored tree. Unknown absolute paths, missing artifacts, active
fulltext hash/size mismatches and invalid active/historical reference graphs fail
before publication. The existing checkpoint reference validator propagates scope
through artifact references. Final paths point into the published output, not the
temporary staging directory.

The selected `profile_relative_path`, when present, also becomes the derived
configuration's profile file. Its bytes and SHA-256 must match the single active
profile record. Configuration is rewritten with a local state directory, safe
relative paths, and delivery/legacy cloud synchronization disabled.

Archived `archive://` markers and their reference-provenance document remain
unchanged and inert. Plain local rollback `backup_path` references are rejected
until reviewed archival provenance exists. Recovery never executes cleanup,
rollback, or restore commands. `recovery.json` records relocation and the changed
logical hash; `operational_ready` becomes true only after these checks. This flag
certifies local reference readiness, not a completed live collection, publication,
or scheduler integration.

## Site-only backup before the first main import

`export_site_backup(site_call, output_dir)` uses the same atomic capture,
generation checksum proof, event high-water checks and large-event chunks. It
also accepts a valid empty main head `{revision: 0, import_id: null}`. No main
callback is supplied and no main completeness claim is made.

The distinct `academic-radar-site-only-backup` format contains `site-backup.json`
and the same `site/` evidence tree. `site_backup_sha256` pins the canonical root
file. `verify_site_backup` checks every listed member and all generation/event
proofs. The full main backup verifier rejects this separate format. This read-only
capture is suitable before an initial main-store import; it neither imports main
rows nor changes the Site's active view or acknowledgements.

## Tests and acceptance

`python -m unittest discover -s tests -p test_d1_backup.py -v` uses synthetic
native callback fixtures and an actual local JavaScript Worker bridge. Coverage includes a current revision whose row counts,
values and artifacts differ from the original import; complete main/Site backup;
exact base restore; pending feedback/profile replay; large Unicode event chunks;
revision drift; missing rows/count mismatch; bad row/chunk/generation hashes;
event gaps; main/meta watermark mismatch; profile conflicts; unknown papers;
symlink/tampered bundles; missing atomic capture; existing destinations; and
unchanged original main package after replay; revision-zero Site-only capture;
explicit operational relocation; profile override consistency; active reference
validation; and disabled archival rollback provenance. The real Worker test
advances both main revision and Site events after capture to prove the export
remains pinned, including a 100,000-character Unicode profile event.

A private local real-data Worker roundtrip recovered 106,692 rows and 376 files
with the exact original manifest and logical inventory. It used 460 native read
calls: 252 row pages, 50 descriptor batches, 152 chunk batches, one file page, two
main metadata calls, and three Site calls. That fixture contained real main data
and an empty synthetic Site, not the live Site history. Both source database
byte hashes and timestamps were unchanged. These measurements are local evidence.

A live deployment still needs independently observed native-adapter integration,
full export/recovery acceptance and private backup storage before it can be
considered operational. Synthetic tests and an offline private-data package
roundtrip do not establish remote deployment, scheduling, credentials, availability
or successful daily execution.


## SQLite builds that retain shared-memory files

Some SQLite/VFS builds retain a `-shm` file after connection close and even after
SQLite completes a checkpoint and switches the database to DELETE journal mode.
Neither close nor garbage collection is therefore a portable proof that a
snapshot has no sidecars. The strict source-sidecar rejection remains unchanged;
never remove sidecars to force an arbitrary user database through that check.

The trusted-schema builder now stays separate from the empty schema supplied to
backup and restore. Migrations can use their normal WAL workspace. After the
current or bundled historical DDL has passed the existing fingerprint checks,
that DDL is materialized into a new DELETE-mode database containing no seed rows.
Explicit trusted-schema inputs still require their exact file hash, stable stamp,
safe path and absent sidecars before their independently trusted DDL is used.

Pending-profile restoration reuses the fresh restore-owned DELETE connection for
the existing transactional profile-file reconciliation. Ordinary application
callers retain their previous connection behavior. No external Site event is
acknowledged by restoring a backup.

Synthetic tests use SQLite's backup API to create independent, absent snapshot
files from their owned test writers, while preserving any retained writer sidecar.
They also prove that a sidecar-bearing source remains rejected, that schema
snapshots contain no seed rows, and that pending-profile restore never enables WAL.
Actual isolated CLI tests cover exact-main and pending-event restores with both
bundled and explicitly pinned schemas. A platform-specific synthetic gate must
pass before beginning a new live read-only backup.

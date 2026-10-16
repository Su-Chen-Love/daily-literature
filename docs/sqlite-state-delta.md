# Closed-snapshot SQLite delta transfer

This optional, database-only transfer avoids resending an unchanged large
migration archive after the source's last daily run. It is not a migration,
backup replacement, history-file transport, scheduler, or cutover command.
Use reviewed source code and Python 3.9 or later. No credentials, network calls,
pickle loading, executable patch payloads, or patch-supplied SQL are involved.

## Prerequisites and ordering

1. Let the running source job finish normally. Confirm its final state and stop
   all writers for the snapshot window. Do not patch a live installation or
   silently change a running job's status.
2. Retain the exact original baseline snapshot from the already verified
   migration transfer. Retain the completed source's final snapshot separately.
   Use the existing SQLite online-backup/checkpoint workflow to obtain standalone
   copies, then close them. Do not copy a live SQLite main file, delete WAL files,
   or use a file copy to discard committed WAL contents. This tool rejects any
   adjacent `-wal`, `-shm`, or `-journal`, even empty ones.
3. Both inputs must have exactly the same schema, schema ledger, and normalization
   policy. Finish authorized PDF omissions on **disposable source snapshots**,
   preserving the full omission audit and original PDFs as the existing
   checkpoint policy requires. Never erase the installation's `fulltext_files`
   table just to make this tool accept it. The patch has no exclusion switch and
   no PDF-cleanup behavior. `--require-empty-table fulltext_files` merely asserts
   that both already-normalized snapshots have an empty registry. Every table,
   including that registry and `sqlite_sequence`, remains covered by the hashes.
4. Apply the patch to the corresponding **original-path baseline copy before
   path relocation**. An already relocated checkpoint is a different logical
   baseline and will fail verification. Avoid upgrades, migration-ledger edits,
   timestamps, path substitutions, or any other baseline changes in between.
   If schema versions differ, stop: this tool cannot reconcile them. Prepare a
   separately reviewed compatible baseline and target, or transfer a new full
   snapshot. Never edit hashes to force compatibility.
5. Store snapshots, patch, and hashes in private directories. Patches contain
   exact before and after row values and can expose private data or embedded
   secrets already present in the database. The mechanism needs no credentials;
   it does not sanitize arbitrary database contents.

Inputs are read-only copied into private temporary files before SQLite opens
those copies. File identity/size/modification checks and sidecar checks detect
ordinary concurrent changes. This is not a locking protocol for hostile local
writers; use closed snapshots in trusted, non-shared directories. No source
file is opened by SQLite, modified, relocated, or deleted by this tool.

## Export and transfer

Run from the reviewed repository checkout. All path names below are examples,
not production paths. The output file must not already exist.

```sh
python scripts/sqlite-state-delta.py inspect --db /private/base.sqlite3
python scripts/sqlite-state-delta.py inspect --db /private/final.sqlite3
python scripts/sqlite-state-delta.py export \
  --base /private/base.sqlite3 \
  --target /private/final.sqlite3 \
  --output /private/final-delta.json \
  --require-empty-table fulltext_files
```

Equivalent installed-module entry point:
`python -m academic_radar.sqlite_delta export ...`.

Export returns the patch byte length and `patch_sha256`, schema hash, full base
and target logical hashes, and per-table insert/update/delete counts. Save the
complete inspect results and export summary privately beside the transfer audit.
Compare counts and hashes with the approved source inventory. The command's
summary does not print row data. No files are uploaded by these commands.

Transfer the JSON file through the existing approved private transfer route.
Record its size and exact SHA-256 in the separately verified transfer manifest;
verify the actual received bytes against that manifest. A hash bundled with an
untrusted patch is not authentication. Use the trusted checksum, not a freshly
computed checksum of an unverified replacement file. Never run a downloaded
script or `pickle.loads` against a transferred payload. A patch is JSON data.

## Apply to a new copy and verify

```sh
python scripts/sqlite-state-delta.py apply \
  --base /private/original-normalized-base.sqlite3 \
  --patch /private/final-delta.json \
  --destination /private/new-final.sqlite3 \
  --sha256 TRUSTED_64_CHARACTER_PATCH_SHA256
python scripts/sqlite-state-delta.py inspect --db /private/new-final.sqlite3
```

The CLI requires `--sha256`. The Python API offers `expected_sha256` for callers
whose separate verified transport already enforces the checksum; production
callers should supply it when available. The destination and any destination
sidecars must be absent, with an existing real parent directory. Existing files,
symlinks, symlink ancestors, and parent-traversal paths are rejected. Inputs may
not be overwritten. A successful output is mode 0600 on POSIX.

Apply verifies the entire baseline inventory and schema, every changed row's
full before image/hash, and the absence of inserted keys. It then modifies only
a disposable baseline copy in one transaction. Changed rows are removed before
replacement rows are inserted, preserving valid unique-key swaps and FK cycles.
Foreign-key *actions* are disabled in this private transaction so cascade deletes
cannot silently alter rows absent from the patch. Full foreign-key and integrity
checks run on the complete target before commit. Every target table/count/hash,
hidden rowid mapping, and `sqlite_sequence` row/high-water mark must match.
Validation runs again after closing and reopening the staged output.

Only then is the verified file atomically linked to the absent destination on
the same filesystem. A raced-in destination is never replaced. Errors roll back
and discard the staging copy; no failed database is published. A process killed
abruptly can leave a private `.sqlite-delta-*` temporary directory, which is not
an accepted output. Inspect and remove stale temporary copies manually only
under the normal file-deletion rules. The original baseline and patch remain
untouched, so a corrected authorized attempt can use a fresh destination.

Compare the final inspect inventory to the exported target inventory, including
all tables and row counts. Logical equivalence does not promise identical
SQLite file bytes, page layout, freelists, or journal mode. Never use a whole-file
hash of the target snapshot as the expected hash of the reconstructed database.

## History files and path relocation remain checkpoint work

The delta transfers **only SQLite**. It does not transfer, rewrite, remove, or
assert availability of PDFs, queues, result JSON, digests, reports, profile files,
configuration files, or historical databases referenced by rows.

After the final source job, inventory its newly created or changed history files
and provide them through the existing approved checkpoint/history transfer.
Use a separately reviewed SHA-256 manifest naming each exact relative path,
size, and byte hash. Reject absolute paths, `..`, symlinks, duplicate/conflicting
paths, missing files, and unlisted files. Do not execute restored artifacts or
extract an unverified archive. Existing checkpoint verification/restore is the
supported validator; this delta tool intentionally does not add another archive
extractor or implement file-manifest application. Deleting history needs a
separate explicit record and authorization; absence from the delta means nothing
about a file's retention or availability.

Once the reconstructed original-path database and all approved artifacts are
staged, create a fresh full checkpoint using the existing reviewed reference
policy and restore it into a fresh destination. This performs the existing
operational-versus-historical reference checks, exact relocation, audit retention,
PDF omission policy, and restore verification. A completed/abandoned attachment
may use an explicit `archived_local`/`unavailable` decision when approved; missing
operational attachments still block acceptance. Do not bypass these checks
because the SQLite delta succeeded.

Only that verified full checkpoint is eligible for the later approved cloud
publication/cutover workflow. See [checkpoint-format.md](checkpoint-format.md),
[cloud-migration-runbook.md](cloud-migration-runbook.md), and
[library-checkpoint-protocol.md](library-checkpoint-protocol.md). Delta success
alone never activates a schedule, stops a Mac job, or declares migration complete.

## Format and compatibility

- Version 1 uses bounded JSON with exact keys, explicit lists for inserts,
  updates, and deletions for **every** table, including empty deletions. Duplicate
  JSON keys, JSON floats, NaN/Infinity constants, noncanonical encodings, unknown
  fields, conflicting operations, missing tables, and extra tables are rejected.
- Null, signed 64-bit integer, IEEE-754 REAL, UTF-8 text (including embedded NUL),
  and BLOB have explicit type tags. Integers use canonical decimal strings;
  REAL uses 16 lowercase hex characters for its exact big-endian 64-bit bit
  pattern; BLOB uses canonical base64. REAL infinities are supported as tagged
  bits; NaNs are rejected (SQLite does not preserve them as REAL). No arbitrary
  Python objects or deserialization hooks are accepted.
- Each table hash starts with `sha256(pickle.dumps(column_name_list, protocol=4))`
  and adds each ordered row tuple with `pickle.dumps(tuple, protocol=4)`, in
  declared primary-key order. Only SQLite primitive values are serialized;
  **pickle is never loaded**. Separate rowid-aware hashes preserve ordinary
  tables' hidden rowids, even for non-integer or composite primary keys.
- Schema fingerprints include exact `sqlite_master` declarations, indexes,
  `user_version`, `application_id`, and encoding. The patch stores the fingerprint
  and table metadata, never schema SQL. No schema change is supported.
- User tables require declared, stable, non-null, unambiguous primary keys.
  Nullable *declarations* are accepted only when every actual key is non-null.
  PK changes appear as an explicit delete plus insert. `sqlite_sequence` is the
  sole special internal table: its validated unique non-null `name` is its
  logical key; its actual rowids and full contents are preserved.
- WITHOUT ROWID tables are supported. Triggers, views, virtual tables, generated
  or hidden columns, expression indexes, custom collations, no-PK tables,
  all-shadowed rowid names, and other SQLite internal tables (including ANALYZE
  statistics) are rejected. Built-in BINARY/NOCASE/RTRIM collations are accepted.
  Rejection is deliberate; do not drop source schema features to get around it.
- Bounds: 64 MiB patch, 16 MiB decoded scalar, 256 tables, 1,024 columns per
  table, 2,000,000 total rows per snapshot, 100,000 changed-row operations,
  2,000,000 JSON nodes, 32 JSON nesting levels. The exporter and importer use
  these same limits. Use a new full checkpoint if a change exceeds the limits.
  Snapshots and inventories are processed locally in memory; budget space for
  two snapshot copies plus row inventories when exporting, and baseline/staging
  copies when applying. This is a bounded migration tool, not a streaming DBMS.

## Synthetic verification

```sh
PYTHONPATH=src PYTHONWARNINGS=error python -m unittest discover -s tests -p test_sqlite_delta.py -v
PYTHONPATH=src PYTHONWARNINGS=error python -m unittest discover -s tests -v
```

Tests use only generated fixtures, including the repository's real migration
schema. Passing tests do not authorize accessing, transferring, or applying a
patch to the owner's private data.

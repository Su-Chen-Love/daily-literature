# Lossless D1 main-store package, version 1

This is an **offline normalization and recovery format**, not a deployment or an
approval to upload private state. `academic_radar.d1_package` contains no network,
credential, publication-to-service, or original-state deletion operations.

## Guarantees and trust boundary

- Export covers every supported SQLite table, including empty tables and
  `sqlite_sequence`. It retains exact schema declarations, declared columns and
  primary-key order, stored values, hidden rowids, autoincrement high-water marks,
  database encoding, `user_version`, and `application_id`.
- TEXT is preserved as exact UTF-8 content; JSON-looking TEXT is never parsed or
  reformatted. INTEGER is a signed 64-bit decimal string, REAL is its exact
  big-endian IEEE-754 binary64 bit pattern, BLOB is bytes, and NULL stays NULL.
- All supplied runtime/history files are included with exact bytes, safe relative
  names, ordinary permission bits, and empty directories. Embedded database/file
  paths are not rewritten. A separately authorized runtime relocation layer must
  handle paths when a different execution environment requires that.
- Repeated content is replaced only by an exact SHA-256 content reference. TEXT
  and BLOB may share the same raw object while retaining different scalar types.
  Files share the same object store. There is no semantic deduplication, numeric
  normalization, JSON normalization, truncation, or omission of older history.
- Restore never executes SQL from the package. Its mandatory
  `trusted_schema_database` must be an independently trusted, closed database,
  created from known application migrations or an independently verified source.
  It must not come from the import or a file accompanying that untrusted import.
  The complete schema fingerprint must match before DDL is executed. DDL is read
  only from the independently trusted database. A schema database inside the
  package is explicitly rejected. A matching hash provided by the package is not
  an independent trust decision.
- All output is staged in a private sibling directory. Every object, reference,
  table count, row image, schema, logical table hash, rowid hash, foreign key, and
  SQLite integrity result must validate before atomic no-overwrite publication.
  An existing destination, including one raced into place, is never replaced.
  Failures discard staging and leave the requested destination absent.
- Export requires paused writers and a closed, checkpointed database. WAL, SHM,
  or journal sidecars are rejected. Runtime files cannot install sidecars at the
  restored database path. Inputs are read-only; the original database
  is copied before SQLite opens it. Source identity/size/timestamps and complete
  runtime-tree membership are checked again immediately before publication.

SHA-256 provides integrity, not sender authentication. When restoring a transfer,
pass `expected_manifest_sha256` from a separately verified transport/root record.
Private package files must stay private; this format does not redact state.

## Layout

```
manifest.json
objects/<chunk-sha256>
tables/<sha256-utf8-table-name>.jsonl
```

Every JSON document/line uses ASCII JSON with sorted keys, compact separators,
`ensure_ascii=True`, and no floating-point JSON numbers. Every disk JSON document
and row line has exactly one final newline. Object files are raw bytes.
Duplicate JSON members, noncanonical scalars, noncanonical JSON, extra/missing
members, path traversal, absolute paths, control characters or paths longer than
1,000 characters, Windows path syntax, symlinks, device
files, file/directory collisions, and unlisted package members are rejected.
No pickle payload is loaded: the legacy audit formula only *writes* primitive
SQLite values with Python pickle protocol 4 before hashing them.

### Root manifest

The root has these exact fields:

- `format`: `academic-radar-d1-main-store`; `version`: `1`
- `hash_algorithm`: the existing SQLiteDelta audit algorithm, or the explicitly
  distinct rowid-fallback algorithm described below
- `database_path`, `database_mode`: original safe relative destination and ordinary
  mode bits; the default database path is `papers.sqlite3`
- `schema`: `objects`, `user_version`, `application_id`, `encoding`; objects are
  ordered `[type, name, tbl_name, sql]` arrays from `sqlite_master`
- `schema_sha256`: SHA-256 of canonical schema JSON without a final newline
- `inventory`: existing SQLiteDelta table metadata, counts and audit hashes
- `tables`: ordered table-file descriptors
- `objects`: whole-object descriptors sorted by whole-object SHA-256
- `files`: `{path, mode, bytes, sha256}` entries sorted by path
- `directories`: `{path, mode}` entries sorted by path
- `statistics`: `referenced_object_bytes`, `unique_object_bytes`,
  `unique_chunk_bytes`, `object_count`, `chunk_count`

A table-file descriptor is:

```
{name, path, bytes, rows, sha256, table_chain_sha256, key_kind}
```

`sha256` hashes the exact JSONL bytes. `key_kind` is `primary_key` or `rowid`.
Column names, primary keys, and hidden-rowid metadata are in
`inventory.tables`, joined by `name`. Every table, including every empty table,
has both inventory and table-file entries.

`manifest_sha256` is SHA-256 of the complete canonical `manifest.json` bytes,
**including the final newline**. It is returned outside the manifest, avoiding a
self-referential hash. It is the immutable root identifier.

### Typed rows and stable identity

A normalized row contains exactly:

```
{table, key_sha256, key, values, rowid, row_sha256, record_sha256}
```

`values` follows inventory column order. Inline scalar shapes are:

```
{"t":"null"}
{"t":"integer","v":"9223372036854775807"}
{"t":"real","v":"3ff0000000000000"}
{"t":"text","v":"exact text"}
{"t":"blob","v":"AAH/"}
```

By default TEXT/BLOB values at least 1,024 raw bytes become:

```
{"t":"text","ref":"<whole-object-sha256>","bytes":12345}
```

BLOB references have `t: "blob"`. Their decoded bytes must match the descriptor
size and whole-object hash. The typed wrapper retains the original storage class.

`key` is an inline typed-scalar array of primary-key values in declared PK order.
If a table has no primary key or its current primary key contains NULLs or is
ambiguous, the entire table uses hidden rowid and `key` is the one-element typed
rowid array. `rowid` is a typed INTEGER for rowid tables, otherwise JSON null.
`key_sha256` hashes canonical key JSON without a newline. Hashes do not authorize
an alternative key: readers verify key values against the actual row/schema.

`record_sha256` hashes canonical JSON of the row envelope excluding that one
field, without a newline. `row_sha256` hashes canonical JSON of
`{values: normalized_values, rowid: typed_rowid_or_null}`, without a newline.
This covers typed references as stored, allowing the Worker and Python reader to
verify it identically. Legacy pickle4 hashes remain exclusively in the complete
SQLite inventory; recovery expands every reference and independently recomputes
that full original inventory. No opaque, unverifiable per-row audit hash is
accepted by the Worker.

JSONL records are sorted by `key_sha256` in ascending binary order. The table
verification chain is initialized to SHA-256 of empty bytes. For each row:

```
next = SHA256(ASCII(previous_hex + "\n" + key_sha256 + "\n" + record_sha256))
```

This transport order is independent of SQLite logical audit order. Export uses a
private SQLite sorting spool rather than keeping all normalized rows in memory.

### Logical audit compatibility

For ordinary valid-primary-key tables, `inventory` is byte-for-byte equivalent
as JSON data to `sqlite_delta.inspect_snapshot`. Per-table logical hashes are
SHA-256 initialized with pickle4 column names, then updated with pickle4 row
values in SQLite `ORDER BY` primary-key order. Rowid audit hashes additionally
include hidden rowid. The inventory's `logical_sha256` hashes canonical JSON of
schema fingerprint and complete table inventories.

For rowid fallback, logical rows instead use hidden-rowid order. The manifest
explicitly identifies `sha256-pickle4-columns-pk-or-rowid-ordered-rows-v1`; it does
not claim compatibility with SQLiteDelta's narrower supported input subset.
Both declared primary keys and the chosen fallback strategy are retained.

### Exact objects and chunks

A whole-object descriptor is:

```
{sha256, bytes, chunks: [{sha256, bytes}, ...]}
```

`sha256` hashes raw whole-object bytes. The ordered chunk list reconstructs those
bytes exactly. Every nonfinal chunk is exactly 128 KiB; the final chunk is at
most 128 KiB. Empty objects have no chunks and the SHA-256 of empty bytes.
Chunk identifiers hash raw chunk bytes. Identical chunks are stored once, even
when repeated within one object. Hash/size conflicts and missing chunks fail.
The same object can be referenced by many rows/files, but unreferenced descriptors
are rejected from a complete package.

The root manifest is itself chunked for transport, with a separate whole-object
descriptor. Its descriptor does not occur recursively in its own `objects` list.

## Offline API

```
export_package(database_path, output_dir,
               files_root=None,
               database_relative_path="papers.sqlite3",
               externalize_threshold=1024)

verify_package(package_dir, expected_manifest_sha256=None)

restore_package(package_dir, output_dir,
                trusted_schema_database=independently_trusted_database,
                expected_manifest_sha256=None)

iter_native_pages(package_dir, max_page_bytes=512*1024, max_records=500)
iter_rows(package_dir, table_names=None, key_sha256s=None)
```

Export/verify return `{manifest_sha256, manifest}`. Restore returns only an audit
summary. `verify_package` verifies transfer hashes, row images, references and
coverage. Full SQLite logical-inventory equivalence is additionally verified by
restore, because reconstruction checks SQLite affinities, constraints and ordering.

Native pages are ordinary dictionaries `{kind, records}` and remain below the
requested canonical JSON byte limit. Kinds are `chunks`, `objects`, `manifest`,
then `rows`. Chunk records are `{sha256, bytes, base64}`. Object and manifest
records are whole-object descriptors. Row records use the row envelope above.
No single 317 MB SQLite upload or oversized manifest argument is needed.
A byte limit too small for one chunk or row fails rather than silently exceeding
it. Upload protocol framing may add bytes, so callers must retain headroom below
their MCP request ceiling and persist acknowledgements for resumability.

The initial import/recovery is complete. `iter_rows` can select a bounded working
set for recurring work, but omission is **never** a deletion instruction. A
runtime must retain each fetched base hash, commit only explicit changed rows,
and use revision/CAS protection. It must not compare an incomplete local database
to the full remote state and delete unseen rows. Objects and canonical records
allow unchanged content to remain in D1 between runs; this package module itself
does not send, deploy, switch the main store, or schedule work.

Shared adapter helpers are `ObjectStore`, `ObjectReader`, `normalize_row`, and
`read_row`. Their local object directory must exist. The normalizer uses the same
codec, stable keys and record hashes as complete export.

## Supported bounds and exclusions

Version 1 accepts ordinary tables and indexes with built-in collations. It fails
closed on views, triggers, virtual tables, generated/hidden columns, expression
indexes, nonstandard internal tables, or all hidden rowid names being shadowed.
These restrictions are explicit; no unsupported table is silently skipped.
Current guards include 256 tables, 1,024 columns/table, 2,000,000 rows, 16 MiB
per SQLite scalar, 128 KiB per normalized row, 64 MiB manifest, 100,000 runtime
files/directories, and 32 GiB aggregate unique content. Native pages default to
512 KiB. Ordinary mode bits are preserved; setuid/setgid/sticky bits, ownership,
ACLs, timestamps, sparse layout and physical SQLite page layout are not restored.
Source owner permissions must permit reading and directory traversal.

## Verification

`python -m unittest discover -s tests -p test_d1_package.py -v` runs synthetic-only
fixtures covering complete application schema, empty tables, types/float bits,
rowids and sequence high-water marks, UTF-16 database encoding, exact repeated
content, multi-chunk files, working-set selection, canonical hashes, missing or
corrupt content, resealed invalid keys and logical inventories, schema drift and
SQL injection, symlinks/traversal/collisions, source changes, WAL rejection,
existing/raced destinations, unchanged sources and bounded native pages.
Actual private-state acceptance should additionally compare all source/restored
SQLite logical inventories and file hashes independently, and record only
aggregate evidence outside the private staging area.

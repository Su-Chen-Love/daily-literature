# Portable private-state checkpoints (version 1)

This format is a **private full application-state backup**, not the read-only
website record export. It preserves every ordinary table in the primary SQLite
database, including profile versions, feedback/event history, run membership,
immutable recommendation history, agent jobs, and unknown future ordinary tables.
Explicit, audited migration exceptions for historical file availability and omitted PDFs are described below. Without that opt-in policy, missing references still fail closed.
The format is independent of the eventual private object-storage provider. It
contains no network client, credential provisioner, scheduler, or public URL.

## API and safe workflow

```python
from pathlib import Path
from academic_radar.checkpoints import (
    create_checkpoint, verify_checkpoint, restore_checkpoint, inventory_references,
)

# Pause the application/scheduler and any process writing artifacts first.
result = create_checkpoint(
    Path("/private/radar-state"),
    Path("/private/checkpoints/checkpoint-001"),
    # config_path=Path("/private/config/radar.toml"),  # optional external config
)
manifest = verify_checkpoint(Path(result["checkpoint"]))
restored = restore_checkpoint(
    Path(result["checkpoint"]), Path("/private/new-radar-state")
)
```

Both checkpoint output and restored destination must be **absent**, including an
empty directory. There is deliberately no overwrite flag. Restore into a new
path, inspect the restored application, and explicitly choose a separate
production promotion procedure. The checkpoint must be outside the source state.

The config must identify the selected state directory. An explicitly supplied
config may reside outside that directory and is normalized to `config.toml`.
The configured profile and every included data artifact must reside beneath
state. Explicit relocation entries can map legacy paths onto existing staged files. Use canonical paths without symbolic links or `..` components. On macOS,
resolve conventional aliases such as `/tmp` to their real locations first.

`create_checkpoint` and `restore_checkpoint` return JSON-serializable summaries.
`verify_checkpoint` returns the full validated manifest. Corrupt, incomplete,
unsafe, or unsupported checkpoints raise `CheckpointError` (a `ValueError`).
An existing output/destination raises `FileExistsError`. Storage/locking failures
can raise `OSError` or SQLite errors. No destination is exposed until validation
and staged preparation have succeeded. A directory-fsync failure after the
atomic rename can report an error despite the complete destination being present;
inspect that destination before retrying.

## Layout and identity

```text
checkpoint-001/
  manifest.json
  payload/
    papers.sqlite3
    config.toml
    research-profile.md
    fulltexts/<PDFs>
    agent_queue/<queues>
    results/<judgments>
    digests/<digests>
    ... additional supported/referenced artifacts
```

The UTF-8 manifest is an object with these fields:

- `format`: `academic-radar-state`
- `version`: integer `1`
- `database`: `papers.sqlite3`
- `created_at`: UTC ISO-8601 timestamp
- `files`: sorted `{path, size, sha256}` records; paths are relative to `payload/`
- `table_counts`: number of records in every non-internal SQLite table
- `excluded`: relative paths with explicit omission reasons
- `policy`: credential, reference, and external-side-effect policy labels
- `checkpoint_id`: lowercase SHA-256 of the manifest **without** `checkpoint_id`

Canonical JSON uses UTF-8, sorted keys, no insignificant whitespace, unescaped
Unicode, and no NaN/Infinity. `manifest.json` has a trailing newline, which is not
included in the checkpoint-ID hash. Consequently, an opaque transport's SHA-256
of the raw manifest file is distinct from `checkpoint_id`.

Each payload SHA-256 covers its **portable** contents. SQLite and JSON path
normalization changes those bytes, as does config re-serialization. Restored DB
and JSON bytes legitimately differ again because the final destination is
substituted. PDF and other opaque artifact bytes are preserved. Transport must
retain the exact checkpoint directory contents and run `verify_checkpoint`
after download. It must not interpret the directory as a tar/zip archive.

A checksum proves consistency and detects accidental corruption; it does **not**
authenticate the sender or provide encryption. Only use a trusted private store,
protected storage/transport, and an authenticated expected manifest/head. No
storage upload or new access grant is authorized by creating a local checkpoint.

## Consistency and portability

The creator holds a SQLite `BEGIN IMMEDIATE` lock while another read-only
connection uses the online-backup API. Thus committed WAL contents and all
ordinary tables are included consistently, with no raw WAL/SHM copying. The
snapshot uses rollback-journal mode and is compacted with `VACUUM` after path
normalization to remove stale freed pages. Integrity and foreign-key checks must
pass. No schema migration or application data mutation is performed on the
source. Runtime files are required to be stable: copied files are fingerprinted,
and supported artifact-directory inventories are checked again before publish.

The database lock is **not** a filesystem transaction. Pause every writer,
including external result writers, for an end-to-end consistent DB-plus-files
snapshot. Fingerprint/inventory checks detect ordinary concurrent changes, but
are not a substitute for a quiescent application or a trusted owned filesystem.

Known and future `*_path` SQLite columns are rebased, including:

- `fulltext_files.stored_path`
- `agent_jobs.queue_path` and `results_path`
- `notifications.digest_path`
- `cleanup_audits.backup_path` and `report_path`

Every `*_json` column and included JSON artifact is parsed recursively. Named
path fields (including `profile_path`, `queue_path`, `results_path`, and
`digest_path`), any `*_path` field, and standalone absolute filesystem-path strings
are rebased. This covers `pipeline_runs.details_json` and queue profile paths.
In the checkpoint they use `state://<safe relative path>`; restore substitutes the
**final destination**, never the staging path. Every reference must identify an
included regular file. Broken, external, excluded, directory, or traversal
references fail closed instead of silently creating a partial backup. Relative
references are interpreted relative to the state root.

The restored config uses `state_dir = "."` and a state-relative `profile_file`.
All other absolute filesystem configuration values are currently unsupported.
Source TOML is parsed and serialized so comments and old absolute paths do not
leak. Delivery and legacy website cloud sync are disabled, and the cloud-sync
credential-file pointer is removed. Re-establish credentials and explicitly
enable those integrations after verification. This does not install or start any
scheduler. Application run/job statuses are preserved; operational recovery of a
stale `running` job remains the worker's explicit responsibility.

Referenced historical cleanup backup databases are copied as opaque immutable
artifacts. Their internal paths are not recursively migrated, and they must
already be standalone backups without pending sidecars. Primary live-state
restore is the supported portable recovery mechanism.

## Included and excluded data

Always included:

- Primary SQLite database, normalized selected config, configured profile
- Regular artifacts in `fulltexts`, `agent_queue`, `agent_results`, `results`,
  `digests`, `official`, and `reports`, excluding credential-like files
- Additional regular files under state referenced by recognized DB/JSON paths,
  including a referenced cleanup report or standalone backup

Not included by default; recorded in `excluded` when present at the scanned
state level:

- `cloud-sync.json`, `.env` variants, credential/token/password/secret-like files,
  private-key/certificate containers (`.pem`, `.key`, `.p12`, `.pfx`, `.kdbx`)
- Unreferenced `backups`, code, environments, runtime files, logs, locks, service
  definitions, caches, arbitrary other state-root files/directories
- SQLite WAL/SHM/journal sidecars (represented by the consistent primary snapshot)
- Environment variables, OS keychains, browser authentication, provider keys,
  scheduler installation, object-store access, and website deployment

Recognized credential-like config/JSON fields, SQLite columns, and metadata keys
are rejected rather than redacted from logical state. `*_env` variable **names**
are permitted; their environment values are never read. Credentials must live in
an external secret store/environment, not in application state.

This is **not a general secret-discovery scanner**: it cannot infer that arbitrary
free-form prose, PDF bytes, or an opaque user file contains a secret. Operators
must keep credentials out of those files and review the checkpoint scope before
upload. Research profiles, feedback, and full texts remain private data even when
they contain no credentials.

Unrecognized path references embedded in prose or opaque binary data are not
rewritten. Virtual SQLite tables and persisted triggers are unsupported and
rejected. Archive extraction, external-reference relocation, file permission
restoration, Windows paths, and Windows atomic publication are unsupported.

## Restore defenses and publication

The verifier checks manifest identity, duplicate JSON keys, strict safe relative
paths, duplicate manifest members, exact file membership, every size and SHA-256,
SQLite integrity/foreign keys/table counts, PDF hashes/sizes recorded in SQLite,
normalized safe config, and every recognized reference. Symlinks and special
files are refused throughout the tree. There is no archive extraction.

Restore checks the complete manifest **before** constructing its private sibling
staging directory. Each copied file is checked again against the validated
manifest to detect changing input. Only staged SQLite/JSON files are rebased.
Files are private (`0600`) and the top-level staged/restored directory is `0700`.
After files/directories are flushed, publication uses Linux `renameat2` with
`RENAME_NOREPLACE` or macOS `renamex_np` with `RENAME_EXCL`; a raced destination,
even an empty directory, is never replaced. Other OS implementations fail closed.
No partially restored tree is exposed at the destination. Use trusted owned
parent directories; this is not a sandbox against another process controlling the
same account or filesystem.

## Synthetic verification

Run:

```sh
python -m unittest discover -s tests -p test_checkpoints.py -v
```

Tests cover complete cross-path state preservation, original-state immutability,
WAL inclusion, full-text integrity, missing/corrupt files, traversal, symlinks,
manifest tampering, excluded credentials, raw-credential rejection, atomic
no-overwrite races, and importing a restored exported job after deleting its
original source state. Tests use generated state and a synthetic PDF only.


## Explicit migration inventory and historical availability

`create_checkpoint(..., reference_policy=policy)` accepts a JSON-serializable
object with exactly `version: 1` and `entries: [...]`. The default remains strict;
there is no implicit missing-file fallback or terminal-job blanket exemption.
`inventory_references(state_dir)` performs a read-only, WAL-aware transaction and
returns each DB reference with row identity, status, JSON pointer, and
`reference_scope` (`operational`, `historical`, or `preview_cleanup`). It does not
open referenced files, rewrite state, or authorize any omission. This is a DB
reference inventory; included JSON file contents are additionally inspected when
creating/verifying a checkpoint.

Each policy entry names an exact original `source` string. Prefixes, glob patterns,
and paths outside state that should be read are unsupported. Duplicate explicit
sources are rejected. Source paths are retained as **private provenance**, so
opt-in checkpoint manifests can intentionally contain legacy absolute paths.
Never publish these manifests or provenance files in a public repository/UI.

Supported dispositions:

```json
{
  "version": 1,
  "entries": [
    {
      "source": "/old-installation/state/results/completed.json",
      "disposition": "relocate",
      "target": "results/completed.json"
    },
    {
      "source": "/old-installation/state/backups/old-restore.sqlite3",
      "disposition": "archived_local",
      "reason": "Retained in the original local backup archive",
      "archive_label": "Original local backup",
      "operation_disabled": true,
      "revalidation_required": true
    },
    {
      "source": "/old-installation/state/digests/completed.md",
      "disposition": "unavailable",
      "reason": "Explicitly inventoried history awaiting transfer"
    }
  ]
}
```

- `relocate`: `target` must be an existing, safe, state-relative regular file.
  It is copied/rebased normally. Optional `sha256` and `size` must match its raw
  bytes. This is useful when legacy DB paths still point at another computer or
  a previous repository. The original external source is never opened. The
  selected primary database may be a relocation target for audit `database`
  references; it is represented by the consistent snapshot, not raw-copied.
- `archived_local`: requires `reason` and `archive_label`. Bytes are deliberately
  not copied; optional `sha256`/`size` record the caller's inventory but do not
  claim that a remote archive was accessed or verified.
- `unavailable`: requires `reason` and explicitly records incomplete historical
  artifact availability. It does not turn an absent operational dependency into
  a successful backup.

Only imported/abandoned agent jobs, completed/abandoned pipeline history,
completed cleanup audits, and completed notifications qualify as historical.
Any job referenced by `cloud_runtime_export:*` metadata stays operational even
when its job status is imported, because retry/resume can still read its queue
and results. Every occurrence is checked separately. Shared active/historical
paths cannot be globally exempted. Rowid tables use their true row identity,
including tables with nullable primary keys; `WITHOUT ROWID` tables use their
non-null composite primary keys.

Included JSON artifacts inherit the scope of their DB owners. The verifier also
propagates operational references through JSON-to-file links, so a historical
artifact cannot hide an active dependency. Unowned JSON artifacts are treated as
operational. Profile/config/primary-data references always stay strict.

Historical omissions use inert `archive://<64-lowercase-hex>` markers. The hash is
the canonical policy-entry identity, not a downloadable URL. All audit/job/run
rows and statuses remain intact. JSON `database` and `backup` references are
handled as paths; prose restore commands are never executed or silently enabled.

### Pending cleanup previews

A preview audit is **not** a completed operation. Its `backup_path` or
`report_path` may use `archived_local` only when that exact policy entry supplies
both `operation_disabled: true` and `revalidation_required: true`. The exception
also applies to archived backup references inside its included report JSON.
It does not apply to unrelated jobs, running pipelines, PDFs, or profiles, and
it does not change the audit's `preview` status. The ordinary application must
refuse applying an archived preview until its required files are explicitly
restored and the preview is revalidated. No cloud rollback becomes possible just
because the audit row was retained.

### Fresh cloud PDF registry with private migration audit

A separately authorized omission of an existing PDF uses this narrow policy:

```json
{
  "source": "/old-installation/state/fulltexts/example.pdf",
  "disposition": "not_migrated",
  "reason": "Start the new installation with an empty PDF registry; reupload later",
  "sha256": "<the existing fulltext_files.sha256 value>",
  "size": 12345
}
```

This disposition requires an exact matching `fulltext_files.stored_path`, SHA-256,
and byte size. It removes only matching fulltext registry rows from the
**checkpoint copy**, records every original row (including ID, paper identity,
original filename, stored path, hash, size, and timestamp) in the private
`omitted_fulltexts` manifest audit, and excludes corresponding PDF bytes from
artifact auto-inventory. Original SQLite and original files remain untouched.
There is no active PDF archive marker, stale availability row, or deduplication
stub in restored state. A later upload of the same PDF is an ordinary new upload.
Unlisted PDFs still require their real files and valid hashes. A stale/mismatched
omission inventory fails. Triggers and declared table dependencies on
`fulltext_files` are rejected before deletion to prevent collateral cascades or
other mutations. The omission does not delete paper metadata or recommendation
history.

### Provenance through successive checkpoints

The manifest's optional `reference_provenance` array retains normalized policy
entries and their `id`; optional `omitted_fulltexts` stores `{policy_id, row}`
audits. These fields are covered by the manifest identity. Restore writes a
reserved, private `reference-provenance.json` alongside the restored state. Its
format/version, entry identities, omitted-row relationships, and checksum are
validated on every subsequent checkpoint. It is not an ordinary artifact or a
file that may be supplied through a relocation target.

The persisted file retains already-inert markers and audit history; it does
**not** automatically exempt new missing paths. Every archived marker is checked
against its current DB/artifact scope again. Explicit `relocate` entries using an
archived entry's original source can hydrate historical artifacts later. Omitted
PDF audits persist across subsequent checkpoints without repopulating the active
PDF registry. Invalid/missing marker provenance, symlink provenance, invalid
checksums, or operational misuse fail closed. Original filesystem paths in
provenance are metadata only and are never opened during verification/restore.

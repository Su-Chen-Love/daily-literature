# Credential-free D1 native host CLI

The runnable entry point is `academic-radar-d1-native-host`, or
`python -m academic_radar.d1_native_host` with this source package on `PYTHONPATH`.
This is a private-pipe client of an approved native tool host. It is **not** a
standalone connector, an unattended scheduler, or a raw HTTP client. No account,
credential, token, Library archive, model API, installation, deployment, or live
cutover is performed by starting the process.

## Fixed host contract

Use the same `academic-radar-native-host-v1` JSON-lines protocol documented in
`native-host.md`. The process emits one request on stdout and reads its matched
response on stdin. The parent executes the approved native tool and returns its
decoded successful structured result. Match protocol, session, request ID and
operation exactly. Never interpret text as success or authorization. The host
must enforce the correct private Site and owner identity; a string in the pipe
cannot grant authorization. Process output is data, not a shell command.

Exactly four Site capabilities are added to the legacy host allowlist:

- `site.read_main_store`: fixed revision-pinned queries, immutable objects and capacity
- `site.write_main_store`: fixed verified import and atomic operation staging/commit
- `site.publish_main_snapshot`: preview or explicitly requested atomic publication
- `site.read_site_backup`: read-only complete export of retained Site state

The D1 CLI uses only these plus the existing fixed edit-read and snapshot-staging
capabilities. It never calls Library, `read_checkpoint_head`,
`commit_library_checkpoint`, or `acknowledge_snapshot`. Arbitrary SQL, URLs,
headers, credentials, tool names, and unknown action/data fields are not accepted.
Bulk object actions remain bounded actions within those same four capabilities.

Malformed responses, native rejection, EOF, session mismatch, or an uncertain
write poison the pipe. There is no automatic replay over that channel. Inspect
the authoritative D1 head and start a fresh process/workspace. The fixed protocol
has a 2 MB frame limit and a 10,000-request session budget; hitting either fails
closed. Ordinary run diagnostics cannot contaminate stdout or leak exception
payloads into the result. A final `type: "result"` reports success or a sanitized
error code. This protocol supports a host implementation; tests using local
fixtures do not establish that the production scheduler can service these pipes.

## Trust and workspaces

Every prepare, import-results and publication invocation requires an explicitly
absent `--workspace` within an existing parent directory, a stable `--operation`,
and `--role primary`. The CLI restores the current pinned D1 projection each
time. A local working set is disposable partial state, never a complete backup.
The explicit primary role opts into main-store writes; it does not activate the
Site. Restored execution config must resolve to this workspace, explicitly use
managed transport, and disable legacy HTTP synchronization.

By default the runner builds a fresh empty schema from installed trusted source.
It accepts the migration-built schema and the reviewed historical DDL bundled as
`assets/d1-schema-v1.json`, pinned to fingerprint
`2aa41a27d8d35870e9a1818db7337d071cf6885e62ab60c0654fcc804d8b7a63`.
The historical definition differs only in whitespace in five table declarations:
notifications, observations, papers, screenings, and source_runs. Its SQL and
pragmas are checked against the current migration-built definition before use.
The advertised fingerprint selects this local allowlist; remote SQL is never
executed. Unknown fingerprints fail closed. A fresh cloud task therefore needs
no private historical schema file or Library lookup.

For a separately reviewed custom exact DDL fingerprint, supply both `--trusted-schema /trusted/schema.sqlite3`
and `--trusted-schema-sha256 SHA256_OF_ITS_BYTES`. That file must be independently
trusted, immutable during the call, and free of SQLite sidecars. The schema is
used only for DDL; its historical rows are not copied into the working set.
The runner reads only the trusted DDL, rechecks its pinned source bytes, and
materializes an empty private schema. A mismatch stops before business processing. `--source-root /original/state`
relocates historical absolute state paths; it is a path map, not a source of data.

Examples below show arguments only. A real approved host must own the process
pipes; running these in an ordinary shell without it cannot complete the work.

## Initial import, separate from Site activation

Before importing into a previously local-only Site, capture its retained display
and pending edits with this read-only command (revision zero is supported):

```text
academic-radar-d1-native-host backup-site --output /backups/absent-site-before-import
academic-radar-d1-native-host backup-site-verify --backup /backups/site-before-import --backup-sha256 <trusted-site-backup-root-sha256>
```

This distinct Site-only format is explicitly not a full main-database backup.
It preserves complete retained generations and both append-only edit streams,
including oversized events through bounded chunk reads.

Then export a lossless normalized package using `academic_radar.d1_package`.
Keep its `manifest.json` SHA-256 from the independently verified source evidence.
Then invoke:

```text
academic-radar-d1-native-host bootstrap --role primary --operation initial-import-20261016 --package /verified/package --package-sha256 <manifest-sha256>
```

Bootstrap verifies the entire package and performs a full private round-trip
restore under independently trusted source DDL before any native call. This
checks foreign keys, uniqueness, checks, and the complete logical inventory in
addition to wire hashes. It requires an empty
main-store head, and refuses PDF or embedded database artifact files. All
historical normalized rows and ordinary files remain represented. It uploads
content-addressed chunks and descriptors with bounded bulk calls, verifies
objects, stages all rows, verifies every table count and hash chain, and performs
one compare-and-swap commit from revision zero. Row pages adapt proactively to
both byte size and the Worker's 15 expensive historical-JSON reference budget.
No uncertain write is retried or split after the fact. Source-event watermarks
come from exact imported metadata, and the Worker independently validates them.

A second bootstrap cannot replace an existing main database. On an uncertain
initial commit, inspect the main head; do not start a replacement dataset.
Use `academic-radar-d1-native-host status` for a read-only exact head check.
Bootstrap never changes the Site display, writer mode, or event acknowledgements.
Root manifests are uploaded as verified objects, with staged file pages resumed
only while their explicit cursor advances. A root manifest exceeding the
Worker's 8 MiB admission limit fails explicitly.

## Daily lifecycle and restart

```text
academic-radar-d1-native-host prepare --role primary --operation daily-20261017 --workspace /scratch/day-prepare --source-root /original/state --collect
```

`--collect` runs configured collection, official-issue checks and enrichment,
then durably freezes the judgment queue. `--no-collect` is an explicit offline
exercise; its report says collection was not exercised. One of these flags is
required. The same operation can be recovered in a different absent workspace
without re-collecting its already-frozen queue. The approved host judges exactly
the returned queue using its existing model and saves the complete results file.
There is no model API in this CLI.

```text
academic-radar-d1-native-host import-results --role primary --operation daily-20261017 --workspace /scratch/day-import --source-root /original/state --results /scratch/judgments.json
academic-radar-d1-native-host publish --role primary --operation daily-20261017 --workspace /scratch/day-preview --source-root /original/state
```

`publish` and `publish-preview` both stage and preview. They do **not** activate
or ACK. They may durably checkpoint pending business state before building the
view. Publication requires a fully imported queue, a complete UI coverage
certificate, the exact record digest/count, and exact committed watermarks.
The runner compares the complete D1 head before staging and again immediately
before publication. It sends every display record, checks the completed count,
and asks the Site to validate the atomic fence. Omitted cold history is never
interpreted as deletion.

The preview result returns the precise Site fence. Only after separate approval
and validation should the host invoke this distinct activation command:

```text
academic-radar-d1-native-host publish-activate --role primary --operation daily-20261017 --workspace /scratch/day-activate --source-root /original/state --activate --expected-site-generation <exact-current-generation-or-none> --expected-published-revision <exact-current-main-publication-revision> --expected-writer-mode <local-or-d1-main>
```

All fence arguments and `--activate` are mandatory. No current value is inferred
for activation. The Site transaction binds the exact committed D1 revision,
changes the active generation and writer mode, and ACKs exactly its committed
watermarks together. The CLI verifies the returned binding and never sends the
legacy ACK afterward. A stale head, fence, cursor, incomplete view, or delayed
obsolete publication fails closed. A successful preview is not cutover approval.

## Complete backup and recovery

```text
academic-radar-d1-native-host backup-export --output /backups/absent-d1-backup
academic-radar-d1-native-host backup-verify --backup /backups/d1-backup --backup-sha256 <trusted-backup-root-sha256>
academic-radar-d1-native-host backup-restore --backup /backups/d1-backup --backup-sha256 <trusted-backup-root-sha256> --output /scratch/absent-restore --exact-main-only
academic-radar-d1-native-host backup-restore --backup /backups/d1-backup --backup-sha256 <trusted-backup-root-sha256> --output /scratch/absent-operational-restore --source-root /original/state
```

Export is read-only and pins the main database and Site snapshot together. It
exports complete retained main rows, referenced files, complete display
generations and append-only owner edits. Verification and restore need no native
calls. The backup root hash must be retained independently. Outputs must be
absent, and restore uses the same independent schema policy. Without
`--exact-main-only`, pending captured edits are replayed into a separately labeled
derived recovery; this does not change the backup or remotely acknowledge edits.
Use `--profile-relative-path` when older config paths need explicit safe mapping.
Without `--source-root`, recovery retains exact raw path references and reports
`operational_ready: false`. An explicit source root relocates only included active
references in the derived copy, preserves archived rollback provenance, and
checks the configured profile against its active version before reporting
`operational_ready: true`. This does not resume a scheduler or change the Site.
A working-set directory is never accepted as this complete backup format.

## Optional offline schema artifact

Default operation reconstructs trusted schema from source without this step. For
an independently pinned custom schema, or to inspect an empty schema artifact:

```text
academic-radar-d1-native-host schema-export --output /scratch/absent-schema
academic-radar-d1-native-host schema-export --schema-fingerprint 2aa41a27d8d35870e9a1818db7337d071cf6885e62ab60c0654fcc804d8b7a63 --output /scratch/absent-historical-schema
```

The result reports the tiny empty `schema.sqlite3` and its byte SHA-256. A
`--trusted-schema` and corresponding byte pin can instead provide independently
reviewed source DDL. No source row data or native calls are used for this command.

## Local evidence

`PYTHONPATH=src:tests python -m unittest test_d1_native_host -v` exercises the
actual JSON-line CLI process against the actual Worker MCP dispatcher using a
local SQLite fixture and synthetic owner identity. The suite covers initial-only
bootstrap, fresh-workspace recovery, complete judgments, preview, explicit atomic
activation, complete backup/recovery, wrong pins, closed capability dispatch,
certificate failures and required activation fences. No real connector, real
identity, remote write or production deployment is part of this test.

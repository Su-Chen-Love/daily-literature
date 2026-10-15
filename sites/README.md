# Personal Academic Radar · Sites

The Site shares all six local page templates, CSS, JavaScript and icons.
The migration defaults to owner-private access: every application page, read API,
feedback write and profile edit requires the authenticated Site owner. Configure
`OWNER_USER_ID` with the **Site-scoped** hosting identity (preferred), or configure
`OWNER_EMAIL` with the verified owner's email. If neither is present, application
access is denied. `OWNER_USER_ID` takes precedence if both are present.

Sites dispatch supplies `oai-authenticated-user-id` and
`oai-authenticated-user-email`; the Worker must run behind that trusted hosting
boundary. Never expose this Worker through a proxy that accepts caller-supplied
identity headers. A hosting service token does not establish visitor identity.
Keep the Site's hosting audience owner-private as well, and verify anonymous,
other-account and owner behavior after deployment. Local request-header tests
verify application logic, not the production dispatch boundary.

`SITE_ACCESS=public-legacy` exists only for explicit legacy compatibility and
regression tests. It reopens anonymous reads and feedback, but **never** allows
anonymous profile/preference writes. Do not configure it for the owner-private
migration. The loopback preview is also private by default.

Database files, PDFs, paths, credentials, queues, results, logs and backups stay
outside the repository. Private checkpoint objects use the separate authenticated
`/api/state/*` transport and must never be publicly exposed.

## Build and check

From the parent project, run `python3 scripts/sync-site-ui.py` after changing
canonical templates or assets. The copied `shared/` sources are part of the
exact Site commit. Build from this checkout:

```bash
npm ci
npm run db:generate  # only after changing db/schema.ts
npm run build
npm run validate
node scripts/test-worker.mjs
```

Nunjucks templates are precompiled at build time and bundled with its slim
runtime; production uses no runtime compiler or filesystem. The logical `DB`
D1 binding persists data. Drizzle migrations contain schema only. `SYNC_TOKEN`
is an internal transport secret and is never required from Site visitors.

## Synchronization

The local daily task collects scholarly sources and performs a first Codex
judgment only for papers that have never been judged. Existing judgments are
retained across profile, feedback, abstract and rubric changes; daily tasks do
not rescreen old papers. Batch size is a maximum, never a quota.
Feedback, reading and favorite edits save immediately on either surface.
Local saves request background sync immediately; the macOS service also checks
every five minutes. Sync pulls cloud feedback with an event cursor, merges by
UTC edit time, then computes one consistent snapshot of local application data.
If the local content checksum and remote active generation are unchanged,
only the small feedback GET is sent. Changed records reuse matching checksums;
the entire generation is verified before atomic activation. Interrupted uploads
leave the previous complete generation readable. Clear markers are timestamped
neutral records, so older cloud feedback cannot revive a cleared entry.

Both surfaces offer “同步更新”. The local button starts sync immediately. The
cloud button stores a monotonic request for the local service; it cannot wake an
offline computer. Its status stays pending until the local service confirms the
observed request and feedback cursor. Later requests/edits remain pending.
Cloud profile management is described below. PDF import remains a local-app
operation. No production feedback is created for testing. See
`../docs/cloud-sync.md` and `../docs/checkpoint-format.md`.

Edit `worker/` and canonical local UI sources, never generated `dist/` or
`worker/generated-ui.js`. Publish the exact Site commit and build archive through
the Sites hosting workflow, and synchronize these source files with GitHub.
Data changes use the protected application sync API without source redeployment.

Loopback preview: `node scripts/preview.mjs /absolute/path/to/private-records.json`.
The private JSON snapshot must never be committed.

## Owner profile edits and future screening

The owner can edit research interests, the relevance threshold (0–1) and free-text
screening instructions at `/profile`. Saving explicitly confirms the change for
future recommendations. Prior scores and daily selection history are retained;
no automatic rejudging is triggered. A local suggestion may be copied into the
cloud editor for review and explicit confirmation.

`POST /api/profile` accepts `generation`, integer `base_version`, `content`,
`summary`, and `screening_preferences` with exactly `relevance_threshold` and
`instructions`. The normal HTML form posts to `/profile/save`. Both paths require
server-verified owner identity and an exact same-origin `Origin`. The database
checks the active snapshot generation and current event version in the same SQL
statement that appends `cloud_profile_events`. Competing or stale saves return
409; the HTML response preserves submitted text for copying and reconciliation.
Profile bodies are bounded, validated and escaped on display.

`GET /api/sync/profile?after=N` requires the existing server-side `SYNC_TOKEN`.
The response contains ordered immutable events, a cursor and a pagination flag.
The Python consumer verifies hashes, contiguous versions and the expected base
profile/preferences; it commits the profile, preference state, audit ledger and
cursor atomically. A newer conflicting local profile stops the import rather
than being overwritten. A durable pending-file marker repairs an interrupted
profile-file replacement without losing unsaved manual file changes. Repeat pulls
do not duplicate versions or concatenate screening instructions.

The consumer advertises the applied `profile_cursor` inside its next profile
snapshot. Acknowledgement can clear profile-pending state only after that matching
snapshot is active. A later edit remains pending. The event audit is retained
independently of snapshot generations.

For cloud-primary operation, `pull_configured` only drains edits before queue
export; `stage_configured` uploads a complete snapshot with `activate:false`.
The private checkpoint head CAS then activates both the full checkpoint and its
matching reader snapshot. Only afterward may `acknowledge_configured` acknowledge
observed events. Once a checkpoint head exists, SQL fencing prevents legacy sync
clients from activating a competing snapshot. Unfenced snapshot garbage
collection is disabled so it cannot remove a committed or in-flight generation.

## Optional R2 full-text viewing

This route is available only for the separate R2 checkpoint backend. The Library
migration omits existing PDF files and starts with an empty active PDF registry.

The Library's “查看 PDF” link resolves a private full-text record for the signed-in
owner, then streams the file from the currently committed checkpoint. The active
reader snapshot must match the checkpoint head. Arbitrary paths, missing files,
checksum mismatches and uncommitted generations are rejected. Anonymous visitors,
other accounts and service bearer tokens alone cannot use this route, even in
public-legacy mode. No generic private-file endpoint is exposed to browsers.

PDF responses use private/no-store caching, PDF content type, nosniff, sandbox CSP,
and a sanitized Unicode filename. Chunk integrity is verified before each chunk
is sent; a corrupt later chunk aborts the stream. GET and HEAD are supported.
Range requests intentionally receive the entire 200 response (`Accept-Ranges:
none`), not an unverified partial-content response. Uploading a new PDF through
the Site is not implemented, so cloud cards do not show an upload button.

## Managed Site tools (local candidate)

`worker/mcp.js` implements stateless Streamable HTTP at `POST /mcp`; the hosting
manifest declares the `mcp` capability. The supported operations are initialization,
ping, tool discovery and tool calls. Discovery contains schemas only. Every tool
call requires the same server-supplied owner identity as the private application,
including read-only tools and calls in explicit `public-legacy` mode. Missing owner
configuration denies access. A service bearer token cannot substitute for owner
identity. Cross-origin browser requests are rejected. Tool arguments cannot supply
an identity, URL, SQL statement, credential or arbitrary backend operation.

The bounded tool surface is:

- `read_run_status({})`: active generation, pending cursors and exported status.
- `read_profile({})`: effective research profile, preferences and version.
- `read_feedback({after})`: ordered feedback events, up to 500 per response.
- `read_profile_events({after})`: ordered profile events, up to 100 per response.
- `stage_snapshot({action,data})`: existing `begin`, `chunk` (at most 40 records),
  or `finish` payloads. Finish **always** stages; it cannot activate a snapshot.
- `acknowledge_snapshot({data})`: existing generation-bound acknowledgement.
- `read_checkpoint_head({})`: `{generation,publication}`; initially publication
  is null. An existing R2 head requires a separate explicit backend migration.
- `commit_library_checkpoint({data:{expected_head,publication}})`: compare-and-swap
  the exact immutable Library receipt and complete staged display generation.

The exact JSON schemas are returned by `tools/list`. Requests have a streaming
2 MB limit, strict schemas, safe-integer cursors and bounded strings. Tool execution
errors return `isError:true` and a structured status, whereas unauthorized tool
calls return HTTP 403 without touching storage. GET/SSE and sessions are not used.
No local MCP server, custom OAuth flow or new bearer secret is needed.

A Library publication contains `operation_id`, `snapshot_generation`,
`archive_sha256`, `archive_size`, `checkpoint_id`, `library_file_id`,
`current_version`, `file_id`, `file_name`, and `publication_id`. The last field is
SHA-256 of UTF-8 JSON containing all other fields, keys sorted, no extra whitespace
and no ASCII-only escaping. `current_version` is the actual returned version,
never a guessed initial value. Each publication uses a new immutable Library
item. Historical stable IDs are retained in the D1 audit and cannot be reused.

The host must create and byte-verify the Library archive first, preserving the
returned stable ID, current version and backing file ID. The Worker can validate
that exact receipt and the D1 snapshot, but cannot independently fetch or verify
Library bytes. One D1 transaction binds the receipt to the active display snapshot;
this is **not** a cross-service Library/D1 transaction. A failed CAS leaves the old
complete view active and the newly created Library item retained as an orphan.
A repeated matching commit is idempotent; stale or competing writers are rejected.
Acknowledgement happens only after that commit. The R2 transport remains optional
and cannot silently overwrite a Library head.

Existing PDF files are omitted from the Library migration. A Library-backed head
never falls back to old R2 files; `/fulltext/file` refuses those reads. New Site PDF
ingestion is outside this candidate.

Check locally with `node scripts/test-mcp.mjs`, plus the existing Worker and R2
suites. This source does **not** establish a deployed or connected plugin. Deployment,
installation/connection and owner-bound read-only verification remain separate
approval and verification steps. Reuse the Site's managed App/plugin on updates.

Public references: [Sites plugin hosting](https://help.openai.com/en/articles/20001547-hosting-a-plugin-with-chatgpt-sites),
[MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
and [tool schemas and results](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

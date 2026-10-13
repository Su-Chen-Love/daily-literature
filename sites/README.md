# Personal Academic Radar · Sites

The Site shares all six local page templates, CSS, JavaScript and icons. Every
page and feedback edit is available anonymously, as explicitly requested by the
owner. No visitor sign-in or owner gate is used. Anybody with the link can see
the research profile and modify feedback. Private database files, PDFs, paths,
credentials, queues, results, logs and backups remain outside the repository.

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
Collection, profile management and PDF import still execute on the local app.
No production feedback is created for testing. See `../docs/cloud-sync.md`.

Edit `worker/` and canonical local UI sources, never generated `dist/` or
`worker/generated-ui.js`. Publish the exact Site commit and build archive through
the Sites hosting workflow, and synchronize these source files with GitHub.
Data changes use the protected application sync API without source redeployment.

Loopback preview: `node scripts/preview.mjs /absolute/path/to/private-records.json`.
The private JSON snapshot must never be committed.

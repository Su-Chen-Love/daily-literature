# Resume a closed, revision-pinned backup after its deadline

This separate recovery mode preserves a successful read prefix after an ordinary
combined `backup-export` reaches its finite relay deadline. It neither extends the
old deadline nor restarts any writer. The application package and pump stay byte
identical to the original pinned release; only the reviewed relay may differ.

## Eligible evidence

First confirm the original process has exited and its host pump has stopped. Keep
the original source, source manifest, private spool, staging copies, and logs.
The sealer reads them without modifying them. It requires:

* A contiguous successful prefix starting with schema-selection `head`, its
  `read_manifest`, and the original atomic Site `begin`.
* A durable exact `relay_error` / `relay_timeout` terminal at `prefix + 1`.
* A pending Site read at that boundary, with **no invocation marker, response, or
  partial response**. A matching claim without invocation is allowed. Timeout
  before announcement is allowed only with neither pending nor heartbeat file.
* Canonical request/response envelopes, expected claim/invocation hashes, closed
  inventories, private regular files, no links, no later entries, and an elapsed
  boundary deadline when the boundary was announced.

A failed, denied, partially returned, or uncertain invoked boundary is ineligible.
No synthetic success or local cancellation is created to make it pass. The raw
private terminal is checked because the public poll intentionally redacts errors.

A hash proves integrity of the selected evidence, not authenticity of arbitrary
files or permission to use them. The caller must independently retain the exact
original source manifest path/SHA and original spool identity. Do not relocate
source manifests/evidence or recreate a consumed reservation to regain a run.

## Seal offline, then start one new bounded continuation

Use an independently reviewed checkout and externally pinned source manifest as
in [the native relay instructions](d1-native-spool.md). These are synthetic paths.
No native tools are called by `seal-timeout-journal`.

```sh
/approved/python -I -B /approved/recovery/scripts/d1-native-relay.py \
  --source-manifest /approved/control/new-source.json \
  --source-manifest-sha256 '<NEW_SOURCE_SHA256>' seal-timeout-journal \
  --spool /approved/runs/expired-backup --prefix 712 \
  --output /approved/control/timeout-journal.json \
  --original-source-root /approved/original \
  --original-source-manifest /approved/control/original-source.json \
  --original-source-manifest-sha256 '<ORIGINAL_SOURCE_SHA256>'
```

Retain the returned journal SHA independently. Before any native read, the new
invocation validates all evidence, reserves a one-use immutable fence under the
original source-manifest directory, and reconstructs the previous prefix **from
local successful responses only**. Copying the journal or spool while preserving
the pinned original source contract cannot create a second reservation.

```sh
/approved/python -I -B /approved/recovery/scripts/d1-native-relay.py \
  --source-manifest /approved/control/new-source.json \
  --source-manifest-sha256 '<NEW_SOURCE_SHA256>' resume-timeout-backup \
  --workspace /approved/runs --spool new-continuation \
  --timeout-seconds 10800 \
  --journal /approved/control/timeout-journal.json \
  --journal-sha256 '<JOURNAL_SHA256>' \
  --output /approved/backups/complete-backup
```

The output and spool must be absent and outside all ancestor evidence spools.
No arbitrary child command, schema override, or write capability is accepted.
The first live request must match the old never-invoked boundary exactly. The
remaining live requests are only captured Site generations and event streams
through the original fixed watermarks. Fresh main reads, head/begin recapture,
publication, ACKs, writes, and changed generation/event fences are forbidden.

Use the unchanged verified pump with `operations: ['site.read_site_backup']` and
start at the first announced `prefix + 1` request. The new invocation has its own
explicit finite deadline, no greater than 86400 seconds. Old consume/deadline
markers are untouched. Stop on denial or an uncertain native outcome.

The application rebuilds the exact main package, completed generations and any
partial record accumulator from validated local replies. It does not trust a
partially copied SQLite/JSONL staging tree as proof of completion. This repeats
local computation, not remote transfer. Keep staging copies for independent
comparison and forensics.

## A later eligible timeout

If the continuation itself reaches its deadline, it may be sealed again only if
it made at least one new successful read and ended at another never-invoked Site
boundary. Supply `--previous-journal` and `--previous-journal-sha256` to the sealer.
It verifies every inherited replay receipt, exact response equivalence, complete
replay summary, parent one-use reservation and target spool, plus actual invoke
markers for fresh successful reads. Source, snapshot, capture time and ancestry
remain pinned. Preserve the first capture’s exact `--original-source-*` paths and
SHA for every link; a successor rejects even an identical manifest relocated to
another directory. The continuation relay has its own independently verified new
source pin, while the captured application and pump remain unchanged.
Gaps, incomplete local replay, no progress, changed sessions,
cycles or more than 16 retained links fail closed. The 10000 logical-request limit
also remains in force; a new deadline is not a reset of that limit.

Each link retains its own already-consumed fence. No old fence is reset and no old
native call is repeated. Review the closed boundary before authorizing a fresh
continuation; this mechanism is not an automatic retry policy.

## Completion and counters

Require the normal complete backup verifier, independent exact-main restore and
all captured Site history checks, successful host receipt, and final continuation process
exit 0. No partial output is labeled complete. The canonical final backup is
byte-identical to an uninterrupted capture of the same responses.

`replay-summary.json` separates cached envelope bytes and locally replayed reads
from the pump's new external call count. The application's `transport_call_count`
is a logical callback total and includes local replay; do not describe it as new
network/native calls. Saving the archive and independently restoring it remain
separate acceptance results.

## Why this does not solve recurring history transfer efficiency

Current Site storage deduplicates payloads by checksum, but its `records` endpoint
joins each generation back to full bytes and sends them again in bounded chunks.
Every historical generation must still be preserved. A future, separately
reviewed protocol can return ordered `(kind,key,checksum,bytes)` manifests plus
bounded payload-by-checksum reads with a verified cross-generation cache. It can
expand cached bytes into the existing JSONL archive and retain current generation
count/hash verification. Compression or client-only hashing cannot remove today's
repeated native payload transfer. This timeout repair deliberately leaves the
live Site and archive format unchanged.

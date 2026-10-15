# Optional immutable Library checkpoint protocol

Status: local candidate code and synthetic tests only. This is not a deployed scheduler, a verified unattended credential route, or an automatic switch from R2. No Library file is created or read by importing or testing this module. Existing access denials must be respected; a denied archive read blocks verification and publication.

## Why immutable Library files

Every publication creates a new private Library file. The returned `library_file_id`, backing `file_id`, filename and actual current version are recorded exactly. A version is never assumed to start at zero or one. This protocol does not replace a stable Library file and does not depend on retrieving its old versions. It does not implement Library `expected_current_version` replacement CAS because immutable creation supersedes that design.

The Site's compare-and-swap head is the sole publication authority. An uploaded Library artifact is only staged until that Site CAS succeeds. Files uploaded by losing or interrupted publishers remain retained orphans; they are not automatically deleted. A separate reviewed retention policy would be required to delete anything. Published files must remain private, readable and unchanged. If someone changes or deletes one outside this protocol, restore detects a missing object or checksum mismatch; the protocol cannot recreate deleted bytes.

There is no cross-service transaction. The ordering is:

1. Complete and import the frozen runtime run; verify its operation, queue and results identities.
2. Stage the complete D1 view without activating it. Persist the matching committing/library runtime journal, original expected Site head and staged snapshot generation.
3. Create the complete portable checkpoint from the paused writer, verify its runtime proof, package it with a publication intent, then persist a local receipt.
4. Create a new Library file and save its exact returned identity.
5. Materialize that immutable file into an isolated directory and verify archive SHA256, size, archive structure, every checkpoint file, SQLite integrity and publication intent.
6. Atomically CAS the Site head and activate the already-staged D1 snapshot in the same Site transaction.
7. Acknowledge feedback/profile cursors only after confirmed publication, through the separate runtime protocol.

Before step 6, an upload or verification failure leaves the previous complete Site view active. After an uncertain step 6, read the authoritative Site head before retrying. A competing publisher's head is never overwritten using a freshly substituted expected value.

## Local API

The implementation is `academic_radar.library_checkpoint`.

- `pack_checkpoint(checkpoint, archive, publication_intent=None)` produces deterministic gzip/USTAR bytes and returns the archive SHA256, size, basename and checkpoint ID.
- `unpack_checkpoint(archive, destination, expected_sha256=..., expected_checkpoint_id=..., expected_size=...)` verifies before publishing to an absent destination.
- `verify_publication_binding(checkpoint, operation_id, snapshot_generation, expected_site_head)` validates the embedded committing/library journal, operation-to-run mapping, imported job, included queue/results, run/profile identities and canonical frozen hashes.
- `prepare_publication(checkpoint, archive, receipt, operation_id=..., snapshot_generation=..., expected_site_head=...)` requires that proof, packages the verified checkpoint plus intent and saves a `prepared` receipt. It rejects an unrelated checkpoint even when its file hashes are otherwise valid.
- `LibraryCheckpointAdapter.publish(receipt, archive)` creates, verifies and activates using explicitly injected host callbacks.
- `reconcile_created(receipt, metadata)` recovers an uncertain upload only after the host identifies the specific new file; its bytes and intent are independently verified.
- `download_checkpoint(publication, destination)` restores the exact published artifact as a verified checkpoint directory.
- `recover_published(receipt, checkpoint_destination)` recovers after all local files are lost: it reads the Site head, verifies its immutable archive and embedded intent, then reconstructs a published local receipt. It creates no new Library file and performs no Site activation.

There is no generic bootstrap bypass. A first migration must pass through a normal prepared/imported run (an empty queue is valid when appropriate), stage its view and save its committing/library journal. Proof is checked before packaging and again during retained-archive verification or restore.

This adapter is not automatically wired into `cloud_runtime.commit`. The
[bounded native host runner](native-host-runner.md) connects staging, checkpoint
creation, publication, recovery and subsequent acknowledgement through explicit
JSON-lines callbacks. A real approved host must still invoke the native tools;
the runner does not supply connector authorization or live acceptance.

## Host callback contract

These are Python callables, not HTTP credential discovery or implementations of native tools:

- `create_archive(path, operation_id, archive_sha256)` creates one new retained private Library item. It returns exactly `{library_file_id, current_version, file_id, file_name}`. The host maps the actual native `current_version_number` to `current_version`, without inventing IDs or versions. It must not replace another Library item.
- `materialize_archive(metadata, empty_directory)` materializes the specified immutable artifact into that isolated directory and returns its local path. The host should use the exact known backing file ID when the native API supports pinning. Latest-only reads are acceptable only while this unique artifact remains unchanged; the byte hash and size must still match. No previous-version retention guarantee is assumed.
- `read_site_head()` returns exactly `{generation, publication}`, with `{generation: 0, publication: null}` for an uninitialized Library head.
- `activate_site(expected_head, publication)` performs one atomic Site CAS: verify the expected head, validate the staged snapshot, then update the Library head and active D1 snapshot together. It returns the new `{generation, publication}`. A mismatch raises `PublicationConflict` and does not modify either pointer.

The managed Site Worker contract is `read_checkpoint_head({})` and `commit_library_checkpoint({data: {expected_head, publication}})`. The worker validates descriptor shape and the D1 snapshot, but cannot independently read or verify Library bytes. Host-side materialization verification is therefore mandatory. A pre-existing R2 head is not silently converted to Library mode.

A publication contains:

- `operation_id`, `snapshot_generation`
- `archive_sha256`, `archive_size`, `checkpoint_id`
- exact `library_file_id`, `current_version`, `file_id`, `file_name`
- `publication_id`, the SHA256 of canonical sorted UTF-8 JSON containing all the other publication fields

The local receipt retains only these normalized fields, the original expected head, the package identity and its phase. Callback responses with unexpected metadata fields are rejected so credentials and opaque tool session data cannot be accidentally serialized.

## Crash recovery

- Before receipt creation: if archive publication succeeded but the receipt write was interrupted, the same preparation can reuse that archive only after complete verification and an exact intent match. An unrelated existing archive is rejected.
- `prepared`: no upload is known to have started. Verify the unchanged local archive and original Site fence before calling create.
- `create_pending`: the create outcome is uncertain. Never repeat create blindly. The host must locate the candidate using the operation/filename/hash and provide its exact metadata to `reconcile_created`. If it cannot do so, remain blocked. Extra files are never silently deleted.
- `verify_pending`: exact Library identity is saved, but its bytes have not passed read verification. Repeat only that read/verification when authorized. A scope denial is a blocker, not permission to use a different identity or transfer route.
- `archived`: the full immutable archive has passed verification, while the Site still shows its prior complete snapshot.
- `activation_pending`: read the Site head. If it names the same publication ID, mark success without activation. If it is still the original expected head, retry the same guarded CAS. Otherwise report a conflict and retain the staged file.
- `published`: retrying reads the authoritative head and recognizes the same publication; it does not create another artifact.

The archive wrapper includes `publication-intent.json` before upload. It records operation ID, snapshot generation, checkpoint ID and the original expected Site head. It contains neither the archive's own hash nor a future Library identity, avoiding recursive hashing. Extraction verifies the intent against the published descriptor and returns it alongside the verified checkpoint manifest; the intent is not inserted into or used to rewrite the checkpoint's canonical payload tree.

## Archive safety and resource limits

Archives are deterministic gzip-compressed USTAR. Their original checkpoint files remain byte-for-byte intact. Files are processed in bounded chunks; the implementation does not load the SQLite database or whole archive into memory. It permits at most 10,000 regular members and 16 GiB of compressed/expanded archive data by default. USTAR paths must fit the supported 255-character path bound; unsupported paths fail rather than being truncated.

Extraction manually parses fixed-size tar headers. It rejects PAX/GNU extension records, symlinks, hard links, device/FIFO members, absolute/traversing paths, duplicates, malformed padding, truncated content and nonzero trailing data. Size checks occur before extracting members. Files are created in a private staging directory, verified against the checkpoint manifest, and published with atomic no-overwrite semantics. Existing destinations and symlink ancestors are refused.

## Validation and remaining deployment gates

Synthetic tests cover mandatory runtime proof and negative identity/hash cases, deterministic compressed round-trip, actual non-default Library versions, immutable-file isolation, old-head restore after a newer upload, uncertain upload/activation recovery, full workspace loss, stale publication races, archive-read denial, path/link/device/duplicate/expansion attacks and metadata filtering. They perform no real Library or Site operations.

Before enabling an actual host workflow, verify authorized private Library creation/materialization for the real archive size, reliable artifact retention, actual native metadata normalization, Site MCP authorization, atomic head/view CAS, the real scheduler environment and end-to-end restore from a retained checkpoint. Do not infer that these have been proven from local tests or the existence of tool names.

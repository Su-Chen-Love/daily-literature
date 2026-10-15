# Native managed Site bridge

`ManagedSiteClient` accepts a host-owned `call_tool(name, arguments)` callback.
The callback uses the owner's approved managed Site connection and returns the
decoded tool payload, or its standard `structuredContent` envelope. Python does
not create credentials, make HTTP requests, install plugins or infer identity.
An MCP error or absent structured response must be treated as a failed call.
Never synthesize an owner identity header in the host bridge.

Implemented tool contracts are `read_feedback`, `read_profile_events`,
`stage_snapshot`, `acknowledge_snapshot`, `read_checkpoint_head` and
`commit_library_checkpoint`; the Site additionally exposes `read_profile` and
`read_run_status`. The bridge uses the same sequence validation, atomic event
application, profile-file recovery and snapshot checksums as the existing sync
client. Snapshot finish is forced to `activate:false`. ACK is separate.

```python
from academic_radar.managed_site import ManagedSiteClient
from academic_radar import cloud_runtime

site = ManagedSiteClient(approved_host_tool_callback)
# Capture this before materializing/restoring the head's immutable checkpoint.
head = site.read_checkpoint_head()
# ...materialize the exact Library item, verify archive and restore checkpoint...
prepared = cloud_runtime.prepare(config_path, operation_id,
    expected_head=head, sync_client=site)
# The actual host model judges the frozen queue, then:
cloud_runtime.import_results(config_path, operation_id, results_path)
staged = cloud_runtime.stage_library_checkpoint(config_path, operation_id,
    new_checkpoint_path, sync_client=site)
# This embeds the verified imported job, queue/results hashes, expected head
# and staged view into the checkpoint journal. Then use
# LibraryCheckpointAdapter's immutable upload + Site CAS protocol.
# ACK only after the exact Site head/publication has been read back successfully:
site.acknowledge_configured(config_path, staged['snapshot_generation'], staged['cursor'],
    staged['request_id'], staged['profile_cursor'])
```

This is an injectable integration seam, not an installed unattended runner.
The host must materialize native tool files through the supported Library flow;
local paths do not transfer between executors. The concrete native callback,
owner-scoped installation, upload quota/type support, complete paginated event
transfer, publication recovery and a fresh scheduled execution still need live
acceptance. No raw bearer token is needed by this adapter.

Set `cloud_sync.transport = "managed"` to make cloud preparation refuse to run
without the approved host bridge. `cloud_runtime.prepare(..., sync_client=site)`
works with the managed bridge. The bounded [native host runner](native-host-runner.md)
can exchange fixed JSON-lines requests with an approved tool host; it cannot
invoke or authorize a connector by itself. The legacy CLI still cannot inject
a host callback on its own.
`cloud_runtime.commit` remains the R2 transport commit path. It can take this
bridge for its small sync/ACK calls, but must not be mistaken for the separate
Library publication protocol. Do not pass a Library head to an R2 commit.

On an uncertain native upload, reconcile the immutable Library result before
retrying. On loss of the local workspace after publication, use the Library
adapter's `recover_published` against the authoritative Site head; do not upload
again or re-run judgment solely because a local receipt disappeared. Pending
feedback/profile events are acknowledged only if included in the recovered
checkpoint and its exact display generation.

import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar.checkpoints import CheckpointError, create_checkpoint, verify_checkpoint
from academic_radar.library_checkpoint import (
    LibraryCheckpointAdapter, PublicationConflict, RecoveryRequired,
    pack_checkpoint, prepare_publication, read_receipt, unpack_checkpoint,
)
from academic_radar.product import initialize_installation


class Host:
    def __init__(self):
        self.head = {"generation": 0, "publication": None}
        self.files = {}
        self.created = []
        self.activations = 0
        self.fail_create = False
        self.fail_activation = False
        self.commit_then_fail = False

    def create(self, archive, operation_id, sha256):
        self.assert_digest = hashlib.sha256(archive.read_bytes()).hexdigest() == sha256
        number = len(self.created) + 1
        metadata = {"library_file_id": f"libfile-test-{number}", "current_version": 7,
                    "file_id": f"file-test-{number}", "file_name": archive.name}
        self.files[metadata["library_file_id"]] = archive.read_bytes()
        self.created.append(metadata)
        if self.fail_create:
            raise RuntimeError("uncertain create")
        return metadata

    def materialize(self, metadata, directory):
        path = directory / metadata["file_name"]
        path.write_bytes(self.files[metadata["library_file_id"]])
        return path

    def activate(self, expected, publication):
        self.activations += 1
        if self.head != expected:
            raise PublicationConflict("Site CAS conflict")
        if self.fail_activation:
            raise RuntimeError("unavailable")
        self.head = {"generation": expected["generation"] + 1, "publication": publication}
        if self.commit_then_fail:
            raise RuntimeError("response lost")
        return self.head

    def adapter(self):
        return LibraryCheckpointAdapter(create_archive=self.create, materialize_archive=self.materialize,
                                        read_site_head=lambda: self.head, activate_site=self.activate)


class LibraryCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        initialize_installation(self.state)
        self.checkpoint = self.root / "checkpoint"
        self.archive = self.root / "checkpoint.tar.gz"
        self.receipt = self.root / "publication.json"
        self.host = Host()
        self.adapter = self.host.adapter()
        self.signature = None
        self.seed_checkpoint("operation-1", self.host.head)

    def seed_checkpoint(self, operation, expected):
        signature = json.dumps([operation, expected], sort_keys=True)
        if signature == self.signature:
            return
        with sqlite3.connect(self.state / "papers.sqlite3") as db:
            profile, profile_id = db.execute("SELECT profile_hash,id FROM profile_versions WHERE status='active'").fetchone()
            run = "run-" + operation
            queue = {"run_id": run, "profile_hash": profile, "profile_path": str(self.state / "research-profile.md"), "papers": []}
            results = {"run_id": run, "profile_hash": profile, "results": [], "model": "synthetic", "source_failures": []}
            queue_path = self.state / "agent_queue" / (run + ".json")
            results_path = self.state / "agent_results" / (run + ".json")
            queue_path.parent.mkdir(exist_ok=True); results_path.parent.mkdir(exist_ok=True)
            queue_path.write_text(json.dumps(queue)); results_path.write_text(json.dumps(results))
            canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            saved = {"operation_id": operation, "phase": "committing", "backend": "library", "run_id": run,
                     "snapshot_generation": "a" * 64, "expected_head": expected,
                     "queue_sha256": hashlib.sha256(canonical({key: value for key, value in queue.items() if key != "profile_path"})).hexdigest(),
                     "results_sha256": hashlib.sha256(canonical(results)).hexdigest()}
            db.execute("INSERT OR REPLACE INTO pipeline_runs(run_id,kind,status,started_at,details_json) VALUES(?,?,?,?,?)",
                       (run, "agent-export", "succeeded", "now", "{}"))
            db.execute("INSERT OR REPLACE INTO agent_jobs(run_id,profile_hash,status,queue_path,results_path,created_at,profile_version_id,imported_count) VALUES(?,?,?,?,?,?,?,?)",
                       (run, profile, "imported", str(queue_path), str(results_path), "now", profile_id, 0))
            for key, value in (("cloud_runtime_operation:" + operation, json.dumps(saved)), ("cloud_runtime_export:" + operation, run)):
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
        if self.checkpoint.exists():
            shutil.rmtree(self.checkpoint)
        create_checkpoint(self.state, self.checkpoint)
        self.signature = signature

    def prepare(self, operation="operation-1", expected=None, archive=None, receipt=None):
        expected = expected or self.host.head
        self.seed_checkpoint(operation, expected)
        return prepare_publication(self.checkpoint, archive or self.archive, receipt or self.receipt,
                                   operation_id=operation, snapshot_generation="a" * 64,
                                   expected_site_head=expected)

    def test_publication_requires_matching_imported_runtime_proof(self):
        with sqlite3.connect(self.state / "papers.sqlite3") as db:
            original = json.loads(db.execute("SELECT value FROM meta WHERE key='cloud_runtime_operation:operation-1'").fetchone()[0])
        changes = [
            {"phase": "imported"}, {"backend": "r2"}, {"operation_id": "other"},
            {"snapshot_generation": "b" * 64}, {"run_id": "different"},
            {"expected_head": {"generation": 1, "publication": None}},
            {"queue_sha256": "b" * 64}, {"results_sha256": "c" * 64},
        ]
        for number, change in enumerate(changes):
            with self.subTest(change=change):
                with sqlite3.connect(self.state / "papers.sqlite3") as db:
                    db.execute("UPDATE meta SET value=? WHERE key='cloud_runtime_operation:operation-1'", (json.dumps({**original, **change}),))
                candidate = self.root / f"proof-{number}"
                create_checkpoint(self.state, candidate)
                archive, receipt = self.root / f"proof-{number}.tar.gz", self.root / f"proof-{number}.json"
                with self.assertRaises(CheckpointError):
                    prepare_publication(candidate, archive, receipt, operation_id="operation-1", snapshot_generation="a" * 64,
                                        expected_site_head=self.host.head)
                self.assertFalse(archive.exists())
                self.assertFalse(receipt.exists())
        self.assertFalse(self.host.created)

    def test_unrelated_operation_and_unimported_job_are_rejected(self):
        with self.assertRaises(CheckpointError):
            prepare_publication(self.checkpoint, self.archive, self.receipt, operation_id="unrelated", snapshot_generation="a" * 64,
                                expected_site_head=self.host.head)
        with sqlite3.connect(self.state / "papers.sqlite3") as db:
            db.execute("UPDATE agent_jobs SET status='exported' WHERE run_id='run-operation-1'")
        candidate = self.root / "unimported"
        create_checkpoint(self.state, candidate)
        with self.assertRaises(CheckpointError):
            prepare_publication(candidate, self.archive, self.receipt, operation_id="operation-1", snapshot_generation="a" * 64,
                                expected_site_head=self.host.head)

    def test_archive_is_deterministic_and_verified_roundtrip(self):
        first = pack_checkpoint(self.checkpoint, self.archive)
        second = pack_checkpoint(self.checkpoint, self.root / "second.tar.gz")
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        restored = self.root / "downloaded"
        manifest = unpack_checkpoint(self.archive, restored, expected_sha256=first["archive_sha256"],
                                     expected_checkpoint_id=first["checkpoint_id"], expected_size=first["archive_size"])
        self.assertEqual(verify_checkpoint(restored)["checkpoint_id"], manifest["checkpoint_id"])
        self.assertLess(first["archive_size"], (self.checkpoint / "payload/papers.sqlite3").stat().st_size)
        with self.assertRaises(FileExistsError):
            pack_checkpoint(self.checkpoint, self.archive)

    def test_prepare_recovers_archive_written_before_receipt(self):
        with patch("academic_radar.library_checkpoint._save_receipt", side_effect=OSError("disk interrupted")):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertTrue(self.archive.exists())
        self.assertFalse(self.receipt.exists())
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        recovered = self.prepare()
        self.assertEqual(recovered["package"]["archive_sha256"], digest)
        self.assertEqual(read_receipt(self.receipt)["phase"], "prepared")

    def test_prepare_rejects_existing_archive_for_another_operation(self):
        with patch("academic_radar.library_checkpoint._save_receipt", side_effect=OSError("disk interrupted")):
            with self.assertRaises(OSError):
                self.prepare()
        with self.assertRaises(CheckpointError):
            self.prepare(operation="different-operation")
        self.assertFalse(self.receipt.exists())

    def test_immutable_create_precedes_exact_version_site_activation(self):
        self.prepare()
        result = self.adapter.publish(self.receipt, self.archive)
        self.assertEqual(result["phase"], "published")
        self.assertEqual(result["publication"]["current_version"], 7)
        self.assertEqual(result["publication"]["library_file_id"], self.host.created[0]["library_file_id"])
        self.assertEqual(self.host.head["publication"], result["publication"])
        retry = self.adapter.publish(self.receipt, self.archive)
        self.assertTrue(retry["recovered"])
        self.assertEqual(len(self.host.created), 1)
        self.assertEqual(self.host.activations, 1)
        downloaded = self.root / "download"
        self.adapter.download_checkpoint(result["publication"], downloaded)
        self.assertEqual(verify_checkpoint(downloaded)["checkpoint_id"], result["publication"]["checkpoint_id"])

    def test_managed_site_bridge_integrates_with_first_publication(self):
        from academic_radar.managed_site import ManagedSiteClient
        def call_tool(name, args):
            if name == 'read_checkpoint_head':
                return {'structuredContent': self.host.head}
            self.assertEqual(name, 'commit_library_checkpoint')
            data = args['data']
            return {'structuredContent': {'ok': True, 'head': self.host.activate(
                data['expected_head'], data['publication'])}}
        bridge = ManagedSiteClient(call_tool)
        adapter = LibraryCheckpointAdapter(create_archive=self.host.create,
            materialize_archive=self.host.materialize, read_site_head=bridge.read_checkpoint_head,
            activate_site=bridge.activate_library_checkpoint)
        self.prepare()
        result = adapter.publish(self.receipt, self.archive)
        self.assertEqual(result['phase'], 'published')
        self.assertEqual(self.host.activations, 1)

    def test_uncertain_create_requires_verified_reconciliation_no_duplicate(self):
        self.prepare()
        self.host.fail_create = True
        with self.assertRaises(RecoveryRequired):
            self.adapter.publish(self.receipt, self.archive)
        self.assertEqual(read_receipt(self.receipt)["phase"], "create_pending")
        self.assertIsNone(self.host.head["publication"])
        with self.assertRaises(RecoveryRequired):
            self.adapter.publish(self.receipt, self.archive)
        self.assertEqual(len(self.host.created), 1)
        self.adapter.reconcile_created(self.receipt, self.host.created[0])
        self.adapter.publish(self.receipt, self.archive)
        self.assertEqual(len(self.host.created), 1)

    def test_uncertain_create_rejects_wrong_immutable_bytes(self):
        self.prepare()
        self.host.fail_create = True
        with self.assertRaises(RecoveryRequired):
            self.adapter.publish(self.receipt, self.archive)
        metadata = self.host.created[0]
        self.host.files[metadata["library_file_id"]] = b"different"
        with self.assertRaises(CheckpointError):
            self.adapter.reconcile_created(self.receipt, metadata)
        self.assertEqual(read_receipt(self.receipt)["phase"], "verify_pending")
        self.assertIsNone(self.host.head["publication"])

    def test_activation_failure_retains_prior_view_and_staged_file(self):
        self.prepare()
        first = self.adapter.publish(self.receipt, self.archive)
        old_head = self.host.head
        archive = self.root / "next.tar.gz"
        receipt = self.root / "next.json"
        self.prepare("operation-2", archive=archive, receipt=receipt)
        self.host.fail_activation = True
        with self.assertRaises(RecoveryRequired):
            self.adapter.publish(receipt, archive)
        self.assertEqual(self.host.head, old_head)
        self.assertEqual(len(self.host.files), 2)
        self.assertEqual(read_receipt(receipt)["phase"], "activation_pending")
        # The old Site head still names a distinct immutable artifact and can be
        # restored even though a newer checkpoint has already been uploaded.
        self.adapter.download_checkpoint(first["publication"], self.root / "old-view-checkpoint")
        self.host.fail_activation = False
        self.adapter.publish(receipt, archive)
        self.assertEqual(len(self.host.created), 2)
        self.assertEqual(self.host.head["generation"], 2)

    def test_full_workspace_loss_recovers_from_site_archive_intent(self):
        self.prepare()
        published = self.adapter.publish(self.receipt, self.archive)
        self.receipt.unlink()
        self.archive.unlink()
        shutil.rmtree(self.checkpoint)
        shutil.rmtree(self.state)
        destination = self.root / "recovered-checkpoint"
        receipt = self.root / "recovered-receipt.json"
        restored = self.adapter.recover_published(receipt, destination)
        self.assertEqual(restored["publication"], published["publication"])
        self.assertEqual(read_receipt(receipt)["phase"], "published")
        self.assertEqual(verify_checkpoint(destination)["checkpoint_id"], published["publication"]["checkpoint_id"])
        self.assertEqual(len(self.host.created), 1)
        self.assertEqual(self.host.activations, 1)

    def test_archive_read_denial_blocks_activation_and_preserves_identity(self):
        self.prepare()
        def denied(*args):
            raise PermissionError("archive access denied")
        self.adapter.materialize_archive = denied
        with self.assertRaises(RecoveryRequired):
            self.adapter.publish(self.receipt, self.archive)
        saved = read_receipt(self.receipt)
        self.assertEqual(saved["phase"], "verify_pending")
        self.assertEqual(saved["publication"]["library_file_id"], self.host.created[0]["library_file_id"])
        self.assertIsNone(self.host.head["publication"])
        self.assertEqual(self.host.activations, 0)

    def test_activation_response_loss_recovers_without_reactivation(self):
        self.prepare()
        self.host.commit_then_fail = True
        with self.assertRaises(RecoveryRequired):
            self.adapter.publish(self.receipt, self.archive)
        self.assertEqual(self.host.head["generation"], 1)
        result = self.adapter.publish(self.receipt, self.archive)
        self.assertTrue(result["recovered"])
        self.assertEqual(self.host.activations, 1)

    def test_site_fence_changed_before_upload_creates_nothing(self):
        self.prepare()
        second_receipt, second_archive = self.root / "second.json", self.root / "second.tar.gz"
        self.prepare("other", receipt=second_receipt, archive=second_archive)
        self.adapter.publish(second_receipt, second_archive)
        with self.assertRaises(PublicationConflict):
            self.adapter.publish(self.receipt, self.archive)
        self.assertEqual(len(self.host.created), 1)

    def test_site_fence_race_retains_orphan_and_does_not_overwrite(self):
        self.prepare()
        original_create = self.host.create
        other_receipt, other_archive = self.root / "other.json", self.root / "other.tar.gz"
        self.prepare("winner", receipt=other_receipt, archive=other_archive)
        self.adapter.publish(other_receipt, other_archive)
        winner = self.host.head
        self.host.head = {"generation": 0, "publication": None}
        def raced_create(*args):
            metadata = original_create(*args)
            self.host.head = winner
            return metadata
        self.adapter.create_archive = raced_create
        with self.assertRaises(PublicationConflict):
            self.adapter.publish(self.receipt, self.archive)
        self.assertEqual(self.host.head, winner)
        self.assertEqual(len(self.host.files), 2)
        self.assertEqual(read_receipt(self.receipt)["phase"], "archived")

    def test_changed_archive_is_not_uploaded(self):
        self.prepare()
        self.archive.write_bytes(b"different")
        with self.assertRaises(CheckpointError):
            self.adapter.publish(self.receipt, self.archive)
        self.assertFalse(self.host.created)

    def malicious(self, members):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name, data, kind in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.size = len(data) if kind == tarfile.REGTYPE else 0
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = "/outside"
                tar.addfile(info, io.BytesIO(data) if info.size else None)
        path = self.root / "bad.tar.gz"
        path.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
        return path

    def test_unsafe_archive_members_rejected_without_publication(self):
        cases = [
            [("../escape", b"x", tarfile.REGTYPE)],
            [("/absolute", b"x", tarfile.REGTYPE)],
            [("payload/link", b"", tarfile.SYMTYPE)],
            [("payload/link", b"", tarfile.LNKTYPE)],
            [("payload/device", b"", tarfile.CHRTYPE)],
            [("payload/pax", b"", tarfile.XHDTYPE)],
            [("manifest.json", b"{}", tarfile.REGTYPE), ("manifest.json", b"{}", tarfile.REGTYPE)],
        ]
        for number, members in enumerate(cases):
            with self.subTest(members=members):
                archive = self.malicious(members)
                destination = self.root / f"bad-{number}"
                with self.assertRaises(CheckpointError):
                    unpack_checkpoint(archive, destination, expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), expected_checkpoint_id="a" * 64)
                self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".checkpoint-unpack-*")))

    def test_expansion_limit_and_symlink_destination(self):
        package = pack_checkpoint(self.checkpoint, self.archive)
        with self.assertRaisesRegex(CheckpointError, "size limit"):
            unpack_checkpoint(self.archive, self.root / "too-large", expected_sha256=package["archive_sha256"], expected_checkpoint_id=package["checkpoint_id"], max_expanded_bytes=4096)
        linked = self.root / "linked"
        linked.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(CheckpointError):
            unpack_checkpoint(self.archive, linked / "new", expected_sha256=package["archive_sha256"], expected_checkpoint_id=package["checkpoint_id"])

    def test_callback_metadata_extras_are_not_persisted(self):
        self.prepare()
        create = self.host.create
        def unsafe(*args):
            return {**create(*args), "access_token": "must-not-be-saved"}
        self.adapter.create_archive = unsafe
        with self.assertRaises(RecoveryRequired):
            self.adapter.publish(self.receipt, self.archive)
        self.assertNotIn("must-not-be-saved", self.receipt.read_text())
        self.assertIsNone(self.host.head["publication"])


if __name__ == "__main__":
    unittest.main()

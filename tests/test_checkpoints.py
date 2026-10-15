import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from academic_radar.checkpoints import (
    CheckpointError, create_checkpoint, restore_checkpoint, verify_checkpoint, inventory_references,
)
from academic_radar.product import initialize_installation, load_config


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def reseal(checkpoint):
    """Deliberately forge checksums to test structural validation, not authenticity."""
    manifest_path = checkpoint / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["files"]:
        path = checkpoint / "payload" / entry["path"]
        if path.is_file():
            raw = path.read_bytes()
            entry.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    manifest.pop("checkpoint_id")
    manifest["checkpoint_id"] = hashlib.sha256(canonical(manifest)).hexdigest()
    manifest_path.write_bytes(canonical(manifest) + b"\n")


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "old-mac-state"
        initialize_installation(self.state)
        self.db_path = self.state / "papers.sqlite3"
        self.checkpoint = self.root / "checkpoint"
        self.destination = self.root / "new-cloud-state"
        db = sqlite3.connect(self.db_path)
        self.profile_hash, profile_id = db.execute("SELECT profile_hash,id FROM profile_versions WHERE status='active'").fetchone()
        pdf = b"%PDF-1.4\nsynthetic checkpoint test\n%%EOF\n"
        self.pdf = self.state / "fulltexts" / "paper.pdf"
        self.pdf.parent.mkdir()
        self.pdf.write_bytes(pdf)
        for directory in ("agent_queue", "results", "digests"):
            (self.state / directory).mkdir()
        queue = {"schema_version": 1, "run_id": "pending", "profile_hash": self.profile_hash,
                 "profile_path": str(self.state / "research-profile.md"), "papers": [], "source_failures": []}
        self.queue = self.state / "agent_queue" / "pending.json"
        self.queue.write_text(json.dumps(queue))
        (self.state / "agent_queue" / "historical.json").write_text(json.dumps(dict(queue, run_id="historical")))
        results = {"run_id": "pending", "profile_hash": self.profile_hash, "results": [], "model": "synthetic-test", "source_failures": []}
        self.results = self.state / "results" / "pending.json"
        self.results.write_text(json.dumps(results))
        (self.state / "results" / "historical.json").write_text(json.dumps(dict(results, run_id="historical")))
        self.digest = self.state / "digests" / "historical.md"
        self.digest.write_text("Historical digest\n")
        with db:
            db.execute("INSERT INTO papers(identity,title,first_seen,updated_at) VALUES('doi:test','Synthetic paper','now','now')")
            db.execute("INSERT INTO fulltext_files(identity,stored_path,original_name,sha256,size_bytes,imported_at) VALUES(?,?,?,?,?,?)",
                       ("doi:test", str(self.pdf), "paper.pdf", hashlib.sha256(pdf).hexdigest(), len(pdf), "now"))
            db.execute("INSERT INTO paper_feedback(identity,interest,reason,favorite,reading_status,created_at,updated_at) VALUES('doi:test','interested','Synthetic reason',1,'read_later','now','now')")
            db.execute("INSERT INTO feedback_events(identity,interest,reason,favorite,reading_status,created_at) VALUES('doi:test','interested','Synthetic reason',1,'read_later','now')")
            for run in ("pending", "historical"):
                details = {"queue_path": str(self.state / "agent_queue" / (run + ".json")), "profile_path": str(self.state / "research-profile.md")}
                if run == "historical":
                    details.update(results_path=str(self.state / "results" / (run + ".json")), digest_path=str(self.digest))
                db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at,details_json) VALUES(?,?,?,?,?)",
                           (run, "agent-export", "running" if run == "pending" else "succeeded", "now", json.dumps(details)))
                db.execute("INSERT INTO agent_jobs(run_id,profile_hash,status,queue_path,results_path,created_at,profile_version_id) VALUES(?,?,?,?,?,?,?)",
                           (run, self.profile_hash, "exported" if run == "pending" else "imported", str(self.state / "agent_queue" / (run + ".json")),
                            None if run == "pending" else str(self.state / "results" / (run + ".json")), "now", profile_id))
            db.execute("INSERT INTO recommendation_snapshots(run_id,identity,score,reasons,evidence_source) VALUES('historical','doi:test',0.91,'historical judgment','screening')")
            db.execute("CREATE TABLE future_state (identifier TEXT PRIMARY KEY, value BLOB)")
            db.execute("INSERT INTO future_state VALUES('preserve me',?)", (b'\x00\x01untouched',))
        db.close()

    def create(self):
        return create_checkpoint(self.state, self.checkpoint)

    def test_full_state_roundtrip_across_paths_and_source_unchanged(self):
        source_bytes = self.db_path.read_bytes()
        source_config = (self.state / "config.toml").read_bytes()
        result = self.create()
        self.assertGreater(result["table_counts"]["recommendation_snapshots"], 0)
        self.assertEqual(self.db_path.read_bytes(), source_bytes)
        self.assertEqual((self.state / "config.toml").read_bytes(), source_config)
        manifest = verify_checkpoint(self.checkpoint)
        self.assertEqual(result["checkpoint_id"], manifest["checkpoint_id"])
        self.assertNotIn(str(self.state).encode(), (self.checkpoint / "payload" / "papers.sqlite3").read_bytes())
        restored = restore_checkpoint(self.checkpoint, self.destination)
        self.assertEqual(restored["integrity"], "ok")
        a, b = sqlite3.connect(self.db_path), sqlite3.connect(self.destination / "papers.sqlite3")
        try:
            for table in ("profile_versions", "paper_feedback", "feedback_events", "recommendation_snapshots", "future_state", "schema_migrations"):
                self.assertEqual(a.execute('SELECT * FROM "' + table + '"').fetchall(), b.execute('SELECT * FROM "' + table + '"').fetchall())
            stored = b.execute("SELECT stored_path FROM fulltext_files").fetchone()[0]
            self.assertEqual(Path(stored), self.destination / "fulltexts" / "paper.pdf")
            self.assertEqual(Path(stored).read_bytes(), self.pdf.read_bytes())
            details = json.loads(b.execute("SELECT details_json FROM pipeline_runs WHERE run_id='historical'").fetchone()[0])
            self.assertEqual(details["digest_path"], str(self.destination / "digests" / "historical.md"))
        finally:
            a.close(); b.close()
        queue = json.loads((self.destination / "agent_queue" / "pending.json").read_text())
        self.assertEqual(queue["profile_path"], str(self.destination / "research-profile.md"))
        self.assertEqual(load_config(self.destination / "config.toml")["state_dir"], ".")

    def test_restored_exported_job_imports_using_restored_paths(self):
        self.create()
        restore_checkpoint(self.checkpoint, self.destination)
        spec = importlib.util.spec_from_file_location("checkpoint_test_monitor", Path(__file__).parents[1] / "scripts" / "paper_monitor.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        # Delete old state to prove the import cannot fall back to the Mac.
        import shutil
        shutil.rmtree(self.state)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.agent_import(self.destination / "config.toml", self.destination / "results" / "pending.json"), 0)
        db = sqlite3.connect(self.destination / "papers.sqlite3")
        try:
            status, path = db.execute("SELECT status,results_path FROM agent_jobs WHERE run_id='pending'").fetchone()
            self.assertEqual(status, "imported")
            self.assertEqual(path, str(self.destination / "results" / "pending.json"))
        finally:
            db.close()

    def test_missing_pdf_fails_before_output_publication(self):
        self.pdf.unlink()
        with self.assertRaisesRegex(CheckpointError, "Missing"):
            self.create()
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(list(self.root.glob(".checkpoint-*")), [])

    def test_modified_pdf_disagrees_with_database(self):
        self.pdf.write_bytes(b"%PDF corrupted")
        with self.assertRaisesRegex(CheckpointError, "PDF checksum"):
            self.create()
        self.assertFalse(self.checkpoint.exists())

    def test_corrupt_or_missing_file_rejected_without_destination(self):
        self.create()
        member = self.checkpoint / "payload" / "fulltexts" / "paper.pdf"
        member.write_bytes(b"corrupt")
        with self.assertRaisesRegex(CheckpointError, "checksum"):
            restore_checkpoint(self.checkpoint, self.destination)
        self.assertFalse(self.destination.exists())
        member.unlink()
        with self.assertRaisesRegex(CheckpointError, "Missing"):
            restore_checkpoint(self.checkpoint, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".restore-*")), [])

    def test_manifest_corruption_and_unlisted_file_rejected(self):
        self.create()
        path = self.checkpoint / "manifest.json"
        original = path.read_bytes()
        data = json.loads(original)
        data["table_counts"]["papers"] = 999
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(CheckpointError, "manifest checksum"):
            verify_checkpoint(self.checkpoint)
        path.write_bytes(original)
        (self.checkpoint / "payload" / "extra.txt").write_text("unexpected")
        with self.assertRaisesRegex(CheckpointError, "unexpected"):
            verify_checkpoint(self.checkpoint)

    def test_forged_manifest_traversal_and_absolute_paths_rejected(self):
        self.create()
        path = self.checkpoint / "manifest.json"
        original = json.loads(path.read_text())
        for malicious in ("../outside", "/tmp/outside", "a/../../outside", "a\\..\\outside", "a//b", "C:/outside"):
            data = json.loads(json.dumps(original))
            data["files"][0]["path"] = malicious
            data.pop("checkpoint_id")
            data["checkpoint_id"] = hashlib.sha256(canonical(data)).hexdigest()
            path.write_bytes(canonical(data))
            with self.assertRaisesRegex(CheckpointError, "Unsafe relative"):
                restore_checkpoint(self.checkpoint, self.destination)
            self.assertFalse(self.destination.exists())

    def test_source_and_checkpoint_symlinks_rejected(self):
        self.pdf.unlink()
        self.pdf.symlink_to(self.results)
        with self.assertRaisesRegex(CheckpointError, "Symlink"):
            self.create()
        self.pdf.unlink()
        self.pdf.write_bytes(b"%PDF-1.4\nsynthetic checkpoint test\n%%EOF\n")
        self.create()
        member = self.checkpoint / "payload" / "fulltexts" / "paper.pdf"
        member.unlink()
        member.symlink_to(self.pdf)
        with self.assertRaisesRegex(CheckpointError, "[Ss]ymlink"):
            restore_checkpoint(self.checkpoint, self.destination)
        self.assertFalse(self.destination.exists())

    def test_external_reference_fails_closed(self):
        external = self.root / "outside.json"
        external.write_text("{}")
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute("UPDATE agent_jobs SET queue_path=? WHERE run_id='pending'", (str(external),))
        db.close()
        with self.assertRaisesRegex(CheckpointError, "outside state"):
            self.create()
        self.assertFalse(self.checkpoint.exists())

    def test_forged_database_reference_rejected_before_restore(self):
        self.create()
        db = sqlite3.connect(self.checkpoint / "payload" / "papers.sqlite3")
        with db:
            db.execute("UPDATE agent_jobs SET queue_path='state://../outside'")
        db.close()
        reseal(self.checkpoint)
        with self.assertRaisesRegex(CheckpointError, "Unsafe relative"):
            restore_checkpoint(self.checkpoint, self.destination)
        self.assertFalse(self.destination.exists())

    def test_credentials_excluded_and_side_effects_disabled(self):
        (self.state / "cloud-sync.json").write_text('{"sync_token":"synthetic-secret"}')
        (self.state / ".env").write_text("PASSWORD=synthetic-secret")
        with (self.state / "config.toml").open("a") as handle:
            handle.write('\n[cloud_sync]\nenabled = true\nendpoint = "https://example.chatgpt.site"\ncredentials_file = "/private/credentials.json"\n')
        self.create()
        manifest = verify_checkpoint(self.checkpoint)
        excluded = {item["path"] for item in manifest["excluded"]}
        self.assertTrue({"cloud-sync.json", ".env"}.issubset(excluded))
        config = load_config(self.checkpoint / "payload" / "config.toml")
        self.assertFalse(config["cloud_sync"]["enabled"])
        self.assertNotIn("credentials_file", config["cloud_sync"])
        self.assertNotIn(b"synthetic-secret", b"".join(p.read_bytes() for p in self.checkpoint.rglob("*") if p.is_file()))

    def test_raw_credentials_in_config_rejected(self):
        with (self.state / "config.toml").open("a") as handle:
            handle.write('\n[unsafe]\napi_key = "synthetic-secret"\n')
        with self.assertRaisesRegex(CheckpointError, "Credential"):
            self.create()

    def test_output_and_restore_cannot_mutate_source_trees(self):
        with self.assertRaisesRegex(CheckpointError, "outside the source"):
            create_checkpoint(self.state, self.state / "nested" / "backup")
        self.assertFalse((self.state / "nested").exists())
        self.create()
        with self.assertRaisesRegex(CheckpointError, "outside the immutable"):
            restore_checkpoint(self.checkpoint, self.checkpoint / "nested" / "restored")
        self.assertFalse((self.checkpoint / "nested").exists())
        verify_checkpoint(self.checkpoint)

    def test_existing_destination_never_overwritten(self):
        self.create()
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):
            restore_checkpoint(self.checkpoint, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])
        with self.assertRaises(FileExistsError):
            self.create()

    def test_racing_empty_destination_never_overwritten(self):
        from academic_radar import checkpoints
        self.create()
        publish = checkpoints._publish
        def race(stage, destination):
            destination.mkdir()
            return publish(stage, destination)
        with patch.object(checkpoints, "_publish", race):
            with self.assertRaises(FileExistsError):
                restore_checkpoint(self.checkpoint, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(list(self.root.glob(".restore-*")), [])

    def test_cleanup_backup_and_report_references_roundtrip(self):
        from academic_radar.governance import preview_cleanup, apply_cleanup_preview
        report = preview_cleanup(self.db_path, self.state, 0.70)
        self.create()
        restore_checkpoint(self.checkpoint, self.destination)
        restored_report = self.destination / Path(report["report_path"]).relative_to(self.state)
        data = json.loads(restored_report.read_text())
        self.assertEqual(data["database"], str(self.destination / "papers.sqlite3"))
        self.assertTrue(Path(data["backup"]).is_file())
        applied = apply_cleanup_preview(self.destination / "papers.sqlite3", self.destination, restored_report)
        self.assertEqual(applied["status"], "applied")

    def test_external_configuration_and_profile_alias_normalize(self):
        outside = self.root / "external-config.toml"
        text = (self.state / "config.toml").read_text()
        text = text.replace('state_dir = "."', 'state_dir = ' + json.dumps(str(self.state)))
        outside.write_text(text)
        result = create_checkpoint(self.state, self.checkpoint, config_path=outside)
        self.assertEqual(result["files"], len(verify_checkpoint(self.checkpoint)["files"]))
        self.assertEqual(load_config(self.checkpoint / "payload" / "config.toml")["state_dir"], ".")

    def test_missing_database_leaves_no_staging_directory(self):
        self.db_path.unlink()
        with self.assertRaisesRegex(CheckpointError, "Missing"):
            self.create()
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(list(self.root.glob(".checkpoint-*")), [])

    def test_forged_pdf_resealed_manifest_still_rejected_by_database(self):
        self.create()
        (self.checkpoint / "payload" / "fulltexts" / "paper.pdf").write_bytes(b"%PDF other bytes")
        reseal(self.checkpoint)
        with self.assertRaisesRegex(CheckpointError, "PDF checksum"):
            restore_checkpoint(self.checkpoint, self.destination)
        self.assertFalse(self.destination.exists())

    def test_database_trigger_is_rejected_not_executed(self):
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute("CREATE TRIGGER unexpected AFTER UPDATE ON agent_jobs BEGIN UPDATE meta SET value='trigger-ran'; END")
        db.close()
        with self.assertRaisesRegex(CheckpointError, "triggers"):
            self.create()
        self.assertFalse(self.checkpoint.exists())

    def test_credentials_inside_json_and_sqlite_metadata_rejected(self):
        queue = json.loads(self.queue.read_text())
        queue["api_key"] = "synthetic-secret"
        self.queue.write_text(json.dumps(queue))
        with self.assertRaisesRegex(CheckpointError, "Credential"):
            self.create()
        queue.pop("api_key")
        self.queue.write_text(json.dumps(queue))
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute("INSERT INTO meta VALUES('api_key','synthetic-secret')")
        db.close()
        with self.assertRaisesRegex(CheckpointError, "Credential"):
            self.create()

    def test_source_parent_traversal_and_missing_json_reference_rejected(self):
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute("UPDATE pipeline_runs SET details_json=? WHERE run_id='pending'", (json.dumps({"queue_path": "../outside.json"}),))
        db.close()
        with self.assertRaisesRegex(CheckpointError, "Unsafe relative"):
            self.create()
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute("UPDATE pipeline_runs SET details_json=? WHERE run_id='pending'", (json.dumps({"results_path": "results/missing.json"}),))
        db.close()
        with self.assertRaisesRegex(CheckpointError, "Missing"):
            self.create()

    def archive_entry(self, source, disposition="archived_local", **extra):
        entry = {"source": str(source), "disposition": disposition, "reason": "Explicit synthetic migration inventory"}
        if disposition == "archived_local":
            entry["archive_label"] = "Synthetic local backup"
        return dict(entry, **extra)

    def omitted_pdf_entry(self):
        db = sqlite3.connect(self.db_path)
        row = db.execute("SELECT stored_path,sha256,size_bytes FROM fulltext_files").fetchone()
        db.close()
        return self.archive_entry(row[0], "not_migrated", sha256=row[1], size=row[2])

    def test_explicit_missing_history_archives_persist_across_recheckpoint(self):
        paths = [self.state / "agent_queue" / "historical.json", self.state / "results" / "historical.json", self.digest]
        entries = [self.archive_entry(path, "unavailable" if i == 1 else "archived_local") for i, path in enumerate(paths)]
        for path in paths:
            path.unlink()
        with self.assertRaises(CheckpointError):
            self.create()
        create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": entries})
        manifest = verify_checkpoint(self.checkpoint)
        self.assertEqual(len(manifest["reference_provenance"]), 3)
        restore_checkpoint(self.checkpoint, self.destination)
        self.assertTrue((self.destination / "reference-provenance.json").is_file())
        db = sqlite3.connect(self.destination / "papers.sqlite3")
        try:
            paths = db.execute("SELECT queue_path,results_path FROM agent_jobs WHERE run_id='historical'").fetchone()
            self.assertTrue(all(path.startswith("archive://") and len(path) == 74 for path in paths))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recommendation_snapshots").fetchone()[0], 1)
        finally:
            db.close()
        import shutil
        shutil.rmtree(self.state)
        later = self.root / "later-checkpoint"
        create_checkpoint(self.destination, later)
        self.assertEqual(verify_checkpoint(later)["reference_provenance"], manifest["reference_provenance"])

    def test_relocation_and_later_archive_hydration_use_explicit_local_files(self):
        old = "/old-repository/state/agent_results/historical.json"
        target = self.state / "results" / "historical.json"
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute("UPDATE agent_jobs SET results_path=? WHERE run_id='historical'", (old,))
        db.close()
        create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [{"source": old, "disposition": "relocate", "target": "results/historical.json"}]})
        restore_checkpoint(self.checkpoint, self.destination)
        db = sqlite3.connect(self.destination / "papers.sqlite3")
        try:
            self.assertEqual(db.execute("SELECT results_path FROM agent_jobs WHERE run_id='historical'").fetchone()[0], str(self.destination / "results" / "historical.json"))
        finally:
            db.close()
        self.assertTrue(target.exists())

    def test_archive_policy_cannot_exempt_active_or_runtime_linked_job(self):
        for runtime in (False, True):
            db = sqlite3.connect(self.db_path)
            with db:
                if runtime:
                    db.execute("INSERT INTO meta VALUES('cloud_runtime_export:pending-ack','historical')")
                    source = self.state / "agent_queue" / "historical.json"
                else:
                    source = self.queue
            db.close()
            with self.assertRaisesRegex(CheckpointError, "Operational"):
                create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [self.archive_entry(source)]})
            self.assertFalse(self.checkpoint.exists())

    def test_shared_terminal_and_operational_path_cannot_be_archived(self):
        db = sqlite3.connect(self.db_path)
        shared = str(self.state / "agent_queue" / "historical.json")
        with db:
            db.execute("UPDATE agent_jobs SET queue_path=? WHERE run_id='pending'", (shared,))
        db.close()
        with self.assertRaisesRegex(CheckpointError, "Operational"):
            create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [self.archive_entry(shared)]})

    def test_preview_audit_requires_explicit_disable_and_revalidation_flags(self):
        from academic_radar.governance import preview_cleanup
        report = preview_cleanup(self.db_path, self.state, 0.7)
        entry = self.archive_entry(report["backup"])
        with self.assertRaisesRegex(CheckpointError, "preview cleanup"):
            create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [entry]})
        entry.update(operation_disabled=True, revalidation_required=True)
        create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [entry]})
        restore_checkpoint(self.checkpoint, self.destination)
        db = sqlite3.connect(self.destination / "papers.sqlite3")
        try:
            status, backup, report_path = db.execute("SELECT status,backup_path,report_path FROM cleanup_audits").fetchone()
            self.assertEqual(status, "preview")
            self.assertTrue(backup.startswith("archive://"))
            self.assertEqual(json.loads(Path(report_path).read_text())["backup"], backup)
        finally:
            db.close()
        self.assertTrue(Path(report["backup"]).exists())
        create_checkpoint(self.destination, self.root / "again")

    def test_pdf_omission_audits_full_row_without_mutating_source_or_cloud_registry(self):
        pdf_bytes = self.pdf.read_bytes()
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM fulltext_files").fetchone())
        db.close()
        policy = {"version": 1, "entries": [self.omitted_pdf_entry()]}
        create_checkpoint(self.state, self.checkpoint, reference_policy=policy)
        manifest = verify_checkpoint(self.checkpoint)
        self.assertEqual(manifest["table_counts"]["fulltext_files"], 0)
        self.assertEqual(manifest["omitted_fulltexts"][0]["row"], row)
        self.assertNotIn("fulltexts/paper.pdf", {item["path"] for item in manifest["files"]})
        self.assertEqual(self.pdf.read_bytes(), pdf_bytes)
        db = sqlite3.connect(self.db_path)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fulltext_files").fetchone()[0], 1)
        db.close()
        restore_checkpoint(self.checkpoint, self.destination)
        db = sqlite3.connect(self.destination / "papers.sqlite3")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fulltext_files").fetchone()[0], 0)
        db.close()
        later = self.root / "later"
        create_checkpoint(self.destination, later)
        self.assertEqual(verify_checkpoint(later)["omitted_fulltexts"], manifest["omitted_fulltexts"])
        from academic_radar.product import import_fulltext
        import_fulltext(self.destination / "papers.sqlite3", self.destination, "doi:test", "new.pdf", pdf_bytes)
        db = sqlite3.connect(self.destination / "papers.sqlite3")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fulltext_files").fetchone()[0], 1)
        db.close()

    def test_only_explicit_correct_pdf_inventory_allows_omission(self):
        entry = self.omitted_pdf_entry()
        self.pdf.unlink()
        with self.assertRaises(CheckpointError):
            self.create()
        bad = dict(entry, sha256="0" * 64)
        with self.assertRaisesRegex(CheckpointError, "checksum"):
            create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [bad]})
        create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [entry]})
        self.assertEqual(verify_checkpoint(self.checkpoint)["table_counts"]["fulltext_files"], 0)

    def test_omission_refuses_triggers_or_dependent_cascade_tables(self):
        entry = self.omitted_pdf_entry()
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute("CREATE TABLE dependent_pdf(id INTEGER REFERENCES fulltext_files(id) ON DELETE CASCADE)")
            db.execute("INSERT INTO dependent_pdf SELECT id FROM fulltext_files")
        db.close()
        with self.assertRaisesRegex(CheckpointError, "dependent"):
            create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [entry]})
        db = sqlite3.connect(self.db_path)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM dependent_pdf").fetchone()[0], 1)
        with db:
            db.execute("DROP TABLE dependent_pdf")
            db.execute("CREATE TRIGGER unsafe_delete AFTER DELETE ON fulltext_files BEGIN DELETE FROM papers; END")
        db.close()
        with self.assertRaisesRegex(CheckpointError, "triggers"):
            create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [entry]})

    def test_forged_archive_marker_on_active_job_rejected_after_reseal(self):
        entry = self.archive_entry(self.state / "agent_queue" / "historical.json")
        create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [entry]})
        db = sqlite3.connect(self.checkpoint / "payload" / "papers.sqlite3")
        marker = db.execute("SELECT queue_path FROM agent_jobs WHERE run_id='historical'").fetchone()[0]
        with db:
            db.execute("UPDATE agent_jobs SET queue_path=? WHERE run_id='pending'", (marker,))
        db.close()
        reseal(self.checkpoint)
        with self.assertRaisesRegex(CheckpointError, "Operational"):
            restore_checkpoint(self.checkpoint, self.destination)
        self.assertFalse(self.destination.exists())

    def test_inventory_is_read_only_and_classifies_each_reference(self):
        before = self.db_path.read_bytes()
        inventory = inventory_references(self.state)
        queues = [row for row in inventory["references"] if row["table"] == "agent_jobs" and row["column"] == "queue_path"]
        self.assertEqual({row["status"]: row["archive_eligible"] for row in queues}, {"exported": False, "imported": True})
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_later_explicit_relocation_hydrates_archived_history(self):
        source = self.state / "agent_queue" / "historical.json"
        original_queue = json.loads(source.read_text())
        create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [self.archive_entry(source)]})
        restore_checkpoint(self.checkpoint, self.destination)
        original_queue["profile_path"] = str(self.destination / "research-profile.md")
        target = self.destination / "agent_queue" / "historical.json"
        target.write_text(json.dumps(original_queue))
        later = self.root / "hydrated"
        create_checkpoint(self.destination, later, reference_policy={"version": 1, "entries": [{"source": str(source), "disposition": "relocate", "target": "agent_queue/historical.json"}]})
        db = sqlite3.connect(later / "payload" / "papers.sqlite3")
        try:
            self.assertEqual(db.execute("SELECT queue_path FROM agent_jobs WHERE run_id='historical'").fetchone()[0], "state://agent_queue/historical.json")
        finally:
            db.close()
        verify_checkpoint(later)

    def test_active_json_reference_cannot_launder_historical_archive(self):
        result_path = self.state / "results" / "historical.json"
        result = json.loads(result_path.read_text())
        result["digest_path"] = str(self.digest)
        result_path.write_text(json.dumps(result))
        create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [self.archive_entry(self.digest)]})
        path = self.checkpoint / "payload" / "agent_queue" / "pending.json"
        queue = json.loads(path.read_text())
        queue["results_path"] = "state://results/historical.json"
        path.write_text(json.dumps(queue))
        reseal(self.checkpoint)
        with self.assertRaisesRegex(CheckpointError, "Operational"):
            verify_checkpoint(self.checkpoint)

    def test_persisted_provenance_is_required_and_checksum_validated(self):
        entry = self.archive_entry(self.state / "agent_queue" / "historical.json")
        create_checkpoint(self.state, self.checkpoint, reference_policy={"version": 1, "entries": [entry]})
        restore_checkpoint(self.checkpoint, self.destination)
        path = self.destination / "reference-provenance.json"
        original = path.read_bytes()
        data = json.loads(original)
        data["entries"][0]["reason"] = "tampered"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(CheckpointError, "provenance checksum"):
            create_checkpoint(self.destination, self.root / "bad-later")
        path.unlink()
        with self.assertRaisesRegex(CheckpointError, "Unknown or invalid archived"):
            create_checkpoint(self.destination, self.root / "missing-later")

    def test_inventory_includes_committed_live_wal_references(self):
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        self.addCleanup(db.close)
        with db:
            db.execute("UPDATE agent_jobs SET queue_path='agent_queue/new-committed-wal.json' WHERE run_id='pending'")
        self.assertGreater(self.db_path.with_name("papers.sqlite3-wal").stat().st_size, 0)
        inventory = inventory_references(self.state)
        self.assertTrue(any(row["source"] == "agent_queue/new-committed-wal.json" for row in inventory["references"]))

    def test_committed_wal_included_in_consistent_sqlite_snapshot(self):
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        with db:
            db.execute("INSERT INTO meta VALUES('wal_marker','committed')")
        self.addCleanup(db.close)
        self.assertGreater(self.db_path.with_name("papers.sqlite3-wal").stat().st_size, 0)
        self.create()
        restore_checkpoint(self.checkpoint, self.destination)
        restored = sqlite3.connect(self.destination / "papers.sqlite3")
        try:
            self.assertEqual(restored.execute("SELECT value FROM meta WHERE key='wal_marker'").fetchone()[0], "committed")
        finally:
            restored.close()


if __name__ == "__main__":
    unittest.main()

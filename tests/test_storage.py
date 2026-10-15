import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from academic_radar.storage import (
    LEGACY_MIGRATION_CHECKSUMS,
    backup_database,
    database_status,
    latest_schema_version,
    migrate_state,
    restore_database,
    upgrade_database,
)
from academic_radar.governance import apply_cleanup_preview, preview_cleanup


class StorageTests(unittest.TestCase):
    def cleanup_fixture(self, root):
        path = root / "papers.sqlite3"
        upgrade_database(path)
        db = sqlite3.connect(path)
        with db:
            db.execute("""INSERT INTO papers(
              identity,doi,title,venue,first_seen,updated_at,publication_type_raw,publication_type_source
              ) VALUES('doi:10.1/x','10.1/x','A research paper','Journal','now','now','journal-article','crossref')""")
        db.close()
        return path

    def test_archived_pending_cleanup_cannot_apply_or_restore(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = self.cleanup_fixture(root)
            preview = preview_cleanup(db_path, root, 0.70)
            marker = "archive://" + "a" * 64
            db = sqlite3.connect(db_path)
            with db:
                db.execute("UPDATE cleanup_audits SET backup_path=?", (marker,))
            before = db.execute("SELECT * FROM papers").fetchall()
            # Even a report retaining a real backup cannot bypass the disabled
            # authoritative audit. No backup validation/write is attempted.
            with patch("academic_radar.governance._verified_cleanup_backup", side_effect=AssertionError("opened backup")):
                with self.assertRaisesRegex(ValueError, "原存档"):
                    apply_cleanup_preview(db_path, root, Path(preview["report_path"]))
            self.assertEqual(db.execute("SELECT * FROM papers").fetchall(), before)
            self.assertEqual(db.execute("SELECT status FROM cleanup_audits").fetchone()[0], "preview")
            db.close()
            with self.assertRaisesRegex(ValueError, "Archived backup"):
                restore_database(Path(marker), db_path, replace=True)
            with self.assertRaisesRegex(ValueError, "原存档"):
                apply_cleanup_preview(db_path, root, Path(marker))

    def test_cleanup_rejects_changed_paper_and_preserves_feedback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = self.cleanup_fixture(root)
            preview = preview_cleanup(db_path, root, 0.70)
            db = sqlite3.connect(db_path)
            with db:
                db.execute("UPDATE papers SET title='Editorial Board'")
                db.execute("""INSERT INTO paper_feedback(identity,favorite,created_at,updated_at)
                  VALUES('doi:10.1/x',1,'now','now')""")
            db.close()

            with self.assertRaisesRegex(ValueError, "预览后改变"):
                apply_cleanup_preview(db_path, root, Path(preview["report_path"]))

            db = sqlite3.connect(db_path)
            self.assertEqual(db.execute("SELECT title,eligibility_status FROM papers").fetchone(),
                             ("Editorial Board", "quarantine"))
            self.assertEqual(db.execute("SELECT favorite FROM paper_feedback").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM cleanup_audits").fetchone()[0], "preview")
            db.close()

    def test_cleanup_rejects_other_database_and_replaced_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = self.cleanup_fixture(root)
            preview = preview_cleanup(db_path, root, 0.70)
            other = root / "other.sqlite3"
            upgrade_database(other)
            with self.assertRaisesRegex(ValueError, "其他数据库"):
                apply_cleanup_preview(other, root, Path(preview["report_path"]))
            backup_database(other, Path(preview["backup"]))
            with self.assertRaisesRegex(ValueError, "备份与预览快照不一致"):
                apply_cleanup_preview(db_path, root, Path(preview["report_path"]))

    def test_cleanup_rejects_tampered_decision_and_duplicate_items(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = self.cleanup_fixture(root)
            preview = preview_cleanup(db_path, root, 0.70)
            report_path = Path(preview["report_path"])
            for altered in (
                [dict(preview["items"][0], decision={"publication_type": "Editorial", "eligibility_status": "excluded"})],
                preview["items"] * 2,
            ):
                payload = dict(preview, items=altered)
                report_path.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ValueError, "清洗决策"):
                    apply_cleanup_preview(db_path, root, report_path)
            db = sqlite3.connect(db_path)
            self.assertEqual(db.execute("SELECT eligibility_status FROM papers").fetchone()[0], "quarantine")
            db.close()

    def test_cleanup_marks_changed_governance_for_rescreen_and_cannot_reapply(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = self.cleanup_fixture(root)
            preview = preview_cleanup(db_path, root, 0.70)
            db = sqlite3.connect(db_path)
            with db:
                db.execute("""INSERT INTO paper_feedback(identity,favorite,created_at,updated_at)
                  VALUES('doi:10.1/x',1,'now','now')""")
            db.close()
            applied = apply_cleanup_preview(db_path, root, Path(preview["report_path"]))
            db = sqlite3.connect(db_path)
            self.assertEqual(db.execute("SELECT eligibility_status,needs_rescreen FROM papers").fetchone(),
                             ("eligible", 1))
            self.assertEqual(db.execute("SELECT favorite FROM paper_feedback").fetchone()[0], 1)
            db.close()
            backup = sqlite3.connect(applied["backup"])
            self.assertEqual(backup.execute("SELECT favorite FROM paper_feedback").fetchone()[0], 1)
            self.assertEqual(backup.execute("SELECT eligibility_status FROM papers").fetchone()[0], "quarantine")
            backup.close()
            with self.assertRaisesRegex(ValueError, "已经应用"):
                apply_cleanup_preview(db_path, root, Path(preview["report_path"]))

    def test_backup_and_restore_reject_same_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "papers.sqlite3"
            upgrade_database(path)
            with self.assertRaisesRegex(ValueError, "must differ"):
                backup_database(path, path)
            with self.assertRaisesRegex(ValueError, "must differ"):
                restore_database(path, path, replace=True)
            self.assertEqual(database_status(path)["integrity"], "ok")

    def test_restore_replaces_committed_wal_and_live_readers_see_restored_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.sqlite3"
            upgrade_database(source)
            db = sqlite3.connect(source)
            with db:
                db.execute("INSERT INTO meta VALUES('marker','backup')")
            db.close()
            snapshot = root / "backup.sqlite3"
            backup_database(source, snapshot)
            destination = root / "destination.sqlite3"
            upgrade_database(destination)
            live = sqlite3.connect(destination)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            with live:
                live.execute("INSERT INTO meta VALUES('marker','newer destination')")
            self.assertGreater(destination.with_name(destination.name + "-wal").stat().st_size, 0)

            result = restore_database(snapshot, destination, replace=True)

            self.assertEqual(live.execute("SELECT value FROM meta WHERE key='marker'").fetchone()[0], "backup")
            preserved = sqlite3.connect(result["preserved_previous"])
            self.assertEqual(preserved.execute("SELECT value FROM meta WHERE key='marker'").fetchone()[0],
                             "newer destination")
            preserved.close()
            live.close()
            self.assertEqual(database_status(destination)["integrity"], "ok")

    def test_upgrade_is_idempotent_and_creates_product_tables(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            first = upgrade_database(db_path)
            second = upgrade_database(db_path)
            self.assertEqual(first["applied"], list(range(1, latest_schema_version() + 1)))
            self.assertEqual(second["applied"], [])
            status = database_status(db_path)
            self.assertEqual(status["schema_version"], latest_schema_version())
            self.assertEqual(status["integrity"], "ok")
            self.assertIn("paper_feedback", status["counts"])
            self.assertIn("fulltext_files", status["counts"])

    def test_legacy_database_is_upgraded_without_losing_papers(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.sqlite3"
            db = sqlite3.connect(db_path)
            db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE papers(identity TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL,
              abstract TEXT, venue TEXT, published TEXT, url TEXT, authors_json TEXT,
              first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO papers VALUES('doi:10.1/example','10.1/example','Example','','Venue',
              '2026-01-01','https://doi.org/10.1/example','[]','now','now');
            """)
            db.commit()
            db.close()
            result = upgrade_database(db_path)
            self.assertEqual(result["applied"], list(range(1, latest_schema_version() + 1)))
            self.assertEqual(database_status(db_path)["counts"]["papers"], 1)

    def test_partially_upgraded_legacy_database_is_reconciled(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "partial.sqlite3"
            db = sqlite3.connect(db_path)
            db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE papers(identity TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL,
              abstract TEXT, venue TEXT, published TEXT, url TEXT, authors_json TEXT,
              first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
              abstract_source TEXT NOT NULL DEFAULT 'unknown');
            CREATE TABLE screenings(identity TEXT NOT NULL, profile_hash TEXT NOT NULL,
              provider TEXT NOT NULL, model TEXT, relevant INTEGER NOT NULL, score REAL NOT NULL,
              reasons TEXT, themes_json TEXT, confidence REAL, screened_at TEXT NOT NULL,
              PRIMARY KEY(identity, profile_hash, provider, model));
            CREATE TABLE agent_jobs(run_id TEXT PRIMARY KEY, profile_hash TEXT NOT NULL,
              status TEXT NOT NULL, queue_path TEXT, results_path TEXT,
              exported_count INTEGER NOT NULL DEFAULT 0,
              imported_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
              imported_at TEXT, profile_version_id INTEGER);
            """)
            db.commit()
            db.close()

            result = upgrade_database(db_path)

            self.assertEqual(result["applied"], list(range(1, latest_schema_version() + 1)))
            db = sqlite3.connect(db_path)
            paper_columns = {row[1] for row in db.execute("PRAGMA table_info(papers)")}
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_jobs)")}
            self.assertIn("low_priority", paper_columns)
            self.assertIn("feedback_snapshot_json", job_columns)
            db.close()

    def test_future_schema_is_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "future.sqlite3"
            upgrade_database(db_path)
            db = sqlite3.connect(db_path)
            db.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(999,'future','x','now')"
            )
            before = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            db.commit()
            db.close()

            with self.assertRaisesRegex(RuntimeError, "unknown future migrations"):
                upgrade_database(db_path)

            db = sqlite3.connect(db_path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], before)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            db.close()

    def test_known_legacy_migration_checksum_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy-checksum.sqlite3"
            upgrade_database(db_path)
            db = sqlite3.connect(db_path)
            db.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version=10",
                (next(iter(LEGACY_MIGRATION_CHECKSUMS[10])),),
            )
            db.commit()
            db.close()

            result = upgrade_database(db_path)

            self.assertEqual(result["applied"], [])

    def test_backup_restore_and_pre_restore_preservation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.sqlite3"
            upgrade_database(source)
            db = sqlite3.connect(source)
            db.execute("INSERT INTO meta VALUES('marker','source')")
            db.commit()
            db.close()
            backup = root / "backup.sqlite3"
            self.assertEqual(backup_database(source, backup)["integrity"], "ok")

            destination = root / "destination.sqlite3"
            upgrade_database(destination)
            result = restore_database(backup, destination, replace=True)
            self.assertTrue(Path(result["preserved_previous"]).exists())
            db = sqlite3.connect(destination)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='marker'").fetchone()[0], "source")
            db.close()

    def test_state_migration_copies_artifacts_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "legacy-state"
            destination = root / "new-state"
            source.mkdir()
            (source / "config.toml").write_text('state_dir = "."', encoding="utf-8")
            (source / "research-profile.md").write_text("profile", encoding="utf-8")
            upgrade_database(source / "papers.sqlite3")
            result = migrate_state(source, destination)
            self.assertEqual(result["database"]["integrity"], "ok")
            self.assertTrue(result["profile_seeded"])
            self.assertTrue((destination / "config.toml").exists())
            self.assertEqual(database_status(destination / "papers.sqlite3")["counts"]["profile_versions"], 1)
            manifest = json.loads((destination / "migration-manifest.json").read_text())
            self.assertEqual(manifest["destination"], str(destination.resolve()))

    def test_state_migration_rebases_absolute_config_and_fulltext_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "legacy-state"
            destination = root / "new-state"
            (source / "fulltexts").mkdir(parents=True)
            (source / "config.toml").write_text(
                f'state_dir = {json.dumps(str(source.resolve()))}\nprofile_file = "research-profile.md"\n',
                encoding="utf-8",
            )
            (source / "research-profile.md").write_text("profile", encoding="utf-8")
            pdf = source / "fulltexts" / "paper.pdf"
            pdf.write_bytes(b"%PDF test")
            upgrade_database(source / "papers.sqlite3")
            db = sqlite3.connect(source / "papers.sqlite3")
            db.execute(
                """INSERT INTO papers(
                identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                ) VALUES('doi:10.1/x','10.1/x','Paper','','V','','','[]','now','now')"""
            )
            db.execute(
                """INSERT INTO fulltext_files(
                identity,stored_path,original_name,sha256,size_bytes,imported_at
                ) VALUES('doi:10.1/x',?,'paper.pdf','hash',9,'now')""",
                (str(pdf.resolve()),),
            )
            db.commit()
            db.close()

            result = migrate_state(source, destination)

            self.assertTrue(result["config_rebased"])
            self.assertEqual(result["fulltext_paths_rebased"], 1)
            self.assertIn('state_dir = "."', (destination / "config.toml").read_text())
            db = sqlite3.connect(destination / "papers.sqlite3")
            stored_path = Path(db.execute("SELECT stored_path FROM fulltext_files").fetchone()[0])
            db.close()
            self.assertEqual(stored_path, (destination / "fulltexts" / "paper.pdf").resolve())
            self.assertTrue(stored_path.exists())


if __name__ == "__main__":
    unittest.main()

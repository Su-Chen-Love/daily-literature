"""Independent safety regressions for archival checkpoint references."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from academic_radar.checkpoints import _database


class CheckpointReferenceReviewTests(unittest.TestCase):
    def test_nullable_primary_keys_do_not_collapse_distinct_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE future_state(key TEXT PRIMARY KEY, file_path TEXT)')
                db.executemany('INSERT INTO future_state VALUES(NULL,?)', [('first',), ('second',)])
            _database(path, lambda value, context: 'state://' + value, change=True)
            with sqlite3.connect(path) as db:
                rows = db.execute('SELECT file_path FROM future_state ORDER BY rowid').fetchall()
            self.assertEqual(rows, [('state://first',), ('state://second',)])

    def test_without_rowid_composite_primary_key_preserves_distinct_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE future_state(a TEXT,b TEXT,file_path TEXT,PRIMARY KEY(a,b)) WITHOUT ROWID')
                db.executemany('INSERT INTO future_state VALUES(?,?,?)', [('a','one','first'), ('a','two','second')])
            _database(path, lambda value, context: 'state://' + value, change=True)
            with sqlite3.connect(path) as db:
                rows = db.execute('SELECT file_path FROM future_state ORDER BY b').fetchall()
            self.assertEqual(rows, [('state://first',), ('state://second',)])


class ArchivalPolicyReviewTests(unittest.TestCase):
    def setUp(self):
        import test_checkpoints
        self.fixture = test_checkpoints.CheckpointTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    @staticmethod
    def policy(source):
        return {'version': 1, 'entries': [{'source': str(source), 'disposition': 'archived_local',
                'reason': 'Retained on the original computer', 'archive_label': 'Original offline archive'}]}

    def test_terminal_queue_archive_survives_restore_and_second_checkpoint(self):
        import json
        from academic_radar.checkpoints import create_checkpoint, restore_checkpoint, verify_checkpoint
        f = self.fixture
        historical = f.state / 'agent_queue' / 'historical.json'
        create_checkpoint(f.state, f.checkpoint, reference_policy=self.policy(historical))
        first = verify_checkpoint(f.checkpoint)
        self.assertNotIn('agent_queue/historical.json', {row['path'] for row in first['files']})
        self.assertIn('agent_queue/pending.json', {row['path'] for row in first['files']})
        restore_checkpoint(f.checkpoint, f.destination)
        with sqlite3.connect(f.destination / 'papers.sqlite3') as db:
            marker = db.execute("SELECT queue_path FROM agent_jobs WHERE run_id='historical'").fetchone()[0]
        self.assertTrue(marker.startswith('archive://'))
        historical.unlink()  # Neither verification nor re-checkpoint may open this original.
        second_dir = f.root / 'checkpoint-again'
        create_checkpoint(f.destination, second_dir)
        second = verify_checkpoint(second_dir)
        self.assertEqual(first['reference_provenance'], second['reference_provenance'])
        with sqlite3.connect(second_dir / 'payload/papers.sqlite3') as db:
            self.assertEqual(db.execute("SELECT queue_path FROM agent_jobs WHERE run_id='historical'").fetchone()[0], marker)

    def test_shared_operational_and_historical_path_cannot_be_archived(self):
        from academic_radar.checkpoints import CheckpointError, create_checkpoint
        f = self.fixture
        shared = f.state / 'agent_queue' / 'historical.json'
        with sqlite3.connect(f.db_path) as db:
            db.execute("UPDATE agent_jobs SET queue_path=? WHERE run_id='pending'", (str(shared),))
        original = f.db_path.read_bytes()
        with self.assertRaisesRegex(CheckpointError, 'Operational'):
            create_checkpoint(f.state, f.checkpoint, reference_policy=self.policy(shared))
        self.assertEqual(f.db_path.read_bytes(), original)
        self.assertFalse(f.checkpoint.exists())

    def test_imported_runtime_job_remains_operational(self):
        from academic_radar.checkpoints import CheckpointError, create_checkpoint
        f = self.fixture
        historical = f.state / 'agent_queue' / 'historical.json'
        with sqlite3.connect(f.db_path) as db:
            db.execute("INSERT INTO meta(key,value) VALUES('cloud_runtime_export:pending-publication','historical')")
        with self.assertRaisesRegex(CheckpointError, 'Operational'):
            create_checkpoint(f.state, f.checkpoint, reference_policy=self.policy(historical))
        self.assertFalse(f.checkpoint.exists())

    def test_pending_cleanup_backup_cannot_be_archived(self):
        from academic_radar.checkpoints import CheckpointError, create_checkpoint
        f = self.fixture
        backup = f.state / 'backups' / 'preview.sqlite3'
        backup.parent.mkdir()
        backup.write_bytes(b'synthetic standalone historical bytes')
        report = f.state / 'reports' / 'preview.json'
        report.parent.mkdir()
        report.write_text('{}')
        with sqlite3.connect(f.db_path) as db:
            db.execute("INSERT INTO cleanup_audits(audit_id,status,backup_path,report_path,before_json,created_at) VALUES('pending-cleanup','preview',?,?,'{}','now')", (str(backup),str(report)))
        with self.assertRaisesRegex(CheckpointError, 'Operational'):
            create_checkpoint(f.state, f.checkpoint, reference_policy=self.policy(backup))
        self.assertFalse(f.checkpoint.exists())


class PdfOmissionReviewTests(unittest.TestCase):
    def setUp(self):
        import test_checkpoints
        self.fixture = test_checkpoints.CheckpointTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with sqlite3.connect(self.fixture.db_path) as db:
            db.row_factory = sqlite3.Row
            self.pdf_row = dict(db.execute('SELECT * FROM fulltext_files').fetchone())
        self.policy = {'version': 1, 'entries': [{'source': self.pdf_row['stored_path'],
            'disposition': 'not_migrated', 'reason': 'User will re-upload PDFs later',
            'sha256': self.pdf_row['sha256'], 'size': self.pdf_row['size_bytes']}]}

    def test_pdf_omission_is_snapshot_only_audit_survives_and_reupload_works(self):
        from unittest.mock import patch
        from academic_radar import checkpoints
        from academic_radar.product import import_fulltext
        f = self.fixture
        original_db = f.db_path.read_bytes()
        pdf_bytes = f.pdf.read_bytes()
        original_read = checkpoints._read
        def never_read_omitted_pdf(path):
            self.assertNotEqual(Path(path), f.pdf, 'Omitted PDF bytes must not be read')
            return original_read(path)
        with patch.object(checkpoints, '_read', side_effect=never_read_omitted_pdf):
            checkpoints.create_checkpoint(f.state, f.checkpoint, reference_policy=self.policy)
        self.assertEqual(f.db_path.read_bytes(), original_db)
        first = checkpoints.verify_checkpoint(f.checkpoint)
        self.assertNotIn('fulltexts/paper.pdf', {item['path'] for item in first['files']})
        self.assertEqual(first['table_counts']['fulltext_files'], 0)
        self.assertEqual(first['omitted_fulltexts'][0]['row'], self.pdf_row)
        checkpoints.restore_checkpoint(f.checkpoint, f.destination)
        second_dir = f.root / 'checkpoint-second'
        checkpoints.create_checkpoint(f.destination, second_dir)
        second = checkpoints.verify_checkpoint(second_dir)
        self.assertEqual(first['omitted_fulltexts'], second['omitted_fulltexts'])
        with sqlite3.connect(f.destination / 'papers.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM fulltext_files').fetchone()[0], 0)
        uploaded = import_fulltext(f.destination / 'papers.sqlite3', f.destination,
                                   self.pdf_row['identity'], 'reuploaded.pdf', pdf_bytes)
        self.assertFalse(uploaded['deduplicated'])
        self.assertEqual(Path(uploaded['stored_path']).read_bytes(), pdf_bytes)

    def test_pdf_omission_must_not_cascade_delete_unrelated_table(self):
        from academic_radar.checkpoints import CheckpointError, create_checkpoint
        f = self.fixture
        with sqlite3.connect(f.db_path) as db:
            db.execute('CREATE TABLE future_pdf_annotations(file_id INTEGER REFERENCES fulltext_files(id) ON DELETE CASCADE, note TEXT)')
            db.execute('INSERT INTO future_pdf_annotations VALUES(?,?)', (self.pdf_row['id'],'Preserve this annotation'))
        with self.assertRaises(CheckpointError):
            create_checkpoint(f.state, f.checkpoint, reference_policy=self.policy)
        self.assertFalse(f.checkpoint.exists())

    def test_pdf_omission_rejects_triggers_before_the_delete(self):
        from academic_radar.checkpoints import CheckpointError, create_checkpoint
        f = self.fixture
        with sqlite3.connect(f.db_path) as db:
            db.execute("CREATE TRIGGER must_never_run BEFORE DELETE ON fulltext_files BEGIN SELECT RAISE(ABORT,'DELETE trigger was executed'); END")
        with self.assertRaisesRegex(CheckpointError, '[Tt]rigger'):
            create_checkpoint(f.state, f.checkpoint, reference_policy=self.policy)
        self.assertFalse(f.checkpoint.exists())


class LiveInventoryReviewTests(unittest.TestCase):
    def test_live_inventory_includes_committed_wal_references(self):
        from academic_radar.checkpoints import inventory_references
        from academic_radar.product import initialize_installation
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            initialize_installation(state)
            db = sqlite3.connect(state / 'papers.sqlite3')
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute("INSERT INTO notifications(identity,sent_at,digest_path) VALUES('wal-paper','now','/only/in/committed-wal.md')")
                db.commit()
                self.assertTrue((state / 'papers.sqlite3-wal').exists())
                inventory = inventory_references(state)
                self.assertIn('/only/in/committed-wal.md', {row['source'] for row in inventory['references']})
            finally:
                db.close()


class DisabledPreviewReviewTests(unittest.TestCase):
    def test_disabled_preview_survives_restore_blocks_apply_and_allows_normal_pipeline(self):
        import json
        import test_checkpoints
        from unittest.mock import patch
        from academic_radar import cloud_runtime
        from academic_radar.checkpoints import create_checkpoint, restore_checkpoint, verify_checkpoint
        from academic_radar.governance import apply_cleanup_preview
        f = test_checkpoints.CheckpointTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        backup = f.state / 'backups' / 'preview.sqlite3'
        backup.parent.mkdir()
        backup.write_bytes(b'Local-only restore database must never be read by this checkpoint')
        report = f.state / 'reports' / 'preview.json'
        report.parent.mkdir()
        report.write_text(json.dumps({'audit_id':'pending-cleanup','status':'preview','snapshot_version':1,
            'paper_fingerprint':'synthetic fingerprint','database':str(f.db_path),'backup':str(backup)}))
        with sqlite3.connect(f.db_path) as db:
            db.execute("UPDATE agent_jobs SET status='abandoned' WHERE run_id='pending'")
            db.execute("UPDATE pipeline_runs SET status='abandoned' WHERE run_id='pending'")
            db.execute("INSERT INTO cleanup_audits(audit_id,status,backup_path,report_path,before_json,created_at) VALUES('pending-cleanup','preview',?,?,'{}','now')", (str(backup),str(report)))
        policy = ArchivalPolicyReviewTests.policy(backup)
        policy['entries'][0].update(operation_disabled=True,revalidation_required=True)
        create_checkpoint(f.state, f.checkpoint, reference_policy=policy)
        verify_checkpoint(f.checkpoint)
        restore_checkpoint(f.checkpoint, f.destination)
        restored_db = f.destination / 'papers.sqlite3'
        restored_report = f.destination / 'reports' / 'preview.json'
        with sqlite3.connect(restored_db) as db:
            status, archived = db.execute("SELECT status,backup_path FROM cleanup_audits WHERE audit_id='pending-cleanup'").fetchone()
        self.assertEqual(status, 'preview')
        self.assertTrue(archived.startswith('archive://'))
        with patch('academic_radar.governance._verified_cleanup_backup', side_effect=AssertionError('Must not inspect archived backup')):
            with self.assertRaises(ValueError):
                apply_cleanup_preview(restored_db, f.destination, restored_report)
            # Editing the report alone must not reactivate its disabled audit.
            modified = json.loads(restored_report.read_text())
            modified['backup'] = str(backup)
            restored_report.write_text(json.dumps(modified))
            with self.assertRaises(ValueError):
                apply_cleanup_preview(restored_db, f.destination, restored_report)
        config = f.destination / 'config.toml'
        config.write_text(config.read_text() + '\n[cloud_runtime]\nrole="staging"\n')
        prepared = cloud_runtime.prepare(config, 'unrelated-normal-run', no_collect=True)
        self.assertEqual(prepared['phase'], 'awaiting_judgment')
        with sqlite3.connect(restored_db) as db:
            self.assertEqual(db.execute("SELECT status FROM cleanup_audits WHERE audit_id='pending-cleanup'").fetchone()[0], 'preview')


if __name__ == '__main__':
    unittest.main()

"""Synthetic integration coverage for Site edits at local run boundaries."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from academic_radar.engagement import (active_profile, confirm_profile,
                                      create_profile_draft, seed_active_profile)
from academic_radar.profile_sync import apply_remote_profiles, reconcile_profile_file
from academic_radar.storage import connect
import test_paper_monitor as monitor_tests

SCRIPT = Path(__file__).parents[1] / 'scripts/paper_monitor.py'
spec = importlib.util.spec_from_file_location('profile_pipeline_monitor', SCRIPT)
pm = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pm
spec.loader.exec_module(pm)


def digest(content):
    return hashlib.sha256(content.encode()).hexdigest()


class ProfilePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'papers.sqlite3'
        self.profile = self.root / 'research-profile.md'
        self.profile.write_text('Original interests')
        seed_active_profile(self.path, 'Original interests')
        self.config = self.root / 'config.toml'
        self.config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n'
                               'relevance_threshold = 0.70\n'
                               '[cloud_sync]\nenabled = true\n'
                               'endpoint = "https://test.chatgpt.site"\n'
                               'credentials_file = "synthetic-credentials.json"\n'
                               '[[sources]]\nname = "Example"\ntype = "crossref"\nissn = "1234"\n')
        (self.root / 'synthetic-credentials.json').write_text('{"sync_token":"synthetic-test-token"}')
        self.event = {
            'seq': 1, 'version': 1, 'base_version': 0, 'base_generation': 'a' * 64,
            'base_profile_hash': digest('Original interests'),
            'base_preferences': {'relevance_threshold': .7, 'instructions': ''},
            'profile_hash': digest('New interests'), 'content': 'New interests',
            'screening_preferences': {'relevance_threshold': .85, 'instructions': 'Require mechanism evidence.'},
            'summary': 'Owner clarified the scope', 'updated_at': '2026-10-18T00:00:00Z',
        }
        self.calls = []

    def request(self, endpoint, path, credentials, data=None):
        self.calls.append(path)
        if path.startswith('/api/sync/feedback?'):
            return {'events': [], 'cursor': 0, 'profile_events_version': 1}
        if path == '/api/sync/profile?after=0':
            return {'events': [self.event], 'cursor': 1}
        if path == '/api/sync/profile?after=1':
            return {'events': [], 'cursor': 1}
        raise AssertionError('A pre-run pull must not upload or acknowledge: ' + path)

    def add_paper(self, identity='doi:10.1/new'):
        db = pm.db_open(self.path)
        paper = pm.Paper(identity, identity.removeprefix('doi:'), 'Synthetic research paper',
                         'Abstract', 'Example', '2026-01-01', '', [], 'synthetic',
                         publication_type_raw='journal-article', publication_type_source='crossref')
        pm.upsert(db, paper, '2026-10-18T00:00:00Z')
        db.commit()
        db.close()
        return paper

    def export(self):
        with patch('builtins.print') as output:
            self.assertEqual(pm.agent_export(self.config, no_collect=True), 0)
        summary = json.loads(output.call_args.args[0])
        return json.loads(Path(summary['queue_path']).read_text())

    def test_collection_pulls_profile_before_collecting(self):
        def collect(cfg, db, now, run_id):
            self.assertEqual(self.profile.read_text(), 'New interests')
            self.assertEqual(db.execute("SELECT content FROM profile_versions WHERE status='active'").fetchone()[0], 'New interests')
            return [], [], []
        with patch('academic_radar.cloud_sync.request_json', side_effect=self.request), \
                patch.object(pm, 'collect_into_db', side_effect=collect) as collector, patch('builtins.print'):
            self.assertEqual(pm.collect_only(self.config), 0)
        collector.assert_called_once()
        self.assertEqual(self.calls, ['/api/sync/feedback?after=0', '/api/sync/profile?after=0'])

    def test_export_freezes_new_interests_and_never_requeues_old_judgments(self):
        paper = self.add_paper()
        old = self.add_paper('doi:10.1/old')
        with connect(self.path) as db:
            db.execute("INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,screened_at) VALUES(?,?,'codex-agent','host',0,.2,'before')", (old.identity, digest('Original interests')))
            before = [tuple(row) for row in db.execute('SELECT * FROM screenings')]
        with patch('academic_radar.cloud_sync.request_json', side_effect=self.request):
            queue = self.export()
        self.assertEqual(queue['profile_hash'], digest('New interests'))
        self.assertEqual(queue['profile_content'], 'New interests')
        self.assertEqual(queue['threshold'], .85)
        self.assertEqual(queue['screening_instructions'], 'Require mechanism evidence.')
        self.assertEqual(queue['screening_preferences_version'], 1)
        self.assertEqual([row['identity'] for row in queue['papers']], [paper.identity])
        with connect(self.path) as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM screenings')], before)

    def test_edits_arriving_during_collection_are_pulled_again_before_freeze(self):
        first_pull = True
        def request(endpoint, path, credentials, data=None):
            nonlocal first_pull
            if path.startswith('/api/sync/feedback?'):
                return {'events': [], 'cursor': 0, 'profile_events_version': 1}
            if first_pull:
                first_pull = False
                return {'events': [], 'cursor': 0}
            return {'events': [self.event], 'cursor': 1}
        def collected(cfg, db, now, run_id):
            self.assertEqual(self.profile.read_text(), 'Original interests')
            return [], [], []
        with patch('academic_radar.cloud_sync.request_json', side_effect=request), \
                patch.object(pm, 'collect_into_db', side_effect=collected), \
                patch.object(pm, 'run_enrichment', return_value={'updated': 0}), patch('builtins.print') as output:
            self.assertEqual(pm.agent_export(self.config), 0)
        queue = json.loads(Path(json.loads(output.call_args.args[0])['queue_path']).read_text())
        self.assertEqual(queue['profile_content'], 'New interests')

    def test_busy_or_failed_pull_blocks_collection_and_queue_creation(self):
        for command in (pm.collect_only, pm.agent_export):
            with self.subTest(command=command.__name__), \
                    patch.object(pm, 'pull_configured', return_value={'status': 'busy'}), \
                    patch.object(pm, 'collect_into_db') as collector:
                with self.assertRaisesRegex(RuntimeError, 'retry'):
                    command(self.config)
                collector.assert_not_called()
            with self.subTest(command=command.__name__, failure=True), \
                    patch.object(pm, 'pull_configured', side_effect=ValueError('synthetic conflict')):
                with self.assertRaisesRegex(ValueError, 'synthetic conflict'):
                    command(self.config)
        self.assertFalse((self.root / 'agent_queue').exists())

    def test_unsaved_local_file_survives_and_blocks_queue(self):
        self.profile.write_text('Unsaved local work')
        with patch('academic_radar.cloud_sync.request_json', side_effect=self.request):
            with self.assertRaisesRegex(ValueError, 'file changed'):
                self.export()
        self.assertEqual(self.profile.read_text(), 'Unsaved local work')
        self.assertFalse((self.root / 'agent_queue').exists())

    def test_import_uses_frozen_threshold_after_a_later_preference_edit(self):
        paper = self.add_paper()
        with patch('academic_radar.cloud_sync.request_json', side_effect=self.request):
            queue = self.export()
        second = {**self.event, 'seq': 2, 'version': 2, 'base_version': 1,
                  'base_profile_hash': digest('New interests'),
                  'base_preferences': self.event['screening_preferences'],
                  'screening_preferences': {'relevance_threshold': .95, 'instructions': 'Future queue only.'}}
        with connect(self.path) as db:
            apply_remote_profiles(db, [second], 2, pm.config_load(self.config))
        reconcile_profile_file(self.path, self.profile)
        result = monitor_tests.MonitorTests().structured_result(paper.identity)
        results_path = self.root / 'synthetic-results.json'
        results_path.write_text(json.dumps({'run_id': queue['run_id'], 'profile_hash': queue['profile_hash'],
                                           'model': 'synthetic-host', 'results': [result]}))
        with patch('academic_radar.cloud_sync.sync_configured', return_value={'status': 'succeeded'}), patch('builtins.print'):
            self.assertEqual(pm.agent_import(self.config, results_path), 0)
        with connect(self.path) as db:
            row = db.execute('SELECT relevant,score FROM screenings').fetchone()
            self.assertEqual((row['relevant'], row['score']), (1, .88))


class ProfileConfirmationTests(unittest.TestCase):
    def test_confirmation_holds_write_lock_during_file_replacement(self):
        from academic_radar.engagement import _atomic_write
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root / 'papers.sqlite3'; profile = root / 'profile.md'
            profile.write_text('original'); seed_active_profile(path, 'original')
            draft = create_profile_draft(path, 'revised', 'Owner confirmed change')
            def locked_write(destination, content):
                other = connect(path)
                try:
                    other.execute('PRAGMA busy_timeout=0')
                    with self.assertRaises(sqlite3.OperationalError):
                        other.execute('BEGIN IMMEDIATE')
                finally:
                    other.close()
                _atomic_write(destination, content)
            with patch('academic_radar.engagement._atomic_write', side_effect=locked_write):
                confirm_profile(path, draft['id'], profile)
            self.assertEqual(profile.read_text(), 'revised')

    def test_activation_failure_restores_exact_file_and_active_version(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root / 'papers.sqlite3'; profile = root / 'profile.md'
            original = 'original\r\nExact line endings\r\n'
            profile.write_bytes(original.encode()); old = seed_active_profile(path, original)
            draft = create_profile_draft(path, 'revised', 'Owner confirmed change')
            with connect(path) as db:
                db.execute("CREATE TRIGGER fail_activation BEFORE UPDATE OF status ON profile_versions WHEN NEW.status='active' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic failure'):
                confirm_profile(path, draft['id'], profile)
            self.assertEqual(profile.read_bytes(), original.encode())
            self.assertEqual(active_profile(path)['id'], old['id'])

    def test_confirmation_refuses_pending_remote_file_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root / 'papers.sqlite3'; profile = root / 'profile.md'
            profile.write_text('original'); seed_active_profile(path, 'original')
            draft = create_profile_draft(path, 'revised', 'Owner confirmed change')
            with connect(path) as db:
                db.execute("INSERT INTO meta VALUES('cloud_profile_file_pending','pending')")
            with self.assertRaisesRegex(ValueError, 'pending cloud profile'):
                confirm_profile(path, draft['id'], profile)
            self.assertEqual(profile.read_text(), 'original')

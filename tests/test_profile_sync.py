import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar.engagement import seed_active_profile
from academic_radar.profile_sync import (apply_remote_profiles, effective_config,
                                        reconcile_profile_file, validate_preferences)
from academic_radar.cloud_sync import _pull_remote, _synchronize, apply_remote_feedback, snapshot_records
from academic_radar.storage import connect, upgrade_database


def digest(content):
    return hashlib.sha256(content.encode()).hexdigest()


class ProfileSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'papers.sqlite3'
        self.profile = self.root / 'research-profile.md'
        upgrade_database(self.path)
        seed_active_profile(self.path, 'Original interests')
        self.profile.write_text('Original interests')
        self.db = connect(self.path)
        self.config = {'profile_file': self.profile.name, 'relevance_threshold': .7}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def event(self, seq=1, content='New research interests', base='Original interests', **changes):
        value = {'seq': seq, 'version': seq, 'base_version': seq-1, 'base_generation': 'a'*64,
                 'base_profile_hash': digest(base), 'base_preferences': {'relevance_threshold': .7, 'instructions': ''},
                 'profile_hash': digest(content), 'content': content,
                 'screening_preferences': {'relevance_threshold': .85, 'instructions': 'Prefer mechanism evidence.'},
                 'summary': 'Clarify scope', 'updated_at': '2026-10-15T13:00:00Z'}
        value.update(changes)
        return value

    def active(self):
        return dict(self.db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone())

    def test_atomic_import_and_idempotent_file_reconciliation(self):
        event = self.event(content='New interests\r\nExact bytes preserved')
        self.assertEqual(apply_remote_profiles(self.db, [event], 1, self.config), 1)
        self.assertEqual(self.active()['content'], event['content'])
        self.assertEqual(self.profile.read_bytes(), b'Original interests')
        effective = effective_config(self.config, self.path)
        self.assertEqual(effective['relevance_threshold'], .85)
        self.assertEqual(effective['screening_instructions'], 'Prefer mechanism evidence.')
        self.assertEqual(effective['screening_preferences_version'], 1)
        self.assertEqual(self.config['relevance_threshold'], .7)
        self.assertTrue(reconcile_profile_file(self.path, self.profile))
        self.assertEqual(self.profile.read_bytes(), event['content'].encode())
        self.assertFalse(reconcile_profile_file(self.path, self.profile))
        self.assertEqual(apply_remote_profiles(self.db, [event], 1, self.config), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM applied_cloud_profile_events').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 2)

    def test_caller_owned_reconciliation_keeps_connection_and_rolls_back_failure(self):
        from academic_radar.profile_sync import _reconcile_profile_file
        apply_remote_profiles(self.db,[self.event()],1,self.config)
        before=self.db.execute('PRAGMA journal_mode').fetchone()[0]
        with patch('academic_radar.profile_sync._atomic_write',side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                _reconcile_profile_file(self.db,self.profile)
        self.assertFalse(self.db.in_transaction)
        self.assertIsNotNone(self.db.execute("SELECT value FROM meta WHERE key='cloud_profile_file_pending'").fetchone())
        self.assertTrue(_reconcile_profile_file(self.db,self.profile))
        self.assertFalse(_reconcile_profile_file(self.db,self.profile))
        self.assertEqual(self.db.execute('SELECT 1').fetchone()[0],1)
        self.assertEqual(self.db.execute('PRAGMA journal_mode').fetchone()[0],before)

    def test_failed_file_write_is_reconciled_after_restart(self):
        apply_remote_profiles(self.db, [self.event()], 1, self.config)
        with patch('academic_radar.profile_sync._atomic_write', side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                reconcile_profile_file(self.path, self.profile)
        self.assertIsNotNone(self.db.execute("SELECT value FROM meta WHERE key='cloud_profile_file_pending'").fetchone())
        self.assertEqual(self.active()['content'], 'New research interests')
        reconcile_profile_file(self.path, self.profile)
        self.assertEqual(self.profile.read_text(), 'New research interests')

    def test_crash_after_file_replace_before_marker_commit_is_safe(self):
        apply_remote_profiles(self.db, [self.event()], 1, self.config)
        self.profile.write_text('New research interests')
        with patch('academic_radar.profile_sync._atomic_write', side_effect=AssertionError('Should not rewrite')):
            self.assertTrue(reconcile_profile_file(self.path, self.profile))

    def test_reconciliation_never_overwrites_unsaved_file_changes(self):
        apply_remote_profiles(self.db, [self.event()], 1, self.config)
        self.profile.write_text('My unsaved manual changes')
        with self.assertRaisesRegex(ValueError, 'file changed'):
            reconcile_profile_file(self.path, self.profile)
        self.assertEqual(self.profile.read_text(), 'My unsaved manual changes')

    def test_future_threshold_does_not_hide_todays_completed_selection(self):
        with self.db:
            self.db.execute("INSERT INTO papers(identity,title,first_seen,updated_at,eligibility_status) VALUES('paper','Paper','now','now','eligible')")
            self.db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at) VALUES('run','agent-export','succeeded','2026-10-15T01:00:00Z')")
            self.db.execute("INSERT INTO agent_jobs(run_id,profile_hash,status,created_at,imported_at) VALUES('run',?,'imported','2026-10-15T01:00:00Z','2026-10-15T02:00:00Z')", (digest('Original interests'),))
            self.db.execute("INSERT INTO run_papers(run_id,identity,role) VALUES('run','paper','selected')")
            self.db.execute("INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,screened_at,run_id) VALUES('paper',?,'codex-agent','test',1,.75,'Evidence','2026-10-15T02:00:00Z','run')", (digest('Original interests'),))
        apply_remote_profiles(self.db, [self.event()], 1, self.config)
        with patch('academic_radar.cloud_sync.local_today', return_value=dt.date(2026,10,15)):
            records = snapshot_records(self.path, self.config)
        current = next(json.loads(row['data']) for row in records if row['kind']=='history' and row['key']=='current')
        self.assertEqual(current['papers'][0]['identity'], 'paper')
        self.assertEqual(current['papers'][0]['score'], .75)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM screenings').fetchone()[0], 1)

    def test_entire_page_rolls_back_on_conflict(self):
        first = self.event()
        second = self.event(seq=2, content='Later interests', base='Wrong base', base_preferences=first['screening_preferences'])
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            apply_remote_profiles(self.db, [first, second], 2, self.config)
        self.assertEqual(self.active()['content'], 'Original interests')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM applied_cloud_profile_events').fetchone()[0], 0)
        self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='cloud_profile_cursor'").fetchone())

    def test_preference_only_edit_does_not_duplicate_content_or_touch_judgments(self):
        first = self.event(content='Original interests')
        apply_remote_profiles(self.db, [first], 1, self.config)
        second = self.event(seq=2, content='Original interests', base_preferences=first['screening_preferences'],
                            screening_preferences={'relevance_threshold': .9, 'instructions': 'Focus on operational evidence.'})
        apply_remote_profiles(self.db, [second], 2, self.config)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM applied_cloud_profile_events').fetchone()[0], 2)
        self.assertEqual(self.active()['content'], 'Original interests')
        self.assertEqual(effective_config(self.config, self.path)['screening_instructions'], 'Focus on operational evidence.')

    def test_invalid_events_and_skipped_cursors_are_refused(self):
        cases = [([self.event(profile_hash='b'*64)], 1), ([self.event(seq=2)], 2), ([], 1),
                 ([self.event(updated_at='2026-01-01')], 1), ([self.event(seq=True)], 1),
                 ([self.event(base_preferences={'relevance_threshold': .8, 'instructions': ''})], 1),
                 ([self.event(screening_preferences={'relevance_threshold': .8, 'instructions': '', 'sync_token': 'x'})], 1)]
        for events, cursor in cases:
            with self.subTest(events=events, cursor=cursor), self.assertRaises(ValueError):
                apply_remote_profiles(self.db, events, cursor, self.config)
        self.assertEqual(self.active()['content'], 'Original interests')

    def test_tampered_replay_rejected(self):
        apply_remote_profiles(self.db, [self.event()], 1, self.config)
        with self.assertRaisesRegex(ValueError, 'replay differs'):
            apply_remote_profiles(self.db, [self.event(summary='Changed audit')], 1, self.config)

    def test_preference_validation_refuses_nan_bool_and_unknown_knobs(self):
        for threshold in (True, float('nan'), float('inf'), -.1, 1.1, '0.7'):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                validate_preferences({'relevance_threshold': threshold, 'instructions': ''})

    def test_pull_imports_and_repairs_without_publication(self):
        calls = []
        def request(endpoint, path, credentials, data=None):
            calls.append(path)
            if path.startswith('/api/sync/feedback'):
                return {'events': [], 'cursor': 0, 'request_id': 2, 'profile_events_version': 1}
            if path.startswith('/api/sync/profile'):
                return {'events': [self.event()], 'cursor': 1}
            raise AssertionError(path)
        with patch('academic_radar.cloud_sync.request_json', side_effect=request):
            result = _pull_remote(self.path, self.config, 'https://test.chatgpt.site', {})
        self.assertEqual(result['profiles_imported'], 1)
        self.assertEqual(self.profile.read_text(), 'New research interests')
        self.assertEqual(calls, ['/api/sync/feedback?after=0', '/api/sync/profile?after=0'])
        profile = next(json.loads(row['data']) for row in snapshot_records(self.path, self.config) if row['kind']=='private')
        self.assertEqual(profile['profile_cursor'], 1)
        self.assertEqual(profile['screening_preferences']['relevance_threshold'], .85)

    def test_stage_has_no_ack_activation_or_synced_checkpoint(self):
        calls = []
        records = [{'kind':'paper','key':'p','data':'{}','checksum':'a'*64}]
        def request(endpoint, path, credentials, data=None):
            calls.append((path, data))
            if path.startswith('/api/sync/feedback'):
                return {'events': [], 'cursor': 0, 'request_id': 1}
            if path == '/api/sync/begin':
                return {'active': False}
            if path == '/api/sync/chunk':
                return {'accepted': 1}
            if path == '/api/sync/finish':
                self.assertIs(data['activate'], False)
                return {'generation': data['generation'], 'count': 1, 'staged': True}
            raise AssertionError(path)
        with patch('academic_radar.cloud_sync.request_json', side_effect=request), patch('academic_radar.cloud_sync.snapshot_records', return_value=records):
            result = _synchronize(self.path, self.config, 'https://test.chatgpt.site', {}, activate=False)
        self.assertEqual(result['status'], 'staged')
        self.assertEqual(result['request_id'], 1)
        self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='cloud_last_synced_generation'").fetchone())
        self.assertFalse(any(path=='/api/sync/ack' for path, _ in calls))

    def test_authoritative_feedback_overrides_clock_skew_and_replay(self):
        with self.db:
            self.db.execute("INSERT INTO papers(identity,title,first_seen,updated_at) VALUES('paper','Paper','now','now')")
        event = {'seq':1,'identity':'paper','interest':None,'reason':None,'favorite':1,'reading_status':'read','updated_at':'2099-01-01T00:00:00Z'}
        apply_remote_feedback(self.db, [event], 1)
        neutral = {**event, 'seq':2, 'favorite':0, 'reading_status':'unread', 'updated_at':'2026-01-01T00:00:00Z'}
        self.assertEqual(apply_remote_feedback(self.db, [neutral], 2, authoritative=True), 1)
        self.assertEqual(apply_remote_feedback(self.db, [event, neutral], 2, authoritative=True), 0)
        self.assertEqual(self.db.execute('SELECT favorite FROM paper_feedback').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, 'gap'):
            apply_remote_feedback(self.db, [{**neutral,'seq':4}], 4, authoritative=True)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone()[0], '2')

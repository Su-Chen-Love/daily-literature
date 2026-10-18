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
from academic_radar.cloud_sync import (_pull_remote, _synchronize, canonical,
                                      pull_configured, snapshot_records)
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
        self.assertEqual(effective['relevance_threshold'], .7)
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

    def test_profile_pages_are_drained_in_order(self):
        first = self.event()
        second = self.event(seq=2, content='Later interests', base=first['content'],
                            base_preferences=first['screening_preferences'])
        paths = []
        def request(endpoint, path, credentials, data=None):
            paths.append(path)
            self.assertIsNone(data)
            if path.startswith('/api/sync/feedback'):
                return {'events': [], 'cursor': 0, 'profile_events_version': 1}
            if path.endswith('after=0'):
                return {'events': [first], 'cursor': 1, 'more': True}
            if path.endswith('after=1'):
                return {'events': [second], 'cursor': 2, 'more': False}
            raise AssertionError(path)
        with patch('academic_radar.cloud_sync.request_json', side_effect=request):
            result = _pull_remote(self.path, self.config, 'https://test.chatgpt.site', {})
        self.assertEqual(result['profiles_imported'], 2)
        self.assertEqual(result['profile_cursor'], 2)
        self.assertEqual(self.profile.read_text(), 'Later interests')
        self.assertEqual(paths, ['/api/sync/feedback?after=0', '/api/sync/profile?after=0',
                                 '/api/sync/profile?after=1'])

    def test_invalid_pagination_and_protocol_stop_without_publication(self):
        cases = [
            {'events': [], 'cursor': 0, 'more': True},
            {'events': [], 'cursor': 1, 'more': False},
            {'events': [], 'cursor': 0, 'more': 'false'},
            {'events': [], 'cursor': True},
        ]
        for page in cases:
            paths = []
            def request(endpoint, path, credentials, data=None):
                paths.append(path)
                if path.startswith('/api/sync/feedback'):
                    return {'events': [], 'cursor': 0, 'profile_events_version': 1}
                if path.startswith('/api/sync/profile'):
                    return page
                raise AssertionError('Must not publish or ACK')
            with self.subTest(page=page), patch('academic_radar.cloud_sync.request_json', side_effect=request):
                with self.assertRaises(ValueError):
                    _synchronize(self.path, self.config, 'https://test.chatgpt.site', {})
            self.assertEqual(len(paths), 2)
            self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='cloud_profile_cursor'").fetchone())
        for version in (True, 1.0, '1', 2):
            with self.subTest(version=version), patch('academic_radar.cloud_sync.request_json',
                    return_value={'events': [], 'cursor': 0, 'profile_events_version': version}) as request:
                with self.assertRaisesRegex(ValueError, 'Unsupported'):
                    _pull_remote(self.path, self.config, 'https://test.chatgpt.site', {})
                self.assertEqual(request.call_count, 1)

    def test_legacy_server_does_not_require_profile_endpoint(self):
        with patch('academic_radar.cloud_sync.request_json', return_value={'events': [], 'cursor': 0}) as request:
            result = _pull_remote(self.path, self.config, 'https://test.chatgpt.site', {})
        self.assertEqual(result['profiles_imported'], 0)
        self.assertIsNone(result['profile_events_version'])
        self.assertEqual(request.call_args.args[1], '/api/sync/feedback?after=0')
        self.assertEqual(request.call_count, 1)

    def test_file_failure_blocks_snapshot_and_is_retried_on_next_pull(self):
        paths = []
        def request(endpoint, path, credentials, data=None):
            paths.append(path)
            if path.startswith('/api/sync/feedback'):
                return {'events': [], 'cursor': 0, 'profile_events_version': 1}
            if path == '/api/sync/profile?after=0':
                return {'events': [self.event()], 'cursor': 1}
            if path == '/api/sync/profile?after=1':
                return {'events': [], 'cursor': 1}
            raise AssertionError('Must not publish or ACK')
        with patch('academic_radar.cloud_sync.request_json', side_effect=request):
            with patch('academic_radar.profile_sync._atomic_write', side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError):
                    _synchronize(self.path, self.config, 'https://test.chatgpt.site', {})
            self.assertEqual(self.profile.read_text(), 'Original interests')
            result = _pull_remote(self.path, self.config, 'https://test.chatgpt.site', {})
        self.assertEqual(result['profiles_imported'], 0)
        self.assertEqual(result['profile_cursor'], 1)
        self.assertEqual(self.profile.read_text(), self.event()['content'])
        self.assertTrue(all(path.startswith('/api/sync/feedback') or path.startswith('/api/sync/profile')
                            for path in paths))

    def test_ack_uses_cursor_embedded_in_snapshot(self):
        calls = []
        # Deliberately simulate an older, still valid snapshot. Never read the
        # current database watermark again when acknowledging this generation.
        data = canonical({'content': 'Original interests', 'profile_cursor': 0})
        records = [{'kind': 'private', 'key': 'profile', 'data': data, 'checksum': digest(data)}]
        def request(endpoint, path, credentials, data=None):
            calls.append((path, data))
            if path.startswith('/api/sync/feedback'):
                return {'events': [], 'cursor': 0, 'request_id': 2, 'profile_events_version': 1}
            if path.startswith('/api/sync/profile'):
                return {'events': [self.event()], 'cursor': 1}
            if path == '/api/sync/begin':
                return {'active': False}
            if path == '/api/sync/chunk':
                return {'accepted': len(data['records'])}
            if path == '/api/sync/finish':
                return {'generation': data['generation'], 'count': len(records)}
            if path == '/api/sync/ack':
                self.assertEqual(data['profile_cursor'], 0)
                return {'ok': True, 'acknowledged_profile_cursor': 0}
            raise AssertionError(path)
        with patch('academic_radar.cloud_sync.request_json', side_effect=request), \
                patch('academic_radar.cloud_sync.snapshot_records', return_value=records):
            result = _synchronize(self.path, self.config, 'https://test.chatgpt.site', {})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['profile_cursor'], 0)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='cloud_profile_cursor'").fetchone()[0], '1')
        self.assertEqual(sum(path == '/api/sync/ack' for path, _ in calls), 1)

    def test_unconfirmed_profile_ack_is_not_checkpointed(self):
        def request(endpoint, path, credentials, data=None):
            if path.startswith('/api/sync/feedback'):
                return {'events': [], 'cursor': 0, 'request_id': 0, 'acknowledged_request_id': 0,
                        'acknowledged_cursor': 0, 'acknowledged_profile_cursor': 0, 'profile_events_version': 1}
            if path.startswith('/api/sync/profile'):
                return {'events': [self.event()], 'cursor': 1}
            if path == '/api/sync/begin':
                return {'active': True}
            if path == '/api/sync/ack':
                self.assertEqual(data['profile_cursor'], 1)
                return {'ok': True, 'acknowledged_profile_cursor': 0}
            raise AssertionError(path)
        with patch('academic_radar.cloud_sync.request_json', side_effect=request):
            with self.assertRaisesRegex(ValueError, 'profile acknowledgement'):
                _synchronize(self.path, self.config, 'https://test.chatgpt.site', {})
        self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='cloud_acknowledged_request_id'").fetchone())

    def test_configured_pull_shares_sync_lock_and_never_publishes(self):
        import fcntl
        credentials = self.root / 'synthetic-credentials.json'
        credentials.write_text('{}')
        config = {**self.config, 'cloud_sync': {'enabled': True,
                  'endpoint': 'https://test.chatgpt.site', 'credentials_file': str(credentials)}}
        with patch('academic_radar.cloud_sync.load_config', return_value=config), \
                patch('academic_radar.cloud_sync.resolve_state', return_value=self.root):
            with (self.root / '.cloud-sync.lock').open('a') as lock, \
                    patch('academic_radar.cloud_sync.request_json') as request:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(pull_configured(self.root / 'config.toml'), {'status': 'busy'})
                request.assert_not_called()
            def request(endpoint, path, credentials, data=None):
                self.assertIsNone(data)
                if path.startswith('/api/sync/feedback'):
                    return {'events': [], 'cursor': 0, 'profile_events_version': 1}
                if path.startswith('/api/sync/profile'):
                    return {'events': [self.event()], 'cursor': 1}
                raise AssertionError('Must not publish or ACK')
            with patch('academic_radar.cloud_sync.request_json', side_effect=request):
                result = pull_configured(self.root / 'config.toml')
        self.assertEqual(result['status'], 'pulled')
        self.assertEqual(result['profiles_imported'], 1)
        self.assertFalse((self.root / 'cloud-sync-status.json').exists())
        with patch('academic_radar.cloud_sync.load_config', return_value={}):
            self.assertEqual(pull_configured(self.root / 'config.toml'), {'status': 'disabled'})


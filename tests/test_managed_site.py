import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar.managed_site import ManagedSiteClient
from academic_radar.product import initialize_installation
from academic_radar.storage import connect
from academic_radar import cloud_runtime


class ManagedSiteBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        initialize_installation(self.state)
        self.config = self.state / 'config.toml'
        self.config.write_text('state_dir="."\nprofile_file="research-profile.md"\nsources=[{name="Synthetic",type="crossref"}]\n'
                               '[cloud_runtime]\nrole="primary"\n[cloud_sync]\n'
                               'enabled=false\ntransport="managed"\n')
        self.db = self.state / 'papers.sqlite3'
        with connect(self.db) as db:
            db.execute("INSERT INTO papers(identity,title,first_seen,updated_at) VALUES('doi:fixture','Paper','now','now')")
        self.calls = []; self.records = []; self.activated = False
        self.client = ManagedSiteClient(self.host)

    def host(self, name, args):
        self.calls.append((name, args))
        if name == 'read_feedback':
            events = [] if args['after'] else [{
                'seq': 1, 'identity': 'doi:fixture', 'interest': 'interested', 'reason': 'Useful research',
                'favorite': 1, 'reading_status': 'read_later', 'updated_at': '2026-10-15T00:00:00Z',
            }]
            return {'events': events, 'cursor': 1, 'more': False,
                    'profile_events_version': 1, 'request_id': 2}
        if name == 'read_profile_events':
            return {'events': [], 'cursor': 0, 'more': False}
        if name == 'stage_snapshot':
            action, data = args['action'], args['data']
            if action == 'begin':
                self.expected_count = data['count']; self.records = []
                return {'generation': data['generation'], 'active': False}
            if action == 'chunk':
                self.records.extend(data['records']); return {'accepted': len(data['records'])}
            self.assertIs(data['activate'], False)
            self.assertEqual(len(self.records), self.expected_count)
            fingerprint = '\n'.join(f"{r['kind']}:{r['key']}:{r['checksum']}" for r in self.records)
            self.assertEqual(hashlib.sha256(fingerprint.encode()).hexdigest(), data['generation'])
            return {'generation': data['generation'], 'count': len(self.records), 'staged': True}
        if name == 'acknowledge_snapshot':
            if not self.activated:
                return {'error': 'Checkpoint is not activated'}
            return {'ok': True}
        if name == 'read_checkpoint_head':
            return {'generation': 0, 'publication': None}
        raise AssertionError(name)

    def test_host_tool_pull_stage_ack_without_http_or_credentials(self):
        with patch('academic_radar.cloud_sync.request_json', side_effect=AssertionError('HTTP used')):
            pulled = self.client.pull_configured(self.config)
            self.assertEqual(pulled['cursor'], 1)
            staged = self.client.stage_configured(self.config)
            self.assertEqual(staged['status'], 'staged')
            self.assertFalse(any(name == 'acknowledge_snapshot' for name, _ in self.calls))
            with self.assertRaises(RuntimeError):
                self.client.acknowledge_configured(self.config, staged['generation'], 1, 2)
            self.activated = True
            self.client.acknowledge_configured(self.config, staged['generation'], 1, 2)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT favorite FROM paper_feedback').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='cloud_acknowledged_cursor'").fetchone()[0], '1')
        self.assertNotIn('credentials', json.dumps(self.calls))

    def test_bridge_never_activates_staging_or_forwards_unknown_routes(self):
        for path, data in (('/api/sync/finish', {'generation': 'a' * 64}),
                           ('https://attacker.example/api/sync/begin', {}),
                           ('/api/arbitrary', {}), ('/api/sync/feedback?after=-1', None)):
            with self.assertRaises(ValueError):
                self.client.request('', path, {}, data)
        self.assertEqual(self.calls, [])

    def test_native_error_fails_closed_and_structured_result_is_supported(self):
        bad = ManagedSiteClient(lambda *_: {'isError': True, 'content': []})
        with self.assertRaises(RuntimeError): bad.read_checkpoint_head()
        good = ManagedSiteClient(lambda *_: {'structuredContent': {'generation': 0, 'publication': None}})
        self.assertEqual(good.read_checkpoint_head()['generation'], 0)

    def test_library_activation_callback_returns_exact_head_not_mcp_envelope(self):
        head = {'generation': 3, 'publication': {'publication_id': 'a' * 64}}
        client = ManagedSiteClient(lambda *_: {'ok': True, 'head': head})
        self.assertEqual(client.activate_library_checkpoint({}, {}), head)
        rejected = ManagedSiteClient(lambda *_: {'head': head})
        with self.assertRaises(RuntimeError):
            rejected.activate_library_checkpoint({}, {})

    def test_large_manifest_falls_back_to_bounded_full_snapshot(self):
        seen = []
        client = ManagedSiteClient(lambda name, args: seen.append((name, args)) or {})
        data = {'generation': 'a' * 64, 'count': 1000,
                'manifest': [{'kind': 'paper', 'key': 'x' * 800, 'checksum': 'b' * 64}] * 1000}
        client.request('', '/api/sync/begin', {}, data)
        self.assertNotIn('manifest', seen[0][1]['data'])
        self.assertIn('manifest', data)

    def test_primary_prepare_requires_bridge_and_applies_events_before_queue(self):
        head = {'generation': 0, 'publication': None}
        with self.assertRaisesRegex(ValueError, 'approved host tool bridge'):
            cloud_runtime.prepare(self.config, 'managed-run', no_collect=True, expected_head=head)
        with patch('academic_radar.cloud_sync.request_json', side_effect=AssertionError('HTTP used')):
            prepared = cloud_runtime.prepare(self.config, 'managed-run', no_collect=True,
                                             expected_head=head, sync_client=self.client)
        self.assertEqual(prepared['phase'], 'awaiting_judgment')
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone()[0], '1')

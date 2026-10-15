import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar import cloud_runtime as runtime
from academic_radar.storage import connect


class CloudRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.toml'
        self.config.write_text('state_dir = "."\nprofile_file = "profile.md"\nrelevance_threshold = 0.7\n'
                               '[cloud_runtime]\nrole = "staging"\n[[sources]]\nname = "Synthetic"\ntype = "crossref"\n')
        (self.root / 'profile.md').write_text('Human-in-the-loop interactive optimization and routing decisions')
        pm = runtime._monitor()
        db = pm.db_open(self.root / 'papers.sqlite3')
        for i in range(2):
            pm.upsert(db, pm.Paper('doi:10.1/' + str(i), '10.1/' + str(i), 'Interactive optimization ' + str(i),
                      'Abstract', 'Journal', '2026-10-15', '', [], 'Synthetic', publication_type_raw='journal-article',
                      publication_type_source='crossref'), '2026-10-15')
        db.commit()
        db.close()

    def prepare(self, key='daily-2026-10-15'):
        return runtime.prepare(self.config, key, no_collect=True)

    def results(self, prepared, *, malformed=False):
        # Same evidence-grounded structured fixture as the existing importer tests.
        from test_paper_monitor import MonitorTests
        queue = json.loads(Path(prepared['queue_path']).read_text())
        data = {'run_id': queue['run_id'], 'profile_hash': queue['profile_hash'], 'model': 'synthetic-test-model',
                'source_failures': queue['source_failures'],
                'results': [MonitorTests.structured_result(p['identity']) for p in queue['papers']]}
        if malformed:
            data['results'] = data['results'][:1]
        target = self.root / ('bad-results.json' if malformed else 'results.json')
        target.write_text(json.dumps(data))
        return target

    def test_managed_library_stage_binds_runtime_before_publication(self):
        from academic_radar.managed_site import ManagedSiteClient
        from academic_radar.library_checkpoint import LibraryCheckpointAdapter, prepare_publication
        from test_library_checkpoint import Host
        host = Host()
        staged_records = []
        def call_tool(name, args):
            if name == 'read_checkpoint_head': return host.head
            if name == 'read_feedback':
                return {'events': [], 'cursor': 0, 'more': False, 'profile_events_version': 1, 'request_id': 0}
            if name == 'read_profile_events': return {'events': [], 'cursor': 0, 'more': False}
            if name == 'stage_snapshot':
                data = args['data']
                if args['action'] == 'begin': return {'active': False}
                if args['action'] == 'chunk':
                    staged_records.extend(data['records']); return {'accepted': len(data['records'])}
                return {'generation': data['generation'], 'count': len(staged_records), 'staged': True}
            if name == 'commit_library_checkpoint':
                data = args['data']
                return {'ok': True, 'head': host.activate(data['expected_head'], data['publication'])}
            raise AssertionError(name)
        site = ManagedSiteClient(call_tool)
        first = runtime.prepare(self.config, 'native-run', no_collect=True,
                                expected_head=host.head, sync_client=site)
        runtime.import_results(self.config, first['operation_id'], self.results(first))
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td)
            checkpoint = artifacts / 'checkpoint'
            staged = runtime.stage_library_checkpoint(self.config, 'native-run', checkpoint, sync_client=site)
            self.assertEqual(staged['status'], 'staged')
            self.assertIsNone(host.head['publication'])
            again = runtime.stage_library_checkpoint(self.config, 'native-run', checkpoint, sync_client=site)
            self.assertEqual(staged['checkpoint_id'], again['checkpoint_id'])
            archive, receipt = artifacts / 'native.tar.gz', artifacts / 'receipt.json'
            prepare_publication(checkpoint, archive, receipt, operation_id='native-run',
                                snapshot_generation=staged['snapshot_generation'], expected_site_head=host.head)
            adapter = LibraryCheckpointAdapter(create_archive=host.create, materialize_archive=host.materialize,
                read_site_head=site.read_checkpoint_head, activate_site=site.activate_library_checkpoint)
            published = adapter.publish(receipt, archive)
            self.assertEqual(published['phase'], 'published')
            self.assertEqual(host.head['publication']['checkpoint_id'], staged['checkpoint_id'])
            with self.assertRaisesRegex(RuntimeError, 'head changed'):
                runtime.stage_library_checkpoint(self.config, 'native-run', checkpoint, sync_client=site)

    def test_retry_reuses_frozen_queue_and_blocks_new_operation(self):
        first = self.prepare()
        again = self.prepare()
        self.assertEqual(first['run_id'], again['run_id'])
        self.assertEqual(first['queue_sha256'], again['queue_sha256'])
        self.assertTrue(again['resumed'])
        with self.assertRaises(runtime.Busy):
            self.prepare('another-operation')

    def test_invalid_result_can_be_fixed_then_retried_idempotently(self):
        first = self.prepare()
        with self.assertRaisesRegex(ValueError, 'complete queue'):
            runtime.import_results(self.config, first['operation_id'], self.results(first, malformed=True))
        good = self.results(first)
        result = runtime.import_results(self.config, first['operation_id'], good)
        self.assertEqual(result['imported'], 2)
        again = runtime.import_results(self.config, first['operation_id'], good)
        self.assertTrue(again['resumed'])
        with contextlib.closing(connect(self.root / 'papers.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM screenings').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM agent_jobs WHERE status='imported'").fetchone()[0], 1)
        changed = json.loads(good.read_text()); changed['model'] = 'different'
        good.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'different results'):
            runtime.import_results(self.config, first['operation_id'], good)

    def test_queue_tamper_is_rejected(self):
        first = self.prepare()
        p = Path(first['queue_path']); data = json.loads(p.read_text()); data['threshold'] = 0.1
        p.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.prepare()

    def test_frozen_threshold_survives_config_edit(self):
        first = self.prepare()
        self.config.write_text(self.config.read_text().replace('0.7', '0.999'))
        result = runtime.import_results(self.config, first['operation_id'], self.results(first))
        self.assertEqual(result['relevant'], 2)

    def test_no_site_sync_during_import(self):
        first = self.prepare()
        with patch('academic_radar.cloud_sync.sync_configured') as sync:
            runtime.import_results(self.config, first['operation_id'], self.results(first))
        sync.assert_not_called()

    def test_no_collection_or_model_for_backup_role(self):
        self.config.write_text(self.config.read_text().replace('staging', 'backup'))
        with self.assertRaisesRegex(ValueError, 'backups cannot run analysis'):
            self.prepare()

    def test_bad_operation_key_fails(self):
        with self.assertRaises(ValueError):
            self.prepare('../../escape')

    def test_export_transaction_recovers_without_runner_status(self):
        first = self.prepare()
        with contextlib.closing(connect(self.root / 'papers.sqlite3')) as db, db:
            db.execute('DELETE FROM meta WHERE key=?', ('cloud_runtime_operation:' + first['operation_id'],))
        again = self.prepare()
        self.assertEqual(again['run_id'], first['run_id'])
        self.assertTrue(again['resumed'])

    def test_import_commit_recovers_if_journal_save_is_interrupted(self):
        first = self.prepare(); results = self.results(first)
        original = runtime._save
        def fail_after_import(db, op, values):
            if values.get('phase') == 'imported':
                raise OSError('simulated interruption')
            return original(db, op, values)
        with patch.object(runtime, '_save', side_effect=fail_after_import):
            with self.assertRaises(OSError):
                runtime.import_results(self.config, first['operation_id'], results)
        recovered = runtime.import_results(self.config, first['operation_id'], results)
        self.assertEqual(recovered['phase'], 'imported')
        self.assertTrue(recovered['resumed'])


    def test_primary_requires_restore_head_before_analysis(self):
        self.config.write_text(self.config.read_text().replace('staging', 'primary'))
        with self.assertRaisesRegex(ValueError, 'head captured'):
            self.prepare()

    def test_commit_cas_then_ack_retry_does_not_republish(self):
        from academic_radar.checkpoint_transport import CheckpointConflict
        head = {'generation': 0, 'manifest_sha256': None, 'snapshot_generation': None}
        first = runtime.prepare(self.config, 'daily-2026-10-15', no_collect=True, expected_head=head)
        runtime.import_results(self.config, first['operation_id'], self.results(first))
        class Client:
            def __init__(self):
                self.head = dict(head); self.uploads = 0; self.commits = 0
            def get_head(self): return dict(self.head)
            def upload_checkpoint(self, directory):
                from academic_radar.checkpoints import verify_checkpoint
                verify_checkpoint(directory)
                self.uploads += 1
                return {'manifest_sha256': 'a' * 64}
            def compare_and_swap_head(self, expected, manifest, snapshot, operation_id=None):
                if self.head['generation'] != expected['generation']:
                    raise CheckpointConflict('stale')
                self.commits += 1
                self.head = {'generation': 1, 'manifest_sha256': manifest,
                             'snapshot_generation': snapshot, 'operation_id': operation_id}
                return self.head
        client = Client()
        output = self.root.parent / (self.root.name + '-checkpoint')
        self.addCleanup(lambda: __import__('shutil').rmtree(output, ignore_errors=True))
        staged = {'status': 'staged', 'generation': 'b' * 64, 'cursor': 0, 'profile_cursor': 0, 'request_id': 0}
        with patch('academic_radar.cloud_sync.stage_configured', return_value=staged) as stage, \
             patch('academic_radar.cloud_sync.acknowledge_configured', side_effect=[OSError('interrupted ACK'), {'ok': True}]):
            with self.assertRaises(OSError):
                runtime.commit(self.config, first['operation_id'], output, client=client)
            result = runtime.commit(self.config, first['operation_id'], output, client=client)
        self.assertEqual((client.uploads, client.commits), (1, 1))
        self.assertEqual(result['phase'], 'published')
        self.assertTrue(result['resumed'])
        stage.assert_called_once()

    def test_commit_stale_restore_fails_before_staging(self):
        from academic_radar.checkpoint_transport import CheckpointConflict
        from unittest.mock import Mock
        head = {'generation': 0, 'manifest_sha256': None}
        first = runtime.prepare(self.config, 'daily-2026-10-15', no_collect=True, expected_head=head)
        runtime.import_results(self.config, first['operation_id'], self.results(first))
        client = Mock(); client.get_head.return_value = {'generation': 1, 'manifest_sha256': 'a' * 64}
        with patch('academic_radar.cloud_sync.stage_configured') as stage:
            with self.assertRaises(CheckpointConflict):
                runtime.commit(self.config, first['operation_id'], self.root / 'unused', client=client)
        stage.assert_not_called(); client.upload_checkpoint.assert_not_called()


if __name__ == '__main__':
    unittest.main()

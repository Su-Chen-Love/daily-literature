"""Independent crash-window regressions for cloud checkpoint publication."""
import shutil
import unittest
from unittest.mock import patch

from academic_radar import cloud_runtime as runtime
from academic_radar.checkpoints import restore_checkpoint, verify_checkpoint
import test_cloud_runtime
from test_checkpoint_transport import MemoryClient


class CloudRuntimeReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_cloud_runtime.CloudRuntimeTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = MemoryClient()
        self.prepared = runtime.prepare(self.fixture.config, 'review-operation', no_collect=True,
                                        expected_head=self.client.get_head())
        runtime.import_results(self.fixture.config, 'review-operation', self.fixture.results(self.prepared))
        self.output = self.fixture.root.parent / (self.fixture.root.name + '-review-checkpoint')
        self.restored = self.fixture.root.parent / (self.fixture.root.name + '-restored')
        self.addCleanup(lambda: shutil.rmtree(self.output, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(self.restored, ignore_errors=True))
        self.stage = {'status': 'staged', 'generation': 'b' * 64, 'cursor': 0,
                      'profile_cursor': 0, 'request_id': 0}

    def test_retry_after_checkpoint_publish_before_journal_save(self):
        original_save = runtime._save
        def interrupt(db_path, operation_id, values):
            if values.get('phase') == 'committing' and values.get('checkpoint_id'):
                raise OSError('Process died after checkpoint publication')
            return original_save(db_path, operation_id, values)
        with patch('academic_radar.cloud_sync.stage_configured', return_value=self.stage), \
             patch('academic_radar.cloud_sync.acknowledge_configured', return_value={'ok': True}):
            with patch.object(runtime, '_save', side_effect=interrupt):
                with self.assertRaises(OSError):
                    runtime.commit(self.fixture.config, 'review-operation', self.output, client=self.client)
            verify_checkpoint(self.output)
            self.assertEqual(self.client.get_head()['generation'], 0)
            recovered = runtime.commit(self.fixture.config, 'review-operation', self.output, client=self.client)
        self.assertEqual(recovered['phase'], 'published')
        self.assertEqual(self.client.get_head()['generation'], 1)

    def test_workspace_loss_after_cas_resumes_ack_from_committed_checkpoint(self):
        with patch('academic_radar.cloud_sync.stage_configured', return_value=self.stage), \
             patch('academic_radar.cloud_sync.acknowledge_configured', side_effect=OSError('Lost ACK')):
            with self.assertRaises(OSError):
                runtime.commit(self.fixture.config, 'review-operation', self.output, client=self.client)
        head = self.client.get_head()
        restore_checkpoint(self.output, self.restored)
        shutil.rmtree(self.fixture.root)
        restored_config = self.restored / 'config.toml'
        # An orchestration retry may call prepare again before discovering it
        # only needs to acknowledge the already durable publication.
        resumed = runtime.prepare(restored_config, 'review-operation', no_collect=True, expected_head=head)
        self.assertTrue(resumed['resumed'])
        with patch('academic_radar.cloud_sync.stage_configured', side_effect=AssertionError('Already committed')), \
             patch('academic_radar.cloud_sync.acknowledge_configured', return_value={'ok': True}):
            recovered = runtime.commit(restored_config, 'review-operation', self.output, client=self.client)
        self.assertTrue(recovered['resumed'])
        self.assertEqual(recovered['phase'], 'published')
        self.assertEqual(self.client.get_head(), head)


if __name__ == '__main__':
    unittest.main()

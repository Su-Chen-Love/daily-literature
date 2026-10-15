import json
import tempfile
import unittest
from pathlib import Path

from academic_radar.cloud_sync import snapshot_records
from academic_radar.cloud_runtime import _monitor
from academic_radar.product import import_fulltext
from academic_radar.storage import connect


class CloudFulltextReferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.db = self.root / 'papers.sqlite3'
        pm = _monitor(); db = pm.db_open(self.db)
        pm.upsert(db, pm.Paper('doi:10.1/pdf', '10.1/pdf', 'A PDF paper', 'Abstract', 'Journal',
                  '2026-10-15', '', [], 'Synthetic'), '2026-10-15')
        db.commit(); db.close()
        self.pdf = import_fulltext(self.db, self.root, 'doi:10.1/pdf', 'input.pdf', b'%PDF-synthetic fixture')

    def test_cloud_snapshot_uses_only_portable_pdf_reference(self):
        records = snapshot_records(self.db, {'cloud_runtime': {'role': 'primary'}})
        record = next(row for row in records if row['key'] == 'fulltext:' + str(self.pdf['id']))
        data = json.loads(record['data'])
        self.assertTrue(data['path'].startswith('payload/fulltexts/'))
        self.assertNotIn(str(self.root), record['data'])
        self.assertEqual(data['sha256'], self.pdf['sha256'])
        self.assertEqual(data['size_bytes'], self.pdf['size_bytes'])

    def test_external_pdf_reference_refuses_cloud_publication(self):
        db = connect(self.db)
        with db: db.execute("UPDATE fulltext_files SET stored_path='/outside/secret.pdf'")
        db.close()
        with self.assertRaisesRegex(ValueError, 'relocated'):
            snapshot_records(self.db, {'cloud_runtime': {'role': 'primary'}})

    def test_legacy_sync_does_not_claim_checkpoint_pdf_access(self):
        self.assertFalse(any(row['key'].startswith('fulltext:') for row in snapshot_records(self.db, {})))

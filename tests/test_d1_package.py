"""Synthetic-only lossless normalization, publication and adversarial fixtures."""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from academic_radar import d1_package as package
from academic_radar import sqlite_delta
from academic_radar.storage import upgrade_database


class D1PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'source'
        self.state.mkdir()
        self.db = self.state / 'papers.sqlite3'
        self.output = self.root / 'package'
        self.restored = self.root / 'restored'

    def fixture(self):
        with sqlite3.connect(self.db) as db:
            db.executescript('''
                PRAGMA user_version=42;
                PRAGMA application_id=73;
                CREATE TABLE parent(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT UNIQUE);
                CREATE TABLE child(k TEXT PRIMARY KEY, parent INTEGER REFERENCES parent(id), v, feedback_snapshot_json TEXT);
                CREATE TABLE composite(a TEXT,b INTEGER,v BLOB,PRIMARY KEY(a,b)) WITHOUT ROWID;
                CREATE TABLE empty_table(k TEXT PRIMARY KEY);
                INSERT INTO parent VALUES(1,'first'),(50,'highwater');
                DELETE FROM parent WHERE id=50;
                CREATE INDEX child_parent ON child(parent);
            ''')
            repeated = '{ "items": ["synthetic\\n' + '中文😀' * 1000 + '"], "n": 1.00 }'
            values = [None, b'\x00\xff', 'a\0中文😀\x7f', 2**63-1, -(2**63), 1.2345678901234567, float('inf'), -float('inf'), -0.0, struct.unpack('>d', bytes.fromhex('8000000000000001'))[0]]
            for index, value in enumerate(values):
                db.execute('INSERT INTO child(rowid,k,parent,v,feedback_snapshot_json) VALUES(?,?,?,?,?)', (index+20, str(index), 1, value, repeated))
            db.execute('INSERT INTO composite VALUES(?,?,?)', ('x', 1, b'blob'*1000))
        (self.state / 'history' / 'empty').mkdir(parents=True)
        (self.state / 'history' / 'literal.json').write_bytes(b'{ "number" : 1.00, "value": "original spacing" }\n')
        (self.state / 'same-blob.bin').write_bytes(b'blob'*1000)
        os.chmod(self.state / 'history' / 'literal.json', 0o640)

    def exported(self):
        self.fixture()
        return package.export_package(self.db, self.output, files_root=self.state)

    def manifest(self):
        return json.loads((self.output / 'manifest.json').read_bytes())

    def save_manifest(self, manifest):
        (self.output / 'manifest.json').write_bytes(package.canonical(manifest) + b'\n')

    def reseal_table(self, manifest, table, records):
        records.sort(key=lambda row: row['key_sha256'])
        chain = hashlib.sha256(b'').hexdigest()
        for row in records:
            row['record_sha256'] = package.digest({k:v for k,v in row.items() if k != 'record_sha256'})
            chain = hashlib.sha256((chain+'\n'+row['key_sha256']+'\n'+row['record_sha256']).encode()).hexdigest()
        raw = b''.join(package.canonical(row)+b'\n' for row in records)
        (self.output / table['path']).write_bytes(raw)
        table.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), table_chain_sha256=chain)
        self.save_manifest(manifest)

    def rejection(self, pattern='.'):
        originals = {str(path.relative_to(self.state)): path.read_bytes() for path in self.state.rglob('*') if path.is_file()}
        with self.assertRaisesRegex((package.PackageError, sqlite_delta.DeltaError), pattern):
            package.restore_package(self.output, self.restored, trusted_schema_database=self.db)
        self.assertFalse(self.restored.exists())
        self.assertEqual(originals, {str(path.relative_to(self.state)): path.read_bytes() for path in self.state.rglob('*') if path.is_file()})
        self.assertFalse(list(self.root.glob('.d1-restore-*')))

    def test_full_roundtrip_exact_types_rowids_schema_files_and_no_mutation(self):
        result = self.exported()
        source_hash = hashlib.sha256(self.db.read_bytes()).hexdigest()
        manifest = result['manifest']
        self.assertEqual(manifest['inventory'], sqlite_delta.inspect_snapshot(self.db))
        self.assertEqual(manifest['hash_algorithm'], sqlite_delta.HASH_ALGORITHM)
        restored = package.restore_package(self.output, self.restored, trusted_schema_database=self.db, expected_manifest_sha256=result['manifest_sha256'])
        self.assertEqual(restored['logical_sha256'], manifest['inventory']['logical_sha256'])
        self.assertEqual(sqlite_delta.inspect_snapshot(self.db), sqlite_delta.inspect_snapshot(self.restored / 'papers.sqlite3'))
        self.assertEqual(source_hash, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertTrue((self.restored / 'history' / 'empty').is_dir())
        for path in self.state.rglob('*'):
            if path.is_file() and path != self.db:
                target = self.restored / path.relative_to(self.state)
                self.assertEqual(path.read_bytes(), target.read_bytes())
                self.assertEqual(path.stat().st_mode & 0o777, target.stat().st_mode & 0o777)
        with sqlite3.connect(self.restored / 'papers.sqlite3') as db:
            self.assertEqual(db.execute('SELECT seq FROM sqlite_sequence').fetchall(), [(50,)])
            self.assertEqual(db.execute('SELECT rowid FROM child ORDER BY rowid').fetchall(), [(x,) for x in range(20,30)])
        self.assertLess(manifest['statistics']['unique_object_bytes'], manifest['statistics']['referenced_object_bytes'] // 5)
        self.assertFalse(list(self.state.glob('*-wal')))
        self.assertFalse(list(self.state.glob('*-journal')))

    def test_application_all_tables_preserved(self):
        upgrade_database(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute('PRAGMA journal_mode=DELETE')
        result = package.export_package(self.db, self.output)
        self.assertGreaterEqual(len(result['manifest']['tables']), 22)
        package.restore_package(self.output, self.restored, trusted_schema_database=self.db)
        self.assertEqual(sqlite_delta.inspect_snapshot(self.db), sqlite_delta.inspect_snapshot(self.restored / 'papers.sqlite3'))

    def test_deterministic_package_without_timestamps_or_absolute_paths(self):
        first = self.exported()
        other = self.root / 'other-package'
        second = package.export_package(self.db, other, files_root=self.state)
        self.assertEqual(first, second)
        self.assertEqual((self.output / 'manifest.json').read_bytes(), (other / 'manifest.json').read_bytes())
        self.assertNotIn(str(self.root).encode(), (self.output / 'manifest.json').read_bytes())
        for path in self.output.rglob('*'):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (other / path.relative_to(self.output)).read_bytes())

    def test_shared_exact_raw_content_deduplicates_text_blob_and_files(self):
        raw = ('unique \x00 中文' * 2000).encode()
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE t(id INTEGER PRIMARY KEY,v)')
            db.executemany('INSERT INTO t VALUES(?,?)', [(1,raw),(2,raw.decode()),(3,raw)])
        (self.state / 'same').write_bytes(raw)
        result = package.export_package(self.db,self.output,files_root=self.state)
        self.assertEqual(result['manifest']['statistics']['object_count'], 1)
        self.assertEqual(result['manifest']['statistics']['referenced_object_bytes'], len(raw)*4)
        records = list(package.iter_rows(self.output))
        self.assertEqual({r['values'][1]['t'] for r in records}, {'text','blob'})
        self.assertEqual({r['values'][1]['ref'] for r in records}, {hashlib.sha256(raw).hexdigest()})
        package.restore_package(self.output,self.restored,trusted_schema_database=self.db)
        self.assertEqual(sqlite_delta.inspect_snapshot(self.db),sqlite_delta.inspect_snapshot(self.restored/'papers.sqlite3'))

    def test_repeated_blob_columns_do_not_change_pickle_identity_audit(self):
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE t(id INTEGER PRIMARY KEY,a BLOB,b BLOB,c TEXT,d TEXT)')
            raw=b'blob'*2000
            text='text'*2000
            db.execute('INSERT INTO t VALUES(1,?,?,?,?)',(raw,raw,text,text))
        package.export_package(self.db,self.output)
        package.restore_package(self.output,self.restored,trusted_schema_database=self.db)
        self.assertEqual(sqlite_delta.inspect_snapshot(self.db),sqlite_delta.inspect_snapshot(self.restored/'papers.sqlite3'))

    def test_large_files_chunked_and_native_pages_bounded(self):
        self.fixture()
        raw = b'x' * package.CHUNK_BYTES + b'y' * package.CHUNK_BYTES + b'x' * package.CHUNK_BYTES + b'end'
        (self.state / 'larger.bin').write_bytes(raw)
        package.export_package(self.db,self.output,files_root=self.state)
        manifest = self.manifest()
        obj = next(obj for obj in manifest['objects'] if obj['sha256']==hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(obj['chunks']),4)
        self.assertEqual(obj['chunks'][0],obj['chunks'][2])
        pages = list(package.iter_native_pages(self.output,max_page_bytes=256*1024,max_records=3))
        self.assertTrue(all(len(package.canonical(page))<=256*1024 and len(page['records'])<=3 for page in pages))
        chunks = {r['sha256']:base64.b64decode(r['base64']) for p in pages if p['kind']=='chunks' for r in p['records']}
        descriptor = next(p['records'][0] for p in pages if p['kind']=='manifest')
        reconstructed = b''.join(chunks[c['sha256']] for c in descriptor['chunks'])
        self.assertEqual(reconstructed,(self.output/'manifest.json').read_bytes())
        self.assertEqual(hashlib.sha256(reconstructed).hexdigest(),descriptor['sha256'])
        package.restore_package(self.output,self.restored,trusted_schema_database=self.db)
        self.assertEqual(raw,(self.restored/'larger.bin').read_bytes())

    def test_missing_primary_key_and_null_primary_keys_use_rowid_fallback(self):
        with sqlite3.connect(self.db) as db:
            db.executescript('CREATE TABLE no_pk(v); CREATE TABLE nullable(k TEXT PRIMARY KEY,v);')
            db.executemany('INSERT INTO no_pk(rowid,v) VALUES(?,?)',[(19,'same'),(41,'same')])
            db.executemany('INSERT INTO nullable(rowid,k,v) VALUES(?,?,?)',[(10,None,'first'),(20,None,'second'),(30,'x','third')])
        result = package.export_package(self.db,self.output)
        self.assertEqual(result['manifest']['hash_algorithm'],package.EXTENDED_HASH_ALGORITHM)
        self.assertTrue(all(t['key_kind']=='rowid' for t in result['manifest']['tables']))
        package.restore_package(self.output,self.restored,trusted_schema_database=self.db)
        with sqlite3.connect(self.db) as source, sqlite3.connect(self.restored/'papers.sqlite3') as restored:
            for table in ('no_pk','nullable'):
                sql='SELECT rowid,* FROM '+table+' ORDER BY rowid'
                self.assertEqual(source.execute(sql).fetchall(),restored.execute(sql).fetchall())

    def test_fallback_key_single_column_integer_pk_desc(self):
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE t(k INTEGER PRIMARY KEY DESC,v)')
            db.executemany('INSERT INTO t VALUES(NULL,?)',[('first',),('second',)])
        result = package.export_package(self.db,self.output)
        self.assertEqual(result['manifest']['tables'][0]['key_kind'],'rowid')
        package.restore_package(self.output,self.restored,trusted_schema_database=self.db)

    def test_working_set_selection_is_not_a_deletion_instruction(self):
        result = self.exported()
        all_rows = list(package.iter_rows(self.output,table_names=['child']))
        chosen = list(package.iter_rows(self.output,table_names=['child'],key_sha256s=[all_rows[0]['key_sha256']]))
        self.assertEqual(chosen,[all_rows[0]])
        self.assertEqual(len(list(package.iter_rows(self.output,table_names=['child']))),10)
        self.assertEqual(result['manifest'],self.manifest())
        with self.assertRaisesRegex(package.PackageError,'Unknown'):
            list(package.iter_rows(self.output,table_names=['missing']))

    def test_rejects_untrusted_schema_and_never_executes_package_sql(self):
        self.exported()
        manifest = self.manifest()
        manifest['schema']['objects'][0][3] = 'ATTACH DATABASE "/tmp/malicious-d1-test" AS evil'
        manifest['schema_sha256'] = package.digest(manifest['schema'])
        manifest['inventory']['schema_sha256'] = manifest['schema_sha256']
        manifest['inventory']['logical_sha256'] = package.digest({k:v for k,v in manifest['inventory'].items() if k!='logical_sha256'})
        self.save_manifest(manifest)
        self.rejection('trusted allowlisted schema')

    def test_schema_drift_in_independently_trusted_database_rejected(self):
        self.exported()
        other = self.root/'trusted.sqlite3'
        shutil.copyfile(self.db,other)
        with sqlite3.connect(other) as db:
            db.execute('PRAGMA user_version=43')
        with self.assertRaisesRegex(package.PackageError,'trusted allowlisted schema'):
            package.restore_package(self.output,self.restored,trusted_schema_database=other)
        self.assertFalse(self.restored.exists())

    def test_changed_chunk_rejected(self):
        self.exported()
        path = next((self.output/'objects').iterdir())
        path.write_bytes(path.read_bytes()[:-1]+b'!')
        self.rejection('SHA-256')

    def test_missing_chunk_and_unexpected_members_rejected(self):
        self.exported()
        path = next((self.output/'objects').iterdir())
        original=path.read_bytes(); path.unlink()
        self.rejection('members')
        path.write_bytes(original)
        (self.output/'extra').write_text('extra')
        self.rejection('members')

    def test_bad_whole_object_hash_even_if_chunk_hashes_valid_rejected(self):
        self.exported()
        manifest=self.manifest()
        obj=manifest['objects'][0]
        old=obj['sha256']; obj['sha256']='0'*64
        manifest['objects'].sort(key=lambda item:item['sha256'])
        for entry in manifest['files']:
            if entry['sha256']==old: entry['sha256']='0'*64
        self.save_manifest(manifest)
        self.rejection('Whole-object')

    def test_missing_object_reference_and_resealed_bad_scalar_rejected(self):
        self.exported()
        manifest=self.manifest(); table=next(t for t in manifest['tables'] if t['name']=='child')
        records=[json.loads(line) for line in (self.output/table['path']).read_bytes().splitlines()]
        records[0]['values'][-1]['ref']='f'*64
        self.reseal_table(manifest,table,records)
        self.rejection('missing scalar object')

    def test_resealed_wrong_canonical_row_hash_rejected(self):
        self.exported()
        manifest=self.manifest(); table=next(t for t in manifest['tables'] if t['name']=='child')
        records=[json.loads(line) for line in (self.output/table['path']).read_bytes().splitlines()]
        records[0]['row_sha256']='0'*64
        self.reseal_table(manifest,table,records)
        self.rejection('Canonical row image')

    def test_resealed_key_mismatch_rejected(self):
        self.exported()
        manifest=self.manifest(); table=next(t for t in manifest['tables'] if t['name']=='child')
        records=[json.loads(line) for line in (self.output/table['path']).read_bytes().splitlines()]
        records[0]['key']=[{'t':'text','v':'other'}]
        records[0]['key_sha256']=package.digest(records[0]['key'])
        self.reseal_table(manifest,table,records)
        self.rejection('Stable row key')

    def test_resealed_logical_inventory_mismatch_rejected_before_publish(self):
        self.exported()
        manifest=self.manifest()
        manifest['inventory']['tables'][0]['sha256']='0'*64
        manifest['inventory']['logical_sha256']=package.digest({k:v for k,v in manifest['inventory'].items() if k!='logical_sha256'})
        self.save_manifest(manifest)
        self.rejection('Full restored logical inventory')

    def test_path_traversal_absolute_backslash_and_file_parent_collisions(self):
        self.exported()
        original=self.manifest()
        for path in ('../escape','/tmp/escape','a\\b','a/../b','a//b','a\nb','a\x7fb'):
            with self.subTest(path=path):
                manifest=json.loads(json.dumps(original)); manifest['files'][0]['path']=path
                self.save_manifest(manifest)
                self.rejection('relative path')
        manifest=json.loads(json.dumps(original))
        manifest['database_path']=manifest['files'][0]['path']+'/child.sqlite3'
        self.save_manifest(manifest)
        self.rejection('file as its parent')

    def test_untrusted_runtime_files_cannot_install_database_sidecars(self):
        self.exported()
        original=self.manifest()
        for suffix in ('-wal','-shm','-journal'):
            manifest=json.loads(json.dumps(original))
            manifest['files'][0]['path']=manifest['database_path']+suffix
            manifest['files'].sort(key=lambda item:item['path'])
            self.save_manifest(manifest)
            self.rejection('install SQLite sidecars')

    def test_symlink_package_object_and_source_files_rejected(self):
        self.exported()
        path=next((self.output/'objects').iterdir())
        copy=self.root/'copy';copy.write_bytes(path.read_bytes());path.unlink();path.symlink_to(copy)
        self.rejection('Symlink')
        path.unlink();path.write_bytes(copy.read_bytes())
        (self.state/'link').symlink_to(copy)
        with self.assertRaisesRegex(package.PackageError,'Symlink'):
            package.export_package(self.db,self.root/'second',files_root=self.state)

    def test_existing_or_raced_destination_never_overwritten(self):
        self.exported()
        self.restored.mkdir(); sentinel=self.restored/'sentinel';sentinel.write_text('keep')
        with self.assertRaisesRegex(package.PackageError,'absent'):
            package.restore_package(self.output,self.restored,trusted_schema_database=self.db)
        self.assertEqual(sentinel.read_text(),'keep')
        sentinel.unlink();self.restored.rmdir()
        original=package.checkpoints._publish
        def race(stage,destination):
            destination.mkdir();(destination/'sentinel').write_text('raced')
            return original(stage,destination)
        with patch.object(package.checkpoints,'_publish',side_effect=race):
            with self.assertRaises(package.PackageError):
                package.restore_package(self.output,self.restored,trusted_schema_database=self.db)
        self.assertEqual((self.restored/'sentinel').read_text(),'raced')

    def test_source_change_during_runtime_phase_aborts_export(self):
        self.fixture()
        original=package.ObjectStore.add_stream
        changed=[False]
        def mutate(store,handle):
            result=original(store,handle)
            if getattr(handle,'name',None) is not None and not changed[0]:
                changed[0]=True
                with sqlite3.connect(self.db) as db:
                    db.execute("UPDATE parent SET label='changed' WHERE id=1")
            return result
        with patch.object(package.ObjectStore,'add_stream',mutate):
            with self.assertRaisesRegex(package.PackageError,'Database changed'):
                package.export_package(self.db,self.output,files_root=self.state)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.d1-package-*')))

    def test_live_wal_snapshot_and_unsupported_sql_rejected(self):
        with sqlite3.connect(self.db) as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE t(id INTEGER PRIMARY KEY)');db.commit()
            with self.assertRaisesRegex(package.PackageError,'sidecars'):
                package.export_package(self.db,self.output)
        db.close()
        self.db.unlink()
        with sqlite3.connect(self.db) as db:
            db.executescript('CREATE TABLE t(id INTEGER PRIMARY KEY);CREATE VIEW v AS SELECT * FROM t;')
        with self.assertRaisesRegex(package.PackageError,'views'):
            package.export_package(self.db,self.output)

    def test_manifest_expected_hash_and_noncanonical_json_rejected(self):
        self.exported()
        with self.assertRaisesRegex(package.PackageError,'trusted transfer'):
            package.verify_package(self.output,expected_manifest_sha256='0'*64)
        path=self.output/'manifest.json';path.write_text(json.dumps(self.manifest()))
        self.rejection('canonical')

    def test_duplicate_json_fields_rejected(self):
        self.exported()
        path=self.output/'manifest.json';raw=path.read_bytes()
        path.write_bytes(b'{"version":1,'+raw[1:])
        self.rejection('Duplicate JSON key')

    def test_utf16_database_roundtrip(self):
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA encoding='UTF-16le'")
            db.execute('CREATE TABLE t(id INTEGER PRIMARY KEY,v TEXT)')
            db.execute('INSERT INTO t VALUES(1,?)',('中文😀'*500,))
        package.export_package(self.db,self.output)
        package.restore_package(self.output,self.restored,trusted_schema_database=self.db)
        self.assertEqual(sqlite_delta.inspect_snapshot(self.db),sqlite_delta.inspect_snapshot(self.restored/'papers.sqlite3'))


if __name__ == '__main__':
    unittest.main()

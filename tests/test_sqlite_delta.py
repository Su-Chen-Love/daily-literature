import base64
import copy
import hashlib
import json
import os
import pickle
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from academic_radar import sqlite_delta as delta
from academic_radar.storage import upgrade_database


class SQLiteDeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'base.sqlite3'
        self.target = self.root / 'target.sqlite3'
        self.patch = self.root / 'delta.json'
        self.output = self.root / 'applied.sqlite3'

    def connection(self, path):
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        return db

    def fixture(self):
        db = self.connection(self.base)
        db.executescript('''
          PRAGMA foreign_keys=ON;
          CREATE TABLE parent(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT UNIQUE);
          CREATE TABLE child(key TEXT PRIMARY KEY, parent INTEGER REFERENCES parent(id) ON DELETE CASCADE,
                             value BLOB, untouched TEXT);
          CREATE TABLE typed(key BLOB PRIMARY KEY, value);
          CREATE TABLE composite(a TEXT,b INTEGER,v TEXT,PRIMARY KEY(a,b)) WITHOUT ROWID;
          CREATE TABLE fulltext_files(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
          CREATE TABLE empty_table(id TEXT PRIMARY KEY);
          INSERT INTO parent(id,label) VALUES(1,'first'),(2,'second'),(50,'deleted sequence highwater');
          DELETE FROM parent WHERE id=50;
          INSERT INTO child(rowid,key,parent,value,untouched) VALUES(20,'a',1,x'0001ff','preserve'),(30,'remove',2,NULL,'delete');
          INSERT INTO composite VALUES('x',1,'before');
        ''')
        db.executemany('INSERT INTO typed VALUES(?,?)', [(b'null',None),(b'blob',b'\x00\xff'),(b'text','a\0中文😀'),
                       (b'integer',2**63-1),(b'mininteger',-(2**63)),(b'real',1.2345678901234567),
                       (b'inf',float('inf')),(b'negzero',-0.0)])
        db.commit()
        db.close()
        shutil.copyfile(self.base, self.target)
        db = self.connection(self.target)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute("UPDATE parent SET label='temp' WHERE id=1")
        db.execute("UPDATE parent SET label='first' WHERE id=2")
        db.execute("UPDATE parent SET label='second' WHERE id=1")
        db.execute("UPDATE child SET value=?,rowid=40 WHERE key='a'", (b'new\0\xff',))
        db.execute("DELETE FROM child WHERE key='remove'")
        db.execute("INSERT INTO child(rowid,key,parent,value,untouched) VALUES(30,'new',2,'text','new')")
        db.execute("UPDATE composite SET v='after'")
        db.execute("INSERT INTO composite VALUES('z',2,'insert')")
        db.execute("UPDATE typed SET value=? WHERE key=?", (struct.unpack('>d', bytes.fromhex('8000000000000001'))[0],b'real'))
        db.execute("INSERT INTO typed VALUES(?,?)", (b'new',b'new'))
        db.execute("DELETE FROM typed WHERE key=?", (b'null',))
        db.execute("INSERT INTO parent(label) VALUES('new parent')")
        db.commit()
        db.close()

    def exported(self):
        self.fixture()
        return delta.export_delta(self.base, self.target, self.patch, require_empty_tables=['fulltext_files'])

    def mutate(self, mutate):
        value = json.loads(self.patch.read_bytes())
        mutate(value)
        self.patch.write_bytes(delta._canonical(value))

    def assert_rejected(self, pattern=None):
        before = self.base.read_bytes()
        original_patch = self.patch.read_bytes()
        with self.assertRaisesRegex(delta.DeltaError, pattern or '.'):
            delta.apply_delta(self.base, self.patch, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.base.read_bytes(), before)
        self.assertEqual(self.patch.read_bytes(), original_patch)
        self.assertFalse(list(self.root.glob('.sqlite-delta-*')))

    def test_lossless_roundtrip_preserves_every_row_type_rowid_sequence_and_inputs(self):
        result = self.exported()
        originals = [path.read_bytes() for path in (self.base,self.target,self.patch)]
        applied = delta.apply_delta(self.base,self.patch,self.output,expected_sha256=result['patch_sha256'])
        self.assertEqual(applied['target_logical_sha256'],result['target_logical_sha256'])
        self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))
        self.assertEqual(originals,[path.read_bytes() for path in (self.base,self.target,self.patch)])
        with self.connection(self.output) as db:
            self.assertEqual(db.execute("SELECT rowid,key,untouched FROM child ORDER BY rowid").fetchall(),[(30,'new','new'),(40,'a','preserve')])
            self.assertEqual(db.execute("SELECT seq FROM sqlite_sequence WHERE name='parent'").fetchone(),(51,))
            self.assertEqual(db.execute("SELECT 1/value FROM typed WHERE key=?",(b'negzero',)).fetchone(),(None,))
        image = next(row for row in json.loads(self.patch.read_bytes())['changes'] if row['name']=='empty_table')
        self.assertEqual(image,dict(name='empty_table',insert=[],update=[],delete=[]))

    def test_hash_matches_pickle4_audit_formula(self):
        self.exported()
        inventory = delta.inspect_snapshot(self.target)
        with self.connection(self.target) as db:
            for table in inventory['tables']:
                columns = table['columns']
                digest = hashlib.sha256(pickle.dumps(columns,protocol=4))
                statement = 'SELECT '+','.join(map(delta._quote,columns))+' FROM '+delta._quote(table['name'])+' ORDER BY '+','.join(map(delta._quote,table['primary_key']))
                for row in db.execute(statement):
                    digest.update(pickle.dumps(tuple(row),protocol=4))
                self.assertEqual(table['sha256'],digest.hexdigest())

    def test_noop_patch_and_sequence_only_change(self):
        self.fixture()
        self.target.unlink()
        shutil.copyfile(self.base,self.target)
        with self.connection(self.target) as db:
            db.execute("UPDATE sqlite_sequence SET seq=900 WHERE name='parent'")
        delta.export_delta(self.base,self.target,self.patch)
        delta.apply_delta(self.base,self.patch,self.output)
        self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))
        self.patch.unlink()
        self.output.unlink()
        delta.export_delta(self.base,self.base,self.patch)
        delta.apply_delta(self.base,self.patch,self.output)
        self.assertEqual(delta.inspect_snapshot(self.base),delta.inspect_snapshot(self.output))

    def test_application_schema_roundtrip(self):
        upgrade_database(self.base)
        db = self.connection(self.base)
        db.execute('PRAGMA journal_mode=DELETE')
        db.close()
        shutil.copyfile(self.base,self.target)
        with self.connection(self.target) as db:
            db.execute("INSERT INTO meta VALUES('key','changed')")
            db.execute("INSERT INTO papers(identity,title,first_seen,updated_at) VALUES('doi:test','new','now','now')")
            db.execute("INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,screened_at) VALUES('doi:test','profile','host','model',1,0.95,'now')")
        delta.export_delta(self.base,self.target,self.patch,require_empty_tables=['fulltext_files'])
        delta.apply_delta(self.base,self.patch,self.output)
        self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))

    def test_inputs_with_nullable_primary_key_rejected(self):
        self.fixture()
        with self.connection(self.target) as db:
            db.execute("INSERT INTO child(key,parent) VALUES(NULL,1)")
            db.execute("INSERT INTO child(key,parent) VALUES(NULL,1)")
        with self.assertRaisesRegex(delta.DeltaError,'NULL primary'):
            delta.export_delta(self.base,self.target,self.patch)
        self.assertFalse(self.patch.exists())

    def test_changed_schema_including_user_version_rejected(self):
        for statement in ('CREATE TABLE added(id INTEGER PRIMARY KEY)', 'CREATE INDEX new_index ON child(value)', 'PRAGMA user_version=99', 'PRAGMA application_id=42'):
            with self.subTest(statement=statement):
                self.fixture()
                with self.connection(self.target) as db:
                    db.execute(statement)
                with self.assertRaisesRegex(delta.DeltaError,'Schema changes'):
                    delta.export_delta(self.base,self.target,self.patch)
                self.base.unlink()
                self.target.unlink()

    def test_unsupported_schemas_rejected(self):
        schemas = [
            'CREATE TABLE t(value TEXT)',
            'CREATE TABLE t(id INTEGER PRIMARY KEY,value TEXT);CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET value=\'trigger\';END',
            'CREATE TABLE t(id INTEGER PRIMARY KEY);CREATE VIEW v AS SELECT * FROM t',
            'CREATE TABLE t(id INTEGER PRIMARY KEY,v INTEGER GENERATED ALWAYS AS (id*2) STORED)',
            'CREATE TABLE t(id INTEGER PRIMARY KEY,v TEXT);CREATE INDEX exp ON t(lower(v))',
            'CREATE TABLE t(id INTEGER PRIMARY KEY,rowid TEXT,_rowid_ TEXT,oid TEXT)',
            'CREATE TABLE t(id INTEGER PRIMARY KEY);ANALYZE',
        ]
        for sql in schemas:
            with self.subTest(sql=sql):
                with self.connection(self.base) as db:
                    db.executescript(sql)
                with self.assertRaises(delta.DeltaError):
                    delta.inspect_snapshot(self.base)
                self.base.unlink()

    def test_virtual_table_rejected(self):
        with self.connection(self.base) as db:
            db.execute('CREATE VIRTUAL TABLE t USING fts5(content)')
        with self.assertRaises(delta.DeltaError):
            delta.inspect_snapshot(self.base)

    def test_foreign_key_broken_inputs_rejected(self):
        self.fixture()
        with self.connection(self.target) as db:
            db.execute("UPDATE child SET parent=999 WHERE key='a'")
        with self.assertRaisesRegex(delta.DeltaError,'foreign-key'):
            delta.export_delta(self.base,self.target,self.patch)

    def test_fulltext_guard_never_silently_omits_or_clears_rows(self):
        self.fixture()
        with self.connection(self.target) as db:
            db.execute("INSERT INTO fulltext_files(value) VALUES('retain PDF metadata')")
        with self.assertRaisesRegex(delta.DeltaError,'Required empty'):
            delta.export_delta(self.base,self.target,self.patch,require_empty_tables=['fulltext_files'])
        self.assertFalse(self.patch.exists())
        delta.export_delta(self.base,self.target,self.patch)
        delta.apply_delta(self.base,self.patch,self.output)
        self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))

    def test_full_baseline_hash_rejects_unchanged_table_drift(self):
        self.exported()
        with self.connection(self.base) as db:
            db.execute("INSERT INTO empty_table VALUES('unrelated drift')")
        self.assert_rejected('Full baseline')

    def test_full_target_hash_rejects_missing_delete_or_change(self):
        self.exported()
        self.mutate(lambda value: next(row for row in value['changes'] if row['name']=='typed')['delete'].clear())
        self.assert_rejected('Full target')

    def test_before_image_precondition_checked_even_with_resealed_row_hash(self):
        self.exported()
        def change(value):
            before = next(row for row in value['changes'] if row['name']=='child')['update'][0]['before']
            before['values'][3] = delta.encode_scalar('wrong untouched data')
            before['sha256'] = delta._image(tuple(map(delta.decode_scalar,before['values'])),delta.decode_scalar(before['rowid']))['sha256']
        self.mutate(change)
        self.assert_rejected('before-image precondition')

    def test_row_image_hash_and_invalid_real_payload_rejected(self):
        self.exported()
        self.mutate(lambda value: next(row for row in value['changes'] if row['name']=='child')['update'][0]['before'].__setitem__('sha256','0'*64))
        self.assert_rejected('image hash')
        for scalar in ({'t':'integer','v':'01'},{'t':'integer','v':str(2**63)}, {'t':'real','v':'7ff8000000000000'}, {'t':'blob','v':'AB=='}, {'t':'text','v':'\ud800'},{'t':'integer','v':True}):
            with self.subTest(scalar=repr(scalar)):
                with self.assertRaises(delta.DeltaError):
                    delta.decode_scalar(scalar)

    def test_strict_json_rejects_duplicates_nonfinite_floats_oversize_and_depth(self):
        self.exported()
        for raw in (b'{"format":1,"format":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1.5}', b'{"x":123456789012345678901}', b'['*33+b'0'+b']'*33, b'{"x":"\\ud800"}', b'{} trailing'):
            with self.subTest(raw=raw):
                self.patch.write_bytes(raw)
                self.assert_rejected()
        with patch.object(delta,'MAX_PATCH_BYTES',20):
            self.patch.write_bytes(b' '*21)
            self.assert_rejected('byte limit')
        with patch.object(delta,'MAX_JSON_NODES',2):
            with self.assertRaisesRegex(delta.DeltaError,'node limit'):
                delta._bounded_json(b'[1,2]')

    def test_incomplete_extra_duplicate_conflicting_tables_or_operations_rejected(self):
        self.exported()
        original = self.patch.read_bytes()
        mutations = [lambda v:v['changes'].pop(),lambda v:v.__setitem__('sql','DROP TABLE parent'),
                     lambda v:v['changes'][0].pop('delete'),
                     lambda v:v['changes'].__setitem__(0,copy.deepcopy(v['changes'][1])),
                     lambda v:next(r for r in v['changes'] if r['name']=='child')['update'].append(copy.deepcopy(next(r for r in v['changes'] if r['name']=='child')['update'][0]))]
        for mutate in mutations:
            self.patch.write_bytes(original)
            self.mutate(mutate)
            self.assert_rejected()

    def test_transfer_sha_mismatch_rejected(self):
        self.exported()
        with self.assertRaisesRegex(delta.DeltaError,'trusted transfer'):
            delta.apply_delta(self.base,self.patch,self.output,expected_sha256='0'*64)
        self.assertFalse(self.output.exists())

    def test_destination_existing_same_input_and_symlink_rejected_untouched(self):
        self.exported()
        for destination in (self.base,self.target,self.patch):
            original=destination.read_bytes()
            with self.assertRaisesRegex(delta.DeltaError,'Destination must be absent'):
                delta.apply_delta(self.base,self.patch,destination)
            self.assertEqual(destination.read_bytes(),original)
        self.output.symlink_to(self.base)
        with self.assertRaisesRegex(delta.DeltaError,'Symlink'):
            delta.apply_delta(self.base,self.patch,self.output)
        self.output.unlink()
        parent=self.root/'linked'
        parent.symlink_to(self.root,target_is_directory=True)
        with self.assertRaisesRegex(delta.DeltaError,'Symlink'):
            delta.apply_delta(self.base,self.patch,parent/'new.db')

    def test_source_symlinks_and_sidecars_rejected(self):
        self.exported()
        linked=self.root/'link.db'
        linked.symlink_to(self.base)
        with self.assertRaisesRegex(delta.DeltaError,'Symlink'):
            delta.inspect_snapshot(linked)
        for suffix in ('-wal','-shm','-journal'):
            sidecar=Path(str(self.base)+suffix)
            sidecar.write_bytes(b'')
            self.assert_rejected('sidecars')
            sidecar.unlink()

    def test_destination_race_never_overwrites_raced_file(self):
        self.exported()
        original_publish=delta._publish
        def race(source,destination):
            destination.write_bytes(b'other writer')
            return original_publish(source,destination)
        with patch.object(delta,'_publish',side_effect=race):
            with self.assertRaises(delta.DeltaError):
                delta.apply_delta(self.base,self.patch,self.output)
        self.assertEqual(self.output.read_bytes(),b'other writer')
        self.assertFalse(list(self.root.glob('.sqlite-delta-*')))

    def test_source_change_detected_before_publication(self):
        self.exported()
        original_apply=delta._apply
        def race(db,value,records):
            original_apply(db,value,records)
            with self.connection(self.base) as original:
                original.execute("INSERT INTO empty_table VALUES('concurrent writer')")
        with patch.object(delta,'_apply',side_effect=race):
            with self.assertRaisesRegex(delta.DeltaError,'Source changed'):
                delta.apply_delta(self.base,self.patch,self.output)
        self.assertFalse(self.output.exists())

    def test_foreign_key_failure_rolls_back_and_no_copy_published(self):
        self.exported()
        original_insert=delta._insert
        def corrupt(db,table,image):
            original_insert(db,table,image)
            if table['name']=='child':
                db.execute("UPDATE child SET parent=123456 WHERE key='a'")
        with patch.object(delta,'_insert',side_effect=corrupt):
            self.assert_rejected('foreign-key')

    def test_rowid_only_change_is_included_and_preserved(self):
        with self.connection(self.base) as db:
            db.executescript("CREATE TABLE t(k TEXT PRIMARY KEY,v TEXT);INSERT INTO t(rowid,k,v) VALUES(10,'a','x'),(20,'b','y');")
        shutil.copyfile(self.base,self.target)
        with self.connection(self.target) as db:
            db.execute('UPDATE t SET rowid=99 WHERE rowid=10')
            db.execute('UPDATE t SET rowid=10 WHERE rowid=20')
            db.execute('UPDATE t SET rowid=20 WHERE rowid=99')
        delta.export_delta(self.base,self.target,self.patch)
        value=json.loads(self.patch.read_bytes())
        self.assertEqual(value['base']['tables'][0]['sha256'],value['target']['tables'][0]['sha256'])
        self.assertNotEqual(value['base']['tables'][0]['rowid_sha256'],value['target']['tables'][0]['rowid_sha256'])
        self.assertEqual(len(value['changes'][0]['update']),2)
        delta.apply_delta(self.base,self.patch,self.output)
        self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))

    def test_quoted_identifiers_are_data_not_sql(self):
        with self.connection(self.base) as db:
            db.execute('CREATE TABLE "odd\"\"table; --"("key\"\"column" TEXT PRIMARY KEY,"value" BLOB)')
        shutil.copyfile(self.base,self.target)
        with self.connection(self.target) as db:
            db.execute('INSERT INTO "odd\"\"table; --" VALUES(?,?)',('key',b'\x00'))
        delta.export_delta(self.base,self.target,self.patch)
        delta.apply_delta(self.base,self.patch,self.output)
        self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))

    def test_explicit_integer_primary_key_desc_preserves_independent_rowid(self):
        with self.connection(self.base) as db:
            db.executescript('CREATE TABLE t(id INTEGER PRIMARY KEY DESC,value TEXT);INSERT INTO t(rowid,id,value) VALUES(100,1,\'old\');')
        shutil.copyfile(self.base,self.target)
        with self.connection(self.target) as db:
            db.execute("UPDATE t SET value='new',rowid=200")
        delta.export_delta(self.base,self.target,self.patch)
        delta.apply_delta(self.base,self.patch,self.output)
        self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))

    def test_new_scalar_rows_roundtrip_exact_storage_classes(self):
        with self.connection(self.base) as db:
            db.execute('CREATE TABLE t(id INTEGER PRIMARY KEY,value)')
        shutil.copyfile(self.base,self.target)
        values=[None,0,-(2**63),2**63-1,-0.0,float('inf'),float('-inf'),
                struct.unpack('>d',bytes.fromhex('0000000000000001'))[0],
                '', 'Unicode 文献\0😀', b'', bytes(range(256))]
        with self.connection(self.target) as db:
            db.executemany('INSERT INTO t VALUES(?,?)',enumerate(values,1))
        delta.export_delta(self.base,self.target,self.patch)
        delta.apply_delta(self.base,self.patch,self.output)
        with self.connection(self.target) as target, self.connection(self.output) as actual:
            expected=target.execute('SELECT id,typeof(value),value FROM t ORDER BY id').fetchall()
            result=actual.execute('SELECT id,typeof(value),value FROM t ORDER BY id').fetchall()
        self.assertEqual(pickle.dumps(expected,protocol=4),pickle.dumps(result,protocol=4))

    def test_patch_count_limits_checked_on_both_paths(self):
        self.fixture()
        with patch.object(delta,'MAX_OPERATIONS',1):
            with self.assertRaisesRegex(delta.DeltaError,'operation limit'):
                delta.export_delta(self.base,self.target,self.patch)
        self.assertFalse(self.patch.exists())
        delta.export_delta(self.base,self.target,self.patch)
        with patch.object(delta,'MAX_OPERATIONS',1):
            self.assert_rejected('operation limit')

    def test_duplicate_sequence_keys_rejected_even_without_sqlite_constraint(self):
        self.fixture()
        with self.connection(self.target) as db:
            db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('parent',999)")
        with self.assertRaisesRegex(delta.DeltaError,'Duplicate or ambiguous primary'):
            delta.export_delta(self.base,self.target,self.patch)
        self.assertFalse(self.patch.exists())

    def test_sqlite_build_without_optional_extension_loading_method(self):
        self.fixture()
        original_connect=sqlite3.connect
        class NoExtensions:
            def __init__(self,*args,**kwargs):
                self.connection=original_connect(*args,**kwargs)
            def __getattr__(self,name):
                if name=='enable_load_extension':
                    raise AttributeError(name)
                return getattr(self.connection,name)
        with patch.object(delta.sqlite3,'connect',side_effect=NoExtensions):
            delta.export_delta(self.base,self.target,self.patch)
            delta.apply_delta(self.base,self.patch,self.output)
            self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))

    def test_cli_roundtrip_and_required_checksum(self):
        self.fixture()
        script=Path(__file__).resolve().parents[1]/'scripts/sqlite-state-delta.py'
        result=subprocess.run([sys.executable,str(script),'export','--base',str(self.base),'--target',str(self.target),'--output',str(self.patch),'--require-empty-table','fulltext_files'],capture_output=True,text=True,check=True)
        checksum=json.loads(result.stdout)['patch_sha256']
        subprocess.run([sys.executable,str(script),'apply','--base',str(self.base),'--patch',str(self.patch),'--destination',str(self.output),'--sha256',checksum],capture_output=True,text=True,check=True)
        self.assertEqual(delta.inspect_snapshot(self.target),delta.inspect_snapshot(self.output))
        result=subprocess.run([sys.executable,str(script),'apply','--base',str(self.base),'--patch',str(self.patch),'--destination',str(self.output)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)


if __name__=='__main__':
    unittest.main()

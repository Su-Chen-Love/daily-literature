"""Synthetic native callback transport tests; no remote service is contacted."""
import base64
import contextlib
import subprocess
from types import SimpleNamespace
import copy
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from academic_radar import d1_backup as backup,d1_package as package,sqlite_delta
from academic_radar.storage import upgrade_database


def sha(raw):return hashlib.sha256(raw).hexdigest()


class NativeFixture:
    def __init__(self,root):
        self.root=root;self.state=root/'source';self.state.mkdir()
        self.db=self.state/'papers.sqlite3'
        upgrade_database(self.db)
        self.profile='Original synthetic research profile.'
        self.profile_hash=sha(self.profile.encode())
        with sqlite3.connect(self.db) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute("INSERT INTO papers(identity,title,first_seen,updated_at) VALUES('doi:synthetic','original','2026-01-01','2026-01-01')")
            db.execute("INSERT INTO profile_versions(profile_hash,content,status,source,created_at) VALUES(?,?,'active','test','2026-01-01')",(self.profile_hash,self.profile))
        (self.state/'research-profile.md').write_text(self.profile)
        (self.state/'config.toml').write_text('profile_file = "research-profile.md"\nrelevance_threshold = 0.7\n')
        (self.state/'history').mkdir()
        (self.state/'history'/'old.txt').write_text('historical bytes')
        original=package.export_package(self.db,root/'initial',files_root=self.state)
        self.source_manifest_sha=original['manifest_sha256']
        self.core={k:v for k,v in original['manifest'].items() if k not in ('objects','files','statistics')}
        # A newer revision deliberately differs from the original inventory.
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE papers SET title='new current title' WHERE identity='doi:synthetic'")
            db.execute("INSERT INTO papers(identity,title,first_seen,updated_at) VALUES('doi:new','new paper','2026-01-02','2026-01-02')")
        (self.state/'history'/'new.txt').write_bytes(b'new artifact'*20000)
        current=package.export_package(self.db,root/'current',files_root=self.state)
        self.current_manifest=current['manifest']
        self.revision=2;self.calls=[];self.site_calls=[]
        self.objects={o['sha256']:o for o in self.current_manifest['objects']}
        self.rows={t['name']:list(package.iter_rows(root/'current',table_names=[t['name']])) for t in self.current_manifest['tables']}
        self.files=self.current_manifest['files']
        data='{ "note": "'+('Unicode 中文 and escaped \\n '*1500)+'", "threshold": 0.70 }'
        self.site_record={'kind':'meta','key':'overview','data':data,'checksum':sha(data.encode())}
        self.generation=sha(('meta:overview:'+self.site_record['checksum']).encode())
        self.feedback=[{'seq':1,'identity':'doi:synthetic','interest':'interested','reason':'synthetic relevance','favorite':1,'reading_status':'read_later','updated_at':'2026-01-03T12:00:00Z'}]
        new_profile='Updated synthetic research profile.'
        self.profiles=[{'seq':1,'base_version':0,'base_generation':self.generation,'base_profile_hash':self.profile_hash,
                        'base_preferences':json.dumps({'relevance_threshold':0.7,'instructions':''}),
                        'profile_hash':sha(new_profile.encode()),'content':new_profile,
                        'screening_preferences':json.dumps({'relevance_threshold':0.8,'instructions':'new instructions'}),
                        'summary':'Synthetic update','updated_at':'2026-01-03T13:00:00Z'}]
        self.snapshot={'format':'academic-radar-site-backup','version':1,'pointers':[{'key':'active','value':self.generation}],
                       'generations':[{'id':self.generation,'count':1,'created_at':'2026-01-02','status':'complete'}],
                       'excluded_uncommitted_generations':1,'feedback_cursor':1,'profile_cursor':1,
                       'main_head':{'revision':2,'import_id':self.source_manifest_sha}}
        self.fault=None

    def main(self,action,data):
        self.calls.append((action,copy.deepcopy(data)))
        if 'revision' in data and data['revision']!=2:raise AssertionError('Unpinned revision')
        if action=='read_manifest':return {'revision':2,'manifest_sha256':self.source_manifest_sha,'manifest':copy.deepcopy(self.core),'watermarks':{'feedback_cursor':0,'profile_cursor':0,'request_id':0}}
        if action=='export_summary':
            counts=[{'name':name,'rows':len(rows)} for name,rows in sorted(self.rows.items())]
            if self.fault=='count':counts[0]['rows']+=1
            return {'revision':2,'tables':counts,'file_count':len(self.files)}
        if action=='export_rows':
            rows=[copy.deepcopy(r) for r in self.rows[data['table']] if r['key_sha256']>data.get('after_key','')][:2]
            result={'revision':2,'table':data['table'],'rows':rows,'done':not rows,'next_key':rows[-1]['key_sha256'] if rows else None}
            if self.fault=='revision' and rows:result['revision']=3
            if self.fault=='row' and rows:rows[0]['row_sha256']='0'*64
            return result
        if action=='query_files':
            files=[copy.deepcopy(f) for f in self.files if f['path']>data.get('after_path','')][:2]
            return {'revision':2,'files':files,'done':not files,'next_path':files[-1]['path'] if files else None}
        if action=='read_objects':
            return {'objects':[copy.deepcopy(self.objects[key]) for key in data['sha256s'][:2]]}
        if action=='read_chunks':
            chunks=[]
            for key in data['sha256s'][:2]:
                raw=(self.root/'current'/'objects'/key).read_bytes()
                if self.fault=='chunk':raw=b'!'+raw[1:]
                chunks.append({'sha256':key,'bytes':len(raw),'data_base64':base64.b64encode(raw).decode()})
            return {'chunks':chunks}
        if action=='read_object':return {'object':copy.deepcopy(self.objects[data['sha256']])}
        if action=='read_chunk':
            raw=(self.root/'current'/'objects'/data['sha256']).read_bytes()
            if self.fault=='chunk':raw=b'!'+raw[1:]
            return {'sha256':data['sha256'],'bytes':len(raw),'data_base64':base64.b64encode(raw).decode()}
        raise AssertionError('Unexpected or mutating main action: '+action)

    def site(self,action,data):
        self.site_calls.append((action,copy.deepcopy(data)))
        if action=='begin':
            return {'snapshot':copy.deepcopy(self.snapshot),'snapshot_sha256':package.digest(self.snapshot),'hash_algorithm':'sha256-canonical-json-v1'}
        if action=='records':
            raw=self.site_record['data'].encode();after=data.get('after')
            if after and after['offset']==0:return {'generation':self.generation,'expected_count':1,'chunks':[],'next':None,'done':True}
            offset=after['offset'] if after else 0;piece=raw[offset:offset+20000]
            chunk={k:self.site_record[k] for k in ('kind','key','checksum')}
            chunk.update(bytes=len(raw),offset=offset,base64=base64.b64encode(piece).decode())
            if self.fault=='site_checksum':chunk['checksum']='0'*64
            next_cursor={'kind':'meta','key':'overview','offset':offset+len(piece) if offset+len(piece)<len(raw) else 0}
            return {'generation':self.generation,'expected_count':1,'chunks':[chunk],'next':next_cursor,'done':False}
        if action in ('feedback','profiles'):
            events=copy.deepcopy(self.feedback if action=='feedback' else self.profiles)
            events=[e for e in events if data['after']<e['seq']<=data['until']][:1]
            if self.fault=='event_gap' and events:events[0]['seq']+=1
            cursor=events[-1]['seq'] if events else data['after']
            return {'until':data['until'],'events':events,'next':cursor,'done':not events or cursor==data['until']}
        raise AssertionError('Unexpected or mutating Site action: '+action)


class D1BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.fixture=NativeFixture(self.root)
        self.output=self.root/'backup';self.restored=self.root/'restored'

    def export(self):
        return backup.export_backup(self.fixture.main,self.fixture.site,self.output,trusted_schema_database=self.fixture.db)

    def test_full_export_recomputes_current_inventory_and_keeps_every_file_and_site_record(self):
        result=self.export()
        verified=backup.verify_backup(self.output,expected_backup_sha256=result['backup_sha256'])
        current=sqlite_delta.inspect_snapshot(self.fixture.db)
        self.assertEqual(verified['main_manifest']['inventory'],current)
        self.assertNotEqual(self.fixture.core['inventory'],current)
        self.assertEqual(result['pending_feedback'],1);self.assertEqual(result['pending_profiles'],1)
        self.assertEqual((self.output/'site'/'generations'/(self.fixture.generation+'.jsonl')).read_bytes(),package.canonical(self.fixture.site_record)+b'\n')
        report=backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db,apply_pending=False)
        self.assertTrue(report['exact_main_state'])
        self.assertEqual(sqlite_delta.inspect_snapshot(self.restored/'state'/'papers.sqlite3'),current)
        self.assertEqual((self.restored/'state'/'history'/'new.txt').read_bytes(),(self.fixture.state/'history'/'new.txt').read_bytes())
        self.assertFalse(any(action in ('head','read_object','read_chunk') for action,_ in self.fixture.calls))
        self.assertGreater(result['transport_call_count'],0)
        self.assertIn('read_objects',result['transport_calls']['main'])
        self.assertTrue(all(data.get('revision',2)==2 for _,data in self.fixture.calls))

    def test_sqlite_build_without_extension_loading_api_full_roundtrip(self):
        original_connect=sqlite3.connect
        connections=[]
        def close_connections():
            for db in connections:db.connection.close()
        self.addCleanup(close_connections)
        class NoExtensions:
            def __init__(self,*args,**kwargs):
                self.connection=original_connect(*args,**kwargs)
                connections.append(self)
            def __getattr__(self,name):
                if name=='enable_load_extension':raise AttributeError(name)
                return getattr(self.connection,name)
            def __enter__(self):
                self.connection.__enter__()
                return self
            def __exit__(self,*args):return self.connection.__exit__(*args)
        with patch.object(sqlite3,'connect',side_effect=NoExtensions):
            result=self.export()
            verified=backup.verify_backup(self.output,expected_backup_sha256=result['backup_sha256'])
            report=backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db,
                                         expected_backup_sha256=result['backup_sha256'],apply_pending=False)
            current=sqlite_delta.inspect_snapshot(self.fixture.db)
            self.assertEqual(verified['main_manifest']['inventory'],current)
            self.assertEqual(sqlite_delta.inspect_snapshot(self.restored/'state'/'papers.sqlite3'),current)
        self.assertTrue(connections)
        self.assertTrue(all(not hasattr(db,'enable_load_extension') for db in connections))
        self.assertTrue(report['exact_main_state'])
        self.assertGreater(result['rows'],0)
        self.assertIn('export_rows',[action for action,_ in self.fixture.calls])
        self.assertEqual((self.restored/'state'/'history'/'new.txt').read_bytes(),
                         (self.fixture.state/'history'/'new.txt').read_bytes())
        self.assertEqual((self.restored/'site'/'generations'/(self.fixture.generation+'.jsonl')).read_bytes(),
                         package.canonical(self.fixture.site_record)+b'\n')

    def test_schema_connection_disables_extension_loading_and_schema_trust(self):
        path=self.root/'schema.sqlite3'
        db=sqlite3.connect(path);self.addCleanup(db.close)
        probe=Mock(wraps=db)
        # Keep the synthetic present-API case runnable on builds without it too.
        disable=getattr(db,'enable_load_extension',None)
        probe.enable_load_extension=Mock(side_effect=disable)
        def check_before_commit():
            self.assertEqual(db.execute('PRAGMA trusted_schema').fetchone(),(0,))
            with self.assertRaisesRegex(sqlite3.OperationalError,'not authorized|no such function'):
                db.execute("SELECT load_extension('synthetic-extension-never-installed')")
            db.commit()
        probe.commit.side_effect=check_before_commit
        with patch.object(backup,'sqlite3',SimpleNamespace(connect=Mock(return_value=probe))):
            tables=backup._schema_database(path,self.fixture.db,self.fixture.core)
        self.assertEqual(tables,[backup._metadata(table) for table in self.fixture.core['inventory']['tables']])
        probe.enable_load_extension.assert_called_once_with(False)
        self.assertEqual(probe.method_calls[0],call.enable_load_extension(False))
        probe.execute.assert_any_call('PRAGMA trusted_schema=OFF')
        probe.close.assert_called_once_with()
        with self.assertRaisesRegex(sqlite3.ProgrammingError,'closed'):
            db.execute('SELECT 1')

    def test_extension_disable_errors_fail_closed_before_exporting_rows(self):
        for error_type in (RuntimeError,AttributeError,sqlite3.OperationalError):
            with self.subTest(error_type=error_type.__name__):
                self.fixture.calls.clear()
                db=sqlite3.connect(':memory:');self.addCleanup(db.close)
                probe=Mock(wraps=db)
                error=error_type('synthetic extension-disable failure')
                probe.enable_load_extension=Mock(side_effect=error)
                with patch.object(backup,'sqlite3',SimpleNamespace(connect=Mock(return_value=probe))):
                    with self.assertRaises(error_type) as raised:self.export()
                self.assertIs(raised.exception,error)
                probe.enable_load_extension.assert_called_once_with(False)
                probe.execute.assert_not_called()
                probe.close.assert_called_once_with()
                with self.assertRaisesRegex(sqlite3.ProgrammingError,'closed'):
                    db.execute('SELECT 1')
                self.assertEqual([action for action,_ in self.fixture.calls],['read_manifest','export_summary'])
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob('.d1-backup-*')))

    def test_schema_fingerprint_and_table_metadata_checked_before_rows(self):
        original=copy.deepcopy(self.fixture.core)
        for mismatch in ('hash','schema','tables'):
            with self.subTest(mismatch=mismatch):
                self.fixture.core=copy.deepcopy(original);self.fixture.calls.clear()
                if mismatch=='hash':self.fixture.core['schema_sha256']='0'*64
                elif mismatch=='schema':
                    self.fixture.core['schema']['objects'][0][3]="SELECT load_extension('untrusted')"
                    self.fixture.core['schema_sha256']=package.digest(self.fixture.core['schema'])
                else:self.fixture.core['inventory']['tables'][0]['columns'].append('untrusted_column')
                connect=Mock(side_effect=AssertionError('Untrusted schema reached writable connection'))
                with patch.object(backup,'sqlite3',SimpleNamespace(connect=connect)):
                    with self.assertRaisesRegex(backup.BackupError,'trusted schema'):self.export()
                connect.assert_not_called()
                self.assertEqual([action for action,_ in self.fixture.calls],['read_manifest','export_summary'])
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob('.d1-backup-*')))

    def test_operational_restore_replays_pending_events_and_retains_exact_base(self):
        result=self.export();before=(self.output/'main'/'manifest.json').read_bytes()
        report=backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db,expected_backup_sha256=result['backup_sha256'])
        self.assertFalse(report['exact_main_state']);self.assertEqual(report['applied'],{'feedback':1,'profiles':1})
        self.assertEqual(before,(self.output/'main'/'manifest.json').read_bytes())
        with sqlite3.connect(self.restored/'state'/'papers.sqlite3') as db:
            self.assertEqual(db.execute("SELECT favorite,reading_status FROM paper_feedback WHERE identity='doi:synthetic'").fetchone(),(1,'read_later'))
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone(),('1',))
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='cloud_profile_cursor'").fetchone(),('1',))
            self.assertEqual(db.execute("SELECT count(*) FROM applied_cloud_profile_events").fetchone(),(1,))
        self.assertEqual((self.restored/'state'/'research-profile.md').read_text(),self.fixture.profiles[0]['content'])
        self.assertTrue((self.restored/'site'/'feedback.jsonl').exists())
        self.assertFalse((self.restored/'state'/'papers.sqlite3-wal').exists())

    def test_rejects_wrong_revision_rows_chunks_counts_site_hash_and_event_gaps(self):
        for fault in ('revision','row','chunk','count','site_checksum','event_gap'):
            with self.subTest(fault=fault):
                self.fixture.fault=fault
                with self.assertRaises((backup.BackupError,package.PackageError)):
                    self.export()
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob('.d1-backup-*')))

    def test_unknown_paper_pending_event_fails_replay_without_publishing(self):
        self.fixture.feedback[0]['identity']='doi:unknown'
        self.export()
        with self.assertRaisesRegex(ValueError,'unknown paper'):
            backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db)
        self.assertFalse(self.restored.exists())
        # Exact base recovery remains available despite the pending-event conflict.
        result=backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db,apply_pending=False)
        self.assertTrue(result['exact_main_state'])

    def test_profile_conflict_fails_operational_restore_but_not_base_backup(self):
        self.fixture.profiles[0]['base_profile_hash']='0'*64
        self.export()
        with self.assertRaisesRegex(ValueError,'conflicts'):
            backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db)
        self.assertFalse(self.restored.exists())
        backup.verify_backup(self.output)

    def test_saved_bundle_tampering_and_symlink_rejected(self):
        result=self.export()
        path=self.output/'site'/'feedback.jsonl';original=path.read_bytes();path.write_bytes(original+b' ')
        with self.assertRaisesRegex(backup.BackupError,'hash mismatch'):
            backup.verify_backup(self.output)
        path.write_bytes(original);outside=self.root/'outside';outside.write_bytes(original);path.unlink();path.symlink_to(outside)
        with self.assertRaises(package.PackageError):backup.verify_backup(self.output)

    def test_saved_backup_root_trusted_hash_and_existing_destination_rejected(self):
        self.export()
        with self.assertRaisesRegex(backup.BackupError,'trusted SHA'):
            backup.verify_backup(self.output,expected_backup_sha256='0'*64)
        self.restored.mkdir();(self.restored/'sentinel').write_text('retain')
        with self.assertRaises(package.PackageError):
            backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db)
        self.assertEqual((self.restored/'sentinel').read_text(),'retain')

    def test_main_watermark_must_equal_reconstructed_meta(self):
        original=self.fixture.main
        def wrong(action,data):
            result=original(action,data)
            if action=='read_manifest':result['watermarks']['feedback_cursor']=1
            return result
        with self.assertRaisesRegex(backup.BackupError,'exact meta'):
            backup.export_backup(wrong,self.fixture.site,self.output,trusted_schema_database=self.fixture.db)
        self.assertFalse(self.output.exists())

    def test_missing_atomic_pin_or_noncanonical_snapshot_hash_rejected(self):
        self.fixture.snapshot.pop('main_head')
        with self.assertRaisesRegex(backup.BackupError,'no committed'):
            self.export()
        self.assertFalse(self.output.exists())

    def test_large_unicode_profile_event_is_reassembled_from_bounded_chunks(self):
        event=self.fixture.profiles[0]
        event['content']='中'*100000
        event['profile_hash']=sha(event['content'].encode())
        raw=package.canonical(event)
        original=self.fixture.site
        def chunked(action,data):
            if action=='profiles' and data['after']==0:
                return {'until':1,'events':[],'next':0,'done':False,
                        'oversized':{'seq':1,'bytes':len(raw),'sha256':sha(raw)}}
            if action=='event_chunks':
                offset=data['offset'];piece=raw[offset:offset+256*1024]
                return {'stream':'profiles','seq':1,'until':1,'offset':offset,
                        'bytes':len(raw),'sha256':sha(raw),'base64':base64.b64encode(piece).decode(),
                        'next':offset+len(piece),'done':offset+len(piece)==len(raw)}
            return original(action,data)
        backup.export_backup(self.fixture.main,chunked,self.output,trusted_schema_database=self.fixture.db)
        self.assertEqual((self.output/'site'/'profiles.jsonl').read_bytes(),raw+b'\n')
        report=backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db)
        self.assertEqual(report['applied']['profiles'],1)
        self.assertEqual((self.restored/'state'/'research-profile.md').read_text(),'中'*100000)

    def test_profile_destination_cannot_install_sqlite_sidecars(self):
        self.export()
        for suffix in ('','-wal','-shm','-journal'):
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(backup.BackupError,'database or SQLite sidecars'):
                    backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db,
                                          profile_relative_path='papers.sqlite3'+suffix)
                self.assertFalse(self.restored.exists())

    def test_resealed_backup_watermark_is_checked_on_exact_base_restore(self):
        self.export()
        path=self.output/'backup.json';manifest=json.loads(path.read_bytes())
        manifest['watermarks']['feedback_cursor']=1
        path.write_bytes(package.canonical(manifest)+b'\n')
        with self.assertRaisesRegex(backup.BackupError,'exact meta'):
            backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db,apply_pending=False)
        self.assertFalse(self.restored.exists())

    def test_site_only_backup_accepts_revision_zero_without_claiming_main_completeness(self):
        self.fixture.snapshot['main_head']={'revision':0,'import_id':None}
        result=backup.export_site_backup(self.fixture.site,self.output)
        self.assertEqual(result['main_revision'],0)
        verified=backup.verify_site_backup(self.output,expected_site_backup_sha256=result['site_backup_sha256'])
        self.assertEqual(verified['manifest']['format'],backup.SITE_ONLY_FORMAT)
        self.assertFalse((self.output/'main').exists())
        self.assertEqual(result['feedback_events'],1)
        self.assertEqual(result['profile_events'],1)
        self.assertEqual(self.fixture.calls,[])
        with self.assertRaises((backup.BackupError,package.PackageError)):
            backup.verify_backup(self.output)

    def test_operational_relocation_carries_selected_profile_override(self):
        self.export()
        report=backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db,
                                    profile_relative_path='other-profile.md',source_root=self.fixture.state)
        self.assertTrue(report['operational_ready']);self.assertFalse(report['exact_main_state'])
        state=self.restored/'state'
        config=backup.checkpoints.tomllib.loads((state/'config.toml').read_text())
        self.assertEqual(config['profile_file'],'other-profile.md')
        self.assertEqual((state/config['profile_file']).read_text(),self.fixture.profiles[0]['content'])
        self.assertEqual((state/'research-profile.md').read_text(),self.fixture.profile)
        self.assertFalse(report['relocation']['local_rollback_enabled'])

    def test_operational_relocation_maps_active_refs_and_preserves_archives(self):
        state=self.root/'derived';shutil.copytree(self.fixture.state,state)
        raw=b'synthetic fulltext bytes';(state/'paper.pdf').write_bytes(raw)
        path=str(self.fixture.state/'paper.pdf')
        artifact={'stored_path':path};(state/'active.json').write_text(json.dumps(artifact))
        cp=backup.checkpoints
        entries=cp._provenance([{'source':'/old/rollback.sqlite3','disposition':'archived_local',
                                'reason':'Disabled historical rollback','archive_label':'offline only',
                                'operation_disabled':True,'revalidation_required':True}])
        cp._write_provenance(state,entries,[]);provenance=(state/cp.PROVENANCE_FILE).read_bytes()
        archived=cp.ARCHIVE_TOKEN+entries[0]['id']
        with sqlite3.connect(state/'papers.sqlite3') as db:
            db.execute("INSERT INTO fulltext_files(identity,stored_path,original_name,sha256,size_bytes,imported_at) VALUES('doi:synthetic',?,'paper.pdf',?,?, 'now')",(path,sha(raw),len(raw)))
            db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at,details_json) VALUES('active','test','running','now',?)",(json.dumps({'report_path':str(self.fixture.state/'active.json')}),))
            db.execute("INSERT INTO cleanup_audits(audit_id,status,backup_path,report_path,before_json,created_at) VALUES('old','applied',?,?,'{}','now')",(archived,archived))
        final=self.root/'published'/'state'
        result=backup._relocate_operational(state,final,self.fixture.current_manifest,self.fixture.state)
        self.assertTrue(result['performed']);self.assertEqual(result['archived_references_preserved'],1)
        self.assertEqual((state/cp.PROVENANCE_FILE).read_bytes(),provenance)
        self.assertEqual(json.loads((state/'active.json').read_text())['stored_path'],str(final/'paper.pdf'))
        with sqlite3.connect(state/'papers.sqlite3') as db:
            self.assertEqual(db.execute('SELECT stored_path FROM fulltext_files').fetchone()[0],str(final/'paper.pdf'))
            self.assertEqual(db.execute('SELECT backup_path FROM cleanup_audits').fetchone()[0],archived)
        self.assertFalse(final.exists())

    def test_operational_relocation_rejects_unmapped_active_reference(self):
        state=self.root/'derived';shutil.copytree(self.fixture.state,state)
        with sqlite3.connect(state/'papers.sqlite3') as db:
            db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at,details_json) VALUES('active','test','running','now',?)",(json.dumps({'report_path':'/unmapped/missing.json'}),))
        with self.assertRaisesRegex(backup.BackupError,'Unmapped reference'):
            backup._relocate_operational(state,self.root/'published',self.fixture.current_manifest,self.fixture.state)

    def test_operational_relocation_rejects_plain_rollback_and_profile_mismatch(self):
        for kind in ('rollback','profile'):
            with self.subTest(kind=kind):
                state=self.root/kind;shutil.copytree(self.fixture.state,state)
                if kind=='rollback':
                    with sqlite3.connect(state/'papers.sqlite3') as db:
                        db.execute("INSERT INTO cleanup_audits(audit_id,status,backup_path,report_path,before_json,created_at) VALUES('old','applied',?,?,'{}','now')",(str(self.fixture.state/'history/old.txt'),str(self.fixture.state/'history/old.txt')))
                else:(state/'research-profile.md').write_text('unexpected profile bytes')
                with self.assertRaisesRegex(backup.BackupError,'rollback|profile bytes/hash'):
                    backup._relocate_operational(state,self.root/'published',self.fixture.current_manifest,self.fixture.state)

    def test_site_only_rejects_malformed_capture_metadata(self):
        original=copy.deepcopy(self.fixture.snapshot)
        for field,value in (('version',True),('feedback_cursor',True),('pointers',[{'key':'active','value':self.fixture.generation}]*2),('main_head',{'revision':0,'import_id':'0'*64})):
            with self.subTest(field=field):
                self.fixture.snapshot=copy.deepcopy(original);self.fixture.snapshot[field]=value
                with self.assertRaises((backup.BackupError,package.PackageError)):
                    backup.export_site_backup(self.fixture.site,self.output)
                self.assertFalse(self.output.exists())

    def test_profile_target_maps_only_explicit_source_root(self):
        state=self.root/'derived';database=state/'papers.sqlite3'
        config={'profile_file':str(self.fixture.state/'research-profile.md')}
        self.assertEqual(backup._profile_target(state,database,config,None,self.fixture.state),state/'research-profile.md')
        with self.assertRaises(ValueError):backup._profile_target(state,database,config,None)
        with self.assertRaises(ValueError):backup._profile_target(state,database,config,None,'/')
        with self.assertRaises(ValueError):backup._profile_target(state,database,config,None,self.root/'other')

    def test_no_events_and_empty_site_generations_are_complete(self):
        self.fixture.feedback=[];self.fixture.profiles=[]
        self.fixture.snapshot.update(feedback_cursor=0,profile_cursor=0,generations=[],pointers=[])
        result=self.export()
        self.assertEqual(result['site_generations'],0)
        restored=backup.restore_backup(self.output,self.restored,trusted_schema_database=self.fixture.db)
        self.assertTrue(restored['exact_main_state'])


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for the real Worker bridge')
class D1BackupWorkerBridgeTests(unittest.TestCase):
    def test_real_worker_revision_pin_large_event_and_bulk_backup(self):
        from academic_radar import d1_runtime as runtime
        project=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='d1-independent-backup-') as temporary:
         root=Path(temporary); fixture=NativeFixture(root); script=root/'bridge.mjs'
         script.write_text("""
        import {createInterface} from 'node:readline';
        import {database} from 'DATABASE';
        import {mainStoreOperation} from 'MAIN';
        import {siteBackup} from 'SITE';
        const db=database();
        for await(const line of createInterface({input:process.stdin})){
         try{
         const {action,data}=JSON.parse(line);let result;
         if(action==='fixture_site'){
         const r=data.record,g=data.generation;
         db.sql.prepare('INSERT INTO generations VALUES(?,?,?,?)').run(g,1,'fixture','complete');
         db.sql.prepare('INSERT INTO records VALUES(?,?,?,?,?)').run(g,r.kind,r.key,r.data,r.checksum);
         db.sql.prepare('INSERT INTO pointers VALUES(?,?)').run('active',g);
         const f=data.feedback;db.sql.prepare('INSERT INTO cloud_feedback VALUES(?,?,?,?,?,?,?)').run(f.seq,f.identity,f.interest,f.reason,f.favorite,f.reading_status,f.updated_at);
         const p=data.profile;db.sql.prepare('INSERT INTO cloud_profile_events VALUES(?,?,?,?,?,?,?,?,?,?)').run(p.seq,p.base_version,p.base_generation,p.base_profile_hash,p.base_preferences,p.profile_hash,p.content,p.screening_preferences,p.summary,p.updated_at);
         result={ok:true};
         }else if(action==='fixture_later_event'){
         db.sql.prepare("INSERT INTO cloud_feedback(identity,interest,reason,favorite,reading_status,updated_at) VALUES('doi:synthetic',NULL,NULL,0,'read','2026-01-04')").run();result={ok:true};
         }else if(action.startsWith('site:'))result=await siteBackup(db,action.slice(5),data);
         else result=await mainStoreOperation(db,action,data);
         process.stdout.write(JSON.stringify({result})+'\\n');
         }catch(error){process.stdout.write(JSON.stringify({error:error.message,stack:error.stack})+'\\n');}
        }
        db.sql.close();
        """.replace('DATABASE',(project/'sites/scripts/d1-local.mjs').as_uri()).replace('MAIN',(project/'sites/worker/main-store.js').as_uri()).replace('SITE',(project/'sites/worker/site-backup.js').as_uri()))
         process=subprocess.Popen(['node',str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
         calls=[]
         def call(action,data):
          calls.append((action,data));process.stdin.write(json.dumps({'action':action,'data':data})+'\n');process.stdin.flush()
          line=process.stdout.readline()
          if not line:raise RuntimeError('Bridge exited: '+process.stderr.read())
          result=json.loads(line)
          if 'error' in result:raise RuntimeError(action+': '+result['error']+'\n'+result['stack'])
          return result['result']
         try:
          current=root/'current';manifest=fixture.current_manifest
          runtime.D1Client(call).upload(SimpleNamespace(directory=current/'objects',chunks={p.name:p.stat().st_size for p in (current/'objects').iterdir()},objects={o['sha256']:o for o in manifest['objects']}))
          identifier=hashlib.sha256((current/'manifest.json').read_bytes()).hexdigest()
          root_chunks=root/'root-chunks';root_chunks.mkdir()
          root_objects=package.ObjectStore(root_chunks);root_objects.add_bytes((current/'manifest.json').read_bytes())
          runtime.D1Client(call).upload(root_objects)
          call('begin_import',{'manifest_sha256':identifier,'manifest':manifest})
          for table,rows in fixture.rows.items():
           for i in range(0,len(rows),100):call('import_rows',{'import_id':identifier,'operation_id':table+'-'+str(i),'rows':rows[i:i+100]})
          for table in fixture.rows:
           cursor=''
           while True:
            v=call('verify_import',{'import_id':identifier,'table':table,'expected_cursor':cursor})
            if v['verified']:break
            cursor=v['cursor']
          call('commit_import',{'import_id':identifier,'expected_revision':0,'operation_id':'initial','watermarks':{'feedback_cursor':0,'profile_cursor':0,'request_id':0}})
          p=fixture.profiles[0];p['content']='中'*100000;p['profile_hash']=hashlib.sha256(p['content'].encode()).hexdigest()
          call('fixture_site',{'generation':fixture.generation,'record':fixture.site_record,'feedback':fixture.feedback[0],'profile':p})
          moved=False
          def site(action,data):
           nonlocal moved
           result=call('site:'+action,data)
           if action=='begin' and not moved:
            moved=True
            old=next(r for r in fixture.rows['papers'] if r['key'][0]['v']=='doi:synthetic')
            row=json.loads(json.dumps(old));columns=next(t['columns'] for t in manifest['inventory']['tables'] if t['name']=='papers')
            row['values'][columns.index('title')]={'t':'text','v':'newer live title excluded by pin'}
            row['row_sha256']=package.digest({'values':row['values'],'rowid':row['rowid']});row.pop('record_sha256');row['record_sha256']=package.digest(row)
            call('commit_operations',{'expected_revision':1,'operation_id':'during-backup','mutations':[{'row':row,'expected_record_sha256':old['record_sha256']}]})
            call('fixture_later_event',{})
           return result
          result=backup.export_backup(call,site,root/'backup',trusted_schema_database=fixture.db)
          assert result['revision']==1 and result['pending_feedback']==1 and result['pending_profiles']==1,result
          verified=backup.verify_backup(root/'backup',expected_backup_sha256=result['backup_sha256'])
          assert verified['main_manifest']['inventory']==sqlite_delta.inspect_snapshot(fixture.db)
          restored=backup.restore_backup(root/'backup',root/'restored',trusted_schema_database=fixture.db,expected_backup_sha256=result['backup_sha256'])
          assert restored['applied']=={'feedback':1,'profiles':1},restored
          state=root/'restored'/'state'
          assert (state/'research-profile.md').read_text()=='中'*100000
          with sqlite3.connect(state/'papers.sqlite3') as db:
           assert db.execute("SELECT title FROM papers WHERE identity='doi:synthetic'").fetchone()[0]=='new current title'
           assert db.execute("SELECT favorite FROM paper_feedback WHERE identity='doi:synthetic'").fetchone()[0]==1
          assert call('head',{})['head']['revision']==2
          assert any(action=='site:event_chunks' for action,_ in calls)
          assert all(data.get('revision',1)==1 for action,data in calls if action in {'read_manifest','export_rows','export_summary','query_files'})
          assert 'read_objects' in result['transport_calls']['main']
          assert 'read_chunks' in result['transport_calls']['main']
          assert not any(action in ('read_object','read_chunk') for action,_ in calls)
         finally:
          process.stdin.close();process.wait(timeout=10);process.stdout.close();process.stderr.close()


if __name__=='__main__':unittest.main()

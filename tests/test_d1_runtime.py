"""Synthetic native transport: no network, real account, credentials or model."""
import base64
import contextlib
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar import cloud_runtime, cloud_sync, d1_package as package, d1_runtime as runtime
from academic_radar import sqlite_delta as delta
from academic_radar.storage import connect


class NativeHost:
    def __init__(self, root, db, package_dir):
        self.root, self.db = root, db
        self.manifest = json.loads((package_dir / 'manifest.json').read_text())
        self.rows = {}
        for table in self.manifest['tables']:
            for line in (package_dir / table['path']).read_text().splitlines():
                row = json.loads(line)
                self.rows[(row['table'], row['key_sha256'])] = row
        self.objects = {item['sha256']: item for item in self.manifest['objects']}
        self.chunks = {path.name: path.read_bytes() for path in (package_dir / 'objects').iterdir()}
        self.files = {item['path']: item for item in self.manifest['files']}
        self.revision, self.watermarks = 1, {'feedback_cursor': 0, 'profile_cursor': 0, 'request_id': 0}
        self.calls, self.pending, self.committed = [], {}, {}
        self.fail_after_commit = False
        self.table = {item['name']: item for item in self.manifest['inventory']['tables']}

    def head(self):
        return {'revision': self.revision, 'import_id': 'synthetic', 'manifest_sha256': 'a' * 64,
                'watermarks': self.watermarks}

    def _values(self, record):
        values = []
        for item in record['values']:
            if 'ref' in item:
                raw = b''.join(self.chunks[part['sha256']] for part in self.objects[item['ref']]['chunks'])
                values.append(raw.decode() if item['t'] == 'text' else raw)
            else:
                values.append(delta.decode_scalar(item))
        return values

    def _query(self, args):
        table = args['table']; scope = args.get('scope')
        sql, params = None, []
        if scope == 'latest_screenings':
            sql = "SELECT rowid FROM screenings s WHERE provider='codex-agent' AND rowid=(SELECT z.rowid FROM screenings z WHERE z.identity=s.identity AND z.provider='codex-agent' ORDER BY screened_at DESC,rowid DESC LIMIT 1)"
        elif scope == 'abstract_retry':
            sql = "SELECT a.rowid FROM abstract_attempts a WHERE a.identity IN (SELECT identity FROM papers WHERE eligibility_status<>'excluded' AND (COALESCE(abstract,'')='' OR publication_type='Unknown' OR eligibility_status='quarantine')) AND (a.rowid=(SELECT z.rowid FROM abstract_attempts z WHERE z.identity=a.identity ORDER BY attempted_at DESC,rowid DESC LIMIT 1) OR (a.status IN ('failed','not_found') AND a.rowid=(SELECT z.rowid FROM abstract_attempts z WHERE z.identity=a.identity AND z.provider=a.provider AND status IN ('failed','not_found') ORDER BY attempted_at DESC,rowid DESC LIMIT 1)))"
        elif scope == 'ui_tasks':
            sql = "SELECT rowid FROM task_runs WHERE status='running' OR rowid IN(SELECT rowid FROM task_runs ORDER BY created_at DESC LIMIT 20)"
        elif scope == 'ui_source_runs':
            sql = "SELECT rowid FROM source_runs s WHERE rowid=(SELECT z.rowid FROM source_runs z WHERE z.source=s.source ORDER BY finished_at DESC,rowid DESC LIMIT 1) OR run_id=?"
            params = ['collect-cloud-' + hashlib.sha256(args['operation_id'].encode()).hexdigest()]
        elif scope == 'runtime_meta':
            sql = "SELECT rowid FROM meta WHERE (substr(key,1,24)='cloud_runtime_operation:' AND (key=? OR COALESCE(json_extract(value,'$.phase'),'unknown')<>'imported')) OR key=? OR substr(key,1,length(?))=? OR (substr(key,1,14)<>'cloud_runtime_' AND substr(key,1,22)<>'cloud_official_import:')"
            op = args['operation_id']; prefix = 'cloud_official_import:' + op + '.official.'
            params = ['cloud_runtime_operation:' + op, 'cloud_runtime_export:' + op, prefix, prefix]
        elif scope == 'runtime_run_papers':
            sql = "SELECT rowid FROM run_papers WHERE run_id=? OR run_id=(SELECT value FROM meta WHERE key=?) OR run_id=(SELECT run_id FROM agent_jobs WHERE status='imported' ORDER BY imported_at DESC LIMIT 1)"
            op = args['operation_id']; params = ['collect-cloud-' + hashlib.sha256(op.encode()).hexdigest(), 'cloud_runtime_export:' + op]
        # ui_jobs and ui_runs fixtures are all genuinely UI-relevant jobs/runs.
        eligible = None
        if sql:
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                eligible = {row[0] for row in db.execute(sql, params)}
        rows = [row for (name, _), row in self.rows.items() if name == table and
                (eligible is None or delta.decode_scalar(row['rowid']) in eligible)]
        if 'keys' in args:
            rows = [row for row in rows if row['key_sha256'] in args['keys']]
        if 'filter' in args:
            rows = [row for row in rows if all(dict(zip(self.table[table]['columns'], self._values(row))).get(key) == value for key, value in args['filter'].items())]
        rows = sorted((row for row in rows if row['key_sha256'] > args.get('after_key', '')), key=lambda row: row['key_sha256'])
        limit = args.get('limit', 200); page = rows[:limit]
        return {'revision': self.revision, 'rows': page, 'done': len(rows) <= limit,
                'next_key': None if len(rows) <= limit else page[-1]['key_sha256']}

    def _apply(self, args):
        operation = args['operation_id']
        if operation in self.committed:
            return self.committed[operation]
        pending = self.pending[operation]
        if pending['begin']['expected_revision'] != self.revision:
            raise RuntimeError('stale revision')
        mutations, files = [], []
        for page in pending['pages'].values():
            mutations.extend(page['mutations']); files.extend(page['files'])
        for item in mutations:
            row = item.get('row'); identity = (row['table'], row['key_sha256']) if row else (item['table'], item['key_sha256'])
            existing = self.rows.get(identity)
            if (existing['record_sha256'] if existing else None) != item['expected_record_sha256']:
                raise RuntimeError('base hash conflict')
        for item in files:
            existing = self.files.get(item['path'])
            if (existing['sha256'] if existing else None) != item['expected_sha256']:
                raise RuntimeError('file conflict')
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('PRAGMA foreign_keys=OFF')
            for item in mutations:
                record = item.get('row')
                identity = (record['table'], record['key_sha256']) if record else (item['table'], item['key_sha256'])
                old = self.rows.get(identity)
                if old:
                    db.execute('DELETE FROM ' + delta._quote(identity[0]) + ' WHERE rowid=?', (delta.decode_scalar(old['rowid']),))
                if record:
                    fields = self.table[identity[0]]['columns'] + ['rowid']
                    values = self._values(record) + [delta.decode_scalar(record['rowid'])]
                    db.execute('INSERT INTO ' + delta._quote(identity[0]) + '(' + ','.join(map(delta._quote, fields)) + ') VALUES(' + ','.join('?' for _ in fields) + ')', values)
                    self.rows[identity] = record
                else:
                    del self.rows[identity]
        for item in files:
            self.files[item['path']] = {key: value for key, value in item.items() if key != 'expected_sha256'}
        self.revision += 1; self.watermarks = pending['begin']['watermarks']
        result = {'head': self.head(), 'ok': True}; self.committed[operation] = result
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise OSError('lost native response after atomic commit')
        return result

    def __call__(self, action, args):
        self.calls.append((action, args))
        if action == 'head': return {'head': self.head()}
        if action in {'read_manifest', 'summary', 'query_rows', 'query_files'} and args['revision'] != self.revision:
            raise RuntimeError('test host only retains current revision')
        if action == 'read_manifest': return {'revision': self.revision, 'manifest': self.manifest}
        if action == 'summary':
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                maxima = {name: str(db.execute('SELECT COALESCE(MAX(rowid),0) FROM ' + delta._quote(name)).fetchone()[0]) for name in self.table}
                db.row_factory = sqlite3.Row
                from academic_radar.cloud_views import history_artifact_availability
                artifacts = history_artifact_availability(db)
            return {'revision': self.revision, 'rowid_maxima': maxima, 'history_artifact_counts': artifacts}
        if action == 'query_rows': return self._query(args)
        if action == 'query_files': return {'revision': self.revision, 'done': True, 'files': [self.files[path] for path in args['paths'] if path in self.files]}
        if action == 'read_objects': return {'objects': [self.objects[sha] for sha in args['sha256s']]}
        if action == 'read_chunks': return {'chunks': [{'sha256':sha,'bytes':len(self.chunks[sha]),'data_base64':base64.b64encode(self.chunks[sha]).decode()} for sha in args['sha256s']]}
        if action == 'read_object': return {'object': self.objects[args['sha256']]}
        if action == 'read_chunk': return {'data_base64': base64.b64encode(self.chunks[args['sha256']]).decode()}
        if action == 'put_chunks':
            for item in args['chunks']: self.chunks[item['sha256']] = base64.b64decode(item['data_base64'])
            return {'chunks':[{'sha256':item['sha256'],'bytes':len(self.chunks[item['sha256']]),'verified':True} for item in args['chunks']]}
        if action == 'put_objects':
            for item in args['objects']: self.objects[item['sha256']] = item
            return {'objects':[{'sha256':item['sha256'],'cursor':len(item['chunks']),'verified':True} for item in args['objects']]}
        if action == 'verify_objects': return {'objects':[{'sha256':item['sha256'],'cursor':item['expected_cursor'],'verified':True} for item in args['objects']]}
        if action == 'put_chunk':
            self.chunks[args['sha256']] = base64.b64decode(args['data_base64']); return {'ok': True}
        if action == 'put_object':
            self.objects[args['object']['sha256']] = args['object']; return {'ok': True}
        if action == 'verify_object': return {'verified': True}
        if action == 'begin_operation':
            self.pending.setdefault(args['operation_id'], {'begin': args, 'pages': {}}); return {'ok': True}
        if action == 'stage_operation':
            self.pending[args['operation_id']]['pages'][args['batch_id']] = args; return {'ok': True}
        if action == 'verify_operation': return {'verified': True}
        if action == 'commit_staged_operation': return self._apply(args)
        raise AssertionError(action)


class JavaScriptHost:
    """Exercise the real Worker dispatcher over private local JSON lines."""
    def __init__(self, root):
        import subprocess
        try:
            supported = subprocess.run(['node', '--input-type=module', '-e', "import {DatabaseSync} from 'node:sqlite'"], capture_output=True)
        except FileNotFoundError:
            raise unittest.SkipTest('Native Worker integration requires Node with node:sqlite') from None
        if supported.returncode:
            raise unittest.SkipTest('Native Worker integration requires Node with node:sqlite')
        project = Path(__file__).resolve().parents[1]
        script = root / 'native-fixture.mjs'
        script.write_text("import {readFileSync} from 'node:fs';\nimport {createInterface} from 'node:readline';\n"
            + "import {database} from " + json.dumps((project / 'sites/scripts/d1-local.mjs').as_uri()) + ";\n"
            + "import {mainStoreOperation} from " + json.dumps((project / 'sites/worker/main-store.js').as_uri()) + ";\n"
            + "const db=database(); db.sql.exec(readFileSync(" + json.dumps(str(project / 'sites/db/main-store-schema.sql')) + ",'utf8')); for await (const line of createInterface({input:process.stdin})){try{const {action,data}=JSON.parse(line);const result=await mainStoreOperation(db,action,data);process.stdout.write(JSON.stringify({result})+'\\n');}catch(error){process.stdout.write(JSON.stringify({error:error.message,stack:error.stack})+'\\n');}}\n")
        self.process = subprocess.Popen(['node', str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
        self.calls = []

    def __call__(self, action, args):
        self.calls.append((action, args))
        self.process.stdin.write(json.dumps({'action': action, 'data': args}) + '\n'); self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('JavaScript native fixture exited')
        reply = json.loads(line)
        if 'error' in reply:
            raise RuntimeError(action + ': ' + reply['error'])
        return reply['result']

    def close(self):
        self.process.stdin.close(); self.process.wait(timeout=10); self.process.stdout.close()



class D1RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'source'; self.source.mkdir()
        self.config = self.source / 'config.toml'
        self.config.write_text('state_dir="."\nprofile_file="profile.md"\nrelevance_threshold=0.7\ntimezone="Asia/Shanghai"\n[cloud_runtime]\nrole="staging"\n[[sources]]\nname="Synthetic"\ntype="crossref"\n')
        (self.source / 'profile.md').write_text('Human-in-the-loop interactive optimization and routing decisions')
        monitor = cloud_runtime._monitor(); db = monitor.db_open(self.source / 'papers.sqlite3')
        for index in range(4):
            monitor.upsert(db, monitor.Paper('doi:10.1/' + str(index), '10.1/' + str(index), 'Interactive optimization ' + str(index),
                'Abstract', 'Journal', '2026-10-15', '', [], 'Synthetic', publication_type_raw='journal-article', publication_type_source='crossref'), '2026-10-15')
        # More cold attempt history than the working set is permitted to fetch.
        for index in range(80):
            db.execute("INSERT INTO abstract_attempts(identity,provider,status,attempted_at) VALUES('doi:10.1/0','crossref',?,?)", ('failed' if index % 2 else 'found', f'2026-09-{index % 28 + 1:02d}T00:00:{index % 60:02d}Z'))
        db.execute("INSERT INTO cleanup_audits VALUES('old','applied',?,?, '{}',NULL,'2025-01-01',NULL)", ('archive://' + 'a' * 64, 'archive://' + 'b' * 64))
        db.commit(); db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.execute('PRAGMA journal_mode=DELETE'); db.close()
        self.trusted = self.root / 'trusted.sqlite3'; shutil.copyfile(self.source / 'papers.sqlite3', self.trusted)
        self.package = self.root / 'package'
        package.export_package(self.source / 'papers.sqlite3', self.package, files_root=self.source)
        self.host = NativeHost(self.source, self.source / 'papers.sqlite3', self.package)

    def open(self, name='work', operation='daily-synthetic'):
        return runtime.WorkingSet.open(runtime.D1Client(self.host), self.root / name, operation,
            trusted_schema=self.trusted, source_root=self.source)

    def results(self, prepared):
        from test_paper_monitor import MonitorTests
        queue = json.loads(Path(prepared['queue_path']).read_text())
        data = {'run_id': queue['run_id'], 'profile_hash': queue['profile_hash'], 'model': 'synthetic-host-model',
                'source_failures': queue['source_failures'],
                'results': [MonitorTests.structured_result(paper['identity']) for paper in queue['papers']]}
        for item, paper in zip(data['results'], queue['papers']):
            item['evidence_anchors'][0]['quote'] = paper['abstract'][:80]
        path = self.root / 'results.json'; path.write_text(json.dumps(data)); return path

    def test_full_pipeline_restarts_from_d1_without_library_or_cold_history(self):
        work = self.open(); prepared = work.prepare(no_collect=True)
        self.assertEqual(prepared['collection_report']['status'], 'not_exercised')
        with contextlib.closing(connect(work.db_path)) as db:
            self.assertLess(db.execute('SELECT COUNT(*) FROM abstract_attempts').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cleanup_audits').fetchone()[0], 0)
        shutil.rmtree(work.root)
        resumed = self.open('replacement').prepare(no_collect=True)
        self.assertEqual(prepared['run_id'], resumed['run_id'])
        self.assertEqual(prepared['queue_sha256'], resumed['queue_sha256'])
        self.assertFalse(resumed['collection_performed'])
        replacement = self.open('third')
        result = replacement.import_results(self.results(resumed)); self.assertEqual(result['imported'], 4)
        final = self.open('fourth'); self.assertEqual(final.prepare(no_collect=True)['phase'], 'imported')
        view = final.snapshot(); self.assertTrue(view['certificate']['complete_ui_projection'])
        complete = cloud_sync.snapshot_records(self.host.db, load_config(final.config_path))
        self.assertEqual(view['records'], complete)
        self.assertEqual(len([row for (name, _), row in self.host.rows.items() if name == 'abstract_attempts']), 80)
        self.assertEqual(len([row for (name, _), row in self.host.rows.items() if name == 'cleanup_audits']), 1)
        queried = {(args['table'], args.get('scope')) for action, args in self.host.calls if action == 'query_rows' and 'keys' not in args}
        self.assertIn(('abstract_attempts', 'abstract_retry'), queried)
        self.assertNotIn(('abstract_attempts', None), queried)
        self.assertNotIn(('cleanup_audits', None), queried)

    def test_rejects_partial_results_atomically(self):
        work = self.open(); prepared = work.prepare(no_collect=True); result = self.results(prepared)
        value = json.loads(result.read_text()); value['results'].pop(); result.write_text(json.dumps(value))
        before = self.host.revision
        with self.assertRaisesRegex(ValueError, 'complete queue'):
            work.import_results(result)
        self.assertEqual(self.host.revision, before)
        self.assertFalse(any(name == 'screenings' for name, _ in self.host.rows))

    def test_lost_commit_response_recovers_exact_frozen_queue(self):
        work = self.open(); work.prepare(no_collect=True)
        second = self.open('second'); prepared = second.prepare(no_collect=True)
        self.host.fail_after_commit = True
        with self.assertRaisesRegex(OSError, 'lost native'):
            second.import_results(self.results(prepared))
        fresh = self.open('fresh')
        self.assertEqual(fresh.prepare(no_collect=True)['phase'], 'imported')
        self.assertTrue(fresh.import_results(self.results(prepared))['resumed'])
        self.assertEqual(sum(name == 'screenings' for name, _ in self.host.rows), 4)

    def test_unfetched_existing_key_cannot_be_overwritten(self):
        work = self.open()
        with contextlib.closing(connect(work.db_path)) as db, db:
            db.execute("INSERT INTO cleanup_audits VALUES('old','applied','different','different','{}',NULL,'2026-01-01',NULL)")
        with self.assertRaisesRegex(runtime.CoverageError, 'unfetched existing key'):
            work.checkpoint('unsafe')
        original = next(row for (name, _), row in self.host.rows.items() if name == 'cleanup_audits')
        self.assertEqual(self.host._values(original)[2], 'archive://' + 'a' * 64)

    def test_existing_observation_insert_ignore_is_preserved(self):
        work = self.open()
        with contextlib.closing(connect(work.db_path)) as db, db:
            db.execute("INSERT OR IGNORE INTO observations VALUES('doi:10.1/0','Synthetic','2099-01-01')")
        self.assertTrue(work.checkpoint('noop')['noop'])
        row = next(row for (name, _), row in self.host.rows.items() if name == 'observations' and self.host._values(row)[0] == 'doi:10.1/0')
        self.assertEqual(self.host._values(row)[2], '2026-10-15')

    def test_retry_history_is_hydrated_after_candidate_eligibility_changes(self):
        work = self.open()
        with contextlib.closing(connect(work.db_path)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM abstract_attempts').fetchone()[0], 0)
            db.execute("UPDATE papers SET abstract='' WHERE identity='doi:10.1/0'")
        work.checkpoint('official-promoted-candidate')
        work.hydrate('abstract_attempts', scope='abstract_retry')
        with contextlib.closing(connect(work.db_path)) as partial, contextlib.closing(connect(self.host.db)) as full:
            expected = full.execute("SELECT MAX(attempted_at) FROM abstract_attempts WHERE identity='doi:10.1/0'").fetchone()[0]
            self.assertEqual(partial.execute("SELECT MAX(attempted_at) FROM abstract_attempts WHERE identity='doi:10.1/0'").fetchone()[0], expected)
            expected = full.execute("SELECT attempted_at FROM abstract_attempts WHERE identity='doi:10.1/0' AND provider='crossref' AND status IN ('failed','not_found') ORDER BY attempted_at DESC LIMIT 1").fetchone()[0]
            self.assertEqual(partial.execute("SELECT attempted_at FROM abstract_attempts WHERE identity='doi:10.1/0' AND provider='crossref' AND status IN ('failed','not_found') ORDER BY attempted_at DESC LIMIT 1").fetchone()[0], expected)
            self.assertLessEqual(partial.execute('SELECT COUNT(*) FROM abstract_attempts').fetchone()[0], 2)

    def test_exact_key_hydration_preserves_base_hash_without_fetching_history(self):
        work = self.open()
        record = next(row for (name, _), row in self.host.rows.items() if name == 'abstract_attempts')
        rows = work.hydrate('abstract_attempts', keys=[record['key_sha256']])
        self.assertEqual(rows, [record])
        identity = ('abstract_attempts', record['key_sha256'])
        self.assertEqual(work.base[identity]['record']['record_sha256'], record['record_sha256'])
        self.assertTrue(work.checkpoint('hydration-only')['noop'])
        with contextlib.closing(connect(work.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM abstract_attempts').fetchone()[0], 1)
        self.assertEqual(sum(name == 'abstract_attempts' for name, _ in self.host.rows), 80)

    def test_pdf_artifact_bodies_cannot_be_checkpointed(self):
        work = self.open(); before = self.host.revision
        (work.root / 'unauthorized.pdf').write_bytes(b'%PDF-1.4')
        with self.assertRaisesRegex(runtime.RuntimeErrorD1, 'PDF bodies'):
            work.checkpoint('artifact')
        self.assertEqual(self.host.revision, before)

    def test_missing_history_blocks_publication(self):
        work = self.open(); prepared = work.prepare(no_collect=True); work.import_results(self.results(prepared))
        with contextlib.closing(connect(work.db_path)) as db, db:
            db.execute('DELETE FROM recommendation_snapshots')
        with self.assertRaisesRegex(runtime.CoverageError, 'Historical recommendation'):
            work.snapshot()

    def _repackage(self):
        shutil.rmtree(self.package)
        with contextlib.closing(connect(self.source / 'papers.sqlite3')) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.execute('PRAGMA journal_mode=DELETE')
        package.export_package(self.source / 'papers.sqlite3', self.package, files_root=self.source)
        self.host = NativeHost(self.source, self.source / 'papers.sqlite3', self.package)

    def test_multiple_historical_days_profiles_and_rescreens_match_complete_view(self):
        for index in range(2):
            prepared = cloud_runtime.prepare(self.config, 'historical-' + str(index), no_collect=True, limit=2)
            cloud_runtime.import_results(self.config, prepared['operation_id'], self.results(prepared))
            stamp = f'2026-10-{10 + index:02d}T12:00:00+00:00'
            with contextlib.closing(connect(self.source / 'papers.sqlite3')) as db, db:
                db.execute('UPDATE agent_jobs SET imported_at=?,created_at=? WHERE run_id=?', (stamp, stamp, prepared['run_id']))
                db.execute('UPDATE pipeline_runs SET started_at=?,finished_at=? WHERE run_id=?', (stamp, stamp, prepared['run_id']))
        content = 'Confirmed second profile: human interactive decisions'
        profile_hash = hashlib.sha256(content.encode()).hexdigest()
        (self.source / 'profile.md').write_text(content)
        with contextlib.closing(connect(self.source / 'papers.sqlite3')) as db, db:
            db.execute("UPDATE profile_versions SET status='superseded'")
            db.execute("INSERT INTO profile_versions(profile_hash,content,status,source,created_at,confirmed_at) VALUES(?,?,'active','manual','2026-10-12','2026-10-12')", (profile_hash, content))
        monitor = cloud_runtime._monitor()
        _, prepared = cloud_runtime._invoke(monitor.agent_export, self.config, no_collect=True, rescreen=True, limit=1)
        cloud_runtime._invoke(monitor.agent_import, self.config, self.results(prepared), sync_after=False)
        with contextlib.closing(connect(self.source / 'papers.sqlite3')) as db, db:
            db.execute("UPDATE agent_jobs SET imported_at='2026-10-12T12:00:00+00:00',created_at='2026-10-12T12:00:00+00:00' WHERE run_id=?", (prepared['run_id'],))
            # Equal timestamps across changed profiles/models must still use
            # the physical-rowid tie break and keep every identity judged.
            db.execute("UPDATE screenings SET screened_at='2026-10-12T12:00:00+00:00'")
            db.execute("UPDATE screenings SET model='second-profile-model' WHERE run_id=?", (prepared['run_id'],))
            for index in range(30):
                db.execute("INSERT INTO source_runs(run_id,source,status,count,finished_at) VALUES(?, 'Synthetic','ok',1,?)", ('cold-' + str(index), f'2025-01-{index%28+1:02d}'))
                db.execute("INSERT INTO task_runs(task_id,task_type,status,created_at) VALUES(?,'abstract_enrichment','succeeded',?)", ('cold-' + str(index), f'2025-01-{index%28+1:02d}'))
        self._repackage()
        work = self.open()
        with contextlib.closing(connect(work.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM screenings').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0], 20)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_runs').fetchone()[0], 1)
        self.assertEqual(work.snapshot()['records'], cloud_sync.snapshot_records(self.host.db, load_config(work.config_path)))
        frozen = work.prepare(no_collect=True)
        self.assertEqual(frozen['candidates'], 0, 'Profile changes cannot authorize repeat judgments')
        work.import_results(self.results(frozen))
        self.assertEqual(sum(name == 'screenings' for name, _ in self.host.rows), 5)
        self.assertEqual(sum(name == 'recommendation_snapshots' for name, _ in self.host.rows), 5)

    def test_daily_collection_stages_are_durable_and_resume_without_recollection(self):
        from academic_radar import official
        from test_official import OfficialIssueTests
        config = self.config.read_text().replace('name="Synthetic"', 'name="Management Science"').replace('type="crossref"', 'type="crossref"\nissn="0025-1909"')
        self.config.write_text(config); self._repackage()
        work = self.open(); monitor = cloud_runtime._monitor(); order = []
        paper = monitor.Paper('doi:10.1287/api', '10.1287/api', 'Interactive optimization API', 'Abstract',
            'Management Science', '2026-10-01', '', [], 'Management Science', publication_type_raw='journal-article', publication_type_source='crossref')
        def official_source(source, spec, as_of):
            order.append('official')
            result = OfficialIssueTests.metadata_package()['sources'][0]
            for issue in result['issues']:
                for item in issue['papers']:
                    item.update(abstract='Abstract original publisher evidence about interactive optimization. ' * 3,
                        abstract_evidence_type='publisher_deposited_metadata', abstract_evidence_url='https://api.crossref.org/works/' + item['doi'])
                    item.pop('abstract_unavailable_traceable'); item.pop('abstract_failure_reason')
            return result
        def api(*args): order.append('api'); return [paper]
        def enrich(*args, **kwargs):
            order.append('enrich'); return {'status':'succeeded','checked':0,'deferred':0,'budget_exhausted':False}
        with patch.object(cloud_runtime, '_monitor', return_value=monitor), patch.object(monitor, 'crossref_collect', side_effect=api), \
             patch.object(monitor, 'run_enrichment', side_effect=enrich), patch.object(monitor, 'enrich_missing_abstracts'), \
             patch.object(official, '_collect_print_metadata_source', side_effect=official_source), \
             patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected real network')):
            original_apply = self.host._apply
            def lose_api_checkpoint_response(arguments):
                result = original_apply(arguments)
                saved = cloud_runtime._read(self.host.db, 'daily-synthetic') or {}
                if saved.get('collection_progress', {}).get('phase') == 'api_completed':
                    raise OSError('lost durable API checkpoint response')
                return result
            sync = __import__('types').SimpleNamespace(pull_configured=lambda config: {'status':'unchanged'})
            with patch.object(self.host, '_apply', side_effect=lose_api_checkpoint_response):
                with self.assertRaisesRegex(OSError, 'durable API checkpoint'):
                    work.prepare(no_collect=False, sync_client=sync)
            self.assertEqual(order, ['api'])
            shutil.rmtree(work.root)
            work = self.open('after-durable-api')
            prepared = work.prepare(no_collect=False, sync_client=__import__('types').SimpleNamespace(pull_configured=lambda config: {'status':'unchanged'}))
            self.assertEqual(order, ['api', 'official', 'enrich'])
            self.assertEqual(prepared['collection_report']['status'], 'succeeded')
            self.assertTrue(prepared['collection_performed'], 'Official collection was newly performed after API recovery')
            resumed = self.open('daily-restart').prepare(no_collect=False, sync_client=__import__('types').SimpleNamespace(pull_configured=lambda config: {'status':'unchanged'}))
            self.assertTrue(resumed['resumed']); self.assertFalse(resumed['collection_performed'])
            self.assertEqual(order, ['api', 'official', 'enrich'])
        self.assertGreater(self.host.revision, 10)
        self.assertTrue(any(path.startswith('official/cloud-daily-synthetic/') for path in self.host.files))

    def test_owner_edits_and_cursors_survive_fresh_workspace(self):
        from academic_radar.profile_sync import apply_remote_profiles, reconcile_profile_file
        from test_profile_sync import ProfileSyncTests
        from types import SimpleNamespace
        # Seed the active profile before importing an owner-confirmed edit.
        work = self.open()
        monitor = cloud_runtime._monitor()
        with contextlib.closing(connect(work.db_path)) as db:
            monitor.confirmed_profile(db, work.root / 'profile.md')
        original = (work.root / 'profile.md').read_text()
        event = ProfileSyncTests.event(None, content='Owner-confirmed revised interests', base=original)
        def pull(config):
            with contextlib.closing(connect(work.db_path)) as db:
                cloud_sync.apply_remote_feedback(db, [{'seq':1,'identity':'doi:10.1/0','interest':'interested','reason':'Mechanism fit',
                    'favorite':1,'reading_status':'read_later','updated_at':'2026-10-15T13:00:00Z'}], 1, authoritative=True)
                apply_remote_profiles(db, [event], 1, load_config(config))
            reconcile_profile_file(work.db_path, work.root / 'profile.md')
            return {'status':'pulled','request_id':7}
        prepared = work.prepare(no_collect=True, sync_client=SimpleNamespace(pull_configured=pull))
        queue = json.loads(Path(prepared['queue_path']).read_text())
        self.assertEqual(queue['profile_hash'], event['profile_hash'])
        self.assertEqual(queue['threshold'], .85)
        self.assertEqual(queue['feedback_event_cursor'], 1)
        self.assertEqual(self.host.watermarks, {'feedback_cursor':1,'profile_cursor':1,'request_id':7})
        fresh = self.open('owner-edits-restart')
        self.assertEqual((fresh.root / 'profile.md').read_text(), event['content'])
        self.assertEqual(fresh.prepare(no_collect=True)['queue_sha256'], prepared['queue_sha256'])
        with contextlib.closing(connect(fresh.db_path)) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='cloud_profile_cursor'").fetchone()[0], '1')
            self.assertEqual(db.execute("SELECT reason FROM paper_feedback WHERE identity='doi:10.1/0'").fetchone()[0], 'Mechanism fit')
        self.assertEqual(sum(name == 'applied_cloud_profile_events' for name, _ in self.host.rows), 1)


    def test_more_than_500_mutations_commit_as_one_revision(self):
        work = self.open(); before = self.host.revision
        with contextlib.closing(connect(work.db_path)) as db, db:
            for index in range(650):
                db.execute("INSERT INTO meta(key,value) VALUES(?,?)", ('synthetic-control-' + str(index), str(index)))
        work.checkpoint('large-atomic-stage')
        self.assertEqual(self.host.revision, before + 1)
        pages = [args for action, args in self.host.calls if action == 'stage_operation']
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page['mutations']) + len(page['files']) <= 400 for page in pages))


    def _javascript_client(self):
        from types import SimpleNamespace
        host = JavaScriptHost(self.root); self.addCleanup(host.close)
        client = runtime.D1Client(host)
        manifest = json.loads((self.package / 'manifest.json').read_text())
        client.upload(SimpleNamespace(directory=self.package / 'objects',
            chunks={path.name: path.stat().st_size for path in (self.package / 'objects').iterdir()},
            objects={item['sha256']: item for item in manifest['objects']}))
        root_directory = self.root / 'manifest-objects'; root_directory.mkdir()
        root_objects = package._Objects(root_directory)
        root_objects.add_bytes((self.package / 'manifest.json').read_bytes())
        client.upload(root_objects)
        imported = host('begin_import', {'manifest': manifest, 'manifest_sha256': hashlib.sha256((self.package / 'manifest.json').read_bytes()).hexdigest()})
        for name in self.host.table:
            rows = [row for (table, _), row in self.host.rows.items() if table == name]
            for index in range(0, len(rows), 100):
                host('import_rows', {'import_id': imported['import_id'], 'operation_id': name + '-' + str(index), 'rows': rows[index:index+100]})
        for name in self.host.table:
            cursor = ''
            while True:
                verified = host('verify_import', {'import_id': imported['import_id'], 'table': name, 'expected_cursor': cursor})
                if verified['verified']: break
                cursor = verified['cursor']
        host('commit_import', {'import_id': imported['import_id'], 'expected_revision': 0, 'operation_id': 'fixture-import',
                              'watermarks': {'feedback_cursor': 0, 'profile_cursor': 0, 'request_id': 0}})
        return client, host

    def test_actual_javascript_worker_end_to_end(self):
        client, host = self._javascript_client()
        work = runtime.WorkingSet.open(client, self.root / 'js-work', 'js-daily', trusted_schema=self.trusted, source_root=self.source)
        prepared = work.prepare(no_collect=True)
        work.import_results(self.results(prepared))
        fresh = runtime.WorkingSet.open(runtime.D1Client(host), self.root / 'js-restart', 'js-daily', trusted_schema=self.trusted, source_root=self.source)
        self.assertEqual(fresh.prepare(no_collect=True)['phase'], 'imported')
        view = fresh.snapshot()
        status = json.loads(next(row['data'] for row in view['records'] if row['kind']=='meta' and row['key']=='ui:status'))
        self.assertEqual(status['history_artifacts'], {'unavailable_files': 2, 'local_restore_backups': 1})

    def test_actual_worker_daily_collection_official_and_enrichment(self):
        from types import SimpleNamespace
        from academic_radar import enrichment, official
        from test_official import OfficialIssueTests
        self.config.write_text(self.config.read_text().replace('name="Synthetic"', 'name="Management Science"')
            .replace('type="crossref"', 'type="crossref"\nissn="0025-1909"'))
        self._repackage(); client, host = self._javascript_client()
        work = runtime.WorkingSet.open(client, self.root / 'js-daily-work', 'js-collect',
            trusted_schema=self.trusted, source_root=self.source)
        monitor = cloud_runtime._monitor(); order = []
        paper = monitor.Paper('doi:10.1287/api', '10.1287/api', 'Interactive optimization API', '',
            'Management Science', '2026-10-01', '', [], 'Management Science',
            publication_type_raw='journal-article', publication_type_source='crossref')
        def api(*args):
            order.append('api'); return [paper]
        def publisher(*args):
            order.append('official')
            source = OfficialIssueTests.metadata_package()['sources'][0]
            for issue in source['issues']:
                for item in issue['papers']:
                    item.update(abstract='Original publisher evidence about interactive optimization. ' * 3,
                        abstract_evidence_type='publisher_deposited_metadata',
                        abstract_evidence_url='https://api.crossref.org/works/' + item['doi'])
                    item.pop('abstract_unavailable_traceable'); item.pop('abstract_failure_reason')
            return source
        def metadata(*args):
            order.append('enrich')
            return {'abstract':'Original metadata evidence about interactive optimization. ' * 3,
                'source_name':'crossref','source_url':'https://api.crossref.org/works/10.1287/api',
                'publication_type_raw':'journal-article','publication_type_source':'crossref',
                'evidence_type':'publisher_deposited_metadata'}
        with patch.object(cloud_runtime, '_monitor', return_value=monitor), \
             patch.object(monitor, 'crossref_collect', side_effect=api), patch.object(monitor, 'enrich_missing_abstracts'), \
             patch.object(official, '_collect_print_metadata_source', side_effect=publisher), \
             patch.object(enrichment, 'PROVIDERS', [('crossref', metadata)]), \
             patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected real network')):
            prepared = work.prepare(sync_client=SimpleNamespace(pull_configured=lambda config: {'status':'unchanged'}))
            self.assertEqual(order, ['api', 'official', 'enrich'])
            self.assertEqual(prepared['collection_report']['enrichment']['checked'], 1)
            work.import_results(self.results(prepared))
        fresh = runtime.WorkingSet.open(runtime.D1Client(host), self.root / 'js-daily-resume', 'js-collect',
            trusted_schema=self.trusted, source_root=self.source)
        self.assertTrue(fresh.prepare(sync_client=SimpleNamespace())['resumed'])
        self.assertTrue(fresh.snapshot()['certificate']['complete_ui_projection'])
        with contextlib.closing(connect(fresh.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM meta WHERE key='cloud_official_import:js-collect.official.0'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM task_runs WHERE task_type='abstract_enrichment'").fetchone()[0], 'succeeded')
        attempts = list(fresh.client.rows(fresh.head['revision'], 'abstract_attempts', filters={'identity':paper.identity}))
        self.assertEqual(len(attempts), 1)


    def test_wrong_trusted_schema_is_rejected(self):
        bad = self.root / 'wrong.sqlite3'
        with sqlite3.connect(bad) as db: db.execute('CREATE TABLE wrong(id INTEGER PRIMARY KEY)')
        with self.assertRaisesRegex(runtime.RuntimeErrorD1, 'trusted schema'):
            runtime.WorkingSet.open(runtime.D1Client(self.host), self.root / 'bad', 'daily', trusted_schema=bad)

    def test_remote_column_metadata_cannot_override_trusted_schema(self):
        paper_table = next(item for item in self.host.manifest['inventory']['tables'] if item['name'] == 'papers')
        paper_table['columns'] = list(reversed(paper_table['columns']))
        with self.assertRaisesRegex(runtime.RuntimeErrorD1, 'table metadata'):
            self.open()
        self.assertEqual(self.host.revision, 1)

    def test_global_rowid_allocator_avoids_omitted_history(self):
        work = self.open()
        with contextlib.closing(connect(work.db_path)) as db, db:
            db.execute("INSERT INTO abstract_attempts(identity,provider,status,attempted_at) VALUES('doi:10.1/0','crossref','found','2026-10-16')")
        work.checkpoint('enrichment')
        newest = max(delta.decode_scalar(row['rowid']) for (name, _), row in self.host.rows.items() if name == 'abstract_attempts')
        self.assertGreater(newest, 80)


from academic_radar.product import load_config
if __name__ == '__main__': unittest.main()

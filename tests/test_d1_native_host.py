"""Native JSON-line + real Worker acceptance. All data and identity are synthetic."""
import contextlib
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from academic_radar import d1_native_host as native, d1_package as package, d1_runtime
from academic_radar.native_host import JsonLineHost, HostProtocolError, RunnerError, SITE_TOOLS
from test_native_host import Pipe
import test_d1_runtime as fixture_module


class WorkerSite:
    def __init__(self, root, *, large_profile=False):
        try:
            result = subprocess.run(['node', '--input-type=module', '-e', "import {DatabaseSync} from 'node:sqlite'"], capture_output=True)
        except FileNotFoundError:
            raise unittest.SkipTest('Node node:sqlite is required') from None
        if result.returncode:
            raise unittest.SkipTest('Node node:sqlite is required')
        project = Path(__file__).resolve().parents[1]
        script = root / 'native-site-fixture.mjs'
        script.write_text("import {createInterface} from 'node:readline';\n"
            + 'import {database} from ' + json.dumps((project / 'sites/scripts/d1-local.mjs').as_uri()) + ';\n'
            + 'import worker from ' + json.dumps((project / 'sites/worker/index.js').as_uri()) + ';\n'
            + "const db=database(),env={DB:db,OWNER_USER_ID:'synthetic-owner'};\n"
            + ("db.sql.prepare('INSERT INTO cloud_profile_events VALUES(1,0,?,?,?,?,?,?,?,?)').run('a'.repeat(64),'b'.repeat(64),'{}','c'.repeat(64),'中文'.repeat(70000),'{}','synthetic large profile','2026-10-16');\n" if large_profile else '')
            + "process.stdout.write('ready\\n');\n"
            + "for await (const line of createInterface({input:process.stdin})){try{const {name,arguments:args}=JSON.parse(line);"
            + "const response=await worker.fetch(new Request('https://unit.invalid/mcp',{method:'POST',headers:{'Content-Type':'application/json','oai-authenticated-user-id':'synthetic-owner'},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}})}),env);"
            + "const value=await response.json();process.stdout.write(JSON.stringify(value)+'\\n');}catch(error){process.stdout.write(JSON.stringify({error:String(error)})+'\\n');}}db.sql.close();\n")
        # A file avoids stderr pipe backpressure and keeps successful tests quiet.
        self.stderr = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(['node', str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True)
        except Exception:
            self.stderr.close()
            raise
        self.calls = []
        if self.process.stdout.readline() != 'ready\n':
            raise self.failure('startup failed')

    def failure(self, reason):
        self.stop()
        self.stderr.seek(0, 2)
        self.stderr.seek(max(0, self.stderr.tell() - 8192))
        detail = self.stderr.read().decode('utf-8', errors='replace').strip()
        self.stderr.close()
        return AssertionError(
            f'Local Worker fixture {reason} (exit {self.process.returncode}). '
            'From the repository root, run: cd sites && npm ci && npm run build. '
            'This installs Worker dependencies and generates the UI required by these tests.\n'
            f'Worker stderr (last 8192 bytes):\n{detail or "(empty)"}')

    def dispatch(self, operation, arguments):
        if not operation.startswith('site.'):
            raise AssertionError('Library or non-Site capability requested')
        name = operation[5:]; self.calls.append((name, copy.deepcopy(arguments)))
        try:
            self.process.stdin.write(json.dumps({'name': name, 'arguments': arguments}) + '\n'); self.process.stdin.flush()
        except BrokenPipeError:
            raise self.failure('closed its input') from None
        line = self.process.stdout.readline()
        if not line:
            raise self.failure('closed its output before replying')
        value = json.loads(line)
        if 'error' in value or value['result']['isError']:
            raise AssertionError(json.dumps(value))
        return value['result']['structuredContent']

    def stop(self):
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass  # Never mask the original Worker startup/dispatch failure.
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait(timeout=10)
        self.process.stdout.close()

    def close(self):
        self.stop(); self.stderr.close()


class D1NativeWireTests(unittest.TestCase):
    def test_worker_startup_failure_reports_dependencies_and_stderr(self):
        popen = subprocess.Popen
        processes = []
        def failed_worker(_command, **kwargs):
            process = popen([sys.executable, '-c',
                "import sys; sys.stderr.write(\"ERR_MODULE_NOT_FOUND: Cannot find package 'nunjucks'\\n\"); sys.exit(1)"], **kwargs)
            processes.append((process, kwargs['stderr']))
            return process
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                patch.object(subprocess, 'Popen', side_effect=failed_worker):
            with self.assertRaisesRegex(AssertionError, 'Local Worker fixture startup failed') as error:
                WorkerSite(Path(temporary))
        self.assertIn('cd sites && npm ci && npm run build', str(error.exception))
        self.assertIn("ERR_MODULE_NOT_FOUND: Cannot find package 'nunjucks'", str(error.exception))
        process, stderr = processes[0]
        self.assertEqual(process.returncode, 1)
        self.assertTrue(process.stdin.closed); self.assertTrue(process.stdout.closed)
        self.assertTrue(stderr.closed)

    def test_exactly_four_capabilities_added(self):
        legacy = {'read_run_status', 'read_feedback', 'read_profile_events', 'read_profile', 'stage_snapshot',
                  'acknowledge_snapshot', 'read_checkpoint_head', 'commit_library_checkpoint'}
        self.assertEqual(SITE_TOOLS - legacy, {'read_main_store', 'write_main_store', 'publish_main_snapshot', 'read_site_backup'})

    def test_closed_actions_reject_swapped_capability_sql_or_unknown_fields_before_send(self):
        pipe = Pipe(lambda *_: {}); host = JsonLineHost(pipe, pipe)
        attempts = [('read_main_store', {'action': 'commit_operations', 'data': {}}),
                    ('write_main_store', {'action': 'head', 'data': {}}),
                    ('read_main_store', {'action': 'sql', 'data': {}}),
                    ('read_main_store', {'action': 'head', 'data': {'sql': 'DROP TABLE papers'}}),
                    ('read_site_backup', {'action': 'delete', 'data': {}})]
        for name, args in attempts:
            with self.assertRaises(ValueError): host.site_call(name, args)
        self.assertEqual(pipe.requests, [])

    def test_cli_path_cannot_use_legacy_ack_library_or_head(self):
        pipe = Pipe(lambda *_: {}); runner = native.D1NativeRunner(JsonLineHost(pipe, pipe))
        for name in ('acknowledge_snapshot', 'commit_library_checkpoint', 'read_checkpoint_head', 'library.create_archive'):
            with self.assertRaises(ValueError): runner.site_call(name, {})
        self.assertEqual(pipe.requests, [])

    def test_malformed_native_result_poison_channel(self):
        for value in ({'head': {'revision': True}}, {'content': 'fake success'}, {'isError': False}, {'head': {'revision': 1, 'import_id': 'a'*64, 'manifest_sha256': 'b'*64, 'watermarks': {}}}):
            pipe = Pipe(lambda *_: value); host = JsonLineHost(pipe, pipe)
            with self.assertRaises(HostProtocolError): host.site_call('read_main_store', {'action': 'head', 'data': {}})
            self.assertTrue(host.broken)

    def test_primary_activation_and_fence_flags_have_no_implicit_default(self):
        for argv in (['prepare', '--workspace', '/tmp/missing', '--operation', 'daily', '--no-collect'],
                     ['prepare', '--workspace', '/tmp/missing', '--operation', 'daily', '--role', 'backup', '--no-collect'],
                     ['publish-activate', '--workspace', '/tmp/missing', '--operation', 'daily', '--role', 'primary']):
            with self.assertRaises(RunnerError): native.parser().parse_args(argv)
        args = native.parser().parse_args(['publish', '--workspace', '/tmp/missing', '--operation', 'daily', '--role', 'primary'])
        self.assertEqual(args.command, 'publish'); self.assertIsNone(args.expected_site_generation)

    def test_adaptive_pages_respect_history_read_budget(self):
        rows = [{'table': 'pipeline_runs', 'values': [{'t': 'text', 'ref': f'{n:064x}', 'bytes': 2000}]} for n in range(47)]
        pages = list(native.bounded_import_pages(rows, {'pipeline_runs': ['details_json']}))
        self.assertEqual(list(map(len, pages)), [15, 15, 15, 2]); self.assertEqual(sum(pages, []), rows)

    def test_trusted_db_requires_explicit_matching_pin(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'schema.sqlite3'; path.write_bytes(b'not trusted')
            for digest in (None, 'a' * 64):
                args = SimpleNamespace(trusted_schema=path, trusted_schema_sha256=digest)
                with self.assertRaises(RunnerError):
                    with native.trusted_schema(args): pass

    def test_large_event_chunk_action_has_closed_bounded_fields(self):
        args = {'action': 'event_chunks', 'data': {'stream': 'profiles', 'seq': 1, 'offset': 262144, 'until': 1}}
        pipe = Pipe(lambda *_: {'stream': 'profiles', 'seq': 1, 'offset': 262144, 'until': 1,
                               'bytes': 300000, 'sha256': 'a'*64, 'base64': '', 'next': 300000, 'done': True})
        host = JsonLineHost(pipe, pipe)
        self.assertEqual(host.site_call('read_site_backup', args)['next'], 300000)
        self.assertEqual(len(pipe.requests), 1)
        for change in ({'stream': 'secrets'}, {'seq': 2}, {'offset': -1}, {'sql': 'arbitrary'}):
            with self.assertRaises(ValueError):
                host.site_call('read_site_backup', {**args, 'data': {**args['data'], **change}})
        self.assertEqual(len(pipe.requests), 1)

    def test_bundled_historical_schema_is_empty_and_reproducible_from_source(self):
        args = SimpleNamespace(trusted_schema=None, trusted_schema_sha256=None)
        hashes = []
        for _ in range(2):
            with native.trusted_schema(args, native.HISTORICAL_SCHEMA_SHA256) as path:
                with sqlite3.connect(path) as db:
                    schema, tables, _ = package._schema(db)
                    self.assertEqual(package.digest(schema), native.HISTORICAL_SCHEMA_SHA256)
                    for table in tables:
                        self.assertEqual(db.execute('SELECT count(*) FROM "'+table['name']+'"').fetchone()[0], 0)
                hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(hashes[0], hashes[1])
        with self.assertRaisesRegex(RunnerError, 'unsupported_trusted_schema_fingerprint'):
            with native.trusted_schema(args, '0'*64): pass

    def test_publication_rejects_incomplete_stale_or_tampered_certificate_without_staging(self):
        head = {'revision': 1, 'import_id': 'a'*64, 'manifest_sha256': 'a'*64,
                'watermarks': {'feedback_cursor': 0, 'profile_cursor': 0, 'request_id': 0}}
        records = [{'kind': 'private', 'key': 'profile', 'data': '{}', 'checksum': hashlib.sha256(b'{}').hexdigest()}]
        cert = {'format': d1_runtime.VERSION, 'revision': 1, 'operation_id': 'daily', 'complete_ui_projection': True,
                'full_database': False, 'history_preserved': True, 'record_count': 1,
                'records_sha256': package.digest(records), 'watermarks': head['watermarks']}
        for changed in ({'complete_ui_projection': False}, {'record_count': 2}, {'records_sha256': 'b'*64},
                        {'revision': 2}, {'watermarks': {**head['watermarks'], 'profile_cursor': 1}}):
            pipe = Pipe(lambda *_: {'head': head}); runner = native.D1NativeRunner(JsonLineHost(pipe, pipe))
            with self.assertRaises(RunnerError):
                runner.publish(SimpleNamespace(head=head), SimpleNamespace(operation='daily'), records, {**cert, **changed})
            self.assertFalse(any(req['operation'] == 'site.stage_snapshot' for req in pipe.requests))

    def test_head_advance_after_complete_staging_prevents_publication_and_ack(self):
        head = {'revision': 1, 'import_id': 'a'*64, 'manifest_sha256': 'a'*64,
                'watermarks': {'feedback_cursor': 0, 'profile_cursor': 0, 'request_id': 0}}
        baseline = copy.deepcopy(head)
        records = [{'kind': 'private', 'key': 'profile', 'data': '{}', 'checksum': hashlib.sha256(b'{}').hexdigest()}]
        cert = {'format': d1_runtime.VERSION, 'revision': 1, 'operation_id': 'daily', 'complete_ui_projection': True,
                'full_database': False, 'history_preserved': True, 'record_count': 1,
                'records_sha256': package.digest(records), 'watermarks': head['watermarks']}
        def dispatch(operation, args):
            if operation == 'site.read_main_store': return {'head': copy.deepcopy(head)}
            self.assertEqual(operation, 'site.stage_snapshot')
            if args['action'] == 'begin': return {'generation': args['data']['generation'], 'active': False}
            if args['action'] == 'chunk': return {'accepted': len(args['data']['records'])}
            head['revision'] = 2
            return {'generation': args['data']['generation'], 'count': 1, 'staged': True}
        pipe = Pipe(dispatch); runner = native.D1NativeRunner(JsonLineHost(pipe, pipe))
        args = SimpleNamespace(command='publish', operation='daily', expected_site_generation='none',
                               expected_published_revision=0, expected_writer_mode='local')
        with self.assertRaisesRegex(RunnerError, 'main_head_changed'):
            runner.publish(SimpleNamespace(head=baseline), args, records, cert)
        self.assertNotIn('site.publish_main_snapshot', [r['operation'] for r in pipe.requests])
        self.assertNotIn('site.acknowledge_snapshot', [r['operation'] for r in pipe.requests])


class D1TrustedSchemaWorkspaceTests(unittest.TestCase):
    """Private output-adjacent schema workspaces never depend on TMPDIR aliases."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='d1-schema-test-', dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        # Canonicalize only newly created test-owned scratch space; production
        # caller paths remain subject to the unchanged strict symlink checks.
        self.root = Path(self.temporary.name).resolve()
        self.safe = self.root/'safe'; self.safe.mkdir()
        self.source = self.root/'trusted.sqlite3'
        with contextlib.closing(sqlite3.connect(self.source)) as db:
            db.executescript("PRAGMA user_version=42; PRAGMA application_id=73; "
                "CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT); "
                "INSERT INTO sample VALUES(1,'synthetic source row');")
            self.source_schema = package._schema(db)[0]
        self.source_bytes = self.source.read_bytes()
        self.source_stamp = package._stamp(self.source)
        self.pinned = SimpleNamespace(trusted_schema=self.source,
            trusted_schema_sha256=hashlib.sha256(self.source_bytes).hexdigest())
        self.bundled = SimpleNamespace(trusted_schema=None, trusted_schema_sha256=None)
        self.cases = [('pinned', self.pinned, package.digest(self.source_schema)),
                      ('bundled', self.bundled, None),
                      ('historical', self.bundled, native.HISTORICAL_SCHEMA_SHA256)]

    def assert_source_unchanged(self):
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertEqual(package._stamp(self.source), self.source_stamp)
        for suffix in ('-wal', '-shm', '-journal'):
            self.assertFalse(Path(str(self.source)+suffix).exists())

    def test_validated_parent_is_private_fresh_and_cleaned_with_fingerprint_preserved(self):
        directories = set()
        fingerprints = {}
        for name, args, expected in self.cases:
            for repeat in range(2):
                with self.subTest(branch=name, repeat=repeat):
                    with native.trusted_schema(args, expected, temp_parent=self.safe) as path:
                        self.assertEqual(path.parent.parent, self.safe)
                        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                        self.assertNotIn(path.parent, directories); directories.add(path.parent)
                        self.assertEqual(package._path(path), path)
                        with contextlib.closing(sqlite3.connect(path)) as db:
                            schema, tables, _ = package._schema(db)
                            fingerprint = package.digest(schema)
                            self.assertEqual(fingerprint, fingerprints.setdefault(name, fingerprint))
                            if expected is not None:
                                self.assertEqual(fingerprint, expected)
                            for table in tables:
                                # Every branch yields empty independently rebuilt DDL.
                                self.assertEqual(db.execute('SELECT count(*) FROM "'+table['name']+'"').fetchone()[0], 0)
                    self.assertFalse(path.parent.exists())
                    self.assertEqual(list(self.safe.iterdir()), [])
        self.assert_source_unchanged()

    def test_current_schema_rebuilt_separately_from_retained_builder_shm(self):
        from academic_radar import storage,sqlite_delta
        original=storage.upgrade_database;builders=[];raw=b'synthetic retained shm'.ljust(32768,b'\0')
        def build(path):
            result=original(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
                self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchall(),[('delete',)])
            Path(str(path)+'-shm').write_bytes(raw)
            builders.append(path)
            return result
        with patch.object(storage,'upgrade_database',build):
            with native.trusted_schema(self.bundled,temp_parent=self.safe) as snapshot:
                self.assertEqual(len(builders),1)
                self.assertNotEqual(snapshot,builders[0])
                self.assertEqual(Path(str(builders[0])+'-shm').read_bytes(),raw)
                with self.assertRaisesRegex(sqlite_delta.DeltaError,'SQLite sidecars present'):
                    sqlite_delta.inspect_snapshot(builders[0])
                inventory=sqlite_delta.inspect_snapshot(snapshot)
                self.assertTrue(all(table['rows']==0 for table in inventory['tables']))
        self.assertEqual(list(self.safe.iterdir()),[])

    def test_invalid_parent_fails_closed_before_creating_any_temporary_directory(self):
        ordinary_file = self.root/'file'; ordinary_file.write_text('not a directory')
        alias = self.root/'alias'; alias.symlink_to(self.safe, target_is_directory=True)
        (self.safe/'child').mkdir()
        dangling = self.root/'dangling'; dangling.symlink_to(self.root/'missing', target_is_directory=True)
        invalid = [self.root/'missing', ordinary_file, alias, alias/'child', dangling,
                   self.safe/'..'/'safe']
        for name, args, expected in self.cases:
            for parent in invalid:
                with self.subTest(branch=name, parent=parent), \
                        patch.object(native.tempfile, 'TemporaryDirectory') as temporary:
                    with self.assertRaises((RunnerError, package.PackageError)):
                        with native.trusted_schema(args, expected, temp_parent=parent):
                            self.fail('Unsafe temporary parent was accepted')
                    temporary.assert_not_called()
        self.assert_source_unchanged()

    def test_private_directory_is_removed_when_consumer_or_schema_creation_fails(self):
        from academic_radar import storage
        for name, args, expected in self.cases:
            with self.subTest(branch=name, failure='consumer'):
                with self.assertRaisesRegex(RuntimeError, 'consumer failure'):
                    with native.trusted_schema(args, expected, temp_parent=self.safe) as path:
                        self.assertTrue(path.exists())
                        raise RuntimeError('consumer failure')
                self.assertFalse(path.parent.exists())
                self.assertEqual(list(self.safe.iterdir()), [])
            target = storage if name == 'bundled' else native
            attribute = 'upgrade_database' if name == 'bundled' else 'materialize_schema'
            original = getattr(target, attribute)
            def fail_after_creation(*values):
                original(*values)
                raise RuntimeError('schema creation failure')
            with self.subTest(branch=name, failure='creation'), \
                    patch.object(target, attribute, side_effect=fail_after_creation):
                with self.assertRaisesRegex(RuntimeError, 'schema creation failure'):
                    with native.trusted_schema(args, expected, temp_parent=self.safe):
                        self.fail('Failed schema was yielded')
                self.assertEqual(list(self.safe.iterdir()), [])
        self.assert_source_unchanged()

    def test_safe_temporary_parent_does_not_relax_trusted_source_symlink_rejection(self):
        linked_source = self.root/'linked.sqlite3'; linked_source.symlink_to(self.source)
        linked_parent = self.root/'linked-root'; linked_parent.symlink_to(self.root, target_is_directory=True)
        for source in (linked_source, linked_parent/self.source.name):
            args = SimpleNamespace(trusted_schema=source, trusted_schema_sha256=self.pinned.trusted_schema_sha256)
            with self.subTest(source=source), patch.object(native.tempfile, 'TemporaryDirectory') as temporary:
                with self.assertRaisesRegex(package.PackageError, 'Symlinks'):
                    with native.trusted_schema(args, temp_parent=self.safe):
                        self.fail('Linked source was accepted')
                temporary.assert_not_called()
        self.assert_source_unchanged()

    def test_schema_export_uses_output_parent_with_process_local_symlink_tmpdir(self):
        default_target = self.root/'default-temp'; default_target.mkdir()
        default_alias = self.root/'temp-alias'; default_alias.symlink_to(default_target, target_is_directory=True)
        pipe = Pipe(lambda *_: self.fail('Schema export must remain offline'))
        runner = native.D1NativeRunner(JsonLineHost(pipe, pipe))
        for name, source_args, expected in self.cases:
            output = self.safe/name
            argv = ['schema-export', '--output', str(output)]
            if source_args.trusted_schema is not None:
                argv += ['--trusted-schema', str(source_args.trusted_schema),
                         '--trusted-schema-sha256', source_args.trusted_schema_sha256]
            if expected is not None:
                argv += ['--schema-fingerprint', expected]
            with self.subTest(branch=name), patch.dict(os.environ, {'TMPDIR': str(default_alias)}), \
                    patch.object(tempfile, 'tempdir', None), \
                    patch.object(native, 'trusted_schema', wraps=native.trusted_schema) as schema:
                self.assertEqual(tempfile.gettempdir(), str(default_alias))
                args = native.parser().parse_args(argv)
                result = runner.execute(args)
                schema.assert_called_once_with(args, expected, temp_parent=self.safe)
                self.assertEqual(tempfile.gettempdir(), str(default_alias))
                path = Path(result['trusted_schema'])
                self.assertEqual(path, output/'schema.sqlite3')
                self.assertFalse(result['contains_source_rows'])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(output.stat().st_mode & 0o777, 0o700)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result['trusted_schema_sha256'])
                if expected is not None:
                    with contextlib.closing(sqlite3.connect(path)) as db:
                        self.assertEqual(package.digest(package._schema(db)[0]), expected)
                self.assertEqual(list(default_target.iterdir()), [])
        self.assertEqual({path.name for path in self.safe.iterdir()}, {case[0] for case in self.cases})
        self.assertEqual(pipe.requests, [])
        self.assert_source_unchanged()

    def test_output_based_commands_reject_unsafe_output_before_native_or_schema_work(self):
        alias = self.root/'output-alias'; alias.symlink_to(self.safe, target_is_directory=True)
        occupied = self.safe/'occupied'; occupied.mkdir()
        outputs = (alias/'new', occupied, self.root/'missing'/'new', self.safe/'..'/'new')
        pipe = Pipe(lambda *_: self.fail('Invalid output must fail before native requests'))
        runner = native.D1NativeRunner(JsonLineHost(pipe, pipe))
        for command in ('schema-export', 'backup-export', 'backup-restore'):
            for output in outputs:
                argv = [command, '--output', str(output)]
                if command == 'backup-restore':
                    argv += ['--backup', str(self.root/'backup'), '--backup-sha256', 'a'*64]
                with self.subTest(command=command, output=output), patch.object(native, 'trusted_schema') as schema:
                    with self.assertRaises(package.PackageError):
                        runner.execute(native.parser().parse_args(argv))
                    schema.assert_not_called()
        self.assertEqual(pipe.requests, [])


class D1NativeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.D1RuntimeTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.worker = WorkerSite(self.root); self.addCleanup(self.worker.close)
        pipe = Pipe(self.worker.dispatch); self.host = JsonLineHost(pipe, pipe)
        self.runner = native.D1NativeRunner(self.host)
        self.sha = hashlib.sha256((self.fixture.package / 'manifest.json').read_bytes()).hexdigest()

    def command(self, command, name='work', operation='native-daily', *extra):
        argv = [command, '--role', 'primary', '--operation', operation, '--workspace', str(self.root/name),
                '--source-root', str(self.fixture.source)]
        return native.parser().parse_args(argv + list(extra))

    def bootstrap(self):
        args = native.parser().parse_args(['bootstrap', '--role', 'primary', '--operation', 'initial-import',
            '--package', str(self.fixture.package), '--package-sha256', self.sha])
        result = self.runner.execute(args); self.assertEqual(result['head']['revision'], 1)
        return result

    def test_real_worker_bootstrap_restart_judgments_preview_activation_backup_and_restore(self):
        self.bootstrap()
        prepared = self.runner.execute(self.command('prepare', 'first', 'native-daily', '--no-collect'))
        self.assertEqual(prepared['phase'], 'awaiting_judgment'); self.assertFalse(prepared['collection_performed'])
        shutil.rmtree(self.root / 'first')
        resumed = self.runner.execute(self.command('prepare', 'second', 'native-daily', '--no-collect'))
        self.assertEqual(prepared['queue_sha256'], resumed['queue_sha256']); self.assertTrue(resumed['resumed'])
        result_path = self.fixture.results(resumed)
        imported = self.runner.execute(self.command('import-results', 'third', 'native-daily', '--results', str(result_path)))
        self.assertEqual(imported['imported'], 4)
        preview = self.runner.execute(self.command('publish', 'fourth'))
        self.assertFalse(preview['activated']); self.assertEqual(preview['status'], 'preview')
        fence = preview['fence']
        activated = self.runner.execute(self.command('publish-activate', 'fifth', 'native-daily', '--activate',
            '--expected-site-generation', fence['expected_generation'] or 'none',
            '--expected-published-revision', str(fence['expected_published_revision']),
            '--expected-writer-mode', fence['expected_writer_mode']))
        self.assertTrue(activated['activated']); self.assertTrue(activated['acknowledged'])
        self.assertEqual(activated['publication'], preview['publication'])
        backup = self.root/'backup'
        exported = self.runner.execute(native.parser().parse_args(['backup-export', '--output', str(backup)]))
        self.assertEqual(exported['revision'], activated['publication']['revision'])
        restored = self.runner.execute(native.parser().parse_args(['backup-restore', '--backup', str(backup),
            '--backup-sha256', exported['backup_sha256'], '--output', str(self.root/'restored'), '--exact-main-only']))
        self.assertTrue(restored['exact_main_state'])
        self.assertFalse(restored['operational_ready'])
        # This fixture intentionally contains archive:// cleanup tokens without
        # provenance. Forwarding source_root must reach the real relocation
        # validator and refuse to label these unverified references operational.
        with self.assertRaisesRegex(native.checkpoints.CheckpointError, 'archived reference'):
            self.runner.execute(native.parser().parse_args(['backup-restore', '--backup', str(backup),
                '--backup-sha256', exported['backup_sha256'], '--output', str(self.root/'operational'),
                '--source-root', str(self.fixture.source)]))
        self.assertFalse((self.root/'operational').exists())
        self.assertNotIn('acknowledge_snapshot', [name for name, _ in self.worker.calls])
        self.assertFalse(any('library' in name for name, _ in self.worker.calls))
        self.assertFalse(any(name in {'read_checkpoint_head', 'commit_library_checkpoint'} for name, _ in self.worker.calls))

    def test_invalid_bootstrap_pin_and_second_bootstrap_never_replace_main(self):
        args = native.parser().parse_args(['bootstrap', '--role', 'primary', '--operation', 'initial-import',
            '--package', str(self.fixture.package), '--package-sha256', '0'*64])
        with self.assertRaises(package.PackageError): self.runner.execute(args)
        self.assertEqual(self.worker.calls, [])
        self.bootstrap(); self.worker.calls.clear(); args.package_sha256 = self.sha
        with self.assertRaises(RunnerError): self.runner.execute(args)
        self.assertEqual([name for name, _ in self.worker.calls], ['read_main_store'])

    def test_manifest_file_staging_requires_progress_before_rows(self):
        actual = self.worker.dispatch
        def stalled(operation, args):
            result = actual(operation, args)
            if operation == 'site.write_main_store' and args['action'] == 'begin_import':
                return {**result, 'status': 'manifest', 'files_staged': 0, 'file_count': 2}
            return result
        pipe = Pipe(stalled); self.runner = native.D1NativeRunner(JsonLineHost(pipe, pipe))
        with self.assertRaisesRegex(ValueError, 'Manifest file staging did not progress'):
            self.bootstrap()
        actions = [args['action'] for name, args in self.worker.calls if name == 'write_main_store']
        self.assertEqual(actions.count('begin_import'), 2)
        self.assertNotIn('import_rows', actions); self.assertNotIn('commit_import', actions)

    def test_status_reads_exact_head_without_mutation(self):
        value = self.runner.execute(native.parser().parse_args(['status']))
        self.assertEqual(value['head']['revision'], 0)
        self.assertEqual(self.worker.calls, [('read_main_store', {'action': 'head', 'data': {}})])

    def test_resealed_constraint_violation_is_rejected_before_native_calls(self):
        root = self.fixture.package
        manifest = json.loads((root/'manifest.json').read_bytes())
        meta = next(table for table in manifest['tables'] if table['name'] == 'observations')
        table = next(table for table in manifest['inventory']['tables'] if table['name'] == 'observations')
        rows = [json.loads(line) for line in (root/meta['path']).read_text().splitlines()]
        row = rows[0]
        row['values'][table['columns'].index('identity')] = {'t': 'text', 'v': 'doi:missing-parent'}
        row['key'] = [row['values'][table['columns'].index(name)] for name in table['primary_key']]
        row['key_sha256'] = package.digest(row['key'])
        row['row_sha256'] = package.digest({'values': row['values'], 'rowid': row['rowid']})
        row['record_sha256'] = package.digest({key: value for key, value in row.items() if key != 'record_sha256'})
        rows.sort(key=lambda item: item['key_sha256']); chain = hashlib.sha256(b'').hexdigest()
        for record in rows:
            chain = hashlib.sha256((chain+'\n'+record['key_sha256']+'\n'+record['record_sha256']).encode()).hexdigest()
        raw = b''.join(package.canonical(record)+b'\n' for record in rows)
        (root/meta['path']).write_bytes(raw)
        meta.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), table_chain_sha256=chain)
        raw = package.canonical(manifest)+b'\n'; (root/'manifest.json').write_bytes(raw)
        package.verify_package(root)  # transport integrity really does pass
        args = native.parser().parse_args(['bootstrap', '--role', 'primary', '--operation', 'malformed-import',
            '--package', str(root), '--package-sha256', hashlib.sha256(raw).hexdigest()])
        with self.assertRaisesRegex(package.PackageError, 'foreign.key'):
            self.runner.execute(args)
        self.assertEqual(self.worker.calls, [])

    def test_preimport_site_backup_is_readonly_and_explicitly_not_main_backup(self):
        output = self.root/'site-only'
        result = self.runner.execute(native.parser().parse_args(['backup-site', '--output', str(output)]))
        self.assertEqual(result['main_revision'], 0); self.assertFalse(result['full_main_backup'])
        self.assertTrue(all(name == 'read_site_backup' for name, _ in self.worker.calls))
        self.worker.calls.clear()
        verified = self.runner.execute(native.parser().parse_args(['backup-site-verify', '--backup', str(output),
            '--backup-sha256', result['site_backup_sha256']]))
        self.assertEqual(verified['status'], 'verified'); self.assertEqual(self.worker.calls, [])

    def test_large_profile_event_roundtrips_through_real_mcp_and_native_validator(self):
        worker = WorkerSite(self.root, large_profile=True); self.addCleanup(worker.close)
        pipe = Pipe(worker.dispatch); runner = native.D1NativeRunner(JsonLineHost(pipe, pipe))
        result = runner.execute(native.parser().parse_args(['backup-site', '--output', str(self.root/'site-large')]))
        self.assertEqual(result['profile_events'], 1)
        actions = [args['action'] for name, args in worker.calls if name == 'read_site_backup']
        self.assertIn('event_chunks', actions)
        event = json.loads((self.root/'site-large/site/profiles.jsonl').read_text())
        self.assertEqual(event['content'], '中文'*70000)

    def test_schema_export_copies_only_pinned_ddl_without_historical_rows(self):
        output = self.root/'trusted-small'
        source = self.fixture.trusted
        result = self.runner.execute(native.parser().parse_args(['schema-export', '--trusted-schema', str(source),
            '--trusted-schema-sha256', hashlib.sha256(source.read_bytes()).hexdigest(), '--output', str(output)]))
        self.assertEqual(self.worker.calls, [])
        path = Path(result['trusted_schema'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result['trusted_schema_sha256'])
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM papers').fetchone()[0], 0)
            schema = package._schema(db)[0]
        with sqlite3.connect(source) as db:
            self.assertEqual(schema, package._schema(db)[0])

    def test_backup_restore_source_root_is_explicit_and_forwarded(self):
        for supplied in (False, True):
            argv = ['backup-restore', '--backup', str(self.root/'backup'), '--backup-sha256', 'a'*64,
                    '--output', str(self.root/'new-restore')]
            if supplied:
                argv += ['--source-root', str(self.fixture.source)]
            args = native.parser().parse_args(argv)
            expected = self.fixture.source if supplied else None
            self.assertEqual(args.source_root, expected)
            with patch.object(native.d1_backup, 'verify_backup', return_value={'main_manifest': {'schema_sha256': 'a'*64}}), \
                    patch.object(native, 'trusted_schema') as trusted, \
                    patch.object(native.d1_backup, 'restore_backup', return_value={'operational_ready': supplied}) as restore:
                trusted.return_value.__enter__.return_value = self.fixture.trusted
                self.runner.execute(args)
            self.assertEqual(restore.call_args.kwargs['source_root'], expected)
        self.assertEqual(self.worker.calls, [])

    def test_occupied_workspace_rejected_before_any_native_request(self):
        with self.assertRaises(package.PackageError):
            self.runner.execute(self.command('prepare', 'source', 'native-daily', '--no-collect'))
        self.assertEqual(self.worker.calls, [])

    def test_actual_cli_json_line_protocol_with_real_worker(self):
        self.bootstrap()
        project = Path(__file__).resolve().parents[1]
        args = [sys.executable, '-m', 'academic_radar.d1_native_host', 'prepare', '--role', 'primary',
                '--operation', 'pipe-daily', '--workspace', str(self.root/'pipe-work'), '--source-root', str(self.fixture.source), '--no-collect']
        process = subprocess.Popen(args, cwd=project, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            request_id = 0; session = None; final = None
            for line in process.stdout:
                message = json.loads(line)
                if message['type'] == 'result':
                    final = message; break
                self.assertEqual(message['request_id'], request_id + 1); request_id += 1
                if session is None: session = message['session_id']
                self.assertEqual(message['session_id'], session)
                result = self.worker.dispatch(message['operation'], message['arguments'])
                reply = {key: message[key] for key in ('protocol', 'session_id', 'request_id', 'operation')}
                reply.update(type='response', ok=True, result=result)
                process.stdin.write(json.dumps(reply) + '\n'); process.stdin.flush()
            process.stdin.close(); self.assertEqual(process.wait(timeout=10), 0, final)
            self.assertEqual(process.stderr.read(), '')
            self.assertTrue(final['ok']); self.assertEqual(final['result']['phase'], 'awaiting_judgment')
        finally:
            if process.poll() is None: process.kill(); process.wait()
            process.stdout.close(); process.stderr.close()


if __name__ == '__main__':
    unittest.main()

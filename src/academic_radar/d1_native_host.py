"""Credential-free D1 runner over the fixed native JSON-lines host protocol.

The approved parent supplies real Site tool calls. No HTTP, credentials, Library,
connector discovery, deployment, model API or scheduler is implemented here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import checkpoints, cloud_runtime, d1_backup, d1_package as package, d1_runtime
from .managed_site import ManagedSiteClient
from .native_host import (JsonLineHost, HostProtocolError, RunnerError, _Discard,
                          _Parser, _integer, _object, _read_json, _sha)
from .product import load_config, resolve_state

READ_ACTIONS = frozenset({'head', 'capacity', 'export_summary', 'read_manifest', 'query_rows',
    'export_rows', 'query_files', 'export_files', 'summary', 'read_object', 'read_chunk', 'read_object_chunks',
    'read_objects', 'read_chunks'})
WRITE_ACTIONS = frozenset({'put_chunk', 'put_object', 'verify_object', 'begin_import', 'import_rows',
    'verify_import', 'commit_import', 'begin_operation', 'stage_operation', 'verify_operation',
    'commit_staged_operation', 'commit_operations', 'put_chunks', 'put_objects', 'verify_objects'})
ALLOWED_TOOLS = frozenset({'read_main_store', 'write_main_store', 'publish_main_snapshot',
    'read_site_backup', 'read_run_status', 'read_feedback', 'read_profile_events', 'read_profile', 'stage_snapshot'})
HISTORICAL_SCHEMA_SHA256 = '2aa41a27d8d35870e9a1818db7337d071cf6885e62ab60c0654fcc804d8b7a63'
# Closed outer fields; the Worker also validates every domain value and row.
FIELDS = {
    'head': ((), ()), 'capacity': ((), ()),
    'export_summary': (('revision',), ()), 'read_manifest': (('revision',), ()),
    'summary': (('revision',), ('operation_id',)),
    'query_rows': (('revision', 'table'), ('after_key', 'limit', 'keys', 'filter', 'scope', 'operation_id', 'current_run_id')),
    'export_rows': (('revision', 'table'), ('after_key', 'limit')),
    'query_files': (('revision',), ('paths', 'prefix', 'after_path', 'limit')),
    'export_files': (('revision',), ('paths', 'prefix', 'after_path', 'limit')),
    'read_object': (('sha256',), ()), 'read_chunk': (('sha256',), ()),
    'read_object_chunks': (('revision', 'sha256'), ('after', 'limit')),
    'read_objects': (('sha256s',), ()), 'read_chunks': (('sha256s',), ()),
    'put_chunk': (('sha256', 'data_base64'), ()), 'put_object': (('object',), ()),
    'put_chunks': (('chunks',), ()), 'put_objects': (('objects',), ()), 'verify_objects': (('objects',), ()),
    'verify_object': (('sha256', 'expected_cursor'), ()),
    'begin_import': (('manifest_sha256',), ('manifest',)),
    'import_rows': (('import_id', 'operation_id', 'rows'), ()),
    'verify_import': (('import_id', 'table', 'expected_cursor'), ()),
    'commit_import': (('import_id', 'expected_revision', 'operation_id', 'watermarks'), ()),
    'begin_operation': (('operation_id', 'expected_revision', 'watermarks', 'mutation_count', 'file_count', 'chain_sha256'), ()),
    'stage_operation': (('operation_id', 'batch_id', 'mutations', 'files'), ()),
    'verify_operation': (('operation_id', 'expected_cursor'), ()),
    'commit_staged_operation': (('operation_id',), ()),
    'commit_operations': (('expected_revision', 'operation_id', 'mutations'), ('files', 'watermarks')),
}


def watermarks(value):
    _object(value, {'feedback_cursor', 'profile_cursor', 'request_id'})
    for item in value.values():
        _integer(item)
    return value


def main_head(value):
    _object(value, {'revision', 'import_id', 'manifest_sha256', 'watermarks'})
    _integer(value['revision']); watermarks(value['watermarks'])
    if value['revision']:
        _sha(value['import_id']); _sha(value['manifest_sha256'])
        if value['import_id'] != value['manifest_sha256']:
            raise ValueError('Main-store manifest identity differs')
    elif value['import_id'] is not None or value['manifest_sha256'] is not None:
        raise ValueError('Empty main-store head has an import')
    return value


def validate_arguments(name, args):
    if name in {'read_main_store', 'write_main_store'}:
        _object(args, {'action', 'data'})
        allowed = READ_ACTIONS if name == 'read_main_store' else WRITE_ACTIONS
        action, data = args['action'], args['data']
        if action not in allowed:
            raise ValueError('Unsupported closed main-store operation')
        _object(data, *FIELDS[action])
        for key in ('revision', 'expected_revision', 'mutation_count', 'file_count', 'limit', 'after'):
            if key in data:
                _integer(data[key])
        for key in ('sha256', 'manifest_sha256', 'import_id', 'chain_sha256'):
            if key in data:
                _sha(data[key])
        for key in ('operation_id', 'batch_id'):
            if key in data and (not isinstance(data[key], str) or not cloud_runtime.OPERATION.fullmatch(data[key])):
                raise ValueError('Invalid operation identifier')
        if 'watermarks' in data:
            watermarks(data['watermarks'])
        for key in ('rows', 'mutations', 'files', 'paths', 'keys'):
            if key in data and (not isinstance(data[key], list) or len(data[key]) > 500):
                raise ValueError('Invalid bounded main-store page')
        for key in ('objects', 'chunks', 'sha256s'):
            if key in data and (not isinstance(data[key], list) or not 1 <= len(data[key]) <= (200 if action in {'read_objects', 'read_chunks', 'verify_objects'} else 500)):
                raise ValueError('Invalid bounded object page')
        if 'sha256s' in data:
            for sha in data['sha256s']:
                _sha(sha)
        if action in {'import_rows', 'stage_operation'} and len(data.get('rows', data.get('mutations', []))) + len(data.get('files', [])) > 500:
            raise ValueError('Main-store page count exceeds limit')
        if len(package.canonical(data)) > 750_000:
            raise ValueError('Main-store payload exceeds native limit')
    elif name == 'publish_main_snapshot':
        _object(args, {'data'}); data = _object(args['data'], {'operation_id', 'revision', 'snapshot_generation',
            'expected_generation', 'expected_published_revision', 'expected_writer_mode', 'activate'})
        if not isinstance(data['operation_id'], str) or not cloud_runtime.OPERATION.fullmatch(data['operation_id']):
            raise ValueError('Invalid publication operation')
        if _integer(data['revision']) < 1:
            raise ValueError('Publication requires a committed revision')
        _sha(data['snapshot_generation']); _integer(data['expected_published_revision'])
        if data['expected_generation'] is not None:
            _sha(data['expected_generation'])
        if data['expected_writer_mode'] not in {'local', 'd1-main'} or type(data['activate']) is not bool:
            raise ValueError('Invalid publication fence')
    elif name == 'read_site_backup':
        _object(args, {'action', 'data'}); action, data = args['action'], args['data']
        if action == 'begin':
            _object(data, ())
        elif action == 'records':
            _object(data, {'generation'}, {'after'}); _sha(data['generation'])
            if 'after' in data:
                after = _object(data['after'], {'kind', 'key', 'offset'}); _integer(after['offset'])
                if not isinstance(after['kind'], str) or len(after['kind']) > 20 or not isinstance(after['key'], str) or len(after['key']) > 1000:
                    raise ValueError('Invalid backup cursor')
        elif action in {'feedback', 'profiles'}:
            _object(data, {'after', 'until'})
            if _integer(data['after']) > _integer(data['until']):
                raise ValueError('Invalid backup event bounds')
        elif action == 'event_chunks':
            _object(data, {'stream', 'seq', 'offset', 'until'})
            if (data['stream'] not in {'feedback', 'profiles'} or not 1 <= _integer(data['seq']) <= _integer(data['until'])
                    or _integer(data['offset']) > d1_backup.MAX_SITE_RECORD_BYTES):
                raise ValueError('Invalid chunked backup event bounds')
        else:
            raise ValueError('Unknown backup action')
    else:
        raise ValueError('Unknown D1 capability')


def validate_result(name, args, value):
    # JsonLineHost has already rejected text, envelopes and error payloads.
    if name in {'read_main_store', 'write_main_store'}:
        if 'head' in value:
            main_head(value['head'])
        if args['action'] == 'head' and set(value) != {'head'}:
            raise ValueError('Invalid head response')
        if value.get('ok') is False:
            raise ValueError('Main-store action was not accepted')
    elif name == 'publish_main_snapshot':
        _object(value, {'ok', 'publication'}, {'preview', 'active', 'idempotent'})
        publication = _object(value['publication'], {'revision', 'import_id', 'snapshot_generation', 'watermarks', 'operation_id'})
        watermarks(publication['watermarks']); _sha(publication['import_id'])
        data = args['data']
        if value['ok'] is not True or any(publication[key] != data[key] for key in ('revision', 'snapshot_generation', 'operation_id')):
            raise ValueError('Publication binding was not confirmed')
        if data['activate'] and value.get('active') is not True:
            raise ValueError('Publication is no longer active')
        if not data['activate'] and value.get('preview') is not True:
            raise ValueError('Publication preview was not confirmed')
    return value


def materialize_schema(schema, destination):
    """Materialize only caller-owned, already trusted DDL into an absent file."""
    if schema['encoding'] not in {'UTF-8', 'UTF-16le', 'UTF-16be'} or any(row[0] not in {'table', 'index'} for row in schema['objects']):
        raise ValueError('Unsupported trusted schema')
    if destination.exists():
        raise ValueError('Schema destination must be absent')
    with contextlib.closing(sqlite3.connect(destination)) as target:
        if target.execute('PRAGMA journal_mode=DELETE').fetchall() != [('delete',)]:
            raise ValueError('Trusted schema requires DELETE journal mode')
        target.execute("PRAGMA encoding='" + schema['encoding'] + "'")
        for kind in ('table', 'index'):
            for object_kind, name, _table, sql in schema['objects']:
                if object_kind == kind and sql and name != 'sqlite_sequence':
                    target.execute(sql)
        target.execute('PRAGMA user_version=' + str(int(schema['user_version'])))
        target.execute('PRAGMA application_id=' + str(int(schema['application_id'])))
        target.commit()
        actual, _, _ = package._schema(target)
        if package.digest(actual) != package.digest(schema):
            raise ValueError('Trusted schema materialization differs')


@contextlib.contextmanager
def trusted_schema(args, schema_sha256=None, *, temp_parent=None):
    """Never execute the remote manifest's SQL or implicitly trust its DB."""
    if temp_parent is not None:
        # Output-based commands already have a safe existing parent. Keep the
        # generated schema there rather than trusting a platform TMPDIR alias.
        temp_parent = package._path(temp_parent)
        if not temp_parent.is_dir():
            raise RunnerError('trusted_schema_temp_parent_must_be_existing_directory')
    if args.trusted_schema is not None:
        if args.trusted_schema_sha256 is None:
            raise RunnerError('trusted_schema_requires_explicit_sha256')
        path = package._path(args.trusted_schema)
        package._hash(args.trusted_schema_sha256)
        sha, _ = d1_backup._file_sha(path)
        if sha != args.trusted_schema_sha256:
            raise RunnerError('trusted_schema_sha256_mismatch')
        from .sqlite_delta import _sidecars
        _sidecars(path)
        before = package._stamp(path)
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as source:
            schema = {'objects': [list(row) for row in source.execute(
                'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name,tbl_name')],
                'encoding': source.execute('PRAGMA encoding').fetchone()[0],
                'user_version': source.execute('PRAGMA user_version').fetchone()[0],
                'application_id': source.execute('PRAGMA application_id').fetchone()[0]}
        # Recheck the pinned source before executing even trusted DDL. Keep only
        # its schema in a private empty file; never copy historical source rows.
        if package._stamp(path) != before or d1_backup._file_sha(path)[0] != args.trusted_schema_sha256:
            raise RunnerError('trusted_schema_changed')
        with tempfile.TemporaryDirectory(prefix='academic-d1-pinned-schema-', dir=temp_parent) as temporary:
            empty = Path(temporary) / 'schema.sqlite3'
            materialize_schema(schema, empty)
            yield empty
        return
    if args.trusted_schema_sha256 is not None:
        raise RunnerError('trusted_schema_path_required')
    with tempfile.TemporaryDirectory(prefix='academic-d1-trusted-schema-', dir=temp_parent) as temporary:
        # The migration builder can retain platform-owned WAL sidecars after
        # close. It is never the strict snapshot exported to a consumer.
        path = Path(temporary) / 'schema-builder.sqlite3'
        from .storage import upgrade_database
        upgrade_database(path)
        with contextlib.closing(sqlite3.connect(path)) as db:
            current, _, _ = package._schema(db)
        schema = current
        if schema_sha256 is not None and schema_sha256 != package.digest(current):
            # The remote/package hash selects an installed source definition;
            # it never supplies executable SQL. This reviewed historic DDL is
            # whitespace-equivalent to the migration-built schema.
            if schema_sha256 != HISTORICAL_SCHEMA_SHA256:
                raise RunnerError('unsupported_trusted_schema_fingerprint')
            schema = _read_json(Path(__file__).with_name('assets')/'d1-schema-v1.json')
            if package.digest(schema) != HISTORICAL_SCHEMA_SHA256:
                raise RunnerError('bundled_trusted_schema_hash_mismatch')
            normalize = lambda obj: obj[:3] + [''.join(obj[3].split()) if obj[3] else obj[3]]
            if ({key: value for key, value in schema.items() if key != 'objects'} !=
                    {key: value for key, value in current.items() if key != 'objects'}
                    or list(map(normalize, schema['objects'])) != list(map(normalize, current['objects']))):
                raise RunnerError('bundled_schema_requires_source_review')
        # Execute only independently trusted, fingerprint-checked DDL in a
        # fresh DELETE-mode file. Do not copy seed rows or remove sidecars.
        empty = Path(temporary)/'schema.sqlite3'
        materialize_schema(schema, empty)
        yield empty


class D1NativeRunner:
    def __init__(self, host):
        self.host = host
        self.client = d1_runtime.D1Client(self.main_call)
        self.site = ManagedSiteClient(self.site_call)

    def site_call(self, name, arguments):
        if name not in ALLOWED_TOOLS:
            raise ValueError('Legacy checkpoint/ACK and Library operations are forbidden in the D1 runner')
        return self.host.site_call(name, arguments)

    def main_call(self, action, data):
        if action not in READ_ACTIONS | WRITE_ACTIONS:
            raise ValueError('Unknown main-store operation')
        return self.site_call('read_main_store' if action in READ_ACTIONS else 'write_main_store', {'action': action, 'data': data})

    def backup_call(self, action, data):
        return self.site_call('read_site_backup', {'action': action, 'data': data})

    def _fence(self, args, head):
        explicit = [args.expected_site_generation, args.expected_published_revision, args.expected_writer_mode]
        if any(value is not None for value in explicit):
            if not all(value is not None for value in explicit):
                raise RunnerError('complete_publication_fence_required')
            generation = None if args.expected_site_generation == 'none' else _sha(args.expected_site_generation)
            return {'expected_generation': generation, 'expected_published_revision': _integer(args.expected_published_revision),
                    'expected_writer_mode': args.expected_writer_mode}
        if args.command == 'publish-activate':
            raise RunnerError('explicit_activation_fence_required')
        captured, _ = d1_backup._capture(self.backup_call)
        if captured['main_head'] != {key: head[key] for key in ('revision', 'import_id')}:
            raise RunnerError('main_head_changed')
        pointers = {entry['key']: entry['value'] for entry in captured['pointers']}
        if len(pointers) != len(captured['pointers']):
            raise ValueError('Duplicate captured Site pointer')
        generation = pointers.get('active'); published = json.loads(pointers.get('main_store_head', '{}'))
        if generation is not None:
            _sha(generation)
        mode = pointers.get('writer_mode', 'local')
        if mode not in {'local', 'd1-main'}:
            raise ValueError('Unknown Site writer mode')
        return {'expected_generation': generation, 'expected_published_revision': _integer(published.get('revision', 0)),
                'expected_writer_mode': mode}

    def publish(self, work, args, records, certificate):
        _object(certificate, {'format', 'revision', 'operation_id', 'complete_ui_projection', 'full_database',
                             'history_preserved', 'record_count', 'records_sha256', 'watermarks'})
        if (certificate['format'] != d1_runtime.VERSION or certificate['complete_ui_projection'] is not True
                or certificate['full_database'] is not False or certificate['history_preserved'] is not True
                or certificate['operation_id'] != args.operation or certificate['record_count'] != len(records)
                or certificate['records_sha256'] != package.digest(records) or not 1 <= len(records) <= 100_000):
            raise RunnerError('complete_ui_certificate_required')
        head = main_head(self.client.head())
        if head != work.head or certificate['revision'] != head['revision'] or watermarks(certificate['watermarks']) != head['watermarks']:
            raise RunnerError('publication_revision_or_watermarks_changed')
        fence = self._fence(args, head)
        ordered = sorted(records, key=lambda row: (row['kind'], row['key']))
        if ordered != records or len({(r['kind'], r['key']) for r in records}) != len(records):
            raise ValueError('Complete display records must be uniquely ordered')
        for row in records:
            _object(row, {'kind', 'key', 'data', 'checksum'})
            if not isinstance(row['data'], str) or hashlib.sha256(row['data'].encode()).hexdigest() != _sha(row['checksum']):
                raise ValueError('Display checksum mismatch')
        generation = hashlib.sha256('\n'.join(f"{r['kind']}:{r['key']}:{r['checksum']}" for r in records).encode()).hexdigest()
        self.site_call('stage_snapshot', {'action': 'begin', 'data': {'generation': generation, 'count': len(records)}})
        page = []; size = 0
        for row in records:
            amount = len(package.canonical(row))
            if amount > 700_000:
                raise ValueError('One display record exceeds the native staging limit')
            if page and (len(page) == 40 or size + amount > 700_000):
                self.site_call('stage_snapshot', {'action': 'chunk', 'data': {'generation': generation, 'records': page}})
                page = []; size = 0
            page.append(row); size += amount
        if page:
            self.site_call('stage_snapshot', {'action': 'chunk', 'data': {'generation': generation, 'records': page}})
        finished = self.site_call('stage_snapshot', {'action': 'finish', 'data': {'generation': generation, 'activate': False}})
        if finished['count'] != len(records):
            raise RunnerError('complete_snapshot_not_confirmed')
        if main_head(self.client.head()) != head:
            raise RunnerError('main_head_changed')
        activate = args.command == 'publish-activate'
        result = self.site_call('publish_main_snapshot', {'data': {'operation_id': args.operation,
            'revision': head['revision'], 'snapshot_generation': generation, **fence, 'activate': activate}})
        if result['publication'] != {'operation_id': args.operation, 'revision': head['revision'],
                'import_id': head['import_id'], 'snapshot_generation': generation, 'watermarks': head['watermarks']}:
            raise RunnerError('publication_binding_not_confirmed')
        # The Site publication transaction already ACKs exactly these watermarks.
        # A legacy ACK here could race a newer main-store publisher and is forbidden.
        return {'status': 'published' if activate else 'preview', 'operation_id': args.operation,
                'publication': result['publication'], 'acknowledged': activate, 'activated': activate, 'fence': fence,
                'complete_ui_projection': True, 'unattended_host_verified': False}

    def execute(self, args):
        command = args.command
        if command == 'status':
            return {'status': 'ready', 'head': main_head(self.main_call('head', {})['head']),
                    'unattended_host_verified': False}
        if command == 'schema-export':
            output = package._new_directory(args.output)
            with trusted_schema(args, args.schema_fingerprint, temp_parent=output.parent) as trusted, tempfile.TemporaryDirectory(prefix='.d1-schema-', dir=output.parent) as temporary:
                stage = Path(temporary)/'schema'; stage.mkdir(mode=0o700)
                destination = stage/'schema.sqlite3'; shutil.copyfile(trusted, destination); destination.chmod(0o600)
                sha, size = d1_backup._file_sha(destination)
                checkpoints._publish(stage, output)
            return {'status': 'exported', 'trusted_schema': str(output/'schema.sqlite3'),
                    'trusted_schema_sha256': sha, 'bytes': size, 'contains_source_rows': False}
        if command == 'backup-site':
            return {'status': 'exported', **d1_backup.export_site_backup(self.backup_call, args.output),
                    'output': str(args.output.absolute()), 'full_main_backup': False}
        if command == 'backup-site-verify':
            result = d1_backup.verify_site_backup(args.backup, expected_site_backup_sha256=args.backup_sha256)
            return {'status': 'verified', 'site_backup_sha256': result['site_backup_sha256'],
                    'main_revision': result['manifest']['main_revision'], 'full_main_backup': False}
        if command in {'prepare', 'import-results', 'publish', 'publish-preview', 'publish-activate', 'bootstrap'}:
            if args.role != 'primary' or not cloud_runtime.OPERATION.fullmatch(args.operation):
                raise RunnerError('explicit_primary_role_and_operation_required')
        if command in {'publish', 'publish-preview', 'publish-activate'}:
            fence = (args.expected_site_generation, args.expected_published_revision, args.expected_writer_mode)
            if any(value is not None for value in fence) and not all(value is not None for value in fence):
                raise RunnerError('complete_publication_fence_required')
            if command == 'publish-activate' and (not args.activate or any(value is None for value in
                    (args.expected_site_generation, args.expected_published_revision, args.expected_writer_mode))):
                raise RunnerError('explicit_activation_and_complete_fence_required')
            if args.expected_site_generation is not None and args.expected_site_generation != 'none':
                _sha(args.expected_site_generation)
            if args.expected_published_revision is not None:
                _integer(args.expected_published_revision)
        if command == 'bootstrap':
            return self.bootstrap(args)
        if command == 'backup-verify':
            verified = d1_backup.verify_backup(args.backup, expected_backup_sha256=args.backup_sha256)
            return {'status': 'verified', 'backup_sha256': verified['backup_sha256'], 'revision': verified['manifest']['revision']}
        if command in {'backup-export', 'backup-restore'}:
            output = package._new_directory(args.output)
            if command == 'backup-export':
                head = self.client.head()
                selected = self.main_call('read_manifest', {'revision': head['revision']})['manifest']['schema_sha256']
            else:
                selected = d1_backup.verify_backup(args.backup, expected_backup_sha256=args.backup_sha256)['main_manifest']['schema_sha256']
            with trusted_schema(args, selected, temp_parent=output.parent) as trusted:
                if command == 'backup-export':
                    return {'status': 'exported', **d1_backup.export_backup(self.main_call, self.backup_call, args.output,
                        trusted_schema_database=trusted), 'output': str(args.output.absolute())}
                return {'status': 'restored', **d1_backup.restore_backup(args.backup, args.output,
                    trusted_schema_database=trusted, expected_backup_sha256=args.backup_sha256,
                    apply_pending=not args.exact_main_only, profile_relative_path=args.profile_relative_path,
                    source_root=args.source_root)}
        package._new_directory(args.workspace)
        if command == 'prepare' and args.limit is not None and args.limit < 0:
            raise RunnerError('invalid_limit')
        if command == 'import-results':
            _read_json(args.results, 64 * 1024 * 1024)
        head = self.client.head()
        selected = self.main_call('read_manifest', {'revision': head['revision']})['manifest']['schema_sha256']
        with trusted_schema(args, selected) as trusted:
            work = d1_runtime.WorkingSet.open(self.client, args.workspace, args.operation,
                trusted_schema=trusted, source_root=args.source_root)
            config = load_config(work.config_path)
            if (config.get('cloud_runtime', {}).get('role') != 'primary'
                    or config.get('cloud_sync', {}).get('transport') != 'managed'
                    or config.get('cloud_sync', {}).get('enabled') is not False
                    or resolve_state(work.config_path, config) != work.root):
                raise RunnerError('explicit_managed_primary_configuration_required')
            if command == 'prepare':
                result = work.prepare(no_collect=not args.collect, limit=args.limit, sync_client=self.site)
                keys = ('operation_id', 'phase', 'run_id', 'queue_path', 'queue_sha256', 'candidates', 'resumed',
                        'revision', 'collection_performed', 'collection_report')
                return {key: result[key] for key in keys if key in result} | {'unattended_host_verified': False}
            if command == 'import-results':
                return work.import_results(args.results)
            return work.publish(lambda records, certificate: self.publish(work, args, records, certificate))

    def bootstrap(self, args):
        verified = package.verify_package(args.package, expected_manifest_sha256=args.package_sha256)
        root = package._path(args.package); before = package._tree(root); manifest = verified['manifest']
        # No raw SQLite snapshot or PDF files are part of this main-store upload.
        if any(item['path'].lower().endswith(('.pdf', '.sqlite3', '.sqlite', '.db')) for item in manifest['files']):
            raise RunnerError('bootstrap_contains_database_or_pdf_artifact')
        if len(package.canonical(manifest)) + 1 > 8 * 1024 * 1024:
            raise RunnerError('bootstrap_manifest_exceeds_worker_limit')
        # Wire checksums alone cannot prove SQLite constraints or the logical
        # inventory. This one-time full restore gate precedes every native call.
        with trusted_schema(args, manifest['schema_sha256']) as trusted, tempfile.TemporaryDirectory(prefix='academic-d1-bootstrap-proof-') as temporary:
            restored = package.restore_package(root, Path(temporary) / 'restored',
                trusted_schema_database=trusted, expected_manifest_sha256=args.package_sha256)
            if restored['logical_sha256'] != manifest['inventory']['logical_sha256']:
                raise RunnerError('bootstrap_logical_inventory_mismatch')
        head = main_head(self.main_call('head', {})['head'])
        if head['revision'] != 0:
            raise RunnerError('bootstrap_requires_empty_main_store')
        objects = {obj['sha256']: obj for obj in manifest['objects']}
        reader = package.ObjectReader(root, objects)
        columns = {table['name']: table['columns'] for table in manifest['inventory']['tables']}
        marks = {'feedback_cursor': 0, 'profile_cursor': 0, 'request_id': 0}
        mapping = {'cloud_feedback_cursor': 'feedback_cursor', 'cloud_profile_cursor': 'profile_cursor',
                   'd1_runtime_request_id': 'request_id'}
        if 'meta' in columns:
            for row in package.iter_rows(root, table_names=['meta']):
                values = dict(zip(columns['meta'], [reader.scalar(value) for value in row['values']]))
                if values['key'] in mapping:
                    raw = values['value']
                    if not isinstance(raw, str) or not raw.isdecimal():
                        raise ValueError('Invalid imported event watermark')
                    marks[mapping[values['key']]] = _integer(int(raw))
        import_id = verified['manifest_sha256']; begun = False; batch = 0
        def upload_objects(items):
            reply = self.main_call('put_objects', {'objects': items}).get('objects')
            expected = {obj['sha256'] for obj in items}
            if not isinstance(reply, list) or {entry.get('sha256') for entry in reply} != expected or len(reply) != len(items):
                raise ValueError('Object staging coverage differs')
            pending = {entry['sha256']: _integer(entry['cursor']) for entry in reply if entry.get('verified') is not True}
            while pending:
                group = list(pending)[:200]
                response = self.main_call('verify_objects', {'objects': [
                    {'sha256': sha, 'expected_cursor': pending[sha]} for sha in group]}).get('objects')
                if not isinstance(response, list) or not response:
                    raise ValueError('Object verification did not progress')
                seen = set()
                for entry in response:
                    sha = entry.get('sha256')
                    if sha not in group or sha in seen:
                        raise ValueError('Unexpected object verification result')
                    seen.add(sha)
                    cursor = _integer(entry['cursor'])
                    if entry.get('verified') is True:
                        del pending[sha]
                    elif cursor > pending[sha]:
                        pending[sha] = cursor
                    else:
                        raise ValueError('Object verification did not progress')
        def send_rows(rows):
            nonlocal batch
            # Pre-split by the Worker's expensive historical external-JSON limit.
            # No retry follows an uncertain write or poisoned native channel.
            for bounded in bounded_import_pages(rows, columns):
                payload = {'import_id': import_id, 'operation_id': 'bootstrap-page-' + str(batch) + '-' + package.digest(bounded), 'rows': bounded}
                result = self.main_call('import_rows', payload)
                if result.get('accepted') != len(bounded):
                    raise ValueError('Complete import page was not accepted')
                batch += 1
        for page in package.iter_native_pages(root, max_page_bytes=400_000, max_records=500):
            if page['kind'] == 'chunks':
                chunks = [{'sha256': chunk['sha256'], 'data_base64': chunk['base64']} for chunk in page['records']]
                response = self.main_call('put_chunks', {'chunks': chunks}).get('chunks')
                if not isinstance(response, list) or len(response) != len(chunks) or {item.get('sha256') for item in response} != {item['sha256'] for item in chunks}:
                    raise ValueError('Chunk staging coverage differs')
            elif page['kind'] in {'objects', 'manifest'}:
                upload_objects(page['records'])
                if page['kind'] == 'manifest':
                    staged_files = -1
                    while True:
                        result = self.main_call('begin_import', {'manifest_sha256': import_id})
                        if result.get('import_id') != import_id:
                            raise ValueError('Import identity differs')
                        if result.get('status') in {'staging', 'verifying', 'ready'}:
                            break
                        following = result.get('files_staged')
                        if (result.get('status') != 'manifest' or type(following) is not int
                                or not staged_files < following <= len(manifest['files'])
                                or result.get('file_count') != len(manifest['files'])):
                            raise ValueError('Manifest file staging did not progress')
                        staged_files = following
                    begun = True
            elif page['kind'] == 'rows' and begun:
                send_rows(page['records'])
            else:
                raise ValueError('Unexpected native package page order')
        for table in manifest['tables']:
            cursor = ''
            while True:
                result = self.main_call('verify_import', {'import_id': import_id, 'table': table['name'], 'expected_cursor': cursor})
                if result.get('verified') is True:
                    if result.get('checked_count') != table['rows']:
                        raise ValueError('Verified import row count differs')
                    break
                following = result.get('cursor')
                if not isinstance(following, str) or following <= cursor:
                    raise ValueError('Import verification did not progress')
                cursor = following
        if package._tree(root) != before or main_head(self.main_call('head', {})['head']) != head:
            raise RunnerError('bootstrap_package_or_head_changed')
        result = self.main_call('commit_import', {'import_id': import_id, 'expected_revision': 0,
            'operation_id': args.operation, 'watermarks': marks})
        expected = {'revision': 1, 'import_id': import_id, 'manifest_sha256': import_id, 'watermarks': marks}
        if result.get('ok') is not True or result.get('head') != expected:
            raise RunnerError('bootstrap_commit_not_confirmed')
        return {'status': 'imported', 'head': expected, 'site_activated': False, 'acknowledged': False,
                'rows': sum(table['rows'] for table in manifest['tables'])}


def bounded_import_pages(rows, columns):
    """Adapt page size to both bytes and the Worker's 15-reference read budget."""
    history = {'agent_jobs': {'queue_path', 'results_path'}, 'pipeline_runs': {'details_json'},
               'cleanup_audits': {'backup_path', 'report_path', 'before_json', 'after_json'}}
    page, refs, size = [], set(), 0
    for row in rows:
        used = {value['ref'] for column, value in zip(columns[row['table']], row['values'])
                if column in history.get(row['table'], ()) and value.get('t') == 'text' and 'ref' in value}
        amount = len(package.canonical(row)) + 1
        if len(used) > 15 or amount > 400_000:
            raise ValueError('One import row exceeds the native limit')
        if page and (len(page) >= 500 or size + amount > 400_000 or len(refs | used) > 15):
            yield page; page, refs, size = [], set(), 0
        page.append(row); refs |= used; size += amount
    if page:
        yield page


def parser():
    result = _Parser(description='D1 native runner. Requires an approved Site tool host on private stdin/stdout.')
    sub = result.add_subparsers(dest='command', required=True, parser_class=_Parser)
    for command in ('status', 'schema-export', 'prepare', 'import-results', 'publish', 'publish-preview', 'publish-activate',
                    'bootstrap', 'backup-site', 'backup-site-verify', 'backup-export', 'backup-verify', 'backup-restore'):
        item = sub.add_parser(command)
        if command in {'prepare', 'import-results', 'publish', 'publish-preview', 'publish-activate', 'bootstrap'}:
            item.add_argument('--role', choices=('primary',), required=True)
            item.add_argument('--operation', required=True)
        if command in {'prepare', 'import-results', 'publish', 'publish-preview', 'publish-activate'}:
            item.add_argument('--workspace', type=Path, required=True)
            item.add_argument('--source-root', type=Path)
        if command not in {'status', 'backup-verify', 'backup-site', 'backup-site-verify'}:
            item.add_argument('--trusted-schema', type=Path)
            item.add_argument('--trusted-schema-sha256')
        if command == 'schema-export':
            item.add_argument('--schema-fingerprint')
        if command == 'prepare':
            group = item.add_mutually_exclusive_group(required=True)
            group.add_argument('--collect', action='store_true')
            group.add_argument('--no-collect', action='store_true')
            item.add_argument('--limit', type=int)
        if command == 'import-results':
            item.add_argument('--results', type=Path, required=True)
        if command in {'publish', 'publish-preview', 'publish-activate'}:
            required = command == 'publish-activate'
            item.add_argument('--expected-site-generation', required=required, help='Exact current generation, or the literal none')
            item.add_argument('--expected-published-revision', type=int, required=required)
            item.add_argument('--expected-writer-mode', choices=('local', 'd1-main'), required=required)
            if required:
                item.add_argument('--activate', action='store_true', required=True)
        if command == 'bootstrap':
            item.add_argument('--package', type=Path, required=True)
            item.add_argument('--package-sha256', required=True)
        if command in {'schema-export', 'backup-site', 'backup-export', 'backup-restore'}:
            item.add_argument('--output', type=Path, required=True)
        if command in {'backup-site-verify', 'backup-verify', 'backup-restore'}:
            item.add_argument('--backup', type=Path, required=True)
            item.add_argument('--backup-sha256', required=True)
        if command == 'backup-restore':
            item.add_argument('--exact-main-only', action='store_true')
            item.add_argument('--profile-relative-path')
            item.add_argument('--source-root', type=Path)
    return result


def main(argv=None):
    host = JsonLineHost(sys.stdin.buffer, sys.stdout.buffer)
    try:
        args = parser().parse_args(argv)
        with contextlib.redirect_stdout(_Discard()), contextlib.redirect_stderr(_Discard()):
            result = D1NativeRunner(host).execute(args)
        host.finish(result=result)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        code = ('interrupted' if isinstance(error, KeyboardInterrupt) else error.code if isinstance(error, RunnerError)
                else 'native_host_failed' if isinstance(error, HostProtocolError) else 'runtime_busy'
                if isinstance(error, cloud_runtime.Busy) else 'operation_failed')
        try:
            host.finish(error={'code': code, 'message': 'Stopped safely. Inspect the exact host action and pinned D1 revision before retrying in a fresh workspace.'})
        except (OSError, ValueError):
            pass
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == '__main__':
    raise SystemExit(main())

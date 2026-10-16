"""Revision-pinned D1 working sets for the existing Python business pipeline.

The host supplies closed native-tool callbacks. There is no HTTP/token fallback,
Library dependency, model API, implicit re-screen, or inference that an omitted
historical row was deleted. See docs/d1-runtime.md for the coverage contract.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from . import checkpoints, cloud_runtime, d1_package as package, sqlite_delta as delta
from .product import load_config
from .storage import connect

VERSION = 'academic-radar-d1-working-set-v1'
COMPLETE_TABLES = (
    'papers', 'observations', 'paper_feedback', 'feedback_events', 'profile_versions',
    'profile_review_runs', 'recommendation_snapshots', 'official_issue_checks',
    'fulltext_files', 'source_health', 'sqlite_sequence', 'schema_migrations',
)
SCOPED_TABLES = {
    'screenings': 'latest_screenings', 'abstract_attempts': 'abstract_retry',
    'agent_jobs': 'ui_jobs', 'pipeline_runs': 'ui_runs',
    'source_runs': 'ui_source_runs', 'task_runs': 'ui_tasks',
    'meta': 'runtime_meta', 'run_papers': 'runtime_run_papers',
}
COLD_TABLES = frozenset({'cleanup_audits', 'notifications', 'applied_cloud_profile_events'})


class RuntimeErrorD1(RuntimeError):
    """A working set, durable commit, or coverage proof is not safe to use."""


class CoverageError(RuntimeErrorD1):
    """The available projection cannot safely replace a complete Site view."""


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _safe_relative(value):
    return package._relative(value)


class D1Client:
    """Native bridge: call(action, data) -> decoded, successful JSON object."""
    def __init__(self, call):
        if not callable(call):
            raise TypeError('A host-supplied native main-store callback is required')
        self._call = call
        self.cache = {}
        self.descriptors = {}
        self.chunk_cache = {}
        self.uploaded = set()
        self.uploaded_objects = set()

    def call(self, action, data):
        result = self._call(action, data)
        if (not isinstance(result, dict) or result.get('error') or result.get('isError')
                or 'structuredContent' in result or 'content' in result):
            raise RuntimeErrorD1('Host must return a decoded successful main-store result')
        return result

    def head(self):
        head = self.call('head', {})['head']
        if type(head.get('revision')) is not int or head['revision'] < 1:
            raise RuntimeErrorD1('A verified initial D1 import is required')
        return head

    def rows(self, revision, table, *, scope=None, operation_id=None, keys=None, filters=None):
        cursor = None
        seen = set()
        while True:
            data = {'revision': revision, 'table': table, 'limit': 500}
            if scope is not None:
                data.update(scope=scope, operation_id=operation_id,
                            current_run_id='collect-cloud-' + hashlib.sha256(operation_id.encode()).hexdigest())
            if keys is not None:
                data['keys'] = keys
            if filters is not None:
                data['filter'] = filters
            if cursor is not None:
                data['after_key'] = cursor
            result = self.call('query_rows', data)
            if result.get('revision') != revision or type(result.get('done')) is not bool:
                raise RuntimeErrorD1('Unpinned or incomplete row page')
            rows = result.get('rows')
            if not isinstance(rows, list) or len(rows) > 500:
                raise RuntimeErrorD1('Invalid bounded row page')
            for row in rows:
                key = row.get('key_sha256')
                if key in seen or row.get('table') != table:
                    raise RuntimeErrorD1('Duplicate row or unexpected table')
                seen.add(key)
                yield row
            if result['done']:
                if result.get('next_key') is not None:
                    raise RuntimeErrorD1('Terminal page has a continuation')
                return
            next_key = result.get('next_key')
            if not rows or next_key != rows[-1]['key_sha256'] or next_key == cursor:
                raise RuntimeErrorD1('Non-progressing row page')
            cursor = next_key

    def files(self, revision, paths):
        found, cursor = {}, ''
        while True:
            result = self.call('query_files', {'revision': revision, 'paths': sorted(set(paths)),
                                               'after_path': cursor, 'limit': 500})
            if result.get('revision') != revision or type(result.get('done')) is not bool:
                raise RuntimeErrorD1('Unpinned file lookup')
            files = result.get('files')
            if not isinstance(files, list) or any(item['path'] in found or item['path'] not in paths for item in files):
                raise RuntimeErrorD1('Invalid or duplicate exact file lookup')
            found.update({item['path']: item for item in files})
            if result['done']:
                return found
            following = result.get('next_path')
            if not files or following != files[-1]['path'] or following <= cursor:
                raise RuntimeErrorD1('File lookup did not progress')
            cursor = following

    def prefetch(self, hashes):
        """Batch object/chunk reads across a domain, retaining content-addressed reuse."""
        needed = sorted(set(hashes) - set(self.cache))
        for sha in needed:
            package._hash(sha)
        pending = [sha for sha in needed if sha not in self.descriptors]
        while pending:
            requested = pending[:200]
            descriptors = self.call('read_objects', {'sha256s': requested}).get('objects')
            if not isinstance(descriptors, list) or not descriptors:
                raise RuntimeErrorD1('Bulk object lookup did not progress')
            received = set()
            for descriptor in descriptors:
                sha = descriptor.get('sha256')
                if sha not in requested or sha in received or type(descriptor.get('bytes')) is not int:
                    raise RuntimeErrorD1('Invalid bulk object descriptor')
                if not 0 <= descriptor['bytes'] <= delta.MAX_SCALAR_BYTES * 4:
                    raise RuntimeErrorD1('Working-set object exceeds byte limit')
                if not isinstance(descriptor.get('chunks'), list):
                    raise RuntimeErrorD1('Invalid object chunks')
                total = 0
                for chunk in descriptor['chunks']:
                    package._hash(chunk['sha256'])
                    if type(chunk.get('bytes')) is not int or not 0 < chunk['bytes'] <= package.CHUNK_BYTES:
                        raise RuntimeErrorD1('Invalid bounded object chunk')
                    total += chunk['bytes']
                if total != descriptor['bytes']:
                    raise RuntimeErrorD1('Object descriptor length mismatch')
                self.descriptors[sha] = descriptor
                received.add(sha)
            pending = [sha for sha in pending if sha not in received]
        chunks = {chunk['sha256']: chunk['bytes'] for sha in needed for chunk in self.descriptors[sha]['chunks']}
        pending = sorted(set(chunks) - set(self.chunk_cache))
        while pending:
            requested = pending[:200]
            result = self.call('read_chunks', {'sha256s': requested}).get('chunks')
            if not isinstance(result, list) or not result:
                raise RuntimeErrorD1('Bulk chunk lookup did not progress')
            received = set()
            for chunk in result:
                sha = chunk.get('sha256')
                if sha not in requested or sha in received:
                    raise RuntimeErrorD1('Unexpected or repeated bulk chunk')
                raw = base64.b64decode(chunk['data_base64'], validate=True)
                if len(raw) != chunks[sha] or _hash(raw) != sha:
                    raise RuntimeErrorD1('Object chunk checksum mismatch')
                self.chunk_cache[sha] = raw
                self.uploaded.add(sha)
                received.add(sha)
            pending = [sha for sha in pending if sha not in received]
        for sha in needed:
            descriptor = self.descriptors[sha]
            raw = b''.join(self.chunk_cache[chunk['sha256']] for chunk in descriptor['chunks'])
            if len(raw) != descriptor['bytes'] or _hash(raw) != sha:
                raise RuntimeErrorD1('Object checksum mismatch')
            self.cache[sha] = raw
        # Object cache provides deduplication across pages; chunk cache need not
        # retain a second copy of every complete object in executor memory.
        self.chunk_cache.clear()

    def object_bytes(self, sha):
        self.prefetch([sha])
        return self.cache[sha]

    def scalar(self, value):
        if isinstance(value, dict) and 'ref' in value:
            if set(value) != {'t', 'ref', 'bytes'} or value['t'] not in {'text', 'blob'}:
                raise RuntimeErrorD1('Invalid external scalar')
            raw = self.object_bytes(value['ref'])
            if len(raw) != value['bytes']:
                raise RuntimeErrorD1('Scalar object length mismatch')
            return raw.decode('utf-8') if value['t'] == 'text' else raw
        return delta.decode_scalar(value)

    def commit(self, data):
        """Stage arbitrarily many bounded pages, then expose one atomic revision."""
        mutations = sorted(data['mutations'], key=lambda item: (
            item.get('table') or item['row']['table'],
            item.get('key_sha256') or item['row']['key_sha256']))
        files = sorted(data.get('files', []), key=lambda item: item['path'])
        chain = _hash(b'')
        items = [('mutation', item) for item in mutations] + [('file', item) for item in files]
        for kind, item in items:
            key = ('~file:' + item['path']) if kind == 'file' else (
                (item.get('table') or item['row']['table']) + ':' +
                (item.get('key_sha256') or item['row']['key_sha256']))
            chain = _hash((chain + '\n' + key + '\n' + package.digest(item)).encode('ascii'))
        begin = {key: data[key] for key in ('operation_id', 'expected_revision', 'watermarks')}
        begin.update(mutation_count=len(mutations), file_count=len(files), chain_sha256=chain)
        self.call('begin_operation', begin)
        pages, page, size = [], {'mutations': [], 'files': []}, 0
        for kind, item in items:
            encoded = len(package.canonical(item))
            if encoded > 700_000:
                raise RuntimeErrorD1('One normalized mutation exceeds native staging limit')
            if size + encoded > 700_000 or len(page['mutations']) + len(page['files']) >= 400:
                pages.append(page); page, size = {'mutations': [], 'files': []}, 0
            page['mutations' if kind == 'mutation' else 'files'].append(item)
            size += encoded
        if page['mutations'] or page['files']:
            pages.append(page)
        for index, page in enumerate(pages):
            self.call('stage_operation', {'operation_id': data['operation_id'], 'batch_id': 'page-' + str(index), **page})
        cursor = ''
        while True:
            result = self.call('verify_operation', {'operation_id': data['operation_id'], 'expected_cursor': cursor})
            if result.get('verified') is True:
                break
            following = result.get('cursor')
            if not isinstance(following, str) or following <= cursor:
                raise RuntimeErrorD1('Operation verification did not progress')
            cursor = following
        return self.call('commit_staged_operation', {'operation_id': data['operation_id']})

    @staticmethod
    def _pages(items, *, limit=500):
        page, size = [], 0
        for item in items:
            width = len(package.canonical(item)) + 2
            if width > 700_000:
                raise RuntimeErrorD1('A native bulk item exceeds the bounded request limit')
            if page and (size + width > 700_000 or len(page) >= limit):
                yield page
                page, size = [], 0
            page.append(item); size += width
        if page:
            yield page

    def upload(self, objects):
        """Batch new bytes/descriptors and bounded verification through native tools."""
        needed = [descriptor for sha, descriptor in objects.objects.items()
                  if sha not in self.uploaded_objects and sha not in self.descriptors]
        required_chunks = {part['sha256'] for descriptor in needed for part in descriptor['chunks']}
        chunks = ({'sha256': sha, 'data_base64': base64.b64encode((objects.directory / sha).read_bytes()).decode('ascii')}
                  for sha in sorted(required_chunks - self.uploaded))
        for page in self._pages(chunks):
            result = self.call('put_chunks', {'chunks': page}).get('chunks')
            expected = {item['sha256'] for item in page}
            if not isinstance(result, list) or {item.get('sha256') for item in result} != expected:
                raise RuntimeErrorD1('Bulk chunk upload was not fully confirmed')
            self.uploaded.update(expected)
        pending = {}
        for page in self._pages(needed):
            result = self.call('put_objects', {'objects': page}).get('objects')
            expected = {item['sha256'] for item in page}
            if not isinstance(result, list) or {item.get('sha256') for item in result} != expected:
                raise RuntimeErrorD1('Bulk object upload was not fully confirmed')
            for item in result:
                if item.get('verified') is True:
                    self.uploaded_objects.add(item['sha256'])
                elif type(item.get('cursor')) is int and item['cursor'] >= 0:
                    pending[item['sha256']] = item['cursor']
                else:
                    raise RuntimeErrorD1('Invalid object verification state')
        while pending:
            requested = dict(list(pending.items())[:200])
            result = self.call('verify_objects', {'objects': [
                {'sha256': sha, 'expected_cursor': cursor} for sha, cursor in requested.items()]}).get('objects')
            if not isinstance(result, list) or not result:
                raise RuntimeErrorD1('Bulk object verification did not progress')
            received = set()
            for item in result:
                sha = item.get('sha256')
                if sha not in requested or sha in received:
                    raise RuntimeErrorD1('Unexpected object verification result')
                received.add(sha)
                if item.get('verified') is True:
                    self.uploaded_objects.add(sha); del pending[sha]
                elif type(item.get('cursor')) is int and item['cursor'] > requested[sha]:
                    pending[sha] = item['cursor']
                else:
                    raise RuntimeErrorD1('Bulk object verification did not advance')


class WorkingSet:
    """Disposable SQLite projection, backed solely by a pinned D1 revision."""
    def __init__(self, client, root, operation_id, source_root=None):
        if not cloud_runtime.OPERATION.fullmatch(operation_id):
            raise ValueError('Invalid stable operation id')
        self.client, self.root, self.operation_id = client, Path(root).absolute(), operation_id
        self.source_root = str(Path(source_root).absolute()) if source_root else None
        self.db_path, self.config_path = self.root / 'papers.sqlite3', self.root / 'config.toml'
        self.base, self.file_base, self.coverage = {}, {}, {}
        self.manifest = self.head = self.summary = None
        self.tables, self.strategies, self.rowid_maxima = {}, {}, {}

    @classmethod
    def open(cls, client, root, operation_id, *, trusted_schema, source_root=None):
        result = cls(client, root, operation_id, source_root)
        if result.root.exists() or not result.root.parent.is_dir():
            raise ValueError('Working-set destination must be absent in an existing directory')
        package._path(result.root)
        result.head = client.head()
        revision = result.head['revision']
        reply = client.call('read_manifest', {'revision': revision})
        if reply.get('revision') != revision:
            raise RuntimeErrorD1('Manifest revision mismatch')
        result.manifest = reply['manifest']
        result.summary = client.call('summary', {'revision': revision, 'operation_id': operation_id})
        if result.summary.get('revision') != revision:
            raise RuntimeErrorD1('Working-set summary revision mismatch')
        result.rowid_maxima = {key: int(value) for key, value in result.summary['rowid_maxima'].items()}
        result.tables = {item['name']: {key: item[key] for key in ('name', 'columns', 'primary_key', 'rowid')}
                         for item in result.manifest['inventory']['tables']}
        result.strategies = {item['name']: item['key_kind'] for item in result.manifest['tables']}
        supported = set(COMPLETE_TABLES) | set(SCOPED_TABLES) | COLD_TABLES
        if set(result.tables) - supported:
            raise RuntimeErrorD1('Unknown schema tables require an explicit working-set policy')
        result.root.mkdir(mode=0o700)
        try:
            # Read only the independently trusted schema, never its row data
            # and never SQL from the remote manifest. This also avoids copying
            # a large historical SQLite file merely to erase it in scratch.
            trusted = package._path(Path(trusted_schema))
            delta._sidecars(trusted)
            before = package._stamp(trusted)
            with contextlib.closing(sqlite3.connect(trusted.as_uri() + '?mode=ro&immutable=1', uri=True)) as schema_db:
                schema = {'objects': [list(row) for row in schema_db.execute(
                    'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name,tbl_name')],
                    'user_version': schema_db.execute('PRAGMA user_version').fetchone()[0],
                    'application_id': schema_db.execute('PRAGMA application_id').fetchone()[0],
                    'encoding': schema_db.execute('PRAGMA encoding').fetchone()[0]}
            if package._stamp(trusted) != before or package.digest(schema) != result.manifest['schema_sha256']:
                raise RuntimeErrorD1('Independently trusted schema does not match D1')
            with contextlib.closing(sqlite3.connect(result.db_path)) as db:
                if schema['encoding'] not in {'UTF-8', 'UTF-16le', 'UTF-16be'}:
                    raise RuntimeErrorD1('Unsupported trusted schema encoding')
                db.execute("PRAGMA encoding='" + schema['encoding'] + "'")
                for kind in ('table', 'index'):
                    for object_kind, name, _table, sql in schema['objects']:
                        if object_kind == kind and sql and name != 'sqlite_sequence':
                            db.execute(sql)
                db.execute('PRAGMA user_version=' + str(int(schema['user_version'])))
                db.execute('PRAGMA application_id=' + str(int(schema['application_id'])))
                db.commit()
                actual, trusted_tables, _ = package._schema(db)
                if package.digest(actual) != result.manifest['schema_sha256']:
                    raise RuntimeErrorD1('Trusted schema materialization differs from D1')
                if {item['name']: item for item in trusted_tables} != result.tables:
                    raise RuntimeErrorD1('Remote table metadata differs from the trusted schema')
            selections = [(table, None, True) for table in COMPLETE_TABLES
                          if table in result.tables and table != 'sqlite_sequence']
            selections += [(table, scope, False) for table, scope in SCOPED_TABLES.items()
                           if table in result.tables]
            selected = [(table, scope, complete, list(client.rows(revision, table,
                scope=scope, operation_id=operation_id))) for table, scope, complete in selections]
            # Native round trips are expensive. Collect the bounded current/UI
            # envelopes first, then fetch their deduplicated objects together.
            client.prefetch({value['ref'] for _table, _scope, _complete, rows in selected
                             for row in rows for value in row.get('values', [])
                             if isinstance(value, dict) and 'ref' in value})
            for table, scope, complete, rows in selected:
                result._materialize(table, rows)
                result.coverage[table] = {'kind': 'complete' if complete else scope,
                                          'rows': len(rows), 'revision': revision}
            # Today's cards join the latest imported run's scores directly;
            # their evidence is not necessarily the globally latest score if
            # legacy timestamps were repaired or an explicit rescreen differed.
            with contextlib.closing(sqlite3.connect(result.db_path)) as db:
                latest_job = db.execute("SELECT run_id FROM agent_jobs WHERE status='imported' ORDER BY imported_at DESC LIMIT 1").fetchone()
            if latest_job:
                result.hydrate('screenings', filters={'run_id': latest_job[0]})
                result.hydrate('run_papers', filters={'run_id': latest_job[0]})
            if 'sqlite_sequence' in result.tables:
                with contextlib.closing(sqlite3.connect(result.db_path)) as db, db:
                    db.execute('DELETE FROM sqlite_sequence')
                result.hydrate('sqlite_sequence', complete=True)
            result._restore_runtime_files()
            result._write_coverage()
            return result
        except Exception:
            # Leave evidence available for inspection; this directory is not a
            # checkpoint and no API accepts it as an authoritative restore.
            raise

    def _map_path(self, value, to_local):
        if not isinstance(value, str) or not value or value.startswith('archive://'):
            return value
        if to_local:
            if value.startswith('state://'):
                return str(self.root / _safe_relative(value[8:]))
            if self.source_root and (value == self.source_root or value.startswith(self.source_root + '/')):
                relative = value[len(self.source_root):].lstrip('/')
                return str(self.root / _safe_relative(relative)) if relative else str(self.root)
        elif value == str(self.root) or value.startswith(str(self.root) + '/'):
            relative = value[len(str(self.root)):].lstrip('/')
            return 'state://' + _safe_relative(relative) if relative else 'state://'
        return value

    def _transform(self, values, table, to_local):
        transformed = list(values)
        for index, column in enumerate(table['columns']):
            value = transformed[index]
            if not isinstance(value, str) or not value:
                continue
            if column in checkpoints.PATH_FIELDS or column.endswith('_path'):
                transformed[index] = self._map_path(value, to_local)
            elif column.endswith('_json') or (table['name'] == 'meta' and column == 'value' and value.startswith('{')):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    continue
                mapped = checkpoints._transform_json(parsed, lambda path, _context=None: self._map_path(path, to_local))
                if mapped != parsed:
                    transformed[index] = cloud_runtime._canonical(mapped)
        return tuple(transformed)

    @staticmethod
    def _signature(values, rowid):
        return package.digest({'values': [delta.encode_scalar(value) for value in values], 'rowid': rowid})

    def hydrate(self, table_name, *, scope=None, keys=None, filters=None, complete=False):
        rows = list(self.client.rows(self.head['revision'], table_name, scope=scope,
                                    operation_id=self.operation_id, keys=keys, filters=filters))
        # Envelope pages are small and bounded. Aggregate their references before
        # reading objects so a fresh daily executor does not make one tiny native
        # request per page, nor download duplicate calibration snapshots.
        self.client.prefetch({value['ref'] for row in rows for value in row.get('values', [])
                              if isinstance(value, dict) and 'ref' in value})
        self._materialize(table_name, rows)
        if keys is None and filters is None:
            self.coverage[table_name] = {'kind': 'complete' if complete else scope,
                                         'rows': len(rows), 'revision': self.head['revision']}
        return rows

    def _materialize(self, table_name, rows):
        table = self.tables[table_name]
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('PRAGMA foreign_keys=OFF')
            for record in rows:
                identity = (table_name, record['key_sha256'])
                if identity in self.base:
                    if self.base[identity]['record']['record_sha256'] != record['record_sha256']:
                        raise RuntimeErrorD1('Same revision returned different row bytes')
                    continue
                values, rowid = package._validate_record(record, table, self.strategies[table_name], self.client)
                local = self._transform(values, table, True)
                fields = list(table['columns'])
                inserted = list(local)
                if table['rowid'] is not None:
                    fields.append(table['rowid']); inserted.append(rowid)
                db.execute('INSERT INTO ' + delta._quote(table_name) + '(' + ','.join(map(delta._quote, fields)) +
                           ') VALUES(' + ','.join('?' for _ in fields) + ')', inserted)
                self.base[identity] = {'record': record, 'signature': self._signature(local, rowid)}

    def _relative_local(self, value):
        mapped = self._map_path(value, True)
        path = Path(mapped)
        if not path.is_absolute():
            return _safe_relative(mapped)
        try:
            return _safe_relative(path.relative_to(self.root).as_posix())
        except ValueError:
            raise RuntimeErrorD1('Current operation references an unmapped file outside the working set') from None

    def _restore_files(self, paths, *, json_paths=()):
        paths = sorted(set(paths))
        for start in range(0, len(paths), 500):
            selected = paths[start:start + 500]
            entries = self.client.files(self.head['revision'], selected)
            if set(entries) != set(selected):
                raise RuntimeErrorD1('Required durable runtime artifact is missing')
            self.client.prefetch({entry['sha256'] for entry in entries.values()})
            for relative, entry in entries.items():
                relative = _safe_relative(relative)
                raw = self.client.object_bytes(entry['sha256'])
                if len(raw) != entry['bytes']:
                    raise RuntimeErrorD1('File object length mismatch')
                if relative in json_paths:
                    parsed = json.loads(raw)
                    mapped = checkpoints._transform_json(parsed, lambda value, _context=None: self._map_path(value, True))
                    if mapped != parsed:
                        raw = cloud_runtime._canonical(mapped).encode()
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_bytes(raw); path.chmod(0o600)
                self.file_base[relative] = {'entry': entry, 'local_sha256': _hash(raw)}

    def _restore_runtime_files(self):
        self._restore_files(['config.toml'])
        cfg = load_config(self.config_path)
        profile = self._relative_local(str(cfg.get('profile_file', 'research-profile.md')))
        self._restore_files([profile])
        cfg['state_dir'] = '.'
        cfg['profile_file'] = profile
        cfg['cloud_runtime'] = {**cfg.get('cloud_runtime', {}), 'role': 'primary'}
        # All authority stays with native callbacks; never revive legacy HTTP.
        cfg['cloud_sync'] = {**cfg.get('cloud_sync', {}), 'enabled': False, 'transport': 'managed'}
        self.config_path.write_text('\n'.join(checkpoints._toml(k) + ' = ' + checkpoints._toml(v) for k, v in cfg.items()) + '\n')
        self.file_base['config.toml']['local_sha256'] = _hash(self.config_path.read_bytes())
        paths = []
        saved = cloud_runtime._read(self.db_path, self.operation_id) or {}
        progress = saved.get('collection_progress') or {}
        official = progress.get('official') or {}
        if official.get('plan_file'):
            paths.append(official['plan_file'])
        for source in official.get('sources', []):
            paths.extend(source[key] for key in ('package_file', 'preview_file') if source.get(key))
        run = cloud_runtime._meta(self.db_path, 'cloud_runtime_export:' + self.operation_id)
        if run:
            job = cloud_runtime._job(self.db_path, run)
            paths.extend(self._relative_local(job[key]) for key in ('queue_path', 'results_path') if job.get(key))
        self._restore_files(paths, json_paths=paths)

    def _write_coverage(self):
        (self.root / '.d1-working-set.json').write_bytes(package.canonical({
            'format': VERSION, 'revision': self.head['revision'], 'operation_id': self.operation_id,
            'partial': True, 'coverage': self.coverage, 'not_a_full_backup': True}))

    def _scan(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            for name, table in self.tables.items():
                for values, rowid in package._rows(db, table, self.strategies[name]):
                    key = package.digest(package._key(values, rowid, table, self.strategies[name]))
                    yield (name, key), values, rowid

    def _watermarks(self):
        previous = self.head.get('watermarks', {})
        return {field: int(cloud_runtime._meta(self.db_path, key) or previous.get(field, 0)) for field, key in (
            ('feedback_cursor', 'cloud_feedback_cursor'), ('profile_cursor', 'cloud_profile_cursor'),
            ('request_id', 'd1_runtime_request_id'))}

    def checkpoint(self, phase):
        """Commit a complete stage atomically, with row/file base hashes and CAS."""
        changes, present = [], set()
        for identity, values, rowid in self._scan():
            present.add(identity)
            prior = self.base.get(identity)
            if prior is None or prior['signature'] != self._signature(values, rowid):
                changes.append([identity, values, rowid])
        # A new local key may exist in an omitted D1 subset. Never overwrite it
        # by treating absence in this projection as authoritative absence.
        for name in sorted({identity[0] for identity, _, _ in changes if identity not in self.base}):
            keys = [identity[1] for identity, _, _ in changes if identity[0] == name and identity not in self.base]
            for start in range(0, len(keys), 200):
                found = list(self.client.rows(self.head['revision'], name, keys=keys[start:start + 200]))
                if found:
                    raise CoverageError('An unfetched existing key was modified; hydrate it before repeating the business operation: ' + name)
        # Allocate hidden rowids above the global maximum, rather than the
        # maximum of a truncated history. INTEGER PK aliases use sqlite_sequence.
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            for item in changes:
                identity, values, rowid = item
                table = self.tables[identity[0]]
                prior = self.base.get(identity)
                prior_rowid = delta.decode_scalar(prior['record']['rowid']) if prior and prior['record']['rowid'] else None
                alias = len(table['primary_key']) == 1 and table['primary_key'][0] in table['columns'] and (
                    next(row[2] for row in db.execute('PRAGMA table_info(' + delta._quote(identity[0]) + ')')
                         if row[1] == table['primary_key'][0]).upper() == 'INTEGER')
                if rowid is not None and not alias and (prior is None or prior_rowid != rowid):
                    highest = db.execute('SELECT COALESCE(MAX(' + table['rowid'] + '),0) FROM ' + delta._quote(identity[0])).fetchone()[0]
                    allocated = max(self.rowid_maxima.get(identity[0], 0), highest) + 1
                    if allocated >= 2 ** 63:
                        raise RuntimeErrorD1('Rowid allocator exhausted')
                    db.execute('UPDATE ' + delta._quote(identity[0]) + ' SET ' + table['rowid'] + '=? WHERE ' + table['rowid'] + '=?', (allocated, rowid))
                    self.rowid_maxima[identity[0]] = allocated
                    item[2] = allocated
                    if self.strategies[identity[0]] == 'rowid':
                        item[0] = (identity[0], package.digest(package._key(values, allocated, table, 'rowid')))
        with tempfile.TemporaryDirectory(prefix='d1-objects-') as temporary:
            objects = package._Objects(Path(temporary))
            mutations, updated = [], {}
            for identity, values, rowid in changes:
                table = self.tables[identity[0]]
                record = package._record(self._transform(values, table, False), rowid, table,
                                         self.strategies[identity[0]], objects, 1024)
                prior = self.base.get(identity)
                mutations.append({'row': record, 'expected_record_sha256': prior['record']['record_sha256'] if prior else None})
                updated[identity] = {'record': record, 'signature': self._signature(values, rowid)}
            for identity in sorted(set(self.base) - present):
                prior_row = self.base[identity]['record']
                if identity[0] != 'meta' or prior_row['key'][0].get('v') not in {'cloud_profile_file_pending', 'cloud_profile_file_base'}:
                    raise RuntimeErrorD1('Deletion is outside the daily pipeline mutation contract')
                mutations.append({'table': identity[0], 'key_sha256': identity[1], 'delete': True,
                                  'expected_record_sha256': self.base[identity]['record']['record_sha256']})
            files, file_updated = [], {}
            for path in sorted(self.root.rglob('*')):
                if path.is_symlink():
                    raise RuntimeErrorD1('Symlink in working set')
                if not path.is_file():
                    continue
                relative = path.relative_to(self.root).as_posix()
                if relative == 'config.toml' or relative.startswith('.') or relative.startswith('papers.sqlite3'):
                    continue
                if path.suffix.lower() == '.pdf':
                    raise RuntimeErrorD1('PDF bodies are outside the daily runtime artifact contract')
                raw = path.read_bytes()
                prior = self.file_base.get(relative)
                if prior and prior['local_sha256'] == _hash(raw):
                    continue
                local_sha = _hash(raw)
                if path.suffix == '.json':
                    parsed = json.loads(raw)
                    mapped = checkpoints._transform_json(parsed, lambda value, _context=None: self._map_path(value, False))
                    raw = cloud_runtime._canonical(mapped).encode()
                sha, size = objects.add_bytes(raw)
                entry = {'path': relative, 'mode': 0o600, 'bytes': size, 'sha256': sha}
                files.append({**entry, 'expected_sha256': prior['entry']['sha256'] if prior else None})
                file_updated[relative] = {'entry': entry, 'local_sha256': local_sha}
            watermarks = self._watermarks()
            if not mutations and not files:
                return {'head': self.head, 'noop': True}
            for identity, _, _ in changes:
                if identity[0] in {'cleanup_audits', 'notifications', 'schema_migrations'}:
                    raise RuntimeErrorD1('Mutation is outside the daily pipeline table contract')
            self.client.upload(objects)
            data = {'expected_revision': self.head['revision'], 'mutations': mutations,
                    'files': files, 'watermarks': watermarks}
            stable = package.digest(data)
            data['operation_id'] = ('runtime-' + self.operation_id)[:45] + '-' + stable
            result = self.client.commit(data)
            head = result.get('head')
            expected_revision = self.head['revision'] if result.get('noop') is True else self.head['revision'] + 1
            if not isinstance(head, dict) or head.get('revision') != expected_revision:
                raise RuntimeErrorD1('Atomic D1 stage commit was not confirmed')
            self.head = head
            for identity in set(self.base) - present:
                del self.base[identity]
            self.base.update(updated)
            self.file_base.update(file_updated)
            self._write_coverage()
            return result

    def prepare(self, *, no_collect=False, limit=None, sync_client=None):
        """Collect, verify official issues, enrich, then durably freeze one queue."""
        if not no_collect and sync_client is None:
            raise RuntimeErrorD1('Daily collection requires the native owner-edit synchronization callback')
        with cloud_runtime._lock(self.root):
            saved = cloud_runtime._read(self.db_path, self.operation_id)
            run = cloud_runtime._meta(self.db_path, 'cloud_runtime_export:' + self.operation_id)
            if run:
                job = cloud_runtime._job(self.db_path, run)
                queue_path, queue, digest = cloud_runtime._verified_queue(self.root, job, (saved or {}).get('queue_sha256'))
                if job['status'] not in {'exported', 'imported'}:
                    raise RuntimeErrorD1('Existing operation was abandoned or rejected')
                return {**(saved or {}), 'phase': 'imported' if job['status'] == 'imported' else 'awaiting_judgment',
                        'run_id': run, 'queue_path': str(queue_path), 'queue_sha256': digest,
                        'resumed': True, 'collection_performed': False, 'revision': self.head['revision']}
            mode = 'offline' if no_collect else 'daily'
            if saved and saved.get('collection_mode') != mode:
                raise RuntimeErrorD1('Cannot change collection mode during recovery')
            with contextlib.closing(connect(self.db_path)) as db:
                if db.execute("SELECT 1 FROM agent_jobs WHERE status='exported'").fetchone():
                    raise cloud_runtime.Busy('Another durable frozen queue is awaiting judgments')
                if db.execute("SELECT 1 FROM task_runs WHERE status='running'").fetchone():
                    raise cloud_runtime.Busy('A prior running enrichment requires recovery before continuing')
                for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'cloud_runtime_operation:%'"):
                    if row['key'] != 'cloud_runtime_operation:' + self.operation_id and json.loads(row['value']).get('phase') in {'preparing', 'awaiting_judgment'}:
                        raise cloud_runtime.Busy('Another durable operation is still in progress')
            if sync_client is not None:
                pulled = sync_client.pull_configured(self.config_path)
                if pulled.get('status') not in {'pulled', 'succeeded', 'unchanged', 'disabled'}:
                    raise RuntimeErrorD1('Owner edits did not finish pulling')
                if pulled.get('request_id') is not None:
                    with contextlib.closing(connect(self.db_path)) as db, db:
                        db.execute("INSERT INTO meta(key,value) VALUES('d1_runtime_request_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(pulled['request_id']),))
            saved = cloud_runtime._save(self.db_path, self.operation_id, {**(saved or {}), 'phase': 'preparing',
                'collection_mode': mode, 'expected_head': {'revision': self.head['revision']}})
            self.checkpoint('preparing')
            monitor = cloud_runtime._monitor()
            from .cloud_collection import offline_report, run as collect
            if no_collect:
                saved = cloud_runtime._save(self.db_path, self.operation_id, {**saved, 'collection_report': offline_report(),
                                                                             'collection_performed': False})
                batch = None
            else:
                def collection_checkpoint(updated):
                    self.checkpoint('collection')
                    if updated['collection_progress']['phase'] == 'enrichment_running':
                        # API/official evidence can make an existing paper a new
                        # enrichment candidate. Refresh its retry/order history
                        # only after those changes reach the authoritative head.
                        self.hydrate('abstract_attempts', scope='abstract_retry')
                saved = collect(self.config_path, self.operation_id, saved, monitor,
                                checkpoint=collection_checkpoint)
                if saved['collection_progress']['phase'] != 'completed':
                    raise RuntimeErrorD1('Collection stages are not complete')
                batch = saved['collection_progress']['api']['run_id']
            _, summary = cloud_runtime._invoke(monitor.agent_export, self.config_path, no_collect=True,
                batch_run=batch, limit=limit, operation_id=self.operation_id, collection_report=saved['collection_report'])
            job = cloud_runtime._job(self.db_path, summary['run_id'])
            path, queue, digest = cloud_runtime._verified_queue(self.root, job)
            saved = cloud_runtime._save(self.db_path, self.operation_id, {**saved, 'phase': 'awaiting_judgment',
                'run_id': summary['run_id'], 'queue_sha256': digest, 'candidates': len(queue['papers'])})
            self.checkpoint('awaiting_judgment')
            return {**saved, 'queue_path': str(path), 'resumed': False, 'revision': self.head['revision']}

    def import_results(self, results_path):
        # The existing importer validates every result before one SQLite txn.
        # Its D1 representation and files likewise become visible in one CAS.
        result = cloud_runtime.import_results(self.config_path, self.operation_id, Path(results_path))
        self.checkpoint('imported')
        return {**result, 'revision': self.head['revision']}

    def snapshot(self):
        """Return a complete certified UI projection; never activate from a subset."""
        required = set(COMPLETE_TABLES) | set(SCOPED_TABLES)
        if not required <= set(self.coverage):
            raise CoverageError('Missing renderer dependency coverage')
        with contextlib.closing(connect(self.db_path)) as db:
            missing = db.execute("""SELECT aj.run_id FROM agent_jobs aj LEFT JOIN pipeline_runs pr ON pr.run_id=aj.run_id
                WHERE aj.status='imported' AND (pr.run_id IS NULL OR pr.relevant_count>
                (SELECT COUNT(*) FROM recommendation_snapshots rs WHERE rs.run_id=aj.run_id)
                OR EXISTS(SELECT 1 FROM recommendation_snapshots rs WHERE rs.run_id=aj.run_id
                          AND rs.evidence_source='unavailable')) LIMIT 1""").fetchone()
            if missing:
                raise CoverageError('Historical recommendation evidence requires explicit recovery: ' + missing[0])
        from .cloud_sync import snapshot_records
        records = snapshot_records(self.db_path, load_config(self.config_path))
        archive = self.summary.get('history_artifact_counts')
        if not isinstance(archive, dict) or set(archive) != {'unavailable_files', 'local_restore_backups'}:
            raise CoverageError('Missing authoritative historical artifact summary')
        for record in records:
            if record['kind'] == 'meta' and record['key'] == 'ui:status':
                value = json.loads(record['data'])
                value['history_artifacts'] = archive
                record['data'] = cloud_runtime._canonical(value)
                record['checksum'] = _hash(record['data'].encode())
        certificate = {'format': VERSION, 'revision': self.head['revision'], 'operation_id': self.operation_id,
                       'complete_ui_projection': True, 'full_database': False,
                       'history_preserved': True, 'record_count': len(records),
                       'records_sha256': package.digest(records), 'watermarks': self._watermarks()}
        return {'records': records, 'certificate': certificate}

    def publish(self, publish_callback):
        """Host must atomically bind these records to this exact D1 revision."""
        if not callable(publish_callback):
            raise TypeError('A host-owned revision-fenced publication callback is required')
        saved = cloud_runtime._read(self.db_path, self.operation_id) or {}
        if saved.get('phase') != 'imported':
            raise RuntimeErrorD1('Import the entire frozen queue before publication')
        self.checkpoint('before_publication')
        view = self.snapshot()
        return publish_callback(view['records'], view['certificate'])

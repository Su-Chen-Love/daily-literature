"""Read-only native-callback backup of pinned D1 rows, files and Site history.

No HTTP, credentials, Library, remote mutation, or live-head fallback exists.
Exact main-state recovery and derived pending-event replay are separate outputs.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import checkpoints, d1_package as package, sqlite_delta as delta

FORMAT = 'academic-radar-d1-complete-backup'
VERSION = 1
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SITE_RECORD_BYTES = 16 * 1024 * 1024


class BackupError(ValueError):
    """A complete, revision-pinned backup or replay cannot be proved."""


def _fail(message):
    raise BackupError(message)


def _call(callback, action, data):
    result = callback(action, data)
    if type(result) is not dict or result.get('error') or result.get('isError') or 'structuredContent' in result or 'content' in result:
        _fail('Native callback must return decoded successful JSON')
    if len(package.canonical(result)) > MAX_RESPONSE_BYTES:
        _fail('Native callback response exceeds byte limit')
    return result


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _file_sha(path):
    before = package._stamp(path)
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2]:
            _fail('Backup member changed before opening')
        while True:
            raw = handle.read(1024 * 1024)
            if not raw:
                break
            digest.update(raw)
    if package._stamp(path) != before:
        _fail('Backup member changed while hashing')
    return digest.hexdigest(), before[2]


def _write_json(path, value):
    package._write(path, package.canonical(value) + b'\n')


def _metadata(table):
    return {key: table[key] for key in ('name', 'columns', 'primary_key', 'rowid')}


def _expanded(values, rowid):
    return package.digest({'values': [delta.encode_scalar(v) for v in values],
                           'rowid': None if rowid is None else delta.encode_scalar(rowid)})


class _Objects(package.ObjectReader):
    def __init__(self, directory, callback):
        (directory / 'objects').mkdir(mode=0o700)
        super().__init__(directory, {})
        self.callback = callback
        self.chunk_bytes = 0

    def prefetch(self, hashes):
        missing={package._hash(value) for value in hashes}-set(self.objects)
        descriptors={}
        while missing:
            requested=set(sorted(missing)[:200])
            reply=_call(self.callback,'read_objects',{'sha256s':sorted(requested)})
            items=reply.get('objects')
            if type(items) is not list or not items:_fail('Non-progressing object descriptor batch')
            returned=set()
            for descriptor in items:
                package._check_object(descriptor,('sha256','bytes','chunks'))
                sha=package._hash(descriptor['sha256'])
                if sha not in requested or sha in returned or type(descriptor['chunks']) is not list:
                    _fail('Unexpected or duplicate object descriptor')
                returned.add(sha);package._integer(descriptor['bytes'])
                total=0
                for index,chunk in enumerate(descriptor['chunks']):
                    package._check_object(chunk,('sha256','bytes'));package._hash(chunk['sha256'])
                    size=package._integer(chunk['bytes'],package.CHUNK_BYTES)
                    if size==0 or (index<len(descriptor['chunks'])-1 and size!=package.CHUNK_BYTES):
                        _fail('Noncanonical object chunk boundaries')
                    total+=size
                if total!=descriptor['bytes']:_fail('Whole object size mismatch')
                descriptors[sha]=descriptor
            missing-=returned
        chunks={}
        for descriptor in descriptors.values():
            for chunk in descriptor['chunks']:
                if chunk['sha256'] in chunks and chunks[chunk['sha256']]!=chunk['bytes']:
                    _fail('Conflicting chunk sizes')
                if not (self.package/'objects'/chunk['sha256']).exists():
                    chunks[chunk['sha256']]=chunk['bytes']
        while chunks:
            requested=set(sorted(chunks)[:200])
            reply=_call(self.callback,'read_chunks',{'sha256s':sorted(requested)})
            items=reply.get('chunks')
            if type(items) is not list or not items:_fail('Non-progressing object chunk batch')
            returned=set()
            for entry in items:
                sha=package._hash(entry.get('sha256'))
                if sha not in requested or sha in returned or entry.get('bytes')!=chunks[sha]:
                    _fail('Unexpected, duplicate, or wrong-sized object chunk')
                raw=base64.b64decode(entry['data_base64'],validate=True)
                if len(raw)!=chunks[sha] or _sha(raw)!=sha:_fail('Chunk SHA-256/size mismatch')
                package._write(self.package/'objects'/sha,raw)
                self.chunk_bytes+=len(raw)
                if self.chunk_bytes>package.MAX_TOTAL_BYTES:_fail('Backup object byte limit exceeded')
                returned.add(sha)
            for sha in returned:del chunks[sha]
        for sha,descriptor in descriptors.items():
            for _ in package._object_parts(self.package,descriptor):pass
            self.objects[sha]=descriptor
        if len(self.objects)>package.MAX_OBJECTS:_fail('Backup object count limit exceeded')

    def ensure(self, sha):
        self.prefetch([sha])
        return self.objects[sha]

    def scalar(self, value):
        if type(value) is dict and 'ref' in value:
            self.ensure(value['ref'])
        return super().scalar(value)


def _pages(callback, action, revision, table=None, prefetch=None):
    cursor = ''
    field = 'rows' if table is not None else 'files'
    cursor_field = 'next_key' if table is not None else 'next_path'
    after_field = 'after_key' if table is not None else 'after_path'
    while True:
        request = {'revision': revision, 'limit': 500}
        if table is not None:
            request['table'] = table
        if cursor:
            request[after_field] = cursor
        page = _call(callback, action, request)
        if page.get('revision') != revision or type(page.get('done')) is not bool or type(page.get(field)) is not list or len(page[field]) > 500:
            _fail('Unpinned or invalid bounded export page')
        if table is not None and page.get('table') != table:
            _fail('Export table mismatch')
        rows = page[field]
        if prefetch is not None and rows:prefetch(rows)
        for row in rows:
            key = row.get('key_sha256') if table is not None else row.get('path')
            if table is not None:
                package._hash(key)
            else:
                package._relative(key)
            if key <= cursor:
                _fail('Duplicate or non-progressing export cursor')
            cursor = key
            yield row
        if page['done']:
            if page.get(cursor_field) is not None:
                _fail('Terminal export page must have no continuation')
            return
        if not rows or page.get(cursor_field) != cursor:
            _fail('Invalid export continuation')


def _schema_database(path, trusted_path, core):
    with delta._snapshot(trusted_path) as copy, delta._open(copy) as trusted:
        schema, tables, _ = package._schema(trusted)
    if package.digest(schema) != core['schema_sha256'] or schema != core['schema']:
        _fail('Remote schema differs from independently trusted schema')
    if tables != [_metadata(table) for table in core['inventory']['tables']]:
        _fail('Remote table metadata differs from trusted schema')
    db = sqlite3.connect(path)
    try:
        disable_extensions = getattr(db, 'enable_load_extension', None)
        if disable_extensions is not None:
            disable_extensions(False)
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute("PRAGMA encoding='" + schema['encoding'] + "'")
        for key in ('user_version', 'application_id'):
            db.execute('PRAGMA ' + key + '=' + str(schema[key]))
        for kind in ('table', 'index'):
            for obj_kind, name, _, sql in schema['objects']:
                if obj_kind == kind and name != 'sqlite_sequence' and sql is not None:
                    db.execute(sql)
        db.commit()
    finally:
        db.close()
    os.chmod(path, 0o600)
    return tables


def _check_watermarks(db, watermarks):
    expected = {'feedback_cursor': 'cloud_feedback_cursor', 'profile_cursor': 'cloud_profile_cursor',
                'request_id': 'd1_runtime_request_id'}
    if set(watermarks) != set(expected):
        _fail('Invalid main-store watermark fields')
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'meta' not in names:
        if any(watermarks.values()):
            _fail('Watermarks require the application meta table')
        return
    for key, meta_key in expected.items():
        package._integer(watermarks[key])
        row = db.execute('SELECT value FROM meta WHERE key=?', (meta_key,)).fetchone()
        value = 0 if row is None else int(row[0])
        if value != watermarks[key]:
            _fail('Main-store watermarks differ from exact meta rows')


def _materialize(callback, revision, core, summary, watermarks, state, cache, trusted):
    database = state / package._relative(core['database_path'])
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tables = _schema_database(database, trusted, core)
    strategies = {table['name']: table['key_kind'] for table in core['tables']}
    counts = {table['name']: table['rows'] for table in summary['tables']}
    if set(counts) != {table['name'] for table in tables} or len(counts) != len(summary['tables']):
        _fail('Pinned table-count coverage mismatch')
    for count in counts.values():
        package._integer(count, delta.MAX_ROWS)
    if sum(counts.values()) > delta.MAX_ROWS:
        _fail('Total backup row limit exceeded')
    objects = _Objects(cache, callback)
    audit = []
    with contextlib.closing(sqlite3.connect(cache / 'row-audit.sqlite3')) as expected, contextlib.closing(sqlite3.connect(database)) as db:
        expected.execute('CREATE TABLE rows(table_name TEXT,key_sha256 TEXT,expanded_sha256 TEXT,PRIMARY KEY(table_name,key_sha256))')
        db.execute('PRAGMA foreign_keys=OFF')
        sequence = []
        for table in tables:
            count, chain = 0, _sha(b'')
            def prefetch_rows(rows):
                objects.prefetch(value['ref'] for row in rows for value in row['values'] if type(value) is dict and 'ref' in value)
            for row in _pages(callback, 'export_rows', revision, table['name'], prefetch_rows):
                values, rowid = package.read_row(row, table, strategies[table['name']], objects)
                expected.execute('INSERT INTO rows VALUES(?,?,?)', (table['name'],row['key_sha256'],_expanded(values,rowid)))
                chain = _sha((chain+'\n'+row['key_sha256']+'\n'+row['record_sha256']).encode('ascii'))
                count += 1
                if table['name'] == 'sqlite_sequence':
                    sequence.append((table,values,rowid))
                else:
                    package._insert(db,table,values,rowid)
            if count != counts[table['name']]:
                _fail('Pinned table row count mismatch')
            audit.append({'name':table['name'],'rows':count,'table_chain_sha256':chain})
        if any(table['name']=='sqlite_sequence' for table in tables):
            db.execute('DELETE FROM sqlite_sequence')
            for table,values,rowid in sequence:
                package._insert(db,table,values,rowid)
        for table in tables:
            count=0
            for values,rowid in package._rows(db,table,strategies[table['name']]):
                key=package.digest(package._key(values,rowid,table,strategies[table['name']]))
                original=expected.execute('SELECT expanded_sha256 FROM rows WHERE table_name=? AND key_sha256=?',(table['name'],key)).fetchone()
                if original is None or original[0]!=_expanded(values,rowid):
                    _fail('SQLite changed a normalized row storage class, value, key, or rowid')
                count+=1
            if count!=counts[table['name']]:
                _fail('Materialized row coverage mismatch')
        _check_watermarks(db,watermarks)
        delta._validate_database(db)
        db.commit()
    for entry in core['directories']:
        relative=package._relative(entry['path']);package._integer(entry['mode'],0o777)
        (state/relative).mkdir(parents=True,exist_ok=True,mode=0o700)
    files=[]
    for entry in _pages(callback,'query_files',revision,prefetch=lambda rows:objects.prefetch(row['sha256'] for row in rows)):
        package._check_object(entry,('path','mode','bytes','sha256'))
        path=state/package._relative(entry['path'])
        if path==database or entry['path'] in {core['database_path']+s for s in ('-wal','-shm','-journal')}:
            _fail('Runtime artifact collides with database or sidecar')
        package._integer(entry['mode'],0o777)
        obj=objects.ensure(entry['sha256'])
        if obj['bytes']!=entry['bytes']:
            _fail('Runtime object size mismatch')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with path.open('xb') as handle:
            for raw in package._object_parts(cache,obj):handle.write(raw)
        os.chmod(path,entry['mode'])
        files.append(entry)
    if len(files)!=summary['file_count']:
        _fail('Pinned file count mismatch')
    for entry in reversed(core['directories']):os.chmod(state/entry['path'],entry['mode'])
    os.chmod(database,core['database_mode'])
    return database,audit


def _validate_site_snapshot(snapshot,require_main=False):
    if type(snapshot) is not dict or type(snapshot.get('main_head')) is not dict:
        _fail('Atomic Site snapshot has no committed main-store revision or valid empty capture')
    package._check_object(snapshot,('format','version','pointers','generations',
        'excluded_uncommitted_generations','feedback_cursor','profile_cursor','main_head'))
    if snapshot['format']!='academic-radar-site-backup' or type(snapshot['version']) is not int or snapshot['version']!=1:
        _fail('Unsupported Site backup snapshot')
    pin=snapshot['main_head'];package._check_object(pin,('revision','import_id'))
    package._integer(pin['revision'])
    if pin['revision']==0:
        if require_main:_fail('Atomic Site snapshot has no committed main-store revision')
        if pin['import_id'] is not None:_fail('Revision zero cannot identify an imported main store')
    else:package._hash(pin['import_id'])
    for key in ('feedback_cursor','profile_cursor','excluded_uncommitted_generations'):
        package._integer(snapshot[key])
    if type(snapshot['generations']) is not list or len(snapshot['generations'])>100_000:
        _fail('Invalid Site generation inventory')
    ids=[]
    for generation in snapshot['generations']:
        package._check_object(generation,('id','count','created_at','status'))
        ids.append(package._hash(generation['id']));package._integer(generation['count'],delta.MAX_ROWS)
        if generation['status']!='complete' or type(generation['created_at']) is not str:
            _fail('Invalid complete Site generation metadata')
    if ids!=sorted(set(ids)):_fail('Duplicate/unordered Site generations')
    if type(snapshot['pointers']) is not list:_fail('Invalid Site pointer inventory')
    keys=[]
    for pointer in snapshot['pointers']:
        package._check_object(pointer,('key','value'))
        if type(pointer['key']) is not str or type(pointer['value']) is not str:
            _fail('Invalid Site pointer')
        keys.append(pointer['key'])
        if pointer['key']=='active' and pointer['value'] not in ids:_fail('Active Site generation missing')
    if keys!=sorted(set(keys)):_fail('Duplicate/unordered Site pointers')


def _capture(callback,require_main=True):
    response=_call(callback,'begin',{})
    snapshot=response['snapshot']
    # Canonical hash avoids relying on a native tool preserving object key order.
    if response.get('hash_algorithm')!='sha256-canonical-json-v1' or package.digest(snapshot)!=response.get('snapshot_sha256'):
        _fail('Site snapshot needs the canonical native backup hash contract')
    _validate_site_snapshot(snapshot,require_main)
    return snapshot,response['snapshot_sha256']


def _site_generations(callback,snapshot,directory):
    generations=snapshot['generations']
    if type(generations) is not list or len(generations)>100_000:
        _fail('Invalid Site generation inventory')
    ids=[]
    for generation in generations:
        identifier=package._hash(generation['id']);ids.append(identifier)
        count_expected=package._integer(generation['count'],delta.MAX_ROWS)
        if generation['status']!='complete':_fail('Uncommitted Site generation in backup')
        cursor=None;count=0;fingerprint=hashlib.sha256();previous=None;pending=None
        with (directory/(identifier+'.jsonl')).open('xb') as out:
            os.chmod(directory/(identifier+'.jsonl'),0o600)
            while True:
                request={'generation':identifier}
                if cursor is not None:request['after']=cursor
                page=_call(callback,'records',request)
                if page.get('generation')!=identifier or page.get('expected_count')!=count_expected or type(page.get('chunks')) is not list or type(page.get('done')) is not bool:
                    _fail('Unpinned Site generation page')
                if page['done']:
                    if page['chunks'] or page.get('next') is not None or pending is not None:_fail('Truncated Site generation')
                    break
                if not page['chunks']:_fail('Non-progressing Site generation page')
                for chunk in page['chunks']:
                    key=(chunk['kind'],chunk['key'])
                    if key[0] not in ('paper','history','feedback','private','meta') or type(key[1]) is not str or len(key[1])>1000:_fail('Invalid Site record key')
                    package._hash(chunk['checksum']);package._integer(chunk['bytes'],MAX_SITE_RECORD_BYTES);package._integer(chunk['offset'],MAX_SITE_RECORD_BYTES)
                    if pending is None:
                        if chunk['offset']!=0 or (previous is not None and key<=previous):_fail('Invalid Site record order')
                        pending={'key':key,'checksum':chunk['checksum'],'bytes':chunk['bytes'],'data':bytearray()}
                    if key!=pending['key'] or chunk['checksum']!=pending['checksum'] or chunk['bytes']!=pending['bytes'] or chunk['offset']!=len(pending['data']):
                        _fail('Site record chunk identity or offset mismatch')
                    raw=base64.b64decode(chunk['base64'],validate=True)
                    if not raw and chunk['bytes']:_fail('Empty nonterminal Site record chunk')
                    pending['data'].extend(raw)
                    if len(pending['data'])>pending['bytes']:_fail('Site record exceeds declared length')
                    if len(pending['data'])==pending['bytes']:
                        data=bytes(pending['data'])
                        if _sha(data)!=pending['checksum']:_fail('Site record checksum mismatch')
                        record={'kind':key[0],'key':key[1],'checksum':pending['checksum'],'data':data.decode('utf-8')}
                        out.write(package.canonical(record)+b'\n')
                        if count:fingerprint.update(b'\n')
                        fingerprint.update((key[0]+':'+key[1]+':'+pending['checksum']).encode('utf-8'))
                        count+=1;previous=key;pending=None
                    cursor={'kind':key[0],'key':key[1],'offset':len(pending['data']) if pending else 0}
                if page.get('next')!=cursor:_fail('Site record continuation mismatch')
        if count!=count_expected or fingerprint.hexdigest()!=identifier:
            _fail('Complete Site generation count/hash mismatch')
    if ids!=sorted(set(ids)):_fail('Duplicate/unordered Site generations')
    for pointer in snapshot['pointers']:
        if pointer['key']=='active' and pointer['value'] not in ids:_fail('Active Site generation missing')


def _large_event(callback,stream,until,descriptor):
    package._check_object(descriptor,('seq','bytes','sha256'))
    package._integer(descriptor['seq']);package._integer(descriptor['bytes'],8*1024*1024)
    package._hash(descriptor['sha256'])
    raw=bytearray()
    while len(raw)<descriptor['bytes']:
        offset=len(raw)
        page=_call(callback,'event_chunks',{'stream':stream,'seq':descriptor['seq'],'offset':offset,'until':until})
        if (page.get('stream')!=stream or page.get('seq')!=descriptor['seq'] or page.get('until')!=until
                or page.get('offset')!=offset or page.get('bytes')!=descriptor['bytes']
                or page.get('sha256')!=descriptor['sha256'] or type(page.get('done')) is not bool):
            _fail('Large Site event chunk identity mismatch')
        piece=base64.b64decode(page['base64'],validate=True)
        if not piece or len(piece)>256*1024 or offset+len(piece)>descriptor['bytes']:
            _fail('Invalid large Site event chunk size')
        raw.extend(piece)
        if page.get('next')!=len(raw) or page['done']!=(len(raw)==descriptor['bytes']):
            _fail('Large Site event continuation mismatch')
    if _sha(raw)!=descriptor['sha256']:_fail('Large Site event checksum mismatch')
    event=package._json(bytes(raw)+b'\n')
    if event.get('seq')!=descriptor['seq']:_fail('Large Site event sequence mismatch')
    return event


def _site_events(callback,snapshot,directory):
    for action,key in (('feedback','feedback_cursor'),('profiles','profile_cursor')):
        cursor=0;until=snapshot[key]
        with (directory/(action+'.jsonl')).open('xb') as out:
            os.chmod(directory/(action+'.jsonl'),0o600)
            while True:
                page=_call(callback,action,{'after':cursor,'until':until})
                if page.get('until')!=until or type(page.get('events')) is not list or type(page.get('done')) is not bool:_fail('Unpinned Site event page')
                if 'oversized' in page:
                    if page['events'] or page['done'] or page.get('next')!=cursor or page['oversized'].get('seq')!=cursor+1:
                        _fail('Invalid oversized Site event marker')
                    event=_large_event(callback,action,until,page['oversized'])
                    cursor=event['seq'];out.write(package.canonical(event)+b'\n')
                    continue
                for event in page['events']:
                    if type(event) is not dict or type(event.get('seq')) is not int or event['seq']!=cursor+1 or event['seq']>until:_fail('Site event sequence gap or duplicate')
                    cursor=event['seq'];out.write(package.canonical(event)+b'\n')
                if page.get('next')!=cursor:_fail('Site event continuation mismatch')
                if page['done']:
                    if cursor!=until:_fail('Incomplete Site event coverage')
                    break
                if not page['events']:_fail('Non-progressing Site event page')


def export_backup(main_call,site_call,output_dir:Path,*,trusted_schema_database:Path):
    """Download all pinned committed state; native callbacks are read-only."""
    destination=package._new_directory(output_dir)
    calls={'main':{},'site':{}}
    def counted(callback,channel):
        def invoke(action,data):
            calls[channel][action]=calls[channel].get(action,0)+1
            return callback(action,data)
        return invoke
    main_call=counted(main_call,'main');site_call=counted(site_call,'site')
    snapshot,snapshot_sha=_capture(site_call)
    revision=snapshot['main_head']['revision']
    metadata=_call(main_call,'read_manifest',{'revision':revision})
    if metadata.get('revision')!=revision or metadata.get('manifest_sha256')!=snapshot['main_head']['import_id']:
        _fail('Main manifest is not the atomically captured revision')
    core=metadata['manifest'];watermarks=metadata['watermarks']
    if watermarks['feedback_cursor']>snapshot['feedback_cursor'] or watermarks['profile_cursor']>snapshot['profile_cursor']:
        _fail('Main watermarks exceed the captured Site history')
    summary=_call(main_call,'export_summary',{'revision':revision})
    if summary.get('revision')!=revision:_fail('Export summary revision mismatch')
    package._integer(summary['file_count'],package.MAX_FILES)
    with tempfile.TemporaryDirectory(prefix='.d1-backup-',dir=destination.parent) as temporary:
        temporary=Path(temporary);bundle=temporary/'bundle';bundle.mkdir(mode=0o700)
        state=temporary/'state';state.mkdir(mode=0o700)
        cache=temporary/'cache';cache.mkdir(mode=0o700)
        database,audit=_materialize(main_call,revision,core,summary,watermarks,state,cache,trusted_schema_database)
        main=package.export_package(database,bundle/'main',files_root=state,database_relative_path=core['database_path'])
        site=bundle/'site';site.mkdir(mode=0o700);generations=site/'generations';generations.mkdir(mode=0o700)
        _write_json(site/'snapshot.json',snapshot)
        _site_generations(site_call,snapshot,generations)
        _site_events(site_call,snapshot,site)
        files=[]
        for relative in sorted(package._tree(bundle)[0]):
            sha,size=_file_sha(bundle/relative)
            files.append({'path':relative,'bytes':size,'sha256':sha})
        manifest={'format':FORMAT,'version':VERSION,'revision':revision,'source_manifest_sha256':metadata['manifest_sha256'],
                  'main_manifest_sha256':main['manifest_sha256'],'site_snapshot_sha256':snapshot_sha,'watermarks':watermarks,
                  'site_watermarks':{'feedback_cursor':snapshot['feedback_cursor'],'profile_cursor':snapshot['profile_cursor']},
                  'remote_tables':audit,'files':files}
        _write_json(bundle/'backup.json',manifest)
        verify_backup(bundle)
        checkpoints._publish(bundle,destination)
    return {'backup_sha256':_sha(package.canonical(manifest)+b'\n'),'revision':revision,
            'main_manifest_sha256':main['manifest_sha256'],'logical_sha256':main['manifest']['inventory']['logical_sha256'],
            'rows':sum(t['rows'] for t in audit),'files':summary['file_count'],'site_generations':len(snapshot['generations']),
            'pending_feedback':snapshot['feedback_cursor']-watermarks['feedback_cursor'],
            'pending_profiles':snapshot['profile_cursor']-watermarks['profile_cursor'],
            'transport_calls':calls,'transport_call_count':sum(sum(group.values()) for group in calls.values())}


def _json_lines(path, limit=package.MAX_MANIFEST_BYTES):
    before=package._stamp(path)
    with path.open('rb') as handle:
        while True:
            raw=handle.readline(limit+1)
            if not raw:break
            if len(raw)>limit:_fail('Backup JSON line exceeds limit')
            yield package._json(raw)
    if package._stamp(path)!=before:_fail('Backup JSONL changed while reading')


def _verify_site_local(site,snapshot):
    _validate_site_snapshot(snapshot)
    expected={'snapshot.json','feedback.jsonl','profiles.jsonl'}
    ids=[]
    for generation in snapshot['generations']:
        identifier=package._hash(generation['id']);ids.append(identifier)
        relative='generations/'+identifier+'.jsonl';expected.add(relative)
        count=0;previous=None;fingerprint=hashlib.sha256()
        for row in _json_lines(site/relative):
            package._check_object(row,('kind','key','checksum','data'))
            if row['kind'] not in ('paper','history','feedback','private','meta') or type(row['key']) is not str or type(row['data']) is not str:
                _fail('Invalid saved Site record')
            key=(row['kind'],row['key'])
            if previous is not None and key<=previous:_fail('Duplicate/unordered saved Site records')
            if _sha(row['data'].encode('utf-8'))!=package._hash(row['checksum']):_fail('Saved Site record checksum mismatch')
            if count:fingerprint.update(b'\n')
            fingerprint.update((key[0]+':'+key[1]+':'+row['checksum']).encode('utf-8'))
            previous=key;count+=1
        if count!=generation['count'] or fingerprint.hexdigest()!=identifier or generation['status']!='complete':
            _fail('Saved Site generation hash/count mismatch')
    if ids!=sorted(set(ids)):_fail('Duplicate/unordered Site generations')
    for pointer in snapshot['pointers']:
        if pointer['key']=='active' and pointer['value'] not in ids:_fail('Saved active Site generation missing')
    for action,key in (('feedback','feedback_cursor'),('profiles','profile_cursor')):
        cursor=0
        for event in _json_lines(site/(action+'.jsonl')):
            if type(event) is not dict or type(event.get('seq')) is not int or event['seq']!=cursor+1:_fail('Saved Site event gap')
            cursor=event['seq']
        if cursor!=snapshot[key]:_fail('Saved Site event high-water mismatch')
    if set(package._tree(site)[0])!=expected:_fail('Unexpected saved Site files')


def verify_backup(backup_dir:Path,*,expected_backup_sha256=None):
    root=package._path(backup_dir)
    raw=package._read(root/'backup.json',package.MAX_MANIFEST_BYTES)
    digest=_sha(raw)
    if expected_backup_sha256 is not None and package._hash(expected_backup_sha256)!=digest:
        _fail('Backup root does not match trusted SHA-256')
    manifest=package._json(raw)
    package._check_object(manifest,('format','version','revision','source_manifest_sha256','main_manifest_sha256','site_snapshot_sha256','watermarks','site_watermarks','remote_tables','files'))
    if manifest['format']!=FORMAT or manifest['version']!=VERSION or type(manifest['revision']) is not int or manifest['revision']<1:
        _fail('Unsupported complete backup format')
    for field in ('source_manifest_sha256','main_manifest_sha256','site_snapshot_sha256'):package._hash(manifest[field])
    before=package._tree(root)
    if type(manifest['files']) is not list or len(manifest['files'])>package.MAX_OBJECTS+package.MAX_FILES:
        _fail('Invalid backup file inventory')
    names=[]
    for entry in manifest['files']:
        package._check_object(entry,('path','bytes','sha256'))
        relative=package._relative(entry['path']);names.append(relative)
        if not relative.startswith(('main/','site/')):_fail('Unexpected backup namespace')
        sha,size=_file_sha(root/relative)
        if sha!=package._hash(entry['sha256']) or size!=entry['bytes']:_fail('Complete backup member hash mismatch')
    if names!=sorted(set(names)) or set(before[0])!=set(names)|{'backup.json'}:
        _fail('Missing, duplicate, or unlisted backup files')
    if set(before[1])!={'main','main/objects','main/tables','site','site/generations'}:
        _fail('Unexpected backup directories')
    main=package.verify_package(root/'main',expected_manifest_sha256=manifest['main_manifest_sha256'])['manifest']
    snapshot=package._json(package._read(root/'site'/'snapshot.json',package.MAX_MANIFEST_BYTES))
    if package.digest(snapshot)!=manifest['site_snapshot_sha256'] or snapshot['main_head']!={'revision':manifest['revision'],'import_id':manifest['source_manifest_sha256']}:
        _fail('Saved atomic main/Site capture mismatch')
    if manifest['site_watermarks']!={k:snapshot[k] for k in ('feedback_cursor','profile_cursor')}:
        _fail('Saved Site watermarks mismatch')
    for key in ('feedback_cursor','profile_cursor'):
        package._integer(manifest['watermarks'][key]);package._integer(snapshot[key])
        if manifest['watermarks'][key]>snapshot[key]:_fail('Main watermark exceeds captured Site events')
    _verify_site_local(root/'site',snapshot)
    if [(t['name'],t['rows']) for t in manifest['remote_tables']]!=[(t['name'],t['rows']) for t in main['inventory']['tables']]:
        _fail('Remote row coverage differs from recomputed main package')
    for table in manifest['remote_tables']:package._hash(table['table_chain_sha256'])
    if package._tree(root)!=before:_fail('Complete backup changed during verification')
    return {'backup_sha256':digest,'manifest':manifest,'main_manifest':main,'snapshot':snapshot}


def _source_prefix(source_root):
    source=Path(source_root).expanduser()
    if not source.is_absolute() or '..' in source.parts:
        _fail('source_root must be an explicit absolute path without traversal')
    value=str(source).rstrip('/')
    if not value:_fail('Filesystem root is too broad a relocation source')
    return value


def _profile_target(state,database,config,override,source_root=None):
    candidate=override if override is not None else config.get('profile_file','research-profile.md')
    if type(candidate) is not str:_fail('Profile path must be a string')
    if override is None and source_root is not None:
        source=_source_prefix(source_root)
        if candidate.startswith(checkpoints.TOKEN):candidate=candidate[len(checkpoints.TOKEN):]
        elif candidate.startswith(source+'/'):candidate=candidate[len(source)+1:]
    profile=state/package._relative(candidate)
    reserved={database}|{Path(str(database)+suffix) for suffix in ('-wal','-shm','-journal')}
    if profile in reserved:_fail('Profile file cannot replace the restored database or SQLite sidecars')
    return profile


def _apply_pending(state,site,main_manifest,manifest,profile_relative_path,source_root=None):
    from .cloud_sync import apply_remote_feedback
    from .profile_sync import apply_remote_profiles, reconcile_profile_file
    from .product import load_config
    database=state/main_manifest['database_path']
    config_path=state/'config.toml'
    config=load_config(config_path) if config_path.exists() else {}
    applied={'feedback':0,'profiles':0}
    profile=None
    if manifest['site_watermarks']['profile_cursor']>manifest['watermarks']['profile_cursor']:
        profile=_profile_target(state,database,config,profile_relative_path,source_root)
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.row_factory=sqlite3.Row
        _check_watermarks(db,manifest['watermarks'])
        for action,key in (('feedback','feedback_cursor'),('profiles','profile_cursor')):
            for raw in _json_lines(site/(action+'.jsonl')):
                if raw['seq']<=manifest['watermarks'][key]:continue
                if action=='feedback':
                    applied[action]+=apply_remote_feedback(db,[raw],raw['seq'],authoritative=True)
                else:
                    event=dict(raw)
                    for field in ('base_preferences','screening_preferences'):
                        if type(event[field]) is str:event[field]=json.loads(event[field])
                    applied[action]+=apply_remote_profiles(db,[event],event['seq'],config)
        for key,meta_key in (('feedback_cursor','cloud_feedback_cursor'),('profile_cursor','cloud_profile_cursor')):
            row=db.execute('SELECT value FROM meta WHERE key=?',(meta_key,)).fetchone()
            if (int(row[0]) if row else 0)!=manifest['site_watermarks'][key]:_fail('Pending replay did not reach captured cursor')
        db.row_factory=None
        delta._validate_database(db)
    if applied['profiles']:
        reconcile_profile_file(database,profile)
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.execute('PRAGMA journal_mode=DELETE')
        delta._validate_database(db)
    return applied


def _relocate_operational(state,final_state,main_manifest,source_root,profile_relative_path=None):
    """Normalize only the derived copy; old roots are string mappings, never read."""
    if main_manifest['database_path']!=checkpoints.DB_NAME:
        _fail('Operational relocation requires the known application database path')
    source_text=_source_prefix(source_root)
    entries,_=checkpoints._load_provenance(state)
    provenance={entry['id']:entry for entry in entries}
    files={relative:{} for relative in package._tree(state)[0] if relative!=checkpoints.PROVENANCE_FILE}
    changes={'database_and_artifact_references':0}
    def portable(value,context=None):
        context=context or {}
        if value.startswith(checkpoints.ARCHIVE_TOKEN):
            checkpoints._archive(value,context,provenance)
            return value
        if context.get('field')=='backup_path' or context.get('column')=='backup_path':
            _fail('Local-only rollback references require reviewed disabled archival provenance')
        if value.startswith(checkpoints.TOKEN):
            relative=package._relative(value[len(checkpoints.TOKEN):])
        elif value.startswith(source_text+'/'):
            relative=package._relative(value[len(source_text)+1:])
        elif value.startswith(('/', '~')):
            _fail('Unmapped reference outside the explicit source_root')
        else:
            relative=package._relative(value)
        if relative not in files:_fail('Operational reference does not identify an included file')
        path=state/relative;package._stamp(path)
        if context.get('table')=='fulltext_files' and context.get('column')=='stored_path':
            actual,size=_file_sha(path)
            if actual!=context.get('sha256') or size!=context.get('size'):_fail('Active fulltext reference hash/size mismatch')
        result=checkpoints.TOKEN+relative
        if result!=value:changes['database_and_artifact_references']+=1
        return result
    checkpoints._database(state/checkpoints.DB_NAME,portable,change=True)
    for relative in sorted(files):
        if not relative.endswith('.json'):continue
        path=state/relative;raw=checkpoints._read(path);parsed=checkpoints._json(raw)
        mapped=checkpoints._transform_json(parsed,portable,context={'artifact':relative,'historical':True})
        if mapped!=parsed:path.write_bytes(checkpoints._canonical(mapped)+b'\n')
    config_path=state/'config.toml'
    if not config_path.is_file():_fail('Operational relocation requires included config.toml')
    config=checkpoints.tomllib.loads(checkpoints._read(config_path).decode('utf-8'))
    profile_path=_profile_target(state,state/checkpoints.DB_NAME,config,profile_relative_path,source_root)
    profile=profile_path.relative_to(state).as_posix()
    if profile not in files:_fail('Configured profile is not an included operational file')
    with contextlib.closing(sqlite3.connect(state/checkpoints.DB_NAME)) as db:
        active=db.execute("SELECT content,profile_hash FROM profile_versions WHERE status='active'").fetchall()
    if len(active)!=1:_fail('Operational recovery requires exactly one active profile')
    profile_bytes=checkpoints._read(profile_path)
    if profile_bytes!=active[0][0].encode('utf-8') or _sha(profile_bytes)!=active[0][1]:
        _fail('Configured profile bytes/hash differ from the active profile')
    config['state_dir']='.';config['profile_file']=profile
    def config_paths(value):
        if type(value) is dict:return {key:config_paths(item) for key,item in value.items()}
        if type(value) is list:return [config_paths(item) for item in value]
        if type(value) is str and (value.startswith(source_text+'/') or value.startswith(checkpoints.TOKEN)):
            return portable(value,{'field':'config_path'})[len(checkpoints.TOKEN):]
        return value
    config=config_paths(config)
    serialized=('\n'.join(checkpoints._toml(k)+' = '+checkpoints._toml(v) for k,v in config.items())+'\n').encode()
    normalized,_=checkpoints._config(serialized,profile)
    config_path.write_bytes(normalized)
    # This existing validator propagates active/historical scopes through the
    # artifact graph and rejects archived/unavailable references in active use.
    _,scopes=checkpoints._reference_contexts(state,files,provenance)
    def restored(value,context=None):
        context=context or {}
        if value.startswith(checkpoints.ARCHIVE_TOKEN):
            checkpoints._archive(value,context,provenance)
            return value
        if not value.startswith(checkpoints.TOKEN):_fail('Unexpected unmapped operational reference')
        relative=package._relative(value[len(checkpoints.TOKEN):])
        if relative not in files:_fail('Missing operational file during final mapping')
        return str(final_state/relative)
    checkpoints._database(state/checkpoints.DB_NAME,restored,change=True)
    for relative in sorted(files):
        if not relative.endswith('.json'):continue
        path=state/relative;parsed=checkpoints._json(checkpoints._read(path))
        mapped=checkpoints._transform_json(parsed,restored,context={'artifact':relative,
                    'historical':scopes[relative]=='historical','archive_scope':scopes[relative]})
        if mapped!=parsed:path.write_bytes(checkpoints._canonical(mapped)+b'\n')
    return {'performed':True,'source_root':source_text,'included_files_validated':len(files),
            'archived_references_preserved':len(provenance),'local_rollback_enabled':False,**changes}


def restore_backup(backup_dir:Path,output_dir:Path,*,trusted_schema_database:Path,
                   expected_backup_sha256=None,apply_pending=True,profile_relative_path=None,source_root=None):
    """Restore exact base state; optionally derive current state by replaying edits.

    The input main package is never modified. Output contains state/, site/, and
    a labeled recovery.json audit. No Site event is acknowledged remotely.
    """
    destination=package._new_directory(output_dir)
    verified=verify_backup(backup_dir,expected_backup_sha256=expected_backup_sha256)
    root=package._path(backup_dir);before=package._tree(root)
    manifest=verified['manifest'];main=verified['main_manifest']
    with tempfile.TemporaryDirectory(prefix='.d1-backup-restore-',dir=destination.parent) as temporary:
        stage=Path(temporary)/'restored';stage.mkdir(mode=0o700)
        base=package.restore_package(root/'main',stage/'state',trusted_schema_database=trusted_schema_database,
                                     expected_manifest_sha256=manifest['main_manifest_sha256'])
        with contextlib.closing(sqlite3.connect(stage/'state'/main['database_path'])) as exact_db:
            _check_watermarks(exact_db,manifest['watermarks'])
        shutil.copytree(root/'site',stage/'site',symlinks=False)
        applied={'feedback':0,'profiles':0}
        if apply_pending and (manifest['site_watermarks']['feedback_cursor']>manifest['watermarks']['feedback_cursor'] or manifest['site_watermarks']['profile_cursor']>manifest['watermarks']['profile_cursor']):
            applied=_apply_pending(stage/'state',stage/'site',main,manifest,profile_relative_path,source_root)
        relocation={'performed':False,'local_rollback_enabled':False}
        if source_root is not None:
            relocation=_relocate_operational(stage/'state',destination/'state',main,source_root,profile_relative_path)
        database=stage/'state'/main['database_path']
        with delta._snapshot(database) as copy,delta._open(copy) as db:
            schema,tables,strategies=package._schema(db)
            current=package._inventory(db,tables,strategies,package.digest(schema))
        report={'format':'academic-radar-d1-recovery','version':1,'backup_sha256':verified['backup_sha256'],
                'revision':manifest['revision'],'base_logical_sha256':base['logical_sha256'],
                'restored_logical_sha256':current['logical_sha256'],'pending_replay_requested':bool(apply_pending),
                'applied':applied,'exact_main_state':not any(applied.values()) and not relocation['performed'],
                'operational_ready':relocation['performed'],'relocation':relocation,'watermarks':manifest['watermarks'],
                'site_watermarks':manifest['site_watermarks'],'database_path':'state/'+main['database_path']}
        _write_json(stage/'recovery.json',report)
        if package._tree(root)!=before:_fail('Backup changed during recovery')
        checkpoints._publish(stage,destination)
    return report


SITE_ONLY_FORMAT='academic-radar-site-only-backup'


def export_site_backup(site_call,output_dir:Path):
    """Read-only pre-import Site backup, including a valid main revision zero."""
    destination=package._new_directory(output_dir)
    calls={}
    def callback(action,data):
        calls[action]=calls.get(action,0)+1
        return site_call(action,data)
    snapshot,snapshot_sha=_capture(callback,require_main=False)
    with tempfile.TemporaryDirectory(prefix='.d1-site-backup-',dir=destination.parent) as temporary:
        bundle=Path(temporary)/'bundle';bundle.mkdir(mode=0o700)
        site=bundle/'site';site.mkdir(mode=0o700)
        generations=site/'generations';generations.mkdir(mode=0o700)
        _write_json(site/'snapshot.json',snapshot)
        _site_generations(callback,snapshot,generations)
        _site_events(callback,snapshot,site)
        files=[]
        for relative in sorted(package._tree(bundle)[0]):
            sha,size=_file_sha(bundle/relative)
            files.append({'path':relative,'bytes':size,'sha256':sha})
        manifest={'format':SITE_ONLY_FORMAT,'version':1,'site_snapshot_sha256':snapshot_sha,
                  'main_revision':snapshot['main_head']['revision'],'files':files}
        _write_json(bundle/'site-backup.json',manifest)
        verified=verify_site_backup(bundle)
        checkpoints._publish(bundle,destination)
    return {'site_backup_sha256':verified['site_backup_sha256'],'main_revision':manifest['main_revision'],
            'site_generations':len(snapshot['generations']),'feedback_events':snapshot['feedback_cursor'],
            'profile_events':snapshot['profile_cursor'],'transport_calls':calls,'transport_call_count':sum(calls.values())}


def verify_site_backup(backup_dir:Path,*,expected_site_backup_sha256=None):
    root=package._path(backup_dir)
    raw=package._read(root/'site-backup.json',package.MAX_MANIFEST_BYTES)
    sha=_sha(raw)
    if expected_site_backup_sha256 is not None and package._hash(expected_site_backup_sha256)!=sha:
        _fail('Site-only backup root does not match trusted SHA-256')
    manifest=package._json(raw)
    package._check_object(manifest,('format','version','site_snapshot_sha256','main_revision','files'))
    if manifest['format']!=SITE_ONLY_FORMAT or manifest['version']!=1:
        _fail('Unsupported Site-only backup format')
    package._integer(manifest['main_revision'])
    before=package._tree(root);names=[]
    if type(manifest['files']) is not list or len(manifest['files'])>package.MAX_FILES:
        _fail('Invalid Site-only file inventory')
    for entry in manifest['files']:
        package._check_object(entry,('path','bytes','sha256'))
        name=package._relative(entry['path']);names.append(name)
        if not name.startswith('site/'):_fail('Unexpected Site-only namespace')
        digest,size=_file_sha(root/name)
        if digest!=package._hash(entry['sha256']) or size!=entry['bytes']:_fail('Site-only backup member hash mismatch')
    if names!=sorted(set(names)) or set(before[0])!=set(names)|{'site-backup.json'} or set(before[1])!={'site','site/generations'}:
        _fail('Missing, duplicate, or unlisted Site-only backup members')
    snapshot=package._json(package._read(root/'site'/'snapshot.json',package.MAX_MANIFEST_BYTES))
    if package.digest(snapshot)!=package._hash(manifest['site_snapshot_sha256']) or snapshot['main_head']['revision']!=manifest['main_revision']:
        _fail('Site-only snapshot hash/capture mismatch')
    _verify_site_local(root/'site',snapshot)
    if package._tree(root)!=before:_fail('Site-only backup changed during verification')
    return {'site_backup_sha256':sha,'manifest':manifest,'snapshot':snapshot}

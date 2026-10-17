"""Portable local D1 relay; launch with an explicit trusted Python and -I -B.

This file never invokes native tools, discovers credentials, or performs HTTP.
Only a separately approved host may service its private request files.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import errno
import fcntl
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import types

ROOT = Path(__file__).absolute().parent.parent
SOURCE_FORMAT = 'academic-radar-d1-native-source-v1'
MAX = 2_000_000
# Closed, non-sensitive classifications only. Never forward exception text,
# callback fields, paths, or an arbitrary RunnerError code from a child.
TERMINAL_ERROR_CODES = frozenset({'operation_failed', 'native_host_failed', 'runtime_busy', 'interrupted'})
OPERATIONS = frozenset({
    'site.read_main_store', 'site.write_main_store', 'site.publish_main_snapshot', 'site.read_site_backup',
    'site.read_run_status', 'site.read_feedback', 'site.read_profile_events', 'site.read_profile', 'site.stage_snapshot',
})


class RelayError(RuntimeError):
    pass


class PublicationPending(RelayError):
    """A live publisher holds the inode lock; only polling may wait for it."""


def check(condition, code='invalid_arguments'):
    if not condition:
        raise RelayError(code)


def directory(path):
    """Open every absolute ancestor without following symlinks."""
    path = Path(path)
    check(path.is_absolute() and '..' not in path.parts, 'unsafe_path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def file_bytes(path, maximum=None):
    path = Path(path)
    parent = directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            check(stat.S_ISREG(before.st_mode) and (maximum is None or before.st_size <= maximum), 'unsafe_file')
            with os.fdopen(fd, 'rb', closefd=False) as handle:
                raw = handle.read(before.st_size + 1)
            after = os.fstat(fd)
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            check(stamp(before) == stamp(after) == stamp(current) and len(raw) == before.st_size, 'file_changed')
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def inventory(root=ROOT):
    """Relative, reproducible source inventory; bytecode is never executed."""
    root = Path(root)
    fd = directory(root)
    os.close(fd)
    files = [root/'scripts/d1-native-relay.py', root/'scripts/d1-native-pump.js']
    package = root/'src/academic_radar'
    fd = directory(package)
    os.close(fd)
    for current, directories, names in os.walk(package, followlinks=False):
        for name in list(directories):
            path = Path(current)/name
            if name == '__pycache__':
                directories.remove(name)
            else:
                check(not path.is_symlink(), 'source_symlink')
        for name in names:
            if not name.endswith(('.pyc', '.pyo')):
                files.append(Path(current)/name)
    return {str(path.relative_to(root)): hashlib.sha256(file_bytes(path)).hexdigest() for path in sorted(files)}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode() + b'\n'


def pin_source(output, root=ROOT):
    """Explicit inventory creation, never an implicit repair of a failing pin."""
    output, root = Path(output), Path(root)
    check(output.is_absolute() and root not in output.parents, 'pin_must_be_outside_checkout')
    raw = canonical({'format': SOURCE_FORMAT, 'files': inventory(root)})
    parent = directory(output.parent)
    try:
        fd = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fchmod(handle.fileno(), 0o400); os.fsync(handle.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)
    return {'source_manifest_sha256': hashlib.sha256(raw).hexdigest(), 'files': len(json.loads(raw)['files'])}


def unique_pairs(items):
    result = {}
    for key, value in items:
        check(key not in result, 'duplicate_manifest_key')
        result[key] = value
    return result


def verify_source(path, expected, root=ROOT):
    check(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected), 'invalid_source_pin')
    raw = file_bytes(path, MAX)
    check(hashlib.sha256(raw).hexdigest() == expected, 'source_pin_changed')
    value = json.loads(raw, object_pairs_hook=unique_pairs)
    check(isinstance(value, dict) and set(value) == {'format', 'files'} and value['format'] == SOURCE_FORMAT,
          'invalid_source_manifest')
    check(value['files'] == inventory(root), 'reviewed_source_changed')
    return value['files']


class SourceOnlyLoader(importlib.abc.Loader):
    """Compile the pinned .py bytes directly; never consult or create a .pyc."""
    def __init__(self, path, expected):
        self.path, self.expected = path, expected

    def create_module(self, spec):
        return None

    def get_code(self, fullname):
        raw = file_bytes(self.path)
        check(hashlib.sha256(raw).hexdigest() == self.expected, 'reviewed_source_changed')
        return compile(raw, str(self.path), 'exec', dont_inherit=True)

    def exec_module(self, module):
        exec(self.get_code(module.__name__), module.__dict__)


class SourceOnlyFinder(importlib.abc.MetaPathFinder):
    def __init__(self, files, root=ROOT):
        self.files, self.root = files, Path(root)

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'academic_radar' and not fullname.startswith('academic_radar.'):
            return None
        check(re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', fullname), 'invalid_module')
        base = 'src/' + fullname.replace('.', '/')
        package = base + '/__init__.py' in self.files
        relative = base + ('/__init__.py' if package else '.py')
        # Do not fall through to installed/namespace packages for unpinned modules.
        check(relative in self.files, 'unpinned_module')
        filename = self.root/relative
        loader = SourceOnlyLoader(filename, self.files[relative])
        return importlib.util.spec_from_file_location(fullname, filename, loader=loader,
            submodule_search_locations=[str(filename.parent)] if package else None)


def make_transport(manifest, expected, files, root=ROOT, journal=None):
    """Create independent adapter state; importing it never mutates legacy spool."""
    from academic_radar import native_host, d1_native_host
    transport = types.ModuleType('academic_radar._d1_spool_transport')
    transport.__package__ = 'academic_radar'
    transport.__file__ = str(Path(root)/'src/academic_radar/native_spool.py')
    loader = SourceOnlyLoader(Path(transport.__file__), files['src/academic_radar/native_spool.py'])
    exec(loader.get_code(transport.__name__), transport.__dict__)
    original_request, original_response = transport._request, transport._response
    original_announce, original_remaining = transport._announce, transport._Pipe.remaining
    live = None

    def request(value, session, number):
        result = original_request(value, session, number)
        native_host._site_arguments(value['operation'][5:], value['arguments'])
        return result

    def response(raw, req):
        raw = original_response(raw, req)
        value = transport._decode(raw)
        if value['ok']:
            native_host._site_result(req['operation'][5:], req['arguments'], value['result'])
        return raw

    def terminal(value, session, number):
        transport._check(set(value) in (
            {'protocol', 'type', 'session_id', 'request_id', 'ok', 'result'},
            {'protocol', 'type', 'session_id', 'request_id', 'ok', 'error'})
            and value['type'] == 'result' and type(value['ok']) is bool)
        transport._session(value, session, number)
        if value['ok']:
            if journal is not None:
                check(journal.exhausted and journal.replayed == journal.value['prefix'], 'journal_replay_incomplete')
            transport._check(isinstance(value['result'], dict))
            return value  # Private file only; stdout contains metadata.
        transport._check(isinstance(value['error'], dict))
        code = value['error'].get('code')
        code = code if isinstance(code, str) and code in TERMINAL_ERROR_CODES else 'native_host_failed'
        return {**value, 'error': {'code': code}}

    def spawn(arguments, _workspace):
        verify_source(manifest, expected, root)
        return subprocess.Popen([sys.executable, '-I', '-B', str(Path(root)/'scripts/d1-native-relay.py'),
            '--source-manifest', str(manifest), '--source-manifest-sha256', expected, 'host', *arguments],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, bufsize=0)

    def remaining(pipe):
        value = original_remaining(pipe)
        if live is not None:
            spool, name = live
            spool._intact()
            os.utime(name, dir_fd=spool.fd, follow_symlinks=False)
        return value

    def announce(spool, output, value, deadline=None):
        nonlocal live
        if value['type'] == 'request_ready':
            if journal is not None:
                req = transport._decode(read(transport, spool, 'request-%d.json' % value['request_id']))
                replay = journal.response(req)
                if replay is not None:
                    spool.save('claim-%d.json' % req['request_id'], transport._encode({
                        'request_sha256': hashlib.sha256(transport._encode(req)).hexdigest()}))
                    spool.save('replay-%d.json' % req['request_id'], transport._encode({
                        'journal_sha256': journal.sha, 'original_session_id': journal.value['session_id'],
                        'original_request_id': req['request_id'], 'new_session_id': req['session_id'],
                        'original_request_sha256': journal.value['files']['request-%d.json' % req['request_id']]['sha256'],
                        'new_request_sha256': hashlib.sha256(transport._encode(req)).hexdigest(),
                        'new_response_sha256': hashlib.sha256(replay).hexdigest(),
                        'original_response_sha256': journal.value['files']['response-%d.json' % req['request_id']]['sha256'],
                        **(journal.receipt_fields(req['request_id']) if isinstance(journal, CombinedReadJournal) else {})}))
                    spool.save('response-%d.json' % req['request_id'], replay)
                    return
                if req['request_id'] == journal.value['prefix'] + 1:
                    spool.save('replay-summary.json', transport._encode(journal.summary()))
                value = {**value, 'replayed_reads': journal.replayed, 'replayed_response_bytes': journal.replayed_bytes,
                         'cached_response_bytes': journal.value['cached_response_bytes']}
            heartbeat = 'live-%d.json' % value['request_id']
            spool.save(heartbeat, b'{}\n')
            live = spool, heartbeat
            spool.save('pending-%d.json' % value['request_id'], transport._encode({
                'heartbeat': heartbeat, 'deadline_unix_ms': int((time.time() + deadline - time.monotonic()) * 1000),
                'request_sha256': hashlib.sha256(file_bytes(value['path'], MAX)).hexdigest()}))
        return original_announce(spool, output, value, deadline)

    transport.OPERATIONS = OPERATIONS
    transport.native_parser = d1_native_host.parser
    transport._request, transport._response = request, response
    transport._curated_result, transport._spawn_native = terminal, spawn
    transport._announce, transport._Pipe.remaining = announce, remaining
    return transport


def existing(transport, path):
    path = Path(path)
    transport._check(path.is_absolute() and transport.LEAF.fullmatch(path.name), 'unsafe_spool')
    spool = transport.PrivateSpool.__new__(transport.PrivateSpool)
    spool.path, spool.name = path, path.name
    spool.parent_fd = spool.fd = None
    try:
        _, spool.parent_fd = transport._absolute_directory(path.parent)
        spool.parent_identity = os.fstat(spool.parent_fd)
        spool.fd = os.open(path.name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=spool.parent_fd)
        spool.identity = os.fstat(spool.fd)
        spool._intact()
        return spool
    except BaseException:
        spool.close()
        raise

def read(transport, spool, name, *, heartbeat=False):
    spool._intact()
    fd = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=spool.fd)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise PublicationPending('publication_pending') from None
            raise RelayError('unsafe_file') from None
        first = os.fstat(fd)
        transport._check(stat.S_ISREG(first.st_mode) and first.st_nlink == 1 and first.st_uid == os.geteuid()
                         and first.st_mode & 0o077 == 0 and 0 < first.st_size <= MAX, 'unsafe_file')
        chunks = []
        total = 0
        while total <= MAX:
            raw = os.read(fd, min(65536, MAX + 1 - total))
            if not raw: break
            chunks.append(raw); total += len(raw)
        raw = b''.join(chunks)
        last = os.fstat(fd)
        current = os.stat(name, dir_fd=spool.fd, follow_symlinks=False)
        stamp = lambda s: (s.st_dev,s.st_ino,s.st_size) + (() if heartbeat else (s.st_mtime_ns,s.st_ctime_ns))
        transport._check(stamp(first) == stamp(last) == stamp(current) and len(raw) == first.st_size, 'file_changed')
        spool._intact()
        return raw
    finally:
        os.close(fd)

def absent(transport, spool, name):
    try: os.stat(name, dir_fd=spool.fd, follow_symlinks=False)
    except FileNotFoundError: return
    raise transport.RelayError('file_already_exists')

def pending(transport, spool, number):
    transport._check(type(number) is int and 1 <= number <= transport.MAX_REQUESTS)
    absent(transport, spool, 'result.json'); absent(transport, spool, 'response-%d.json' % number)
    raw = read(transport, spool, 'request-%d.json' % number)
    req = transport._decode(raw)
    transport._request(req, None, number)
    state = transport._decode(read(transport, spool, 'pending-%d.json' % number))
    transport._check(set(state) == {'heartbeat', 'deadline_unix_ms', 'request_sha256'}
                     and type(state['deadline_unix_ms']) is int and time.time() * 1000 < state['deadline_unix_ms'], 'relay_deadline_passed')
    transport._check(hashlib.sha256(raw).hexdigest() == state['request_sha256'], 'request_changed')
    heartbeat = 'live-%d.json' % number
    transport._check(state['heartbeat'] == heartbeat, 'invalid_liveness')
    check(read(transport, spool, heartbeat, heartbeat=True) == b'{}\n', 'invalid_liveness')
    info = os.stat(heartbeat, dir_fd=spool.fd, follow_symlinks=False)
    transport._check(0 <= time.time() - info.st_mtime <= 15, 'relay_not_live')
    return req, raw, state

def terminal_metadata(transport, spool):
    value = transport._decode(read(transport, spool, 'result.json'))
    base = {'protocol', 'type', 'session_id', 'request_id', 'ok'}
    transport._check(value.get('protocol') == transport.PROTOCOL and type(value.get('ok')) is bool
        and type(value.get('request_id')) is int and 0 <= value['request_id'] <= transport.MAX_REQUESTS)
    transport._check(value.get('type') in {'result', 'relay_error'})
    if value['ok']:
        transport._check(value['type'] == 'result' and set(value) == base | {'result'} and isinstance(value['result'], dict))
    else:
        transport._check(set(value) == base | {'error'} and isinstance(value['error'], dict)
                         and set(value['error']) == {'code'} and isinstance(value['error']['code'], str)
                         and re.fullmatch('[a-z_]{1,80}', value['error']['code']))
    session, number = value['session_id'], value['request_id']
    transport._check((number == 0 and value['type'] == 'relay_error' and session is None)
                     or (isinstance(session, str) and transport.SESSION.fullmatch(session)))
    if number:
        req = transport._decode(read(transport, spool, 'request-%d.json' % number))
        transport._request(req, session, number)
        if value['ok']:
            transport._response(read(transport, spool, 'response-%d.json' % number), req)
    result = {key:value[key] for key in ('type', 'ok', 'request_id')}
    if not value['ok']:
        code = value['error']['code']
        result['error_code'] = code if code in TERMINAL_ERROR_CODES else 'native_host_failed'
    return result


def io_main(transport, argv=None):
    parser = Parser()
    parser.add_argument('action', choices=('poll','claim','read','preflight','append','publish','result'))
    parser.add_argument('--spool', type=Path, required=True)
    parser.add_argument('--number', type=int)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--count', type=int, default=32768)
    parser.add_argument('--text-json')
    args = parser.parse_args(argv)
    with existing(transport, args.spool) as spool:
        if args.action == 'poll':
            try:
                value = terminal_metadata(transport, spool)
                return {'state':'done', 'ok':value['ok'], 'request_id':value['request_id'],
                        **({'error_code': value['error_code']} if not value['ok'] else {})}
            except PublicationPending:
                return {'state':'waiting', 'request_id':args.number}
            except FileNotFoundError:
                pass
            try:
                pending(transport, spool, args.number)
                return {'state':'ready', 'request_id':args.number}
            except (FileNotFoundError, PublicationPending):
                return {'state':'waiting', 'request_id':args.number}
        if args.action == 'result':
            return terminal_metadata(transport, spool)
        req, raw, state = pending(transport, spool, args.number)
        claim_name = 'claim-%d.json' % args.number
        if args.action == 'claim':
            spool.save(claim_name, transport._encode({'request_sha256':state['request_sha256']}))
            return {'request_id':args.number,'operation':req['operation'],'request_bytes':len(raw),
                    'request_sha256':state['request_sha256']}
        claim = transport._decode(read(transport, spool, claim_name))
        transport._check(claim == {'request_sha256':state['request_sha256']}, 'claim_changed')
        if args.action == 'preflight':
            transport._check(state['deadline_unix_ms'] - time.time() * 1000 >= 1000, 'relay_deadline_passed')
            spool.save('invoke-%d.json' % args.number, transport._encode(claim))
            return {'ready':True, 'request_id':args.number, 'deadline_unix_ms':state['deadline_unix_ms']}
        if args.action == 'read':
            text = raw.decode('utf-8')
            transport._check(0 <= args.offset <= len(text) and 1 <= args.count <= 131072)
            part = text[args.offset:args.offset + args.count]
            return {'offset':args.offset,'next':args.offset + len(part),'total':len(text),'text':part}
        temporary = 'response-%d.partial' % args.number
        if args.action == 'append':
            text = json.loads(args.text_json)
            transport._check(isinstance(text,str) and len(text) <= 32768)
            part = text.encode('utf-8')
            transport._check(0 <= args.offset <= MAX and args.offset + len(part) <= MAX, 'response_too_large')
            flags = os.O_WRONLY|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if args.offset == 0 else 0)
            fd = os.open(temporary, flags, 0o600, dir_fd=spool.fd)
            try:
                info = os.fstat(fd)
                transport._check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                                 and info.st_mode & 0o077 == 0 and info.st_size == args.offset, 'invalid_response_offset')
                os.lseek(fd, 0, os.SEEK_END)
                with os.fdopen(fd, 'wb', closefd=False) as handle:
                    handle.write(part); handle.flush(); os.fsync(fd)
            finally: os.close(fd)
            return {'next':args.offset + len(part)}
        raw = read(transport, spool, temporary)
        raw = transport._response(raw, req)
        spool.save('response-%d.json' % args.number, raw)
        return {'published':True,'request_id':args.number,'response_bytes':len(raw)}


JOURNAL_FORMAT = 'academic-radar-read-backup-journal-v1'


def _journal_state(transport, path, prefix, original_source):
    """Read a closed original run; never accept an uncertain native tail."""
    check(type(prefix) is int and 1 <= prefix < transport.MAX_REQUESTS, 'invalid_journal_prefix')
    check(type(original_source) is dict and set(original_source) == {'root', 'manifest', 'sha256'}, 'invalid_journal_source')
    original_files = verify_source(Path(original_source['manifest']), original_source['sha256'], Path(original_source['root']))
    current_files = inventory()
    check({k:v for k,v in original_files.items() if k.startswith('src/academic_radar/')} ==
          {k:v for k,v in current_files.items() if k.startswith('src/academic_radar/')}, 'journal_backup_source_changed')
    with existing(transport, path) as spool:
        required = {'result.json'}
        optional = set()
        for number in range(1, prefix + 2):
            required.update('%s-%d.json' % (kind, number) for kind in ('request', 'response', 'pending', 'claim', 'live'))
            optional.add('response-%d.partial' % number)
            if number <= prefix:
                required.add('invoke-%d.json' % number)
        names = set(os.listdir(spool.fd))
        check(required <= names <= required | optional, 'invalid_journal_inventory')
        files = {}
        def member(name):
            raw = read(transport, spool, name)
            entry = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            check(name not in files or files[name] == entry, 'journal_changed')
            files[name] = entry
            return raw
        session, first = None, None
        response_bytes = 0
        for number in range(1, prefix + 2):
            request_raw = member('request-%d.json' % number)
            req = transport._decode(request_raw)
            session = transport._request(req, session, number)
            check(req['operation'] == 'site.read_site_backup', 'journal_operation_forbidden')
            expected = {'request_sha256': hashlib.sha256(request_raw).hexdigest()}
            state = transport._decode(member('pending-%d.json' % number))
            check(set(state) == {'heartbeat', 'deadline_unix_ms', 'request_sha256'}
                  and state['heartbeat'] == 'live-%d.json' % number
                  and type(state['deadline_unix_ms']) is int
                  and state['request_sha256'] == expected['request_sha256'], 'invalid_journal_pending')
            check(member('live-%d.json' % number) == b'{}\n', 'invalid_journal_heartbeat')
            check(transport._decode(member('claim-%d.json' % number)) == expected, 'invalid_journal_claim')
            response_raw = member('response-%d.json' % number)
            check(transport._response(response_raw, req) == response_raw, 'noncanonical_journal_response')
            value = transport._decode(response_raw)
            if number <= prefix:
                check(value['ok'] is True, 'journal_response_failed')
                check(transport._decode(member('invoke-%d.json' % number)) == expected, 'invalid_journal_invocation')
                response_bytes += len(response_raw)
                if number == 1:
                    check(req['arguments'] == {'action': 'begin', 'data': {}}, 'journal_begin_required')
                    first = value['result']
                else:
                    check(req['arguments']['action'] != 'begin', 'journal_snapshot_recapture')
            else:
                check(value['ok'] is False and value['error'] == {'code': 'host_action_failed'}, 'journal_tail_not_local_cancel')
                absent(transport, spool, 'invoke-%d.json' % number)
            partial = 'response-%d.partial' % number
            if partial in names:
                partial_raw = member(partial)
                if number <= prefix:
                    check(partial_raw == response_raw, 'journal_partial_mismatch')
                else:
                    check(transport._decode(partial_raw).get('error') == {'code': 'host_action_failed'}
                          and transport._response(partial_raw, req) == response_raw, 'journal_partial_mismatch')
        terminal_raw = member('result.json')
        terminal = transport._decode(terminal_raw)
        check(terminal == {'protocol': transport.PROTOCOL, 'type': 'result', 'session_id': session,
              'request_id': prefix + 1, 'ok': False, 'error': {'code': 'native_host_failed'}}, 'journal_not_closed_locally')
        from academic_radar import d1_backup
        _snapshot, snapshot_sha = d1_backup._capture(lambda action, data: first, require_main=False)
        captured_at = first.get('captured_at')
        check(isinstance(captured_at, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z', captured_at), 'journal_capture_time_missing')
        # Verify all bytes again after validation and bind the complete closed inventory.
        check(set(os.listdir(spool.fd)) == names, 'journal_changed')
        for name in sorted(names):
            member(name)
        return {'format': JOURNAL_FORMAT, 'source_spool': str(Path(path)), 'original_source': original_source,
                'session_id': session, 'prefix': prefix, 'files': files, 'snapshot_sha256': snapshot_sha,
                'captured_at': captured_at, 'cached_response_bytes': response_bytes}


def seal_journal(transport, path, prefix, output, original_source):
    value = _journal_state(transport, path, prefix, original_source)
    output = Path(output)
    check(output.is_absolute() and Path(path) not in output.parents, 'journal_must_be_external')
    raw = canonical(value)
    check(len(raw) <= MAX, 'journal_manifest_too_large')
    parent = directory(output.parent)
    try:
        fd = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fchmod(handle.fileno(), 0o400); os.fsync(handle.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)
    return {'journal_sha256': hashlib.sha256(raw).hexdigest(), 'replayed_reads': prefix,
            'cached_response_bytes': value['cached_response_bytes'], 'captured_at': value['captured_at'],
            'snapshot_sha256': value['snapshot_sha256'], 'next': prefix + 1}


class ReadJournal:
    def __init__(self, transport, path, expected):
        check(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected), 'invalid_journal_pin')
        path = Path(path)
        raw = file_bytes(path, MAX)
        parent = directory(path.parent)
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                  and stat.S_IMODE(info.st_mode) == 0o400, 'unsafe_journal_manifest')
        finally:
            os.close(parent)
        check(hashlib.sha256(raw).hexdigest() == expected, 'journal_pin_changed')
        self.value = json.loads(raw, object_pairs_hook=unique_pairs)
        check(isinstance(self.value, dict) and set(self.value) == {'format', 'source_spool', 'original_source',
              'session_id', 'prefix', 'files', 'snapshot_sha256', 'captured_at', 'cached_response_bytes'}
              and self.value['format'] == JOURNAL_FORMAT, 'invalid_journal_manifest')
        self.transport, self.sha, self.replayed, self.exhausted = transport, expected, 0, False
        self.replayed_bytes = 0
        self.verify()

    def verify(self):
        check(_journal_state(self.transport, self.value['source_spool'], self.value['prefix'],
              self.value['original_source']) == self.value, 'journal_changed')

    def response(self, req):
        check(req['operation'] == 'site.read_site_backup', 'journal_operation_forbidden')
        number = req['request_id']
        check(req['session_id'] != self.value['session_id'], 'journal_fresh_session_required')
        if self.exhausted:
            check(number > self.value['prefix'] + 1, 'journal_replay_revisited')
            return None
        check(number == self.replayed + 1, 'journal_replay_gap')
        with existing(self.transport, self.value['source_spool']) as old:
            def bound(name):
                raw = read(self.transport, old, name)
                check(self.value['files'].get(name) == {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}, 'journal_changed')
                return raw
            previous = self.transport._decode(bound('request-%d.json' % number))
            expected = dict(previous, session_id=req['session_id'])
            check(req == expected, 'journal_request_mismatch')
            if number == self.value['prefix'] + 1:
                self.verify()
                self.exhausted = True
                return None
            value = self.transport._decode(bound('response-%d.json' % number))
            check(value['ok'] is True, 'journal_response_failed')
            value['session_id'] = req['session_id']
            raw = self.transport._encode(value)
            self.transport._response(raw, req)
            self.replayed += 1
            self.replayed_bytes += len(raw)
            return raw

    def summary(self):
        return {'journal_sha256': self.sha, 'replayed_reads': self.replayed,
                'replayed_response_bytes': self.replayed_bytes, 'cached_response_bytes': self.value['cached_response_bytes'],
                'captured_at': self.value['captured_at'], 'snapshot_sha256': self.value['snapshot_sha256']}


COMBINED_JOURNAL_FORMAT = 'academic-radar-combined-read-backup-journal-v1'
COMBINED_READS = frozenset({'site.read_main_store', 'site.read_site_backup'})


def recovery_timestamp(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)',value):
        return False
    try:
        return datetime.fromisoformat(value.replace('Z','+00:00')).utcoffset() is not None
    except ValueError:
        return False


def private_external(path, expected=None):
    """Read immutable evidence without accepting symlinks or shared files."""
    path = Path(path)
    raw = file_bytes(path, MAX)
    parent = directory(path.parent)
    try:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
              and info.st_mode & 0o077 == 0, 'unsafe_recovery_evidence')
    finally:
        os.close(parent)
    if expected is not None:
        check(isinstance(expected, str) and re.fullmatch('[a-f0-9]{64}', expected)
              and hashlib.sha256(raw).hexdigest() == expected, 'recovery_evidence_changed')
    return raw


def external_entry(path, raw):
    return {'path': str(Path(path)), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def write_once_external(path, raw):
    path = Path(path)
    parent = directory(path.parent)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fchmod(handle.fileno(), 0o400); os.fsync(handle.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def _combined_state(transport, path, prefix, original_source, receipt_path, receipt_sha, retry_fence):
    """Closed combined export plus an already-successful, separately authorized read.

    This format grants no retry. The failed boundary is always replayed locally
    from the bound successful evidence; its original failure is never rewritten.
    """
    check(type(prefix) is int and 3 <= prefix < transport.MAX_REQUESTS - 1, 'invalid_combined_prefix')
    check(type(original_source) is dict and set(original_source) == {'root', 'manifest', 'sha256'}, 'invalid_journal_source')
    original_files = verify_source(Path(original_source['manifest']), original_source['sha256'], Path(original_source['root']))
    check({k:v for k,v in original_files.items() if k.startswith('src/academic_radar/')} ==
          {k:v for k,v in inventory().items() if k.startswith('src/academic_radar/')}, 'journal_backup_source_changed')
    receipt_raw = private_external(receipt_path, receipt_sha)
    receipt = json.loads(receipt_raw, object_pairs_hook=unique_pairs)
    check(type(receipt) is dict and set(receipt) == {'format', 'original_spool', 'original_request_id',
          'request_sha256', 'failed_response_sha256', 'terminal_sha256', 'source_manifest_sha256',
          'reserved_fence_sha256', 'retry_count', 'outcome', 'recorded_at', 'native_elapsed_ms',
          'successful_response'}, 'invalid_recovery_receipt')
    check(receipt['format'] == 'academic-radar-diagnostic-read-retry-v1' and receipt['outcome'] == 'success'
          and type(receipt['retry_count']) is int and receipt['retry_count'] == 1
          and receipt['original_spool'] == str(Path(path)) and receipt['original_request_id'] == prefix + 1
          and receipt['source_manifest_sha256'] == original_source['sha256']
          and type(receipt['native_elapsed_ms']) is int and 0 <= receipt['native_elapsed_ms'] <= 86400000
          and recovery_timestamp(receipt['recorded_at']), 'invalid_recovery_receipt')
    success = receipt['successful_response']
    check(type(success) is dict and set(success) == {'path', 'bytes', 'sha256'}, 'invalid_recovery_receipt')
    success_raw = private_external(success['path'], success['sha256'])
    check(success == external_entry(success['path'], success_raw), 'recovery_response_changed')
    fence_raw = private_external(retry_fence, receipt['reserved_fence_sha256'])
    fence = json.loads(fence_raw, object_pairs_hook=unique_pairs)
    check(type(fence) is dict and set(fence) == {'state', 'original_spool', 'original_request_id', 'request_sha256', 'at'}
          and fence['state'] == 'one_diagnostic_retry_reserved' and fence['original_spool'] == Path(path).name
          and fence['original_request_id'] == prefix + 1 and fence['request_sha256'] == receipt['request_sha256']
          and recovery_timestamp(fence['at']), 'invalid_recovery_fence')
    with existing(transport, path) as spool:
        required, optional = {'result.json'}, set()
        for number in range(1, prefix + 2):
            required.update('%s-%d.json' % (kind, number) for kind in ('request', 'response', 'pending', 'claim', 'live', 'invoke'))
            optional.add('response-%d.partial' % number)
        names = set(os.listdir(spool.fd))
        check(required <= names <= required | optional, 'invalid_journal_inventory')
        files = {}
        def member(name):
            raw = read(transport, spool, name)
            entry = {'bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()}
            check(name not in files or files[name] == entry, 'journal_changed')
            files[name] = entry
            return raw
        session, begin, initial_head, response_bytes = None, None, None, 0
        for number in range(1, prefix + 2):
            raw = member('request-%d.json' % number)
            req = transport._decode(raw)
            session = transport._request(req, session, number)
            check(raw == transport._encode(req), 'noncanonical_journal_request')
            check(req['operation'] in COMBINED_READS, 'journal_operation_forbidden')
            expected = {'request_sha256':hashlib.sha256(raw).hexdigest()}
            state = transport._decode(member('pending-%d.json' % number))
            check(set(state) == {'heartbeat', 'deadline_unix_ms', 'request_sha256'}
                  and state['heartbeat'] == 'live-%d.json' % number and type(state['deadline_unix_ms']) is int
                  and state['request_sha256'] == expected['request_sha256'], 'invalid_journal_pending')
            check(member('live-%d.json' % number) == b'{}\n', 'invalid_journal_heartbeat')
            for kind in ('claim', 'invoke'):
                check(transport._decode(member('%s-%d.json' % (kind, number))) == expected, 'invalid_journal_claim')
            response_raw = member('response-%d.json' % number)
            check(transport._response(response_raw, req) == response_raw, 'noncanonical_journal_response')
            value = transport._decode(response_raw)
            if number <= prefix:
                check(value['ok'] is True, 'journal_response_failed')
                response_bytes += len(response_raw)
            else:
                check(req['operation'] == 'site.read_site_backup' and req['arguments']['action'] == 'records'
                      and value['ok'] is False and value['error'] == {'code':'host_action_failed'}, 'invalid_combined_failed_tail')
                check(receipt['request_sha256'] == expected['request_sha256']
                      and receipt['failed_response_sha256'] == hashlib.sha256(response_raw).hexdigest(), 'recovery_binding_changed')
                check(transport._response(success_raw, req) == success_raw and transport._decode(success_raw)['ok'] is True,
                      'recovery_response_failed')
                response_bytes += len(success_raw)
            if number == 1:
                check(req['operation'] == 'site.read_main_store' and req['arguments'] == {'action':'head', 'data':{}},
                      'combined_schema_sequence_changed')
                initial_head = value['result']['head']
            elif number == 2:
                check(req['operation'] == 'site.read_main_store'
                      and req['arguments'] == {'action':'read_manifest', 'data':{'revision':initial_head['revision']}},
                      'combined_schema_sequence_changed')
            elif number == 3:
                check(req['operation'] == 'site.read_site_backup' and req['arguments'] == {'action':'begin', 'data':{}},
                      'journal_begin_required')
                begin = value['result']
            else:
                check(req['operation'] != 'site.read_site_backup' or req['arguments']['action'] != 'begin', 'journal_snapshot_recapture')
            partial = 'response-%d.partial' % number
            if partial in names:
                partial_raw = member(partial)
                if number <= prefix:
                    check(partial_raw == response_raw, 'journal_partial_mismatch')
                else:
                    check(transport._decode(partial_raw).get('error') in
                          ({'code':'native_rejected'}, {'code':'host_action_failed'})
                          and transport._response(partial_raw, req) == response_raw, 'journal_partial_mismatch')
        terminal_raw = member('result.json')
        check(transport._decode(terminal_raw) == {'protocol':transport.PROTOCOL, 'type':'result', 'session_id':session,
              'request_id':prefix+1, 'ok':False, 'error':{'code':'native_host_failed'}}, 'journal_not_closed')
        check(receipt['terminal_sha256'] == hashlib.sha256(terminal_raw).hexdigest(), 'recovery_binding_changed')
        from academic_radar import d1_backup
        snapshot, snapshot_sha = d1_backup._capture(lambda action, data: begin, require_main=True)
        check(initial_head['revision'] == snapshot['main_head']['revision']
              and initial_head['import_id'] == snapshot['main_head']['import_id'], 'combined_initial_head_changed')
        captured_at = begin.get('captured_at')
        check(isinstance(captured_at,str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z',captured_at),
              'journal_capture_time_missing')
        # Pin the first uncached call, including its exact continuation cursor.
        page = transport._decode(success_raw)['result']
        generation = req['arguments']['data']['generation']
        generations = [entry['id'] for entry in snapshot['generations']]
        check(generation in generations and page.get('generation') == generation
              and page.get('expected_count') == snapshot['generations'][generations.index(generation)]['count']
              and type(page.get('done')) is bool and type(page.get('chunks')) is list, 'recovery_page_changed')
        if not page['done']:
            check(bool(page['chunks']), 'recovery_page_changed')
            fresh_args = {'action':'records','data':{'generation':generation,'after':page.get('next')}}
        else:
            check(page['chunks'] == [] and page.get('next') is None, 'recovery_page_changed')
            index = generations.index(generation) + 1
            fresh_args = ({'action':'records','data':{'generation':generations[index]}} if index < len(generations)
                          else {'action':'feedback','data':{'after':0,'until':snapshot['feedback_cursor']}})
        first_fresh = {'operation':'site.read_site_backup','arguments':fresh_args}
        transport._request(dict(req,request_id=prefix+2,**first_fresh),session,prefix+2)
        check(set(os.listdir(spool.fd)) == names, 'journal_changed')
        for name in sorted(names): member(name)
    # Recheck all external evidence after the original closed inventory was read.
    check(private_external(receipt_path,receipt_sha) == receipt_raw
          and private_external(success['path'],success['sha256']) == success_raw
          and private_external(retry_fence,receipt['reserved_fence_sha256']) == fence_raw, 'recovery_evidence_changed')
    resume_fence = Path(success['path']).parent / ('combined-resume-' + receipt['request_sha256'] + '.json')
    return {'format':COMBINED_JOURNAL_FORMAT, 'source_spool':str(Path(path)), 'original_source':original_source,
            'session_id':session, 'prefix':prefix+1, 'original_successful_prefix':prefix, 'files':files,
            'snapshot_sha256':snapshot_sha, 'captured_at':captured_at, 'snapshot':snapshot,
            'cached_response_bytes':response_bytes, 'retry_receipt':external_entry(receipt_path,receipt_raw),
            'retry_fence':external_entry(retry_fence,fence_raw), 'successful_response':success,
            'resume_fence':str(resume_fence), 'first_fresh_call':first_fresh}


def seal_combined_journal(transport,path,prefix,output,original_source,receipt_path,receipt_sha,retry_fence):
    value = _combined_state(transport,path,prefix,original_source,receipt_path,receipt_sha,retry_fence)
    output = Path(output)
    check(output.is_absolute() and Path(path) not in output.parents, 'journal_must_be_external')
    raw = canonical(value)
    check(len(raw) <= MAX, 'journal_manifest_too_large')
    write_once_external(output,raw)
    return {'journal_sha256':hashlib.sha256(raw).hexdigest(), 'replayed_reads':value['prefix'],
            'original_successful_reads':prefix, 'diagnostic_success_replayed':1, 'next':value['prefix']+1,
            'snapshot_sha256':value['snapshot_sha256'], 'captured_at':value['captured_at']}


class CombinedReadJournal:
    def __init__(self,transport,path,expected):
        raw = private_external(path,expected)
        check(stat.S_IMODE(os.stat(path,follow_symlinks=False).st_mode) == 0o400, 'unsafe_journal_manifest')
        self.value = json.loads(raw,object_pairs_hook=unique_pairs)
        check(type(self.value) is dict and set(self.value) == {'format','source_spool','original_source','session_id',
              'prefix','original_successful_prefix','files','snapshot_sha256','captured_at','snapshot',
              'cached_response_bytes','retry_receipt','retry_fence','successful_response','resume_fence','first_fresh_call'}
              and self.value['format'] == COMBINED_JOURNAL_FORMAT, 'invalid_combined_journal')
        self.transport,self.sha,self.replayed,self.replayed_bytes,self.exhausted = transport,expected,0,0,False
        self.reserved = False
        self.new_session = None
        self.next_fresh = self.value['prefix'] + 1
        self.verify()

    def verify(self):
        value = self.value
        check(_combined_state(self.transport,value['source_spool'],value['original_successful_prefix'],
              value['original_source'],value['retry_receipt']['path'],value['retry_receipt']['sha256'],
              value['retry_fence']['path']) == value, 'journal_changed')

    def reserve(self,new_spool):
        check(not self.reserved, 'combined_resume_already_reserved')
        new_spool = Path(new_spool)
        fd = directory(new_spool.parent)
        try:
            check(self.transport.LEAF.fullmatch(new_spool.name) is not None, 'unsafe_spool')
            try: os.stat(new_spool.name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise RelayError('combined_resume_spool_exists')
        finally: os.close(fd)
        self.verify()
        write_once_external(self.value['resume_fence'],canonical({'format':'academic-radar-combined-resume-once-v1',
            'journal_sha256':self.sha,'original_session_id':self.value['session_id'],
            'retry_receipt_sha256':self.value['retry_receipt']['sha256'],'new_spool':str(new_spool),
            'first_fresh_request':self.value['prefix']+1}))
        self.reserved = True

    def _fresh(self,req):
        check(req['operation'] == 'site.read_site_backup', 'combined_fresh_operation_forbidden')
        args,snapshot = req['arguments'],self.value['snapshot']
        action,data = args['action'],args['data']
        if action == 'records':
            check(data['generation'] in {entry['id'] for entry in snapshot['generations']}, 'combined_snapshot_changed')
        elif action in {'feedback','profiles','event_chunks'}:
            stream = data['stream'] if action == 'event_chunks' else action
            check(stream in {'feedback','profiles'}, 'combined_snapshot_changed')
            limit = snapshot['feedback_cursor' if stream == 'feedback' else 'profile_cursor']
            check(data['until'] == limit, 'combined_snapshot_changed')
            if action == 'event_chunks': check(1 <= data['seq'] <= limit, 'combined_snapshot_changed')
            else: check(data['after'] <= limit, 'combined_snapshot_changed')
        else: raise RelayError('journal_snapshot_recapture')

    def response(self,req):
        check(self.reserved, 'explicit_combined_resume_reservation_required')
        check(req['operation'] in COMBINED_READS, 'journal_operation_forbidden')
        number = req['request_id']
        check(req['session_id'] != self.value['session_id'], 'journal_fresh_session_required')
        if self.new_session is None: self.new_session = req['session_id']
        check(req['session_id'] == self.new_session, 'journal_session_changed')
        if number > self.value['prefix']:
            check(self.replayed == self.value['prefix'] and number == self.next_fresh, 'journal_replay_gap')
            if not self.exhausted:
                self.verify()
                check({key:req[key] for key in ('operation','arguments')} == self.value['first_fresh_call'],
                      'combined_first_fresh_mismatch')
            self._fresh(req)
            self.next_fresh += 1
            self.exhausted = True
            return None
        check(not self.exhausted and number == self.replayed+1, 'journal_replay_gap')
        with existing(self.transport,self.value['source_spool']) as old:
            def bound(name):
                raw = read(self.transport,old,name)
                check(self.value['files'].get(name) == {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}, 'journal_changed')
                return raw
            previous = self.transport._decode(bound('request-%d.json' % number))
            check(req == dict(previous,session_id=req['session_id']), 'journal_request_mismatch')
            if number == self.value['prefix']:
                raw = private_external(self.value['successful_response']['path'],self.value['successful_response']['sha256'])
                bound('response-%d.json' % number) # Retain the original failure binding too.
            else: raw = bound('response-%d.json' % number)
            value = self.transport._decode(raw)
            check(value['ok'] is True, 'journal_response_failed')
            value['session_id'] = req['session_id']
            raw = self.transport._encode(value)
            self.transport._response(raw,req)
            self.replayed += 1; self.replayed_bytes += len(raw)
            return raw

    def receipt_fields(self,number):
        if number != self.value['prefix']: return {}
        return {'original_response_sha256':self.value['successful_response']['sha256'],
                'original_failed_response_sha256':self.value['files']['response-%d.json' % number]['sha256'],
                'retry_receipt_sha256':self.value['retry_receipt']['sha256'], 'diagnostic_success_replayed':True}

    def summary(self):
        return {'journal_sha256':self.sha,'replayed_reads':self.replayed,'replayed_response_bytes':self.replayed_bytes,
                'cached_response_bytes':self.value['cached_response_bytes'],'captured_at':self.value['captured_at'],
                'snapshot_sha256':self.value['snapshot_sha256'],'original_successful_reads':self.value['original_successful_prefix'],
                'diagnostic_success_replayed':1,'first_fresh_request':self.value['prefix']+1}


TIMEOUT_JOURNAL_FORMAT = 'academic-radar-timeout-backup-journal-v1'
MAX_TIMEOUT_CHAIN = 16


def _timeout_state(transport, path, prefix, original_source, previous=None, *, ancestors=()):
    """Seal successful reads only; an expired, never-invoked tail is not retried."""
    check(type(prefix) is int and 3 <= prefix < transport.MAX_REQUESTS, 'invalid_timeout_prefix')
    check(type(original_source) is dict and set(original_source) == {'root','manifest','sha256'}, 'invalid_journal_source')
    original_files = verify_source(Path(original_source['manifest']), original_source['sha256'], Path(original_source['root']))
    check({k:v for k,v in original_files.items() if k != 'scripts/d1-native-relay.py'} ==
          {k:v for k,v in inventory().items() if k != 'scripts/d1-native-relay.py'}, 'journal_backup_source_changed')
    path = Path(path)
    check(str(path) not in ancestors and len(ancestors) < MAX_TIMEOUT_CHAIN, 'timeout_journal_cycle_or_limit')
    parent = None
    if previous is not None:
        check(type(previous) is dict and set(previous) == {'path','sha256'}, 'invalid_previous_journal')
        parent = TimeoutReadJournal(transport, previous['path'], previous['sha256'], ancestors=(*ancestors,str(path)))
        check(original_source == parent.value['original_source'], 'timeout_source_anchor_changed')
        check(prefix > parent.value['prefix'], 'timeout_resume_made_no_progress')
        reservation = json.loads(private_external(parent.value['resume_fence']), object_pairs_hook=unique_pairs)
        check(reservation == parent.reservation(path), 'timeout_resume_binding_changed')
    with existing(transport,path) as spool:
        required, optional = {'result.json'}, set()
        for number in range(1,prefix+2):
            required.add('request-%d.json' % number)
            if parent is not None and number <= parent.value['prefix']:
                required.update('%s-%d.json' % (kind,number) for kind in ('claim','replay','response'))
            elif number <= prefix:
                required.update('%s-%d.json' % (kind,number) for kind in ('pending','live','claim','invoke','response'))
                optional.add('response-%d.partial' % number)
            else:
                # Timeout may occur before announcement, after publication, or
                # after claim. An invocation/response is NEVER optional here.
                optional.update('%s-%d.json' % (kind,number) for kind in ('pending','live','claim'))
        if parent is not None: required.add('replay-summary.json')
        names = set(os.listdir(spool.fd))
        check(required <= names <= required|optional, 'invalid_timeout_inventory')
        files = {}
        def member(name):
            raw = read(transport,spool,name)
            entry = {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            check(name not in files or files[name] == entry, 'journal_changed')
            files[name] = entry
            return raw
        session, begin, initial_head, response_bytes = None,None,None,0
        for number in range(1,prefix+2):
            request_raw = member('request-%d.json' % number)
            req = transport._decode(request_raw)
            session = transport._request(req,session,number)
            check(request_raw == transport._encode(req) and req['operation'] in COMBINED_READS, 'journal_operation_forbidden')
            claim = {'request_sha256':hashlib.sha256(request_raw).hexdigest()}
            if 'pending-%d.json' % number in names:
                state = transport._decode(member('pending-%d.json' % number))
                check(set(state) == {'heartbeat','deadline_unix_ms','request_sha256'} and
                      state['heartbeat'] == 'live-%d.json' % number and type(state['deadline_unix_ms']) is int and
                      state['deadline_unix_ms'] >= 0 and state['request_sha256'] == claim['request_sha256'], 'invalid_journal_pending')
                check(member('live-%d.json' % number) == b'{}\n', 'invalid_journal_heartbeat')
                if number > prefix: check(state['deadline_unix_ms'] <= time.time()*1000, 'timeout_deadline_not_elapsed')
            else:
                check('live-%d.json' % number not in names, 'invalid_journal_pending')
            if 'claim-%d.json' % number in names:
                check(transport._decode(member('claim-%d.json' % number)) == claim, 'invalid_journal_claim')
                if number > prefix: check('pending-%d.json' % number in names, 'invalid_journal_pending')
            if number > prefix:
                check(req['operation'] == 'site.read_site_backup' and req['arguments']['action'] != 'begin', 'timeout_tail_must_be_site_read')
                tail = {key:req[key] for key in ('operation','arguments')}
                continue
            response_raw = member('response-%d.json' % number)
            check(transport._response(response_raw,req) == response_raw, 'noncanonical_journal_response')
            value = transport._decode(response_raw)
            check(value['ok'] is True, 'journal_response_failed')
            response_bytes += len(response_raw)
            if parent is not None and number <= parent.value['prefix']:
                old_req,old_raw = parent.bound_pair(number)
                check(req == dict(old_req,session_id=session) and
                      value == dict(transport._decode(old_raw),session_id=session), 'timeout_replay_changed')
                receipt = {'journal_sha256':parent.sha,'original_session_id':parent.value['session_id'],
                    'original_request_id':number,'new_session_id':session,
                    'original_request_sha256':parent.value['files']['request-%d.json' % number]['sha256'],
                    'new_request_sha256':claim['request_sha256'],
                    'new_response_sha256':hashlib.sha256(response_raw).hexdigest(),
                    'original_response_sha256':parent.value['files']['response-%d.json' % number]['sha256']}
                check(transport._decode(member('replay-%d.json' % number)) == receipt, 'timeout_replay_changed')
            else:
                check(transport._decode(member('invoke-%d.json' % number)) == claim, 'invalid_journal_invocation')
                partial = 'response-%d.partial' % number
                if partial in names: check(member(partial) == response_raw, 'journal_partial_mismatch')
                if parent is not None:
                    if number == parent.value['prefix']+1:
                        check({key:req[key] for key in ('operation','arguments')} == parent.value['first_fresh_call'], 'timeout_first_fresh_changed')
                    parent._fresh(req)
            if number == 1:
                check(req['operation'] == 'site.read_main_store' and req['arguments'] == {'action':'head','data':{}}, 'combined_schema_sequence_changed')
                initial_head = value['result']['head']
            elif number == 2:
                check(req['operation'] == 'site.read_main_store' and req['arguments'] ==
                      {'action':'read_manifest','data':{'revision':initial_head['revision']}}, 'combined_schema_sequence_changed')
            elif number == 3:
                check(req['operation'] == 'site.read_site_backup' and req['arguments'] == {'action':'begin','data':{}}, 'journal_begin_required')
                begin = value['result']
            else:
                check(req['operation'] != 'site.read_site_backup' or req['arguments']['action'] != 'begin', 'journal_snapshot_recapture')
        check(transport._decode(member('result.json')) == {'protocol':transport.PROTOCOL,'type':'relay_error',
              'session_id':session,'request_id':prefix+1,'ok':False,'error':{'code':'relay_timeout'}}, 'journal_not_closed_timeout')
        from academic_radar import d1_backup
        snapshot,snapshot_sha = d1_backup._capture(lambda action,data:begin,require_main=True)
        check(initial_head['revision'] == snapshot['main_head']['revision'] and
              initial_head['import_id'] == snapshot['main_head']['import_id'], 'combined_initial_head_changed')
        captured_at = begin.get('captured_at')
        check(recovery_timestamp(captured_at), 'journal_capture_time_missing')
        if parent is not None:
            check(session not in {v['session_id'] for v in parent.lineage()}, 'journal_fresh_session_required')
            check(snapshot == parent.value['snapshot'] and captured_at == parent.value['captured_at'], 'timeout_snapshot_changed')
            summary = transport._decode(member('replay-summary.json'))
            expected_summary = parent.summary()
            expected_summary['replayed_reads'] = parent.value['prefix']
            expected_summary['replayed_response_bytes'] = sum(files['response-%d.json'%i]['bytes'] for i in range(1,parent.value['prefix']+1))
            check(summary == expected_summary, 'timeout_replay_changed')
        check(set(os.listdir(spool.fd)) == names, 'journal_changed')
        for name in sorted(names): member(name)
    value = {'format':TIMEOUT_JOURNAL_FORMAT,'source_spool':str(path),'original_source':original_source,
             'session_id':session,'prefix':prefix,'files':files,'snapshot_sha256':snapshot_sha,'captured_at':captured_at,
             'snapshot':snapshot,'cached_response_bytes':response_bytes,'first_fresh_call':tail,'previous':previous,
             'resume_fence':str(Path(original_source['manifest']).parent/('timeout-resume-'+session+'.json'))}
    # Apply the same closed capture scope to the pending boundary before sealing.
    probe = object.__new__(TimeoutReadJournal);probe.value = value;probe._fresh(req)
    return value


def seal_timeout_journal(transport,path,prefix,output,original_source,previous=None):
    value = _timeout_state(transport,path,prefix,original_source,previous)
    output = Path(output)
    check(output.is_absolute() and Path(path) not in output.parents, 'journal_must_be_external')
    probe = object.__new__(TimeoutReadJournal);probe.value = value
    probe.validate_destinations(output)
    raw = canonical(value);check(len(raw) <= MAX, 'journal_manifest_too_large')
    write_once_external(output,raw)
    return {'journal_sha256':hashlib.sha256(raw).hexdigest(),'replayed_reads':prefix,'next':prefix+1,
            'cached_response_bytes':value['cached_response_bytes'],'snapshot_sha256':value['snapshot_sha256'],
            'captured_at':value['captured_at'],'boundary':'never_invoked'}


class TimeoutReadJournal:
    """One finite continuation; successful continuations can form a sealed chain."""
    _fresh = CombinedReadJournal._fresh

    def __init__(self,transport,path,expected,*,ancestors=()):
        raw = private_external(path,expected)
        check(stat.S_IMODE(os.stat(path,follow_symlinks=False).st_mode) == 0o400, 'unsafe_journal_manifest')
        self.value = json.loads(raw,object_pairs_hook=unique_pairs)
        check(type(self.value) is dict and set(self.value) == {'format','source_spool','original_source','session_id',
              'prefix','files','snapshot_sha256','captured_at','snapshot','cached_response_bytes','first_fresh_call',
              'previous','resume_fence'} and self.value['format'] == TIMEOUT_JOURNAL_FORMAT, 'invalid_timeout_journal')
        self.transport,self.sha,self.ancestors = transport,expected,ancestors
        self.replayed,self.replayed_bytes,self.exhausted = 0,0,False
        self.reserved,self.new_session = False,None
        self.next_fresh = self.value['prefix']+1
        self.verify()
        self.old_sessions = {v['session_id'] for v in self.lineage()}

    def verify(self):
        v = self.value
        check(_timeout_state(self.transport,v['source_spool'],v['prefix'],v['original_source'],v['previous'],
                             ancestors=self.ancestors) == v, 'journal_changed')

    def lineage(self):
        values = [self.value]
        while values[-1]['previous'] is not None:
            check(len(values) < MAX_TIMEOUT_CHAIN, 'timeout_journal_cycle_or_limit')
            previous = values[-1]['previous']
            value = json.loads(private_external(previous['path'],previous['sha256']), object_pairs_hook=unique_pairs)
            check(value['source_spool'] not in {v['source_spool'] for v in values}, 'timeout_journal_cycle_or_limit')
            values.append(value)
        return values

    def validate_destinations(self,*paths):
        for path in map(Path,paths):
            check(path.is_absolute() and '..' not in path.parts, 'invalid_resume_paths')
            fd = directory(path.parent);os.close(fd)
            for value in self.lineage():
                anchor = Path(value['source_spool'])
                check(path != anchor and anchor not in path.parents, 'resume_inside_evidence')

    def reservation(self,new_spool):
        return {'format':'academic-radar-timeout-resume-once-v1','journal_sha256':self.sha,
                'original_session_id':self.value['session_id'],'new_spool':str(Path(new_spool)),
                'first_fresh_request':self.value['prefix']+1}

    def reserve(self,new_spool):
        check(not self.reserved, 'timeout_resume_already_reserved')
        new_spool = Path(new_spool)
        self.validate_destinations(new_spool)
        check(self.transport.LEAF.fullmatch(new_spool.name) is not None, 'unsafe_spool')
        fd = directory(new_spool.parent)
        try:
            try: os.stat(new_spool.name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise RelayError('timeout_resume_spool_exists')
        finally: os.close(fd)
        self.verify()
        write_once_external(self.value['resume_fence'],canonical(self.reservation(new_spool)))
        self.reserved = True

    def bound_pair(self,number):
        with existing(self.transport,self.value['source_spool']) as old:
            values = []
            for kind in ('request','response'):
                name = '%s-%d.json' % (kind,number);raw = read(self.transport,old,name)
                check(self.value['files'].get(name) == {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}, 'journal_changed')
                values.append(raw)
            return self.transport._decode(values[0]),values[1]

    def response(self,req):
        check(self.reserved, 'explicit_timeout_resume_reservation_required')
        check(req['operation'] in COMBINED_READS, 'journal_operation_forbidden')
        number = req['request_id']
        check(req['session_id'] not in self.old_sessions, 'journal_fresh_session_required')
        if self.new_session is None: self.new_session = req['session_id']
        check(req['session_id'] == self.new_session, 'journal_session_changed')
        if number > self.value['prefix']:
            check(self.replayed == self.value['prefix'] and number == self.next_fresh, 'journal_replay_gap')
            if not self.exhausted:
                self.verify()
                check({key:req[key] for key in ('operation','arguments')} == self.value['first_fresh_call'], 'timeout_first_fresh_changed')
            self._fresh(req);self.next_fresh += 1;self.exhausted = True
            return None
        check(not self.exhausted and number == self.replayed+1, 'journal_replay_gap')
        old,raw = self.bound_pair(number)
        check(req == dict(old,session_id=req['session_id']), 'journal_request_mismatch')
        value = self.transport._decode(raw);check(value['ok'] is True, 'journal_response_failed')
        value['session_id'] = req['session_id'];raw = self.transport._encode(value)
        self.transport._response(raw,req)
        self.replayed += 1;self.replayed_bytes += len(raw)
        return raw

    def summary(self):
        return {'journal_sha256':self.sha,'replayed_reads':self.replayed,'replayed_response_bytes':self.replayed_bytes,
                'cached_response_bytes':self.value['cached_response_bytes'],'captured_at':self.value['captured_at'],
                'snapshot_sha256':self.value['snapshot_sha256'],'first_fresh_request':self.value['prefix']+1,
                'boundary':'never_invoked'}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics could echo a private response argument.
        raise RelayError('invalid_arguments')


def main(argv=None):
    check(sys.flags.isolated, 'isolated_python_required')
    parser = Parser(description=__doc__)
    parser.add_argument('--source-manifest', type=Path)
    parser.add_argument('--source-manifest-sha256')
    parser.add_argument('mode', choices=('pin', 'check', 'pump', 'run', 'io', 'host', 'seal-read-journal', 'resume-backup', 'seal-combined-journal', 'resume-combined-backup', 'seal-timeout-journal', 'resume-timeout-backup'))
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.mode == 'pin':
        pin_parser = Parser()
        pin_parser.add_argument('--output', required=True, type=Path)
        value = pin_source(pin_parser.parse_args(args.arguments).output)
    else:
        check(args.source_manifest is not None and args.source_manifest_sha256 is not None, 'explicit_source_pin_required')
        files = verify_source(args.source_manifest, args.source_manifest_sha256)
        if args.mode in {'check', 'pump'}:
            check(not args.arguments)
            value = {'verified': True, 'files': len(files)}
            if args.mode == 'pump':
                raw = file_bytes(ROOT/'scripts/d1-native-pump.js', MAX)
                digest = hashlib.sha256(raw).hexdigest()
                check(digest == files['scripts/d1-native-pump.js'], 'reviewed_source_changed')
                value = {'source': raw.decode('utf-8'), 'source_sha256': digest, 'bytes': len(raw)}
        else:
            sys.meta_path.insert(0, SourceOnlyFinder(files))
            if args.mode == 'host':
                from academic_radar.d1_native_host import main as host_main
                return host_main(args.arguments)
            transport = make_transport(args.source_manifest, args.source_manifest_sha256, files)
            if args.mode == 'seal-timeout-journal':
                command = Parser()
                command.add_argument('--spool',type=Path,required=True)
                command.add_argument('--prefix',type=int,required=True)
                command.add_argument('--output',type=Path,required=True)
                command.add_argument('--original-source-root',type=Path,required=True)
                command.add_argument('--original-source-manifest',type=Path,required=True)
                command.add_argument('--original-source-manifest-sha256',required=True)
                command.add_argument('--previous-journal',type=Path)
                command.add_argument('--previous-journal-sha256')
                opts = command.parse_args(args.arguments)
                check((opts.previous_journal is None) == (opts.previous_journal_sha256 is None), 'incomplete_previous_journal')
                previous = None if opts.previous_journal is None else {
                    'path':str(opts.previous_journal),'sha256':opts.previous_journal_sha256}
                value = seal_timeout_journal(transport,opts.spool,opts.prefix,opts.output,
                    {'root':str(opts.original_source_root),'manifest':str(opts.original_source_manifest),
                     'sha256':opts.original_source_manifest_sha256},previous)
                print(json.dumps(value,separators=(',',':')))
                return 0
            if args.mode == 'resume-timeout-backup':
                command = Parser()
                command.add_argument('--workspace',type=Path,required=True)
                command.add_argument('--spool',required=True)
                command.add_argument('--timeout-seconds',type=float,required=True)
                command.add_argument('--journal',type=Path,required=True)
                command.add_argument('--journal-sha256',required=True)
                command.add_argument('--output',type=Path,required=True)
                opts = command.parse_args(args.arguments)
                check(0 < opts.timeout_seconds <= 86400, 'invalid_resume_timeout')
                check(opts.workspace.is_absolute() and opts.output.is_absolute() and not opts.output.exists(), 'invalid_resume_paths')
                check(transport.LEAF.fullmatch(opts.spool) is not None, 'unsafe_spool')
                journal = TimeoutReadJournal(transport,opts.journal,opts.journal_sha256)
                journal.validate_destinations(opts.workspace/opts.spool,opts.output)
                journal.reserve(opts.workspace/opts.spool)
                transport = make_transport(args.source_manifest,args.source_manifest_sha256,files,journal=journal)
                return transport.main(['--workspace',str(opts.workspace),'--spool',opts.spool,
                    '--timeout-seconds',str(opts.timeout_seconds),'--','backup-export','--output',str(opts.output)])
            if args.mode == 'seal-combined-journal':
                command = Parser()
                command.add_argument('--spool',type=Path,required=True)
                command.add_argument('--prefix',type=int,required=True)
                command.add_argument('--output',type=Path,required=True)
                command.add_argument('--original-source-root',type=Path,required=True)
                command.add_argument('--original-source-manifest',type=Path,required=True)
                command.add_argument('--original-source-manifest-sha256',required=True)
                command.add_argument('--retry-receipt',type=Path,required=True)
                command.add_argument('--retry-receipt-sha256',required=True)
                command.add_argument('--retry-fence',type=Path,required=True)
                opts = command.parse_args(args.arguments)
                value = seal_combined_journal(transport,opts.spool,opts.prefix,opts.output,
                    {'root':str(opts.original_source_root),'manifest':str(opts.original_source_manifest),
                     'sha256':opts.original_source_manifest_sha256},opts.retry_receipt,opts.retry_receipt_sha256,opts.retry_fence)
                print(json.dumps(value,separators=(',',':')))
                return 0
            if args.mode == 'resume-combined-backup':
                command = Parser()
                command.add_argument('--workspace',type=Path,required=True)
                command.add_argument('--spool',required=True)
                command.add_argument('--timeout-seconds',type=float,required=True)
                command.add_argument('--journal',type=Path,required=True)
                command.add_argument('--journal-sha256',required=True)
                command.add_argument('--output',type=Path,required=True)
                opts = command.parse_args(args.arguments)
                check(0 < opts.timeout_seconds <= 86400, 'invalid_resume_timeout')
                check(opts.workspace.is_absolute() and opts.output.is_absolute() and not opts.output.exists(), 'invalid_resume_paths')
                check(transport.LEAF.fullmatch(opts.spool) is not None, 'unsafe_spool')
                journal = CombinedReadJournal(transport,opts.journal,opts.journal_sha256)
                journal.reserve(opts.workspace/opts.spool)
                transport = make_transport(args.source_manifest,args.source_manifest_sha256,files,journal=journal)
                return transport.main(['--workspace',str(opts.workspace),'--spool',opts.spool,
                    '--timeout-seconds',str(opts.timeout_seconds),'--','backup-export','--output',str(opts.output)])
            if args.mode == 'seal-read-journal':
                command = Parser()
                command.add_argument('--spool', type=Path, required=True)
                command.add_argument('--prefix', type=int, required=True)
                command.add_argument('--output', type=Path, required=True)
                command.add_argument('--original-source-root', type=Path, required=True)
                command.add_argument('--original-source-manifest', type=Path, required=True)
                command.add_argument('--original-source-manifest-sha256', required=True)
                opts = command.parse_args(args.arguments)
                value = seal_journal(transport, opts.spool, opts.prefix, opts.output, {
                    'root': str(opts.original_source_root), 'manifest': str(opts.original_source_manifest),
                    'sha256': opts.original_source_manifest_sha256})
                print(json.dumps(value, separators=(',', ':')))
                return 0
            if args.mode == 'resume-backup':
                command = Parser()
                command.add_argument('--workspace', type=Path, required=True)
                command.add_argument('--spool', required=True)
                command.add_argument('--timeout-seconds', type=float, required=True)
                command.add_argument('--journal', type=Path, required=True)
                command.add_argument('--journal-sha256', required=True)
                command.add_argument('--output', type=Path, required=True)
                opts = command.parse_args(args.arguments)
                journal = ReadJournal(transport, opts.journal, opts.journal_sha256)
                transport = make_transport(args.source_manifest, args.source_manifest_sha256, files, journal=journal)
                return transport.main(['--workspace', str(opts.workspace), '--spool', opts.spool,
                    '--timeout-seconds', str(opts.timeout_seconds), '--', 'backup-site', '--output', str(opts.output)])
            if args.mode == 'run':
                return transport.main(args.arguments)
            value = io_main(transport, args.arguments)
    print(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        # No argv, private payloads, exception bodies, or tracebacks on any failure.
        print('{"error":"d1_relay_failed"}')
        raise SystemExit(1)

"""Offline native-host transport/lifecycle acceptance. No connector is invoked."""
import copy
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar import cloud_runtime
from academic_radar.library_checkpoint import PublicationConflict, RecoveryRequired, read_receipt
from academic_radar.native_host import (
    HEAD_FORMAT, MAX_MESSAGE_BYTES, PROTOCOL, HostProtocolError, JsonLineHost,
    NativeHostRunner, RunnerError, normalize_library_metadata, parser,
)
from academic_radar.product import initialize_installation
from test_library_checkpoint import Host


class NativeFixture(Host):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.records = {}
        self.stages = 0
        self.acks = 0
        self.fail_ack = False
        self.deny_materialize = False
        self.change_before_ack = False
        self.reads_after_activation = 0

    def dispatch(self, operation, args):
        self.requests.append((operation, copy.deepcopy(args)))
        if operation == 'library.create_archive':
            value = self.create(Path(args['path']), args['operation_id'], args['archive_sha256'])
            value = dict(value)
            value['current_version_number'] = value.pop('current_version')
            return value
        if operation == 'library.materialize_archive':
            if self.deny_materialize:
                raise PermissionError('secret native denial payload')
            return {'path': str(self.materialize(args['metadata'], Path(args['directory'])))}
        if operation == 'site.read_checkpoint_head':
            if self.change_before_ack and self.head['publication']:
                self.reads_after_activation += 1
                if self.reads_after_activation == 1:
                    # A valid distinct authoritative descriptor, without changing
                    # any uploaded file; it is never read in this conflict test.
                    publication = dict(self.head['publication'])
                    publication['operation_id'] = 'other-winner'
                    publication.pop('publication_id')
                    canonical = json.dumps(publication, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
                    publication['publication_id'] = hashlib.sha256(canonical).hexdigest()
                    self.head = {'generation': self.head['generation'] + 1, 'publication': publication}
            return copy.deepcopy(self.head)
        if operation == 'site.read_feedback':
            return {'events': [], 'cursor': 0, 'more': False, 'profile_events_version': 1, 'request_id': 3}
        if operation == 'site.read_profile_events':
            return {'events': [], 'cursor': 0, 'more': False}
        if operation == 'site.stage_snapshot':
            data = args['data']; generation = data['generation']
            if args['action'] == 'begin':
                self.stages += 1; self.records[generation] = []
                return {'generation': generation, 'active': False}
            if args['action'] == 'chunk':
                self.records[generation].extend(data['records'])
                return {'accepted': len(data['records'])}
            assert data['activate'] is False
            rows = sorted(self.records[generation], key=lambda r: (r['kind'], r['key']))
            assert hashlib.sha256('\n'.join(f"{r['kind']}:{r['key']}:{r['checksum']}" for r in rows).encode()).hexdigest() == generation
            return {'generation': generation, 'count': len(rows), 'staged': True}
        if operation == 'site.commit_library_checkpoint':
            return {'ok': True, 'head': self.activate(args['data']['expected_head'], args['data']['publication'])}
        if operation == 'site.acknowledge_snapshot':
            assert self.head['publication']['snapshot_generation'] == args['data']['generation']
            self.acks += 1
            if self.fail_ack:
                raise PermissionError('secret acknowledgement failure')
            return {'ok': True, 'acknowledged_request_id': args['data']['request_id'],
                    'acknowledged_cursor': args['data']['cursor'],
                    'acknowledged_profile_cursor': args['data']['profile_cursor']}
        raise AssertionError('Unknown fixture operation')


class Pipe:
    """Duplex fixture pipe with the same framed request/response contract."""
    def __init__(self, dispatch, transform=lambda response: response):
        self.dispatch, self.transform = dispatch, transform
        self.pending = None
        self.requests = []

    def write(self, raw):
        request = json.loads(raw)
        self.requests.append(request)
        self.pending = request
        return len(raw)

    def flush(self):
        pass

    def readline(self, limit):
        request = self.pending
        response = {key: request[key] for key in ('protocol', 'session_id', 'request_id', 'operation')}
        response.update(type='response', ok=True)
        try:
            response['result'] = self.dispatch(request['operation'], request['arguments'])
        except Exception:
            response.update(ok=False, error={'code': 'native_rejected', 'message': 'never forward native details'})
        value = self.transform(response)
        raw = value if isinstance(value, bytes) else json.dumps(value).encode() + b'\n'
        return raw[:limit]


class NativeHostWireTests(unittest.TestCase):
    def channel(self, dispatch=lambda *_: {'generation': 0, 'publication': None}, transform=lambda v: v):
        pipe = Pipe(dispatch, transform)
        return JsonLineHost(pipe, pipe), pipe

    def test_exact_monotonic_ids_and_new_session_on_restart(self):
        channel, pipe = self.channel()
        for _ in range(3):
            self.assertEqual(channel.site_call('read_checkpoint_head', {}), {'generation': 0, 'publication': None})
        self.assertEqual([r['request_id'] for r in pipe.requests], [1, 2, 3])
        self.assertEqual(len({r['session_id'] for r in pipe.requests}), 1)
        self.assertNotEqual(channel.session_id, self.channel()[0].session_id)

    def test_wrong_session_id_operation_shape_or_native_error_poison_channel(self):
        changes = [
            lambda r: {**r, 'session_id': 'other'}, lambda r: {**r, 'request_id': 0},
            lambda r: {**r, 'request_id': True}, lambda r: {**r, 'operation': 'site.read_profile'},
            lambda r: {**r, 'type': 'request'}, lambda r: {**r, 'unexpected': 'secret'},
            lambda r: {**r, 'ok': False, 'error': {'secret': 'credential'}},
            lambda r: {**r, 'result': {'isError': True, 'content': 'secret'}},
            lambda r: {**r, 'result': {'content': [{'text': 'authorized'}]}},
            lambda r: {**r, 'result': {'generation': 0, 'publication': None, 'token': 'secret'}},
            lambda r: b'{"protocol":"duplicate","protocol":"again"}\n',
            lambda r: b'{"number":NaN}\n', lambda r: b'{"x":"unterminated"}',
            lambda r: b' ' * (MAX_MESSAGE_BYTES + 1), lambda r: b'\xff\n',
        ]
        for transform in changes:
            with self.subTest(transform=transform):
                channel, pipe = self.channel(transform=transform)
                with self.assertRaises(HostProtocolError) as error:
                    channel.site_call('read_checkpoint_head', {})
                self.assertNotIn('secret', str(error.exception))
                with self.assertRaises(HostProtocolError):
                    channel.site_call('read_checkpoint_head', {})
                self.assertEqual(len(pipe.requests), 1)

    def test_replayed_response_and_request_limit_fail_closed(self):
        saved = []
        def replay(response):
            if not saved:
                saved.append(response)
            return saved[0]
        channel, _ = self.channel(transform=replay)
        channel.site_call('read_checkpoint_head', {})
        with self.assertRaises(HostProtocolError): channel.site_call('read_checkpoint_head', {})
        channel, pipe = self.channel()
        with patch('academic_radar.native_host.MAX_REQUESTS', 1):
            channel.site_call('read_checkpoint_head', {})
            with self.assertRaises(HostProtocolError): channel.site_call('read_checkpoint_head', {})
        self.assertEqual(len(pipe.requests), 1)

    def test_non_utf8_and_overflow_numbers_poison_channel(self):
        dispatch = lambda *_: {'events': [], 'cursor': 0, 'more': False, 'synced_at': 'overflow'}
        transforms = [
            lambda response: (json.dumps(response) + '\n').encode('utf-16-be'),
            lambda response: (json.dumps(response).replace('"overflow"', '1e309') + '\n').encode(),
            lambda response: (json.dumps(response).replace('"overflow"', '-1e309') + '\n').encode(),
        ]
        for transform in transforms:
            with self.subTest(transform=transform):
                channel, pipe = self.channel(dispatch=dispatch, transform=transform)
                with self.assertRaises(HostProtocolError):
                    channel.site_call('read_feedback', {'after': 0})
                with self.assertRaises(HostProtocolError):
                    channel.site_call('read_feedback', {'after': 0})
                self.assertEqual(len(pipe.requests), 1)

    def test_unknown_tools_unbounded_outbound_and_activation_staging_rejected(self):
        channel, pipe = self.channel()
        for name, args in [('install', {}), ('read_checkpoint_head', {'identity': 'owner'}),
                           ('read_feedback', {'after': True}),
                           ('stage_snapshot', {'action': 'finish', 'data': {'generation': 'a' * 64, 'activate': True}})]:
            with self.assertRaises(ValueError): channel.site_call(name, args)
        self.assertEqual(pipe.requests, [])
        with self.assertRaises(HostProtocolError):
            channel._call('library.create_archive', {'huge': 'x' * MAX_MESSAGE_BYTES})
        self.assertEqual(pipe.requests, [])

    def test_native_metadata_version_mapping_is_exact(self):
        native = {'library_file_id': 'libfile-test', 'current_version_number': 19,
                  'file_id': 'file-test', 'file_name': 'archive.tar.gz'}
        self.assertEqual(normalize_library_metadata(native)['current_version'], 19)
        for bad in ({**native, 'credentials': 'secret'}, {**native, 'current_version_number': True},
                    {k: v for k, v in native.items() if k != 'file_id'}):
            with self.assertRaises(ValueError): normalize_library_metadata(bad)

    def test_materialization_must_be_regular_and_within_empty_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); isolated = root / 'isolated'; isolated.mkdir()
            outside = root / 'outside'; outside.write_bytes(b'bytes')
            metadata = {'library_file_id': 'libfile-test', 'current_version': 7, 'file_id': 'file-test', 'file_name': 'archive.tar.gz'}
            channel, _ = self.channel(dispatch=lambda *_: {'path': str(outside)})
            with self.assertRaises(HostProtocolError): channel.materialize_archive(metadata, isolated)
            (isolated / 'link').symlink_to(outside)
            channel, pipe = self.channel(dispatch=lambda *_: {'path': str(isolated / 'link')})
            with self.assertRaises(ValueError): channel.materialize_archive(metadata, isolated)
            self.assertEqual(pipe.requests, [])


class NativeHostLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'; initialize_installation(self.state)
        self.config = self.root / 'runtime.toml'
        self.write_config(self.config, self.state)
        self.host = NativeFixture()
        self.head_file = self.root / 'head.json'
        self.checkpoint = self.root / 'checkpoint'
        self.receipt = self.root / 'receipt.json'
        self.archive = self.root / 'checkpoint.tar.gz'
        self.operation = 'native-test-run'

    def write_config(self, config, state):
        config.write_text('state_dir=' + json.dumps(str(state)) + '\nprofile_file="research-profile.md"\n'
                          'sources=[{name="Synthetic",type="crossref"}]\n'
                          '[cloud_runtime]\nrole="primary"\n[cloud_sync]\nenabled=false\ntransport="managed"\n')

    def arguments(self, command, **overrides):
        values = {'config': self.config, 'state': self.state, 'role': 'primary'}
        if command not in {'capture-head', 'recover'}: values['operation'] = self.operation
        if command in {'capture-head', 'recover', 'prepare'}: values['head-file'] = self.head_file
        if command in {'publish', 'recover', 'reconcile-upload'}:
            values.update(checkpoint=self.checkpoint, receipt=self.receipt)
        if command == 'publish': values['archive'] = self.archive
        values.update({k.replace('_', '-'): v for k, v in overrides.items()})
        result = [command]
        for key, value in values.items():
            if value is True:
                result.append('--' + key)
            else:
                result += ['--' + key, str(value)]
        return result

    def run_command(self, command, **overrides):
        wire = Pipe(self.host.dispatch); channel = JsonLineHost(wire, wire)
        with patch('academic_radar.cloud_sync.request_json', side_effect=AssertionError('HTTP transport must not be used')):
            return NativeHostRunner(channel).execute(parser().parse_args(self.arguments(command, **overrides)))

    def prepare_import(self):
        self.run_command('capture-head')
        prepared = self.run_command('prepare')
        queue = json.loads(Path(prepared['queue_path']).read_text())
        self.assertEqual(queue['papers'], [])
        result = {'run_id': queue['run_id'], 'profile_hash': queue['profile_hash'], 'model': 'synthetic-test-only',
                  'source_failures': queue['source_failures'], 'results': []}
        self.results = self.root / 'results.json'; self.results.write_text(json.dumps(result))
        self.run_command('import-results', results=self.results)
        return prepared

    def recover_for_continuity_check(self):
        self.prepare_import(); self.run_command('publish')
        self.state = self.root / 'restored-state'
        self.config = self.root / 'restored-runtime.toml'; self.write_config(self.config, self.state)
        self.checkpoint = self.root / 'restored-checkpoint'
        self.receipt = self.root / 'recovered-receipt.json'
        self.head_file = self.root / 'restored-head.json'
        self.run_command('recover')

    def assert_new_operation_rejected_before_pull(self):
        calls = len(self.host.requests)
        with self.assertRaises(ValueError):
            self.run_command('prepare', operation='next-day-operation')
        self.assertEqual(len(self.host.requests), calls)
        self.assertEqual(cloud_runtime.status(self.config, 'next-day-operation')['phase'], 'not_started')

    def test_full_empty_queue_workflow_uses_real_runtime_import_and_native_ordering(self):
        prepared = self.prepare_import()
        self.assertIs(prepared['collection_performed'], False)
        result = self.run_command('publish')
        self.assertTrue(result['acknowledged'])
        self.assertEqual(self.host.created[0]['current_version'], 7)
        calls = [name for name, _ in self.host.requests]
        self.assertLess(calls.index('library.create_archive'), calls.index('library.materialize_archive'))
        self.assertLess(calls.index('library.materialize_archive'), calls.index('site.commit_library_checkpoint'))
        self.assertEqual(calls[-2:], ['site.read_checkpoint_head', 'site.acknowledge_snapshot'])
        self.assertEqual(cloud_runtime.status(self.config, self.operation)['phase'], 'published')
        self.assertEqual(read_receipt(self.receipt)['phase'], 'published')

    def test_no_bootstrap_no_silent_configuration_and_no_published_head_capture(self):
        with self.assertRaises(ValueError): self.run_command('publish')
        self.assertEqual(self.host.requests, [])
        self.config.write_text(self.config.read_text().replace('role="primary"', 'role="backup"'))
        with self.assertRaisesRegex(RunnerError, 'explicit_managed'): self.run_command('prepare')
        self.assertEqual(self.host.requests, [])
        self.write_config(self.config, self.state)
        self.prepare_import(); self.run_command('publish')
        with self.assertRaisesRegex(RunnerError, 'requires_recovery'):
            self.run_command('capture-head', head_file=self.root / 'substituted-head.json')

    def test_receipt_retry_after_ack_failure_never_restages_or_reuploads(self):
        self.prepare_import(); self.host.fail_ack = True
        with self.assertRaises(HostProtocolError): self.run_command('publish')
        self.assertEqual(read_receipt(self.receipt)['phase'], 'published')
        counts = self.host.stages, len(self.host.created), self.host.activations
        journal = cloud_runtime.status(self.config, self.operation)
        self.assertEqual(self.run_command('prepare')['status'], 'resume_publication')
        self.assertEqual(cloud_runtime.status(self.config, self.operation), journal)
        with self.assertRaisesRegex(RunnerError, 'without_reimport'):
            self.run_command('import-results', results=self.results)
        self.host.fail_ack = False
        self.assertTrue(self.run_command('publish')['resumed'])
        self.assertEqual((self.host.stages, len(self.host.created), self.host.activations), counts)

    def test_missing_or_lagging_ack_cursors_leave_local_publication_unconfirmed(self):
        self.prepare_import()
        original = self.host.dispatch
        for bad in ({'ok': True}, {'ok': True, 'acknowledged_request_id': 2,
                                  'acknowledged_cursor': 0, 'acknowledged_profile_cursor': 0}):
            def dispatch(operation, args):
                value = original(operation, args)
                return bad if operation == 'site.acknowledge_snapshot' else value
            with patch.object(self.host, 'dispatch', side_effect=dispatch):
                with self.assertRaises(HostProtocolError): self.run_command('publish')
            self.assertEqual(cloud_runtime.status(self.config, self.operation)['phase'], 'committing')
            self.assertEqual(read_receipt(self.receipt)['phase'], 'published')
        self.assertEqual((self.host.stages, len(self.host.created), self.host.activations), (1, 1, 1))
        self.assertTrue(self.run_command('publish')['acknowledged'])

    def test_uncertain_cas_recovers_exact_receipt_without_staging(self):
        self.prepare_import(); self.host.commit_then_fail = True
        with self.assertRaises(RecoveryRequired): self.run_command('publish')
        self.assertEqual(read_receipt(self.receipt)['phase'], 'activation_pending')
        result = self.run_command('publish')
        self.assertTrue(result['acknowledged'])
        self.assertEqual((self.host.stages, len(self.host.created), self.host.activations), (1, 1, 1))

    def test_uncertain_create_requires_reconcile_and_never_blindly_recreates(self):
        self.prepare_import(); self.host.fail_create = True
        with self.assertRaises(RecoveryRequired): self.run_command('publish')
        self.assertEqual(read_receipt(self.receipt)['phase'], 'create_pending')
        with self.assertRaises(RecoveryRequired): self.run_command('publish')
        self.assertEqual(len(self.host.created), 1)
        metadata = self.root / 'metadata.json'; metadata.write_text(json.dumps(self.host.created[0]))
        reconciled = self.run_command('reconcile-upload', metadata=metadata)
        self.assertEqual(reconciled['status'], 'archived')
        self.assertEqual(self.host.activations, 0)
        self.run_command('publish')
        self.assertEqual((self.host.stages, len(self.host.created), self.host.activations), (1, 1, 1))

    def test_materialization_denial_blocks_cas_and_preserves_exact_identity(self):
        self.prepare_import(); self.host.deny_materialize = True
        with self.assertRaises(RecoveryRequired): self.run_command('publish')
        self.assertEqual(self.host.activations, 0)
        saved = read_receipt(self.receipt)
        self.assertEqual(saved['phase'], 'verify_pending')
        self.assertEqual(saved['publication']['library_file_id'], self.host.created[0]['library_file_id'])
        self.host.deny_materialize = False
        self.run_command('publish')
        self.assertEqual(len(self.host.created), 1)

    def test_post_cas_authoritative_head_change_blocks_ack(self):
        self.prepare_import(); self.host.change_before_ack = True
        with self.assertRaises(PublicationConflict): self.run_command('publish')
        self.assertEqual(self.host.acks, 0)
        self.assertEqual(read_receipt(self.receipt)['phase'], 'published')

    def test_foreign_receipt_and_tampered_runtime_proof_rejected(self):
        self.prepare_import(); self.run_command('publish')
        counts = self.host.stages, len(self.host.created), self.host.activations, self.host.acks
        with self.assertRaises(ValueError): self.run_command('publish', operation='foreign-operation')
        queue = Path(cloud_runtime._job(self.state / 'papers.sqlite3', cloud_runtime.status(self.config, self.operation)['run_id'])['queue_path'])
        value = json.loads(queue.read_text()); value['threshold'] = .123; queue.write_text(json.dumps(value))
        with self.assertRaises(ValueError): self.run_command('publish')
        self.assertEqual((self.host.stages, len(self.host.created), self.host.activations, self.host.acks), counts)

    def test_restore_published_head_then_same_operation_preserves_proof_and_only_acks(self):
        self.prepare_import(); self.run_command('publish')
        self.state = self.root / 'restored-state'
        self.config = self.root / 'restored-runtime.toml'; self.write_config(self.config, self.state)
        self.checkpoint = self.root / 'restored-checkpoint'
        self.receipt = self.root / 'recovered-receipt.json'
        self.head_file = self.root / 'restored-head.json'
        self.archive = self.root / 'absent-original-archive.tar.gz'
        counts = self.host.stages, len(self.host.created), self.host.activations
        result = self.run_command('recover')
        self.assertEqual(result['status'], 'restored')
        journal = cloud_runtime.status(self.config, self.operation)
        self.assertEqual(journal['phase'], 'committing')
        self.assertEqual(self.run_command('prepare')['status'], 'already_published')
        self.assertEqual(cloud_runtime.status(self.config, self.operation), journal)
        with self.assertRaisesRegex(RunnerError, 'without_reimport'):
            self.run_command('import-results', results=self.results)
        self.assertTrue(self.run_command('publish')['acknowledged'])
        self.assertEqual((self.host.stages, len(self.host.created), self.host.activations), counts)
        fresh = self.run_command('prepare', operation='next-day-operation')
        self.assertEqual(fresh['phase'], 'awaiting_judgment')
        self.assertEqual(cloud_runtime.status(self.config, 'next-day-operation')['expected_head'], self.host.head)
        retried = self.run_command('prepare', operation='next-day-operation')
        self.assertTrue(retried['resumed'])
        self.assertEqual(retried['run_id'], fresh['run_id'])
        queue = json.loads(Path(fresh['queue_path']).read_text())
        results = self.root / 'next-day-results.json'
        results.write_text(json.dumps({'run_id': queue['run_id'], 'profile_hash': queue['profile_hash'],
            'model': 'synthetic-test-only', 'source_failures': queue['source_failures'], 'results': []}))
        self.run_command('import-results', operation='next-day-operation', results=results)
        self.assertEqual(self.run_command('prepare', operation='next-day-operation')['phase'], 'imported')

    def test_new_operation_rejects_reinitialized_recovered_state(self):
        self.recover_for_continuity_check()
        shutil.rmtree(self.state)
        initialize_installation(self.state)
        self.assert_new_operation_rejected_before_pull()

    def test_new_operation_rejects_missing_prior_publication_journal(self):
        self.recover_for_continuity_check()
        with sqlite3.connect(self.state / 'papers.sqlite3') as db:
            db.execute('DELETE FROM meta WHERE key=?', ('cloud_runtime_operation:' + self.operation,))
        self.assert_new_operation_rejected_before_pull()

    def test_new_operation_rejects_changed_prior_publication_queue(self):
        self.recover_for_continuity_check()
        prior = cloud_runtime.status(self.config, self.operation)
        job = cloud_runtime._job(self.state / 'papers.sqlite3', prior['run_id'])
        queue = Path(job['queue_path'])
        value = json.loads(queue.read_text()); value['threshold'] = .123
        queue.write_text(json.dumps(value))
        self.assert_new_operation_rejected_before_pull()

    def test_new_operation_rejects_changed_prior_publication_results(self):
        self.recover_for_continuity_check()
        prior = cloud_runtime.status(self.config, self.operation)
        job = cloud_runtime._job(self.state / 'papers.sqlite3', prior['run_id'])
        results = Path(job['results_path'])
        value = json.loads(results.read_text()); value['model'] = 'changed'
        results.write_text(json.dumps(value))
        self.assert_new_operation_rejected_before_pull()

    def test_prepare_collect_is_explicit_and_calls_existing_runtime_only(self):
        self.run_command('capture-head')
        with patch.object(cloud_runtime, 'prepare', wraps=cloud_runtime.prepare) as prepare:
            result = self.run_command('prepare')
        self.assertTrue(prepare.call_args.kwargs['no_collect'])
        with patch.object(cloud_runtime, 'prepare', return_value={**result, 'resumed': False}) as prepare:
            collected = self.run_command('prepare', collect=True)
        self.assertFalse(prepare.call_args.kwargs['no_collect'])
        self.assertTrue(collected['collection_performed'])

    def test_subprocess_json_lines_integration_and_redacted_failure(self):
        self.prepare_import()
        command = [sys.executable, '-m', 'academic_radar.native_host', *self.arguments('publish')]
        environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        requests = []
        while True:
            message = json.loads(process.stdout.readline())
            if message['type'] == 'result': break
            requests.append(message)
            result = self.host.dispatch(message['operation'], message['arguments'])
            response = {k: message[k] for k in ('protocol', 'session_id', 'request_id', 'operation')}
            response.update(type='response', ok=True, result=result)
            process.stdin.write(json.dumps(response).encode() + b'\n'); process.stdin.flush()
        process.stdin.close(); self.assertEqual(process.wait(timeout=30), 0)
        self.assertEqual(process.stderr.read(), b'')
        process.stdout.close(); process.stderr.close()
        self.assertTrue(message['result']['acknowledged'])
        self.assertEqual([r['request_id'] for r in requests], list(range(1, len(requests) + 1)))
        self.assertNotIn('profile', json.dumps(message['result']))
        # A raw remote exception body is never returned or printed by the CLI.
        failed = subprocess.Popen([sys.executable, '-m', 'academic_radar.native_host', *self.arguments('capture-head')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        self.addCleanup(lambda: failed.kill() if failed.poll() is None else None)
        request = json.loads(failed.stdout.readline())
        response = {k: request[k] for k in ('protocol', 'session_id', 'request_id', 'operation')}
        response.update(type='response', ok=False, error={'message': 'SECRET_TOKEN_NATIVE_BODY'})
        stdout, stderr = failed.communicate(json.dumps(response).encode() + b'\n', timeout=30)
        self.assertEqual(failed.returncode, 1)
        self.assertNotIn(b'SECRET_TOKEN', stdout + stderr)
        self.assertEqual(json.loads(stdout)['error']['code'], 'native_host_failed')
        self.assertEqual(stderr, b'')


if __name__ == '__main__':
    unittest.main()

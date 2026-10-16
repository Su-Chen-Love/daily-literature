"""Synthetic combined-backup recovery. No native tool, network or credential use."""
import base64
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_d1_native_spool as base
from test_d1_backup import NativeFixture
from academic_radar import d1_backup, d1_native_host, d1_package


relay = base.relay
OLD_SESSION = 'a' * 32
NEW_SESSION = 'b' * 32
CAPTURED_AT = '2020-01-02T03:04:05.678Z'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@unittest.skipUnless(os.name == 'posix', 'Private relay requires POSIX files')
class CombinedReadJournalTests(unittest.TestCase):
    setUp = base.D1NativeSpoolTests.setUp
    command = base.D1NativeSpoolTests.command
    stop = base.D1NativeSpoolTests.stop
    checkout_copy = base.D1NativeSpoolTests.checkout_copy

    @property
    def rejection(self):
        return (relay.RelayError, self.transport.RelayError, ValueError, OSError)

    @property
    def original_source(self):
        return {'root': str(base.PROJECT), 'manifest': str(self.manifest), 'sha256': self.sha}

    def write_private(self, path, value):
        raw = value if isinstance(value, bytes) else self.transport._encode(value)
        with path.open('xb') as out:
            os.chmod(path, 0o600)
            out.write(raw)
        os.chmod(path, 0o400)
        return raw

    def change(self, path, raw):
        if not isinstance(raw, bytes):
            raw = self.transport._encode(raw)
        os.chmod(path, 0o600)
        path.write_bytes(raw)
        os.chmod(path, 0o400)

    def contents(self, directory):
        return {path.name: path.read_bytes() for path in directory.iterdir()}

    def request(self, number, operation, action, data):
        return {'protocol': self.transport.PROTOCOL, 'type': 'request',
                'session_id': OLD_SESSION, 'request_id': number,
                'operation': operation, 'arguments': {'action': action, 'data': data}}

    def response(self, request, result=None, *, ok=True):
        value = {key: request[key] for key in ('protocol', 'session_id', 'request_id', 'operation')}
        value.update(type='response', ok=ok)
        value['result' if ok else 'error'] = result if ok else {'code': 'host_action_failed'}
        return value

    def fixture(self, prefix=3, transcript=None):
        """A closed old spool with an invoked, durably failed records request."""
        if transcript is None:
            self.assertGreaterEqual(prefix, 3)
            watermarks = {'feedback_cursor': 0, 'profile_cursor': 0, 'request_id': 0}
            imported = 'c' * 64
            data = 'x' * (prefix - 2)
            checksum = digest(data.encode())
            generation = digest(('meta:synthetic:' + checksum).encode())
            snapshot = {'format': 'academic-radar-site-backup', 'version': 1,
                        'pointers': [{'key': 'active', 'value': generation}],
                        'generations': [{'id': generation, 'count': 1,
                                         'created_at': '2020-01-01', 'status': 'complete'}],
                        'excluded_uncommitted_generations': 0,
                        'feedback_cursor': 0, 'profile_cursor': 0,
                        'main_head': {'revision': 2, 'import_id': imported}}
            begin = {'snapshot': snapshot, 'snapshot_sha256': d1_package.digest(snapshot),
                     'hash_algorithm': 'sha256-canonical-json-v1', 'captured_at': CAPTURED_AT}
            transcript = []
            first = self.request(1, 'site.read_main_store', 'head', {})
            transcript.append((first, self.response(first, {'head': {
                'revision': 2, 'import_id': imported, 'manifest_sha256': imported,
                'watermarks': watermarks}})))
            second = self.request(2, 'site.read_main_store', 'read_manifest', {'revision': 2})
            transcript.append((second, self.response(second, {
                'revision': 2, 'manifest_sha256': imported,
                'manifest': {'schema_sha256': 'd' * 64}, 'watermarks': watermarks})))
            third = self.request(3, 'site.read_site_backup', 'begin', {})
            transcript.append((third, self.response(third, begin)))
            for number in range(4, prefix + 2):
                offset = number - 4
                arguments = {'generation': generation}
                if offset:
                    arguments['after'] = {'kind': 'meta', 'key': 'synthetic', 'offset': offset}
                req = self.request(number, 'site.read_site_backup', 'records', arguments)
                next_cursor = {'kind': 'meta', 'key': 'synthetic',
                               'offset': 0 if offset + 1 == len(data) else offset + 1}
                result = {'generation': generation, 'expected_count': 1,
                          'chunks': [{'kind': 'meta', 'key': 'synthetic', 'checksum': checksum,
                                      'bytes': len(data), 'offset': offset,
                                      'base64': base64.b64encode(b'x').decode()}],
                          'next': next_cursor, 'done': False}
                transcript.append((req, self.response(req, result)))
        else:
            begin = transcript[2][1]['result']

        old = self.transport.PrivateSpool(self.root, 'old-run')
        self.addCleanup(old.close)
        for number, (req, successful) in enumerate(transcript[:prefix + 1], 1):
            self.assertEqual(req['request_id'], number)
            raw = self.transport._encode(req)
            claim = {'request_sha256': digest(raw)}
            old.save('request-%d.json' % number, raw)
            old.save('pending-%d.json' % number, self.transport._encode({
                **claim, 'heartbeat': 'live-%d.json' % number, 'deadline_unix_ms': 1}))
            old.save('live-%d.json' % number, b'{}\n')
            for kind in ('claim', 'invoke'):
                old.save('%s-%d.json' % (kind, number), self.transport._encode(claim))
            response = successful if number <= prefix else self.response(req, ok=False)
            raw = self.transport._encode(response)
            old.save('response-%d.partial' % number, raw)
            old.save('response-%d.json' % number, self.transport._response(raw, req))
        old.save('result.json', self.transport._encode({
            'protocol': self.transport.PROTOCOL, 'type': 'result', 'session_id': OLD_SESSION,
            'request_id': prefix + 1, 'ok': False, 'error': {'code': 'native_host_failed'}}))

        diagnostic = self.root / 'diagnostic'
        diagnostic.mkdir(mode=0o700)
        request_sha = digest((old.path / ('request-%d.json' % (prefix + 1))).read_bytes())
        fence = diagnostic / 'retry-fence.json'
        self.write_private(fence, {'state': 'one_diagnostic_retry_reserved',
            'original_spool': old.path.name, 'original_request_id': prefix + 1,
            'request_sha256': request_sha, 'at': CAPTURED_AT})
        successful_path = diagnostic / 'successful-response.json'
        successful_raw = self.write_private(successful_path, transcript[prefix][1])
        receipt = diagnostic / 'retry-receipt.json'
        receipt_raw = self.write_private(receipt, {
            'format': 'academic-radar-diagnostic-read-retry-v1', 'original_spool': str(old.path),
            'original_request_id': prefix + 1, 'request_sha256': request_sha,
            'failed_response_sha256': digest((old.path / ('response-%d.json' % (prefix + 1))).read_bytes()),
            'terminal_sha256': digest((old.path / 'result.json').read_bytes()),
            'source_manifest_sha256': self.sha, 'reserved_fence_sha256': digest(fence.read_bytes()),
            'retry_count': 1, 'outcome': 'success', 'recorded_at': CAPTURED_AT, 'native_elapsed_ms': 0,
            'successful_response': {'path': str(successful_path), 'bytes': len(successful_raw),
                                    'sha256': digest(successful_raw)}})
        return {'old': old, 'prefix': prefix, 'transcript': transcript, 'begin': begin,
                'receipt': receipt, 'receipt_sha256': digest(receipt_raw), 'fence': fence,
                'successful_response': successful_path, 'request_sha256': request_sha,
                'resume_fence': diagnostic / ('combined-resume-' + request_sha + '.json')}

    def seal(self, fixture, **overrides):
        parameters = {'transport': self.transport, 'old_spool': fixture['old'].path,
                      'prefix': fixture['prefix'], 'output': self.root / 'combined-journal.json',
                      'original_source': self.original_source,
                      'retry_receipt_path': fixture['receipt'],
                      'retry_receipt_sha256': fixture['receipt_sha256'],
                      'retry_fence_path': fixture['fence']}
        parameters.update(overrides)
        # The positional API is intentional: CLI option spelling is tested separately.
        info = relay.seal_combined_journal(*parameters.values())
        return parameters['output'], info

    def load(self, fixture, *, reserve=True):
        path, info = self.seal(fixture)
        journal = relay.CombinedReadJournal(self.transport, path, info['journal_sha256'])
        if reserve:
            journal.reserve(self.root / 'new-run')
        return journal, path, info

    def replay(self, journal, fixture):
        for req, original in fixture['transcript'][:fixture['prefix'] + 1]:
            fresh = dict(req, session_id=NEW_SESSION)
            actual = self.transport._decode(journal.response(fresh))
            self.assertEqual(actual, dict(original, session_id=NEW_SESSION))

    def fresh_records(self, fixture):
        last = fixture['transcript'][fixture['prefix']][1]['result']
        return dict(self.request(fixture['prefix'] + 2, 'site.read_site_backup', 'records', {
            'generation': last['generation'], 'after': last['next']}), session_id=NEW_SESSION)

    @contextlib.contextmanager
    def isolated_case(self):
        case = CombinedReadJournalTests()
        case.setUp()
        try:
            yield case
        finally:
            case.doCleanups()

    def rewrite_receipt(self, fixture, mutate):
        value = self.transport._decode(fixture['receipt'].read_bytes())
        mutate(value)
        self.change(fixture['receipt'], value)
        fixture['receipt_sha256'] = digest(fixture['receipt'].read_bytes())

    def test_replays_all_543_responses_without_native_retry_or_old_byte_changes(self):
        fixture = self.fixture(prefix=542)
        old_before = self.contents(fixture['old'].path)
        diagnostic_before = self.contents(fixture['receipt'].parent)
        with patch.object(relay.subprocess, 'Popen', side_effect=AssertionError('Native spawn forbidden')):
            journal, _, info = self.load(fixture)
            self.replay(journal, fixture)
            self.assertEqual(journal.replayed, 543)
            self.assertEqual(info['replayed_reads'], 543)
            self.assertEqual(info['next'], 544)
            self.assertEqual(info['captured_at'], CAPTURED_AT)
            self.assertIsNone(journal.response(self.fresh_records(fixture)))
            self.assertTrue(journal.exhausted)
        self.assertEqual(self.contents(fixture['old'].path), old_before)
        after = self.contents(fixture['receipt'].parent)
        self.assertEqual({name: after[name] for name in diagnostic_before}, diagnostic_before)
        self.assertEqual(set(after) - set(diagnostic_before), {fixture['resume_fence'].name})

    def test_mismatched_replay_operation_arguments_sequence_and_session_rejected(self):
        fixture = self.fixture()
        journal, _, _ = self.load(fixture)
        first = dict(fixture['transcript'][0][0], session_id=NEW_SESSION)
        wrong = [dict(first, request_id=2), dict(first, session_id=OLD_SESSION),
                 dict(first, arguments={'action': 'capacity', 'data': {}}),
                 dict(first, operation='site.write_main_store',
                      arguments={'action': 'begin_import', 'data': {'manifest_sha256': 'c' * 64}}),
                 dict(first, operation='site.read_run_status', arguments={})]
        for req in wrong:
            with self.subTest(request=req), self.assertRaises(self.rejection):
                journal.response(req)
            self.assertEqual(journal.replayed, 0)
        self.replay(journal, fixture)
        with self.assertRaises(self.rejection):
            journal.response(first)

    def test_external_success_requires_exact_original_envelope(self):
        changes = {'session': lambda v: v.update(session_id=NEW_SESSION),
                   'number': lambda v: v.update(request_id=v['request_id'] + 1),
                   'operation': lambda v: v.update(operation='site.read_main_store'),
                   'failed': lambda v: (v.pop('result'), v.update(ok=False, error={'code': 'host_action_failed'})),
                   'unknown': lambda v: v.update(extra=True),
                   'error_result': lambda v: v.update(result={'error': 'not a successful native result'}),
                   'wrapped_result': lambda v: v.update(result={'structuredContent': v['result']})}
        for name, mutate in changes.items():
            with self.subTest(defect=name), self.isolated_case() as case:
                fixture = case.fixture()
                value = case.transport._decode(fixture['successful_response'].read_bytes())
                mutate(value)
                case.change(fixture['successful_response'], value)
                case.rewrite_receipt(fixture, lambda receipt: receipt['successful_response'].update(
                    bytes=fixture['successful_response'].stat().st_size,
                    sha256=digest(fixture['successful_response'].read_bytes())))
                with case.assertRaises(case.rejection):
                    case.seal(fixture)

    def test_denied_unknown_missing_and_mismatched_receipts_rejected(self):
        changes = {
            'denied': lambda v: v.update(outcome='denied'),
            'retries': lambda v: v.update(retry_count=2),
            'boolean_retry': lambda v: v.update(retry_count=True),
            'negative_elapsed': lambda v: v.update(native_elapsed_ms=-1),
            'boolean_elapsed': lambda v: v.update(native_elapsed_ms=True),
            'unknown': lambda v: v.update(extra='untrusted'),
            'missing': lambda v: v.pop('successful_response'),
            'source': lambda v: v.update(source_manifest_sha256='0' * 64),
            'request': lambda v: v.update(request_sha256='0' * 64),
            'failed_response': lambda v: v.update(failed_response_sha256='0' * 64),
            'terminal': lambda v: v.update(terminal_sha256='0' * 64),
            'reserved_fence': lambda v: v.update(reserved_fence_sha256='0' * 64),
            'old_spool': lambda v: v.update(original_spool='/tmp/other-synthetic-run'),
            'request_id': lambda v: v.update(original_request_id=v['original_request_id'] + 1),
            'timestamp': lambda v: v.update(recorded_at='not-a-timestamp'),
            'response_bytes': lambda v: v['successful_response'].update(bytes=0),
            'response_sha': lambda v: v['successful_response'].update(sha256='0' * 64),
            'response_unknown': lambda v: v['successful_response'].update(extra=True),
        }
        for name, mutate in changes.items():
            with self.subTest(defect=name), self.isolated_case() as case:
                fixture = case.fixture()
                case.rewrite_receipt(fixture, mutate)
                with case.assertRaises(case.rejection):
                    case.seal(fixture)

    def test_missing_truncated_tampered_and_unsafe_external_artifacts_rejected(self):
        for target in ('receipt', 'successful_response', 'fence'):
            for defect in ('missing', 'truncated', 'tampered', 'symlink', 'public', 'hardlink'):
                with self.subTest(target=target, defect=defect), self.isolated_case() as case:
                    fixture = case.fixture()
                    path = fixture[target]
                    raw = path.read_bytes()
                    if defect == 'missing':
                        path.unlink()
                    elif defect == 'truncated':
                        case.change(path, raw[:-3])
                    elif defect == 'tampered':
                        case.change(path, raw + b' ')
                    elif defect == 'symlink':
                        outside = case.root / 'external-target.json'
                        case.write_private(outside, raw)
                        path.unlink()
                        path.symlink_to(outside)
                    elif defect == 'public':
                        os.chmod(path, 0o644)
                    else:
                        os.link(path, case.root / 'external-hardlink.json')
                    with case.assertRaises(case.rejection):
                        case.seal(fixture)

    def test_reserved_retry_fence_must_match_and_be_closed_schema(self):
        changes = {'state': lambda v: v.update(state='available'),
                   'spool': lambda v: v.update(original_spool='different-run'),
                   'number': lambda v: v.update(original_request_id=999),
                   'request': lambda v: v.update(request_sha256='0' * 64),
                   'timestamp': lambda v: v.update(at='yesterday'),
                   'unknown': lambda v: v.update(extra=True)}
        for name, mutate in changes.items():
            with self.subTest(defect=name), self.isolated_case() as case:
                fixture = case.fixture()
                value = case.transport._decode(fixture['fence'].read_bytes())
                mutate(value)
                case.change(fixture['fence'], value)
                case.rewrite_receipt(fixture, lambda receipt: receipt.update(
                    reserved_fence_sha256=digest(fixture['fence'].read_bytes())))
                with case.assertRaises(case.rejection):
                    case.seal(fixture)

    def test_duplicate_receipt_keys_and_changed_receipt_pin_rejected(self):
        for defect in ('duplicate', 'pin'):
            with self.subTest(defect=defect), self.isolated_case() as case:
                fixture = case.fixture()
                if defect == 'duplicate':
                    raw = fixture['receipt'].read_bytes().replace(b'{', b'{"retry_count":1,', 1)
                    case.change(fixture['receipt'], raw)
                    fixture['receipt_sha256'] = digest(raw)
                else:
                    fixture['receipt_sha256'] = '0' * 64
                with case.assertRaises(case.rejection):
                    case.seal(fixture)

    def test_invalid_prefix_and_existing_journal_are_rejected_without_clobber(self):
        fixture = self.fixture()
        for prefix in (0, 1, 2, True, 3.0, self.transport.MAX_REQUESTS):
            with self.subTest(prefix=prefix), self.assertRaises(self.rejection):
                self.seal(fixture, prefix=prefix)
        output = self.root / 'existing-journal.json'
        self.write_private(output, b'synthetic journal sentinel\n')
        with self.assertRaises(self.rejection):
            self.seal(fixture, output=output)
        self.assertEqual(output.read_bytes(), b'synthetic journal sentinel\n')

    def test_inventory_gaps_unknown_files_and_missing_invocation_rejected(self):
        for defect in ('gap', 'unknown', 'later', 'missing_tail_invoke', 'missing_success_invoke',
                       'missing_terminal', 'response_symlink', 'response_partial_mismatch'):
            with self.subTest(defect=defect), self.isolated_case() as case:
                fixture = case.fixture()
                old = fixture['old']
                if defect == 'gap':
                    (old.path / 'request-2.json').unlink()
                elif defect == 'unknown':
                    old.save('unknown.json', b'{}\n')
                elif defect == 'later':
                    old.save('request-5.json', (old.path / 'request-4.json').read_bytes())
                elif defect == 'missing_tail_invoke':
                    (old.path / 'invoke-4.json').unlink()
                elif defect == 'missing_success_invoke':
                    (old.path / 'invoke-1.json').unlink()
                elif defect == 'missing_terminal':
                    (old.path / 'result.json').unlink()
                elif defect == 'response_symlink':
                    target = old.path / 'response-1.json'
                    outside = case.root / 'external-response.json'
                    case.write_private(outside, target.read_bytes())
                    target.unlink()
                    target.symlink_to(outside)
                else:
                    case.change(old.path / 'response-1.partial', b'{}\n')
                with case.assertRaises(case.rejection):
                    case.seal(fixture)

    def test_terminal_must_be_durable_exact_native_host_failure(self):
        changes = {'number': lambda v: v.update(request_id=3),
                   'session': lambda v: v.update(session_id=NEW_SESSION),
                   'type': lambda v: v.update(type='relay_error'),
                   'code': lambda v: v.update(error={'code': 'interrupted'}),
                   'success': lambda v: (v.pop('error'), v.update(ok=True, result={})),
                   'unknown': lambda v: v.update(extra=True)}
        for name, mutate in changes.items():
            with self.subTest(defect=name), self.isolated_case() as case:
                fixture = case.fixture()
                terminal = fixture['old'].path / 'result.json'
                value = case.transport._decode(terminal.read_bytes())
                mutate(value)
                case.change(terminal, value)
                case.rewrite_receipt(fixture, lambda receipt: receipt.update(
                    terminal_sha256=digest(terminal.read_bytes())))
                with case.assertRaises(case.rejection):
                    case.seal(fixture)

    def test_first_three_requests_tail_and_committed_snapshot_are_mandatory(self):
        for defect in ('head', 'manifest', 'begin', 'write', 'second_begin', 'tail', 'uncommitted'):
            with self.subTest(defect=defect), self.isolated_case() as case:
                fixture = case.fixture(prefix=4)
                old = fixture['old']
                number = {'head': 1, 'manifest': 2, 'begin': 3, 'write': 4,
                          'second_begin': 4, 'tail': 5, 'uncommitted': 3}[defect]
                if defect == 'uncommitted':
                    target = old.path / 'response-3.json'
                    value = case.transport._decode(target.read_bytes())
                    value['result']['snapshot']['main_head'] = {'revision': 0, 'import_id': None}
                    value['result']['snapshot_sha256'] = d1_package.digest(value['result']['snapshot'])
                    raw = case.transport._encode(value)
                    case.change(target, raw)
                    case.change(old.path / 'response-3.partial', raw)
                else:
                    target = old.path / ('request-%d.json' % number)
                    value = case.transport._decode(target.read_bytes())
                    if defect in ('head', 'manifest'):
                        value['arguments'] = {'action': 'capacity', 'data': {}}
                    elif defect in ('begin', 'tail'):
                        value['arguments'] = {'action': 'feedback', 'data': {'after': 0, 'until': 0}}
                    elif defect == 'second_begin':
                        value['arguments'] = {'action': 'begin', 'data': {}}
                    else:
                        value.update(operation='site.write_main_store', arguments={
                            'action': 'begin_import', 'data': {'manifest_sha256': 'c' * 64}})
                    raw = case.transport._encode(value)
                    case.change(target, raw)
                    request_sha = digest(raw)
                    for kind in ('claim', 'invoke'):
                        case.change(old.path / ('%s-%d.json' % (kind, number)), {'request_sha256': request_sha})
                    pending = case.transport._decode((old.path / ('pending-%d.json' % number)).read_bytes())
                    pending['request_sha256'] = request_sha
                    case.change(old.path / ('pending-%d.json' % number), pending)
                with case.assertRaises(case.rejection):
                    case.seal(fixture)

    def test_changed_pins_source_and_journal_schema_rejected(self):
        fixture = self.fixture()
        path, info = self.seal(fixture)
        with self.assertRaises(self.rejection):
            relay.CombinedReadJournal(self.transport, path, '0' * 64)
        original = path.read_bytes()
        for mutation in ('source', 'unknown'):
            value = json.loads(original)
            if mutation == 'source':
                value['original_source']['sha256'] = '0' * 64
            else:
                value['extra'] = True
            raw = relay.canonical(value)
            self.change(path, raw)
            with self.subTest(mutation=mutation), self.assertRaises(self.rejection):
                relay.CombinedReadJournal(self.transport, path, digest(raw))

    def test_original_source_package_must_equal_current_backup_package(self):
        fixture = self.fixture()
        checkout = self.checkout_copy()
        module = checkout / 'src/academic_radar/d1_backup.py'
        module.write_bytes(module.read_bytes() + b'\n# Synthetic mismatched original source.\n')
        manifest = self.root / 'different-source.json'
        sha = relay.pin_source(manifest, checkout)['source_manifest_sha256']
        source = {'root': str(checkout), 'manifest': str(manifest), 'sha256': sha}
        self.rewrite_receipt(fixture, lambda receipt: receipt.update(source_manifest_sha256=sha))
        with self.assertRaises(self.rejection):
            self.seal(fixture, original_source=source)

    def test_post_seal_inventory_or_external_response_tampering_rejected(self):
        for target in ('old_response', 'receipt', 'successful_response', 'fence', 'added_file'):
            with self.subTest(target=target), self.isolated_case() as case:
                fixture = case.fixture()
                path, info = case.seal(fixture)
                if target == 'added_file':
                    fixture['old'].save('unknown.json', b'{}\n')
                else:
                    affected = (fixture['old'].path / 'response-1.json'
                                if target == 'old_response' else fixture[target])
                    case.change(affected, affected.read_bytes() + b' ')
                with case.assertRaises(case.rejection):
                    relay.CombinedReadJournal(case.transport, path, info['journal_sha256'])

    def test_external_response_tampering_after_load_rejected_before_replay(self):
        fixture = self.fixture()
        journal, _, _ = self.load(fixture)
        for req, _ in fixture['transcript'][:fixture['prefix']]:
            journal.response(dict(req, session_id=NEW_SESSION))
        target = fixture['successful_response']
        self.change(target, target.read_bytes() + b' ')
        with self.assertRaises(self.rejection):
            journal.response(dict(fixture['transcript'][fixture['prefix']][0], session_id=NEW_SESSION))
        self.assertEqual(journal.replayed, fixture['prefix'])

    def test_old_response_tampering_after_load_rejected_before_replay(self):
        fixture = self.fixture()
        journal, _, _ = self.load(fixture)
        target = fixture['old'].path / 'response-1.json'
        self.change(target, target.read_bytes() + b' ')
        with self.assertRaises(self.rejection):
            journal.response(dict(fixture['transcript'][0][0], session_id=NEW_SESSION))
        self.assertEqual(journal.replayed, 0)

    def test_reservation_required_and_invalid_spool_does_not_consume_fence(self):
        fixture = self.fixture()
        journal, _, _ = self.load(fixture, reserve=False)
        first = dict(fixture['transcript'][0][0], session_id=NEW_SESSION)
        with self.assertRaises(self.rejection):
            journal.response(first)
        self.assertEqual(journal.replayed, 0)
        existing = self.root / 'existing-spool'
        existing.mkdir(mode=0o700)
        for candidate in (existing, fixture['old'].path, Path('relative-spool')):
            with self.subTest(spool=candidate), self.assertRaises(self.rejection):
                journal.reserve(candidate)
            self.assertFalse(fixture['resume_fence'].exists())
        alias = self.root / 'symlink-parent'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(self.rejection):
            journal.reserve(alias / 'new-run')
        self.assertFalse(fixture['resume_fence'].exists())
        journal.reserve(self.root / 'new-run')
        self.assertIsNotNone(journal.response(first))

    def test_journal_cannot_repoint_derived_resume_fence(self):
        fixture = self.fixture()
        path, _ = self.seal(fixture)
        value = json.loads(path.read_bytes())
        value['resume_fence'] = str(self.root / 'different-resume-fence.json')
        raw = relay.canonical(value)
        self.change(path, raw)
        with self.assertRaises(self.rejection):
            relay.CombinedReadJournal(self.transport, path, digest(raw))

    def test_one_use_resume_fence_no_clobber_survives_copied_or_resealed_journal(self):
        fixture = self.fixture()
        journal, path, info = self.load(fixture, reserve=False)
        self.assertFalse(fixture['resume_fence'].exists())
        journal.reserve(self.root / 'new-run')
        self.assertTrue(fixture['resume_fence'].is_file())
        fence_before = fixture['resume_fence'].read_bytes()
        with self.assertRaises(self.rejection):
            journal.reserve(self.root / 'different-run')
        copied = self.root / 'copied-journal.json'
        self.write_private(copied, path.read_bytes())
        resealed, resealed_info = self.seal(fixture, output=self.root / 'resealed-journal.json')
        for candidate, pin in ((copied, info['journal_sha256']),
                               (resealed, resealed_info['journal_sha256'])):
            with self.subTest(path=candidate), self.assertRaises(self.rejection):
                other = relay.CombinedReadJournal(self.transport, candidate, pin)
                other.reserve(self.root / 'yet-another-run')
        self.assertEqual(fixture['resume_fence'].read_bytes(), fence_before)

    def test_existing_or_symlink_resume_fence_is_never_overwritten(self):
        for defect in ('existing', 'symlink'):
            with self.subTest(defect=defect), self.isolated_case() as case:
                fixture = case.fixture()
                journal, _, _ = case.load(fixture, reserve=False)
                target = fixture['resume_fence']
                if defect == 'existing':
                    case.write_private(target, b'synthetic sentinel\n')
                else:
                    destination = case.root / 'fence-sentinel'
                    case.write_private(destination, b'synthetic sentinel\n')
                    target.symlink_to(destination)
                with case.assertRaises(case.rejection):
                    journal.reserve(case.root / 'new-run')
                self.assertEqual(target.read_bytes(), b'synthetic sentinel\n')

    def test_fresh_requests_reject_begin_main_writes_wrong_generation_and_watermarks(self):
        fixture = self.fixture()
        journal, _, _ = self.load(fixture)
        self.replay(journal, fixture)
        fresh = self.fresh_records(fixture)
        invalid = [
            ('site.read_site_backup', 'begin', {}),
            ('site.read_site_backup', 'records', {'generation': '0' * 64}),
            ('site.read_site_backup', 'feedback', {'after': 0, 'until': 1}),
            ('site.read_site_backup', 'profiles', {'after': 0, 'until': 1}),
            ('site.read_site_backup', 'event_chunks', {'stream': 'profiles', 'seq': 1, 'offset': 0, 'until': 1}),
            ('site.read_main_store', 'head', {}),
            ('site.write_main_store', 'begin_import', {'manifest_sha256': 'c' * 64}),
        ]
        for operation, action, data in invalid:
            with self.subTest(operation=operation, action=action), self.assertRaises(self.rejection):
                journal.response(dict(fresh, operation=operation, arguments={'action': action, 'data': data}))
        with self.assertRaises(self.rejection):
            journal.response(dict(fresh, request_id=fresh['request_id'] + 1))
        self.assertIsNone(journal.response(fresh))
        next_request = dict(fresh, request_id=fresh['request_id'] + 1,
                            arguments={'action': 'feedback', 'data': {'after': 0, 'until': 0}})
        with self.assertRaises(self.rejection):
            journal.response(dict(next_request, session_id='c' * 32))
        self.assertIsNone(journal.response(next_request))
        self.assertIsNone(journal.response(dict(next_request, request_id=next_request['request_id'] + 1,
            arguments={'action': 'profiles', 'data': {'after': 0, 'until': 0}})))

    def test_first_fresh_same_generation_changed_cursor_rejected_without_exhausting(self):
        fixture = self.fixture()
        journal, _, _ = self.load(fixture)
        self.replay(journal, fixture)
        expected = self.fresh_records(fixture)
        changed = copy.deepcopy(expected)
        changed['arguments']['data']['after']['offset'] += 1
        with self.assertRaises(self.rejection):
            journal.response(changed)
        self.assertFalse(journal.exhausted)
        self.assertEqual(journal.replayed, fixture['prefix'] + 1)
        self.assertIsNone(journal.response(expected))
        self.assertTrue(journal.exhausted)

    def test_first_fresh_snapshot_allowed_wrong_method_rejected_without_exhausting(self):
        fixture = self.fixture()
        journal, _, _ = self.load(fixture)
        self.replay(journal, fixture)
        expected = self.fresh_records(fixture)
        changed = dict(expected, arguments={'action': 'feedback', 'data': {
            'after': 0, 'until': fixture['begin']['snapshot']['feedback_cursor']}})
        with self.assertRaises(self.rejection):
            journal.response(changed)
        self.assertFalse(journal.exhausted)
        self.assertEqual(journal.replayed, fixture['prefix'] + 1)
        self.assertIsNone(journal.response(expected))
        self.assertTrue(journal.exhausted)

    def test_terminal_success_requires_complete_replay_and_first_fresh_request(self):
        fixture = self.fixture()
        journal, _, _ = self.load(fixture)
        adapted = relay.make_transport(self.manifest, self.sha, self.files, journal=journal)
        terminal = {'protocol': self.transport.PROTOCOL, 'type': 'result', 'session_id': NEW_SESSION,
                    'request_id': 1, 'ok': True, 'result': {}}
        with self.assertRaises(self.rejection):
            adapted._curated_result(terminal, NEW_SESSION, 1)
        self.replay(journal, fixture)
        terminal['request_id'] = fixture['prefix'] + 1
        with self.assertRaises(self.rejection):
            adapted._curated_result(terminal, NEW_SESSION, terminal['request_id'])
        fresh = self.fresh_records(fixture)
        journal.response(fresh)
        terminal['request_id'] = fresh['request_id']
        self.assertEqual(adapted._curated_result(terminal, NEW_SESSION, terminal['request_id']), terminal)

    def complete_transcript(self):
        """Run the actual backup implementation against entirely local fixture callbacks."""
        fixture = NativeFixture(self.root)
        transcript = []
        owner = self
        class Host:
            def site_call(self, name, arguments):
                action, data = arguments['action'], arguments['data']
                if name == 'read_main_store':
                    result = ({'head': {'revision': fixture.revision,
                               'import_id': fixture.source_manifest_sha,
                               'manifest_sha256': fixture.source_manifest_sha,
                               'watermarks': {'feedback_cursor': 0, 'profile_cursor': 0, 'request_id': 0}}}
                              if action == 'head' else fixture.main(action, data))
                elif name == 'read_site_backup':
                    result = fixture.site(action, data)
                    if action == 'begin':
                        result['captured_at'] = CAPTURED_AT
                else:
                    raise AssertionError('Unexpected native capability: ' + name)
                req = owner.request(len(transcript) + 1, 'site.' + name, action, copy.deepcopy(data))
                transcript.append((req, owner.response(req, copy.deepcopy(result))))
                return result
        args = d1_native_host.parser().parse_args(['backup-export', '--output', str(self.root / 'baseline-backup')])
        result = d1_native_host.D1NativeRunner(Host()).execute(args)
        index = next(index for index, (req, _) in enumerate(transcript)
                     if req['operation'] == 'site.read_site_backup' and req['arguments']['action'] == 'records')
        return fixture, transcript, index, result

    def publish_synthetic_response(self, spool, request, result):
        number = request['request_id']
        def io(action, *extra):
            return relay.io_main(self.transport, [action, '--spool', str(spool), '--number', str(number), *extra])
        io('claim')
        io('preflight')
        raw = self.transport._encode(self.response(request, result))
        text = raw.decode()
        offset = 0
        for start in range(0, len(text), 16000):
            part = text[start:start + 16000]
            progress = io('append', '--offset', str(offset), '--text-json', json.dumps(part))
            offset += len(part.encode())
            self.assertEqual(progress['next'], offset)
        io('publish')

    def test_cli_seals_same_closed_inventory_without_native_calls(self):
        fixture = self.fixture()
        before = self.contents(fixture['old'].path)
        output = self.root / 'cli-sealed-journal.json'
        process = subprocess.run(self.command('seal-combined-journal',
            '--spool', fixture['old'].path, '--prefix', fixture['prefix'], '--output', output,
            '--original-source-root', base.PROJECT, '--original-source-manifest', self.manifest,
            '--original-source-manifest-sha256', self.sha, '--retry-receipt', fixture['receipt'],
            '--retry-receipt-sha256', fixture['receipt_sha256'], '--retry-fence', fixture['fence']),
            capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(process.stderr, '')
        info = json.loads(process.stdout)
        self.assertEqual(info['journal_sha256'], digest(output.read_bytes()))
        self.assertEqual(info['replayed_reads'], fixture['prefix'] + 1)
        journal = relay.CombinedReadJournal(self.transport, output, info['journal_sha256'])
        self.assertEqual(journal.replayed, 0)
        self.assertFalse(fixture['resume_fence'].exists())
        self.assertEqual(self.contents(fixture['old'].path), before)

    def test_cli_cannot_replace_fixed_backup_export_with_arbitrary_command(self):
        fixture = self.fixture()
        path, info = self.seal(fixture)
        process = subprocess.run(self.command('resume-combined-backup', '--workspace', self.root,
            '--spool', 'new-run', '--timeout-seconds', '60', '--journal', path,
            '--journal-sha256', info['journal_sha256'], '--output', self.root / 'finished-backup',
            '--', 'publish-activate'), capture_output=True, text=True, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(fixture['resume_fence'].exists())
        self.assertFalse((self.root / 'new-run').exists())

    def test_real_cli_replays_full_combined_prefix_then_only_fresh_site_reads(self):
        _, transcript, prefix, baseline = self.complete_transcript()
        fixture = self.fixture(prefix=prefix, transcript=transcript)
        before = self.contents(fixture['old'].path)
        path, info = self.seal(fixture)
        output = self.root / 'finished-backup'
        process = subprocess.Popen(self.command('resume-combined-backup', '--workspace', self.root,
            '--spool', 'new-run', '--timeout-seconds', '60', '--journal', path,
            '--journal-sha256', info['journal_sha256'], '--output', output),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, process)
        new = self.root / 'new-run'
        fresh_count = 0
        for expected, successful in transcript[prefix + 1:]:
            line = process.stdout.readline()
            self.assertTrue(line, process.stderr.read() if process.poll() is not None else 'Missing fresh request')
            event = json.loads(line)
            self.assertEqual(event['request_id'], expected['request_id'])
            self.assertEqual(event['operation'], 'site.read_site_backup')
            self.assertEqual(event['replayed_reads'], prefix + 1)
            request = self.transport._decode((new / ('request-%d.json' % expected['request_id'])).read_bytes())
            self.assertEqual(request, dict(expected, session_id=request['session_id']))
            self.assertNotEqual(request['arguments']['action'], 'begin')
            self.publish_synthetic_response(new, request, successful['result'])
            fresh_count += 1
        terminal = json.loads(process.stdout.readline())
        self.assertTrue(terminal['ok'])
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertEqual(process.stderr.read(), '')
        self.assertGreater(fresh_count, 0)
        for number in range(1, prefix + 2):
            self.assertTrue((new / ('replay-%d.json' % number)).is_file())
            self.assertFalse((new / ('invoke-%d.json' % number)).exists())
        replay = self.transport._decode((new / ('response-%d.json' % (prefix + 1))).read_bytes())
        self.assertEqual(replay['result'], transcript[prefix][1]['result'])
        self.assertNotEqual(replay['session_id'], OLD_SESSION)
        summary = self.transport._decode((new / 'replay-summary.json').read_bytes())
        self.assertEqual(summary['replayed_reads'], prefix + 1)
        verified = d1_backup.verify_backup(output)
        self.assertEqual(verified['backup_sha256'], baseline['backup_sha256'])
        self.assertEqual(self.contents(fixture['old'].path), before)
        self.assertTrue(fixture['resume_fence'].is_file())
        blocked = subprocess.run(self.command('resume-combined-backup', '--workspace', self.root,
            '--spool', 'second-run', '--timeout-seconds', '60', '--journal', path,
            '--journal-sha256', info['journal_sha256'], '--output', self.root / 'second-output'),
            capture_output=True, text=True, timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertFalse((self.root / 'second-run').exists())
        self.assertFalse((self.root / 'second-output').exists())


if __name__ == '__main__':
    unittest.main()

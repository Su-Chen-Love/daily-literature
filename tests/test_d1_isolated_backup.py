"""Actual isolated source loader + synthetic read-only backup callbacks.

Only child-process TMPDIR is changed to a disposable symlink fixture. No remote
service, native connector, host setting, real data, or failed spool is used.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from academic_radar import sqlite_delta
from test_d1_backup import NativeFixture

PROJECT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT/'scripts/d1-native-relay.py'
spec = importlib.util.spec_from_file_location('isolated_backup_relay', LAUNCHER)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


@unittest.skipUnless(os.name == 'posix', 'Isolated relay requires POSIX')
class IsolatedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Fixture-only canonical root; production paths remain unmodified.
        self.root = Path(self.temporary.name).resolve()
        self.fixture = NativeFixture(self.root)
        self.manifest = self.root/'source-manifest.json'
        self.pin = relay.pin_source(self.manifest)['source_manifest_sha256']
        self.real_temp = self.root/'system-real-temp'
        self.real_temp.mkdir()
        self.alias_temp = self.root/'mac-temp-alias'
        self.alias_temp.symlink_to(self.real_temp, target_is_directory=True)
        self.calls = []
        self.original_environment = dict(os.environ)

    def isolated(self, arguments, *, allow_reads):
        env = {**os.environ, 'TMPDIR':str(self.alias_temp),
               'TEMP':str(self.alias_temp), 'TMP':str(self.alias_temp)}
        process = subprocess.Popen([sys.executable, '-I', '-B', str(LAUNCHER),
            '--source-manifest', str(self.manifest), '--source-manifest-sha256', self.pin,
            'host', *map(str, arguments)], cwd=self.root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        timer = threading.Timer(60, process.kill)
        timer.daemon = True
        timer.start()
        final = None
        try:
            number = 0
            session = None
            for line in process.stdout:
                self.assertLess(len(line), 2_000_000)
                value = json.loads(line)
                if value['type'] == 'result':
                    final = value
                    break
                self.assertTrue(allow_reads, 'Offline verification/restore requested a callback')
                self.assertEqual(value['request_id'], number + 1)
                number += 1
                session = session or value['session_id']
                self.assertEqual(value['session_id'], session)
                operation, args = value['operation'], value['arguments']
                self.calls.append((operation, args['action']))
                if operation == 'site.read_main_store':
                    if args['action'] == 'head':
                        result = {'head': {'revision':self.fixture.revision,
                            'import_id':self.fixture.source_manifest_sha,
                            'manifest_sha256':self.fixture.source_manifest_sha,
                            'watermarks':{'feedback_cursor':0,'profile_cursor':0,'request_id':0}}}
                    else:
                        result = self.fixture.main(args['action'], args['data'])
                elif operation == 'site.read_site_backup':
                    result = self.fixture.site(args['action'], args['data'])
                else:
                    self.fail('Unexpected capability in read-only backup fixture')
                reply = {key:value[key] for key in ('protocol','session_id','request_id','operation')}
                reply.update(type='response', ok=True, result=result)
                process.stdin.write(json.dumps(reply)+'\n')
                process.stdin.flush()
            process.stdin.close()
            self.assertEqual(process.wait(timeout=10), 0, final)
            self.assertEqual(process.stderr.read(), '')
            self.assertIsNotNone(final)
            self.assertTrue(final['ok'], final)
            self.assertEqual(process.stdout.read(), '')
            return final['result']
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            self.assertEqual(dict(os.environ), self.original_environment)

    def test_isolated_export_verify_restore_with_symlink_system_temp(self):
        for explicit_schema in (False, True):
            with self.subTest(explicit_schema=explicit_schema):
                self.calls.clear()
                suffix = 'explicit' if explicit_schema else 'bundled'
                output = self.root/('backup-'+suffix)
                restored = self.root/('restore-'+suffix)
                trust = (['--trusted-schema', self.fixture.db, '--trusted-schema-sha256',
                          hashlib.sha256(self.fixture.db.read_bytes()).hexdigest()]
                         if explicit_schema else [])
                result = self.isolated(['backup-export','--output',output,*trust], allow_reads=True)
                self.assertEqual(self.calls[:6], [
                    ('site.read_main_store','head'), ('site.read_main_store','read_manifest'),
                    ('site.read_site_backup','begin'), ('site.read_main_store','read_manifest'),
                    ('site.read_main_store','export_summary'), ('site.read_main_store','export_rows')])
                self.assertGreater(result['rows'], 0)
                calls = list(self.calls)
                verified = self.isolated(['backup-verify','--backup',output,
                    '--backup-sha256',result['backup_sha256']], allow_reads=False)
                self.assertEqual(verified['status'], 'verified')
                report = self.isolated(['backup-restore','--backup',output,
                    '--backup-sha256',result['backup_sha256'],'--output',restored,
                    '--exact-main-only',*trust], allow_reads=False)
                self.assertTrue(report['exact_main_state'])
                self.assertEqual(self.calls, calls)
                self.assertEqual(sqlite_delta.inspect_snapshot(restored/'state'/'papers.sqlite3'),
                                 sqlite_delta.inspect_snapshot(self.fixture.db))
                self.assertEqual((restored/'state'/'history'/'new.txt').read_bytes(),
                                 (self.fixture.state/'history'/'new.txt').read_bytes())
                self.assertEqual(list(self.real_temp.iterdir()), [])
                self.assertFalse(list(self.root.glob('academic-d1-*-schema-*')))
                self.assertFalse(list(self.root.glob('.d1-backup-*')))


if __name__ == '__main__':
    unittest.main()

"""Deterministic publication races; synthetic local files and subprocesses only."""
import contextlib
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from academic_radar import native_spool as transport
import test_d1_native_spool as d1_tests
import test_native_spool as native_tests

relay = d1_tests.relay


class SpoolPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.spool = transport.PrivateSpool(self.root, "private")
        self.addCleanup(self.spool.close)

    def poll(self):
        return relay.io_main(transport, ["poll", "--spool", str(self.spool.path), "--number", "1"])

    def request_bytes(self):
        raw = transport._encode(native_tests.request())
        self.spool.save("live-1.json", b"{}\n")
        self.spool.save("pending-1.json", transport._encode({
            "heartbeat": "live-1.json", "deadline_unix_ms": int((time.time() + 60) * 1000),
            "request_sha256": hashlib.sha256(raw).hexdigest()}))
        return raw

    def result_bytes(self):
        return transport._encode({"protocol": transport.PROTOCOL, "type": "result",
            "session_id": native_tests.SESSION, "request_id": 0, "ok": True, "result": {}})

    @contextlib.contextmanager
    def paused_writer(self, name, raw):
        # A distinct process opens its own file descriptors. Pause at the exact
        # hardlink window instead of hoping for a thread scheduling coincidence.
        code = """import importlib.util, os, sys
from pathlib import Path
from academic_radar import native_spool as transport
spec = importlib.util.spec_from_file_location('publication_helper', sys.argv[1])
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
original = os.link
def link(*args, **kwargs):
    original(*args, **kwargs)
    print('linked', flush=True)
    assert sys.stdin.readline() == 'continue\\n'
os.link = link
with helper.existing(transport, Path(sys.argv[2])) as spool:
    spool.save(sys.argv[3], bytes.fromhex(sys.argv[4]))
"""
        process = subprocess.Popen([sys.executable, "-E", "-s", "-B", "-c", code,
            str(d1_tests.LAUNCHER), str(self.spool.path), name, raw.hex()],
            cwd=str(transport.TRUSTED_SOURCE_ROOT), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(5), "writer did not reach publication")
                self.assertEqual(process.stdout.readline(), "linked\n")
            self.assertEqual((self.spool.path / name).stat().st_nlink, 2)
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def finish_writer(self, process):
        output, errors = process.communicate("continue\n", timeout=5)
        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(output, "")
        self.assertEqual(errors, "")

    def test_response_reader_waits_in_exact_link_window_and_until_directory_fsync(self):
        raw = transport._encode(native_tests.response(native_tests.request()))
        original_link, original_fsync = os.link, os.fsync
        observed = []
        def link(*args, **kwargs):
            original_link(*args, **kwargs)
            self.assertEqual((self.spool.path / "response-1.json").stat().st_nlink, 2)
            self.assertIsNone(self.spool.response(1))
            observed.append("link")
        def fsync(fd):
            if fd == self.spool.fd:
                self.assertEqual((self.spool.path / "response-1.json").stat().st_nlink, 1)
                self.assertIsNone(self.spool.response(1))
                observed.append("directory_fsync")
            return original_fsync(fd)
        with patch.object(transport.os, "link", link), patch.object(transport.os, "fsync", fsync):
            self.spool.save("response-1.json", raw)
        self.assertEqual(observed, ["link", "directory_fsync"])
        self.assertEqual(self.spool.response(1), raw)
        self.assertEqual(stat.S_IMODE((self.spool.path / "response-1.json").stat().st_mode), 0o400)

    def test_independent_response_reader_waits_for_writer_then_returns_exact_bytes(self):
        raw = transport._encode(native_tests.response(native_tests.request()))
        with self.paused_writer("response-1.json", raw) as process:
            self.assertIsNone(self.spool.response(1))
            with self.assertRaises(relay.PublicationPending):
                relay.read(transport, self.spool, "response-1.json")
            self.finish_writer(process)
        self.assertEqual(self.spool.response(1), raw)
        self.assertEqual(relay.read(transport, self.spool, "response-1.json"), raw)

    def test_request_poll_waits_for_independent_writer_then_becomes_ready(self):
        raw = self.request_bytes()
        with self.paused_writer("request-1.json", raw) as process:
            self.assertEqual(self.poll(), {"state": "waiting", "request_id": 1})
            self.finish_writer(process)
        self.assertEqual(self.poll(), {"state": "ready", "request_id": 1})
        self.assertEqual(relay.read(transport, self.spool, "request-1.json"), raw)

    def test_result_poll_waits_for_independent_writer_then_reports_done(self):
        raw = self.result_bytes()
        with self.paused_writer("result.json", raw) as process:
            self.assertEqual(self.poll(), {"state": "waiting", "request_id": 1})
            with self.assertRaises(relay.PublicationPending):
                relay.io_main(transport, ["result", "--spool", str(self.spool.path)])
            self.finish_writer(process)
        self.assertEqual(self.poll(), {"state": "done", "ok": True, "request_id": 0})
        self.assertEqual(relay.read(transport, self.spool, "result.json"), raw)

    def test_writer_death_releases_lock_but_abandoned_hardlink_is_rejected(self):
        with self.paused_writer("response-1.json", b"{}\n") as process:
            self.assertIsNone(self.spool.response(1))
            process.kill()
            process.wait(timeout=5)
        self.assertEqual((self.spool.path / "response-1.json").stat().st_nlink, 2)
        with self.assertRaisesRegex(transport.RelayError, "unsafe_response"):
            self.spool.response(1)
        with self.assertRaisesRegex(transport.RelayError, "unsafe_file"):
            relay.read(transport, self.spool, "response-1.json")

    def test_permanent_hardlinks_and_symlinks_still_fail_closed(self):
        self.spool.save("response-1.json", b"{}\n")
        os.link(self.spool.path / "response-1.json", self.root / "outside")
        (self.spool.path / "response-2.json").symlink_to(self.root / "outside")
        for number in (1, 2):
            with self.subTest(number=number):
                with self.assertRaisesRegex(transport.RelayError, "unsafe_response"):
                    self.spool.response(number)
                with self.assertRaises((transport.RelayError, OSError)):
                    relay.read(transport, self.spool, "response-%d.json" % number)

    def test_request_response_and_result_publication_never_clobber(self):
        for name in ("request-1.json", "response-1.json", "result.json"):
            with self.subTest(name=name):
                self.spool.save(name, b'{"first":true}\n')
                before = (self.spool.path / name).stat()
                with self.assertRaises(FileExistsError):
                    self.spool.save(name, b'{"second":true}\n')
                self.assertEqual((self.spool.path / name).read_bytes(), b'{"first":true}\n')
                self.assertEqual((self.spool.path / name).stat().st_ino, before.st_ino)
                self.assertEqual((self.spool.path / name).stat().st_nlink, 1)
                self.assertFalse((self.spool.path / ("." + name + ".tmp")).exists())

    def test_unexpected_lock_errors_fail_closed_without_lockless_fallback(self):
        self.spool.save("response-1.json", b"{}\n")
        for code in (errno.ENOSYS, errno.EBADF, errno.EINVAL):
            with self.subTest(errno=code), patch.object(fcntl, "flock", side_effect=OSError(code, "synthetic")):
                with self.assertRaisesRegex(transport.RelayError, "unsafe_response"):
                    self.spool.response(1)
                with self.assertRaisesRegex(relay.RelayError, "unsafe_file"):
                    relay.read(transport, self.spool, "response-1.json")
                with self.assertRaises(OSError):
                    self.spool.save("request-1.json", b"{}\n")
                self.assertFalse((self.spool.path / "request-1.json").exists())
                self.assertFalse((self.spool.path / ".request-1.json.tmp").exists())

    def test_writer_lock_is_nonblocking_and_failure_cleans_temporary_file(self):
        with patch.object(fcntl, "flock", side_effect=BlockingIOError(errno.EAGAIN, "synthetic")) as lock:
            with self.assertRaises(BlockingIOError):
                self.spool.save("request-1.json", b"{}\n")
        self.assertEqual(lock.call_args.args[1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(list(self.spool.path.iterdir()), [])

    def test_reader_keeps_shared_lock_through_validation_and_detects_changed_bytes(self):
        self.spool.save("response-1.json", b'{"value":1}\n')
        original_read = os.read
        observed = []
        def read(fd, count):
            raw = original_read(fd, count)
            if not observed:
                other = os.open(self.spool.path / "response-1.json", os.O_RDONLY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(other)
                target = self.spool.path / "response-1.json"
                target.chmod(0o600)
                target.write_bytes(b'{"value":2}\n')
                target.chmod(0o400)
                observed.append(True)
            return raw
        with patch.object(transport.os, "read", read):
            with self.assertRaisesRegex(transport.RelayError, "response_changed"):
                self.spool.response(1)
        self.assertEqual(observed, [True])

    def test_held_response_lock_cannot_extend_relay_deadline(self):
        fixture = native_tests.NativeSpoolTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        held = []
        def host(path, req, metadata):
            native_tests.atomic_response(path.parent, native_tests.response(req))
            fd = os.open(path.parent / "response-1.json", os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)
        started = time.monotonic()
        try:
            fixture.assert_failure(callback=host, expected="relay_timeout", timeout=0.2)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            for fd in held:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()

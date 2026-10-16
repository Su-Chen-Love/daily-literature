"""Synthetic pipes/files only. No Site/Library tools, HTTP or real state."""
import contextlib
import io
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar import native_spool as relay
from academic_radar.native_host import MAX_MESSAGE_BYTES, PROTOCOL

SESSION = "a" * 32
BASE_ARGS = ["capture-head", "--config", "/synthetic/config.toml", "--state", "/synthetic/state",
             "--role", "staging", "--head-file", "/synthetic/head.json"]


def request(number=1, operation="site.read_checkpoint_head", **changes):
    return {"protocol": PROTOCOL, "type": "request", "session_id": SESSION,
            "request_id": number, "operation": operation, "arguments": {}, **changes}


def response(value):
    return {key: value[key] for key in ("protocol", "session_id", "request_id", "operation")} | {
        "type": "response", "ok": True, "result": {"generation": 0, "publication": None}}


def atomic_response(directory, value):
    name = "response-%s.json" % value["request_id"]
    path = directory / ("." + name + ".host-tmp")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(relay._encode(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(path, directory / name)


class HostOutput(io.StringIO):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def write(self, text):
        result = super().write(text)
        value = json.loads(text)
        if value["type"] == "request_ready":
            path = Path(value["path"])
            self.callback(path, json.loads(path.read_bytes()), value)
        return result


def child_script(*, request_code="", payload_size=0, after_response="", before_request="",
                 result_code="", second_request=""):
    return '''import json, sys, time, os
P = %r
S = %r
def emit(value):
    sys.stdout.write(json.dumps(value, separators=(',', ':')) + '\\n')
    sys.stdout.flush()
%s
req = {'protocol': P, 'type': 'request', 'session_id': S, 'request_id': 1,
       'operation': 'site.read_checkpoint_head', 'arguments': {}}
if %d:
    req['arguments'] = {'private_payload': 'PRIVATE_PROFILE_' + 'x' * %d}
%s
emit(req)
raw = sys.stdin.buffer.readline(%d)
if not raw:
    sys.exit(7)
received = json.loads(raw)
assert received['session_id'] == S and received['request_id'] == 1
%s
%s
terminal = {'protocol': P, 'type': 'result', 'session_id': S, 'request_id': req['request_id'],
            'ok': True, 'result': {'status': 'captured', 'site_generation': 0}}
%s
emit(terminal)
''' % (PROTOCOL, SESSION, before_request, payload_size, payload_size, request_code,
       MAX_MESSAGE_BYTES + 1, after_response, second_request, result_code)


class NativeSpoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.children = []
        self.threads = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        for child in self.children:
            self.assertIsNotNone(child.poll(), "relay leaked a child process")
            self.assertTrue(child.stdin.closed)
            self.assertTrue(child.stdout.closed)
        self.directory.cleanup()

    @contextlib.contextmanager
    def nested_workspace(self):
        previous = self.workspace
        with tempfile.TemporaryDirectory(dir=previous) as subdir:
            self.workspace = Path(subdir)
            try:
                yield
            finally:
                self.workspace = previous

    def run_child(self, script=None, callback=None, timeout=2):
        def spawn(arguments, workspace):
            self.assertEqual(arguments, BASE_ARGS)
            self.assertEqual(workspace, self.workspace)
            child = subprocess.Popen([sys.executable, "-c", script or child_script()],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     start_new_session=True, bufsize=0)
            self.children.append(child)
            return child
        callback = callback or (lambda path, req, meta: atomic_response(path.parent, response(req)))
        output = HostOutput(callback)
        with patch.object(relay, "_spawn_native", spawn):
            code = relay.relay(self.workspace, "new-spool", BASE_ARGS, timeout=timeout, output=output)
        terminal = json.loads((self.workspace / "new-spool/result.json").read_bytes())
        return code, output.getvalue(), terminal

    def assert_failure(self, script=None, callback=None, expected=None, timeout=2):
        code, visible, result = self.run_child(script, callback, timeout)
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        if expected:
            self.assertEqual(result["error"]["code"], expected)
        for secret in ("PRIVATE_PROFILE", "SECRET_ERROR", "SECRET_TOKEN"):
            self.assertNotIn(secret, visible)
            self.assertNotIn(secret, json.dumps(result))
        return result

    def test_large_round_trip_tiny_stdout_private_immutable_files(self):
        seen = []
        def host(path, req, metadata):
            self.assertGreater(path.stat().st_size, 600000)
            self.assertEqual(metadata["byte_length"], path.stat().st_size)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            answer = response(req)
            answer["result"] = {"content": "PRIVATE_PROFILE_" + "z" * 650000}
            atomic_response(path.parent, answer)
            seen.append(req)
        script = child_script(payload_size=650000,
            before_request="sys.stderr.write('SECRET_ERROR\\n'); sys.stderr.flush()",
            after_response="assert len(received['result']['content']) > 650000",
            result_code="terminal['result'].update(profile={'content':'PRIVATE_PROFILE'}, token='SECRET_TOKEN')")
        code, visible, terminal = self.run_child(script, host)
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        self.assertLess(len(visible), 1000)
        self.assertNotIn("PRIVATE_PROFILE", visible)
        self.assertNotIn("SECRET", visible)
        self.assertEqual(terminal["result"], {"status": "captured", "site_generation": 0})
        self.assertEqual(stat.S_IMODE((self.workspace / "new-spool/result.json").stat().st_mode), 0o400)

    def test_two_requests_require_increasing_ids(self):
        script = child_script(second_request="req['request_id'] = 2; emit(req); assert sys.stdin.buffer.readline()")
        code, visible, terminal = self.run_child(script)
        self.assertEqual(code, 0)
        self.assertEqual(terminal["request_id"], 2)
        self.assertEqual([json.loads(x)["request_id"] for x in visible.splitlines()], [1, 2, 2])

    def test_future_response_cannot_be_preplayed_before_request_is_published(self):
        def host(path, req, metadata):
            atomic_response(path.parent, response(req))
            atomic_response(path.parent, response(request(number=2)))
        script = child_script(second_request="req['request_id'] = 2; emit(req); assert sys.stdin.buffer.readline()")
        self.assert_failure(script, host, "preplayed_response")
        self.assertFalse((self.workspace / "new-spool/request-2.json").exists())

    def test_request_envelope_rejects_wrong_protocol_session_id_operation_or_shape(self):
        for expression in ("req['protocol'] = 'wrong'", "req['session_id'] = '../escape'",
                           "req['request_id'] = 2", "req['request_id'] = True",
                           "req['operation'] = 'shell.exec'", "req['operation'] = '../escape'",
                           "req['arguments'] = []", "req['extra'] = 'SECRET_TOKEN'"):
            with self.subTest(expression=expression):
                with self.nested_workspace():
                    self.assert_failure(child_script(request_code=expression), expected="invalid_protocol")

    def test_second_request_wrong_session_or_repeated_id_is_rejected(self):
        for change in ("req['session_id'] = 'b' * 32; req['request_id'] = 2", "req['request_id'] = 1"):
            with self.subTest(change=change), self.nested_workspace():
                self.assert_failure(child_script(second_request=change + "; emit(req); time.sleep(5)"),
                                    expected="invalid_protocol")

    def test_response_wrong_session_id_operation_or_protocol(self):
        for field, value in (("session_id", "b" * 32), ("request_id", 2), ("request_id", True),
                             ("operation", "site.read_profile"), ("protocol", "wrong"), ("type", "request")):
            def host(path, req, metadata):
                answer = response(req)
                answer[field] = value
                # Always use the expected request filename; corrupt only envelope.
                target = path.parent / "response-1.json"
                target.write_bytes(relay._encode(answer))
                target.chmod(0o600)
            with self.subTest(field=field, value=value), self.nested_workspace():
                self.assert_failure(callback=host, expected="invalid_protocol")

    def test_false_response_forwarded_once_without_remote_error_body(self):
        def host(path, req, metadata):
            answer = response(req)
            del answer["result"]
            answer.update(ok=False, error={"message": "SECRET_ERROR", "token": "SECRET_TOKEN"})
            atomic_response(path.parent, answer)
        script = child_script(after_response="assert received['error'] == {'code': 'host_action_failed'}",
            result_code="terminal.pop('result'); terminal.update(ok=False, error={'code':'native_host_failed', 'message':'SECRET_ERROR'}); emit(terminal); sys.exit(1)")
        terminal = self.assert_failure(script, host, "native_host_failed")
        self.assertEqual(terminal["type"], "result")

    def test_malformed_oversized_duplicate_nonfinite_or_multiline_json(self):
        bad = [b'{"x":', b'\xff\n', b'{"x":1,"x":2}\n', b'{"x":NaN}\n',
               b'{"x":1e999}\n', b'{\n"x":1}\n', b' ' * (MAX_MESSAGE_BYTES + 1),
               b'[' * 60 + b'0' + b']' * 60 + b'\n']
        for raw in bad:
            def host(path, req, metadata):
                destination = path.parent / "response-1.json"
                destination.write_bytes(raw)
                destination.chmod(0o600)
            with self.subTest(size=len(raw), prefix=raw[:15]), self.nested_workspace():
                self.assert_failure(callback=host, expected="invalid_message")

    def test_request_canonical_encoding_cannot_exceed_message_bound(self):
        base = relay._encode(request()).decode().rstrip("\n")
        prefix = base.replace('"arguments":{}', '"arguments":{"numbers":[REPLACE]}')
        child = "import sys,time; raw=%r.replace('REPLACE', ','.join(['1e3']*400000)); sys.stdout.write(raw+'\\n'); sys.stdout.flush(); time.sleep(5)" % prefix
        self.assert_failure(child, expected="invalid_message")
        self.assertFalse((self.workspace / "new-spool/request-1.json").exists())

    def test_partial_file_never_forwarded(self):
        marker = self.workspace / "child-received"
        def host(path, req, metadata):
            target = path.parent / "response-1.json"
            target.write_bytes(relay._encode(response(req))[:-1])
            target.chmod(0o600)
        self.assert_failure(child_script(after_response="open(%r, 'w').write('received')" % str(marker)),
                            host, "invalid_message")
        self.assertFalse(marker.exists())

    def test_temporary_file_is_ignored_until_atomic_handoff(self):
        def host(path, req, metadata):
            temporary = path.parent / ".response-1.json.host-tmp"
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            handle = os.fdopen(fd, "wb")
            raw = relay._encode(response(req))
            handle.write(raw[:25]); handle.flush()
            def finish():
                time.sleep(0.15)
                handle.write(raw[25:]); handle.flush(); os.fsync(handle.fileno()); handle.close()
                os.replace(temporary, path.parent / "response-1.json")
            thread = threading.Thread(target=finish)
            thread.start(); self.threads.append(thread)
        code, _, _ = self.run_child(callback=host)
        self.assertEqual(code, 0)

    def test_symlink_fifo_directory_hardlink_or_public_response_rejected(self):
        for kind in ("symlink", "fifo", "directory", "hardlink", "public"):
            def host(path, req, metadata):
                target = path.parent / "response-1.json"
                if kind == "symlink":
                    target.symlink_to(self.workspace / "outside")
                elif kind == "fifo":
                    os.mkfifo(target, 0o600)
                elif kind == "directory":
                    target.mkdir()
                else:
                    target.write_bytes(relay._encode(response(req)))
                    target.chmod(0o600 if kind == "hardlink" else 0o644)
                    if kind == "hardlink":
                        os.link(target, self.workspace / "outside")
            with self.subTest(kind=kind), self.nested_workspace():
                self.assert_failure(callback=host, expected="unsafe_response")

    def test_child_oversized_malformed_partial_or_unexpected_eof(self):
        for code, expected in (("sys.stdout.write('x' * %d); sys.stdout.flush(); time.sleep(5)" % (MAX_MESSAGE_BYTES + 1), "invalid_message"),
                               ("sys.stdout.write('{bad}\\n'); sys.stdout.flush(); time.sleep(5)", "invalid_message"),
                               ("sys.stdout.write('{bad}'); sys.stdout.flush(); sys.exit(0)", "unexpected_eof"),
                               ("sys.exit(0)", "unexpected_eof")):
            with self.subTest(code=code[:30]), self.nested_workspace():
                self.assert_failure("import sys,time; " + code, expected=expected)

    def test_timeout_waiting_child_or_host_cleans_up(self):
        for script, host in (("import time; time.sleep(5)", None), (child_script(), lambda *args: None)):
            with self.subTest(script=script[:20]), self.nested_workspace():
                self.assert_failure(script, host, "relay_timeout", timeout=0.2)

    def test_unexpected_child_exit_while_host_pending(self):
        script = child_script().replace("raw = sys.stdin.buffer.readline", "sys.exit(0)\nraw = sys.stdin.buffer.readline")
        self.assert_failure(script, lambda *args: None, "unexpected_eof")

    def test_response_to_nonconsuming_child_is_bounded_by_deadline(self):
        script = child_script().replace("raw = sys.stdin.buffer.readline", "time.sleep(5)\nraw = sys.stdin.buffer.readline")
        def host(path, req, metadata):
            answer = response(req)
            answer["result"] = {"content": "x" * 700000}
            atomic_response(path.parent, answer)
        self.assert_failure(script, host, "relay_timeout", timeout=0.2)

    def test_terminal_requires_matching_session_request_id_and_exit(self):
        cases = [("terminal['session_id'] = 'b' * 32", "invalid_protocol"),
                 ("terminal['request_id'] = 0", "invalid_protocol"),
                 ("terminal['ok'] = 1", "invalid_protocol"),
                 ("terminal['result']['status'] = 'SECRET_TOKEN'", "invalid_protocol"),
                 ("emit(terminal); sys.exit(3)", "unexpected_exit"),
                 ("emit(terminal)", "unexpected_output")]
        for change, expected in cases:
            with self.subTest(change=change), self.nested_workspace():
                self.assert_failure(child_script(result_code=change), expected=expected)

    def test_terminal_without_requests_is_valid_and_curated(self):
        script = "import json; print(json.dumps(%r))" % {
            "protocol": PROTOCOL, "type": "result", "session_id": SESSION, "request_id": 0,
            "ok": True, "result": {"phase": "awaiting_judgment", "candidates": 0, "queue_path": "PRIVATE_PROFILE"}}
        code, visible, terminal = self.run_child(script)
        self.assertEqual(code, 0)
        self.assertEqual(terminal["result"], {"phase": "awaiting_judgment", "candidates": 0})
        self.assertNotIn("PRIVATE_PROFILE", visible)

    def test_spool_requires_absent_leaf_and_no_symlink_or_traversal_parents(self):
        (self.workspace / "existing").mkdir()
        (self.workspace / "link").symlink_to(self.workspace, target_is_directory=True)
        invalid = [(self.workspace, "existing"), (self.workspace, "../escape"),
                   (self.workspace, "/absolute"), (self.workspace / "link", "new"),
                   (self.workspace / "..", "new"), (Path("relative"), "new")]
        for workspace, name in invalid:
            with self.subTest(workspace=workspace, name=name), patch.object(relay, "_spawn_native") as spawn:
                with self.assertRaises(RelayErrorTypes):
                    relay.relay(workspace, name, BASE_ARGS, output=io.StringIO())
                spawn.assert_not_called()
        self.assertFalse((self.workspace / "escape").exists())

    def test_request_files_cannot_be_overwritten_and_spool_replacement_is_rejected(self):
        with relay.PrivateSpool(self.workspace, "private") as spool:
            spool.save("request-1.json", b"first\n")
            with self.assertRaises(FileExistsError):
                spool.save("request-1.json", b"second\n")
            self.assertEqual((spool.path / "request-1.json").read_bytes(), b"first\n")
            old = self.workspace / "old"
            spool.path.rename(old)
            spool.path.symlink_to(old, target_is_directory=True)
            with self.assertRaises(relay.RelayError):
                spool.response(1)

    def test_workspace_replacement_invalidates_advertised_absolute_paths(self):
        directory = self.workspace / "workspace"
        directory.mkdir()
        with relay.PrivateSpool(directory, "private") as spool:
            directory.rename(self.workspace / "renamed")
            (directory / "private").mkdir(parents=True, mode=0o700)
            with self.assertRaisesRegex(relay.RelayError, "unsafe_workspace"):
                spool.save("request-1.json", b"first\n")
            with self.assertRaisesRegex(relay.RelayError, "unsafe_workspace"):
                spool.response(1)
        self.assertFalse((directory / "private/request-1.json").exists())

    def test_workspace_ancestor_symlink_replacement_is_rejected(self):
        directory = self.workspace / "workspace"
        directory.mkdir()
        with relay.PrivateSpool(directory, "private") as spool:
            old = self.workspace / "renamed"
            directory.rename(old)
            directory.symlink_to(old, target_is_directory=True)
            with self.assertRaisesRegex(relay.RelayError, "unsafe_workspace"):
                spool.save("request-1.json", b"first\n")

    def test_real_pinned_child_ignores_planted_workspace_and_pythonpath_modules(self):
        marker = self.workspace / "shadow-executed"
        malicious = "open(%r, 'w').write('BAD')\n" % str(marker)
        for directory in (self.workspace, self.workspace / "hostile-imports"):
            package = directory / "academic_radar"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(malicious)
            (package / "native_host.py").write_text(malicious)
        # The pinned real runner sees only absent synthetic config and terminates
        # safely, without native calls or reading any real/private state.
        with patch.dict(os.environ, {"PYTHONPATH": str(self.workspace / "hostile-imports")}):
            child = relay._spawn_native(BASE_ARGS, self.workspace)
            stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 1)
        self.assertIsNone(stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads(stdout)["error"]["code"], "verification_failed")

    def test_sigterm_ignoring_child_is_killed(self):
        self.assert_failure("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(5)",
                            expected="relay_timeout", timeout=0.2)

    def test_metadata_output_supports_regular_file_redirection(self):
        target = self.workspace / "events.jsonl"
        event = {"type": "result_ready", "ok": True, "path": "synthetic"}
        with target.open("w", encoding="utf-8") as output:
            relay._emit(output, event, time.monotonic() + 1)
        self.assertEqual(json.loads(target.read_text()), event)

    def test_unread_full_stdout_cannot_defeat_timeout_or_child_cleanup(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), self.nested_workspace():
                marker = self.workspace / "child-pid"
                parent_code = """import os, subprocess, sys
from pathlib import Path
from academic_radar import native_spool as relay
os.set_blocking(sys.stdout.fileno(), False)
while True:
    try: os.write(sys.stdout.fileno(), b'x' * 65536)
    except BlockingIOError: break
def launch(args, workspace):
    child = subprocess.Popen([sys.executable, '-c', %r], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
    Path(%r).write_text(str(child.pid))
    return child
relay._spawn_native = launch
raise SystemExit(relay.main(%r))
        """ % (child_script(), str(marker), ["--workspace", str(self.workspace), "--spool", "blocked-output",
                                             "--timeout-seconds", "30" if cancelled else "0.3", "--"] + BASE_ARGS)
                parent = subprocess.Popen([sys.executable, "-E", "-s", "-c", parent_code],
                    cwd=str(relay.TRUSTED_SOURCE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    # Deliberately do not drain stdout until after process termination.
                    if cancelled:
                        deadline = time.monotonic() + 2
                        while not marker.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(marker.exists())
                        parent.send_signal(signal.SIGTERM)
                    parent.wait(timeout=3)
                    _, errors = parent.communicate(timeout=3)
                    self.assertEqual(parent.returncode, 130 if cancelled else 1, errors)
                    terminal = json.loads((self.workspace / "blocked-output/result.json").read_bytes())
                    self.assertEqual(terminal["error"]["code"], "interrupted" if cancelled else "output_timeout")
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(marker.read_text()), 0)
                finally:
                    if parent.poll() is None:
                        parent.kill()
                    parent.communicate(timeout=3)
                    if marker.exists():
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(int(marker.read_text()), signal.SIGKILL)

    def test_cli_signals_clean_children_and_restore_handlers_even_after_repeated_sigint(self):
        for termination_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(termination_signal=termination_signal), self.nested_workspace():
                marker = self.workspace / "processes.json"
                child_code = """import json, os, signal, subprocess, sys, time
from pathlib import Path
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
def stop(*_args):
    grandchild.wait(timeout=0.4)
    time.sleep(0.1)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
Path(%r).write_text(json.dumps([os.getpid(), grandchild.pid]))
time.sleep(30)
""" % str(marker)
                parent_code = """import signal, subprocess, sys
from academic_radar import native_spool as relay
def launch(args, workspace):
    return subprocess.Popen([sys.executable, '-c', %r], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
relay._spawn_native = launch
before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
code = relay.main(%r)
assert before == {number: signal.getsignal(number) for number in before}
raise SystemExit(code)
""" % (child_code, ["--workspace", str(self.workspace), "--spool", "cancelled", "--"] + BASE_ARGS)
                parent = subprocess.Popen([sys.executable, "-E", "-s", "-c", parent_code],
                    cwd=str(relay.TRUSTED_SOURCE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                pids = []
                try:
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(marker.exists())
                    pids = json.loads(marker.read_text())
                    parent.send_signal(termination_signal)
                    if termination_signal == signal.SIGINT:
                        time.sleep(0.03)
                        parent.send_signal(signal.SIGINT)
                    output, errors = parent.communicate(timeout=5)
                    self.assertEqual(parent.returncode, 130, errors)
                    self.assertEqual(json.loads(output)["error_code"], "interrupted")
                    for pid in pids:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                finally:
                    if parent.poll() is None:
                        parent.kill()
                    parent.communicate(timeout=5)
                    for pid in pids:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGKILL)

    def test_first_sigint_during_timeout_cleanup_cannot_leave_child_running(self):
        child_pid = self.workspace / "child-pid"
        cleanup_started = self.workspace / "cleanup-started"
        child_code = """import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(%r).write_text(str(os.getpid()))
time.sleep(30)
""" % str(child_pid)
        parent_code = """import subprocess, sys
from pathlib import Path
from academic_radar import native_spool as relay
def launch(args, workspace):
    return subprocess.Popen([sys.executable, '-c', %r], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
original_stop = relay._stop
def stop(child):
    Path(%r).write_text('started')
    return original_stop(child)
relay._spawn_native = launch
relay._stop = stop
raise SystemExit(relay.main(%r))
""" % (child_code, str(cleanup_started), ["--workspace", str(self.workspace), "--spool", "timeout-cleanup",
                                         "--timeout-seconds", "0.3", "--"] + BASE_ARGS)
        parent = subprocess.Popen([sys.executable, "-E", "-s", "-c", parent_code],
            cwd=str(relay.TRUSTED_SOURCE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while not cleanup_started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(cleanup_started.exists())
            self.assertTrue(child_pid.exists())
            time.sleep(0.05)  # First cancellation arrives inside the SIGTERM grace wait.
            parent.send_signal(signal.SIGINT)
            output, errors = parent.communicate(timeout=3)
            self.assertEqual(parent.returncode, 1, errors)
            self.assertEqual(json.loads(output)["error_code"], "relay_timeout")
            self.assertEqual(json.loads((self.workspace / "timeout-cleanup/result.json").read_bytes())
                             ["error"]["code"], "relay_timeout")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(child_pid.read_text()), 0)
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=3)
            if child_pid.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(child_pid.read_text()), signal.SIGKILL)

    def test_real_runner_synthetic_capture_prepare_import_and_publish(self):
        from test_native_host import NativeHostLifecycleTests
        fixture = NativeHostLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        def host(path, req, metadata):
            answer = response(req)
            answer["result"] = fixture.host.dispatch(req["operation"], req["arguments"])
            atomic_response(path.parent, answer)
        for command in ("capture-head", "prepare", "import-results", "publish"):
            arguments = fixture.arguments(command, **({"results": fixture.root / "results.json"}
                                                      if command == "import-results" else {}))
            output = HostOutput(host)
            code = relay.relay(self.workspace, "real-" + command, arguments, timeout=5, output=output)
            terminal = json.loads((self.workspace / ("real-" + command) / "result.json").read_bytes())
            self.assertEqual(code, 0, terminal)
            if command == "prepare":
                self.assertEqual(terminal["result"]["phase"], "awaiting_judgment")
                self.assertIn("+", terminal["result"]["run_id"])
                # Resolve via the real runtime's verified job, not a guessed path.
                from academic_radar import cloud_runtime
                with contextlib.closing(sqlite3.connect((fixture.state / "papers.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
                    db.row_factory = sqlite3.Row
                    operation = json.loads(db.execute("SELECT value FROM meta WHERE key=?",
                        ("cloud_runtime_operation:" + fixture.operation,)).fetchone()[0])
                    job = dict(db.execute("SELECT * FROM agent_jobs WHERE run_id=?", (operation["run_id"],)).fetchone())
                self.assertEqual(operation["run_id"], terminal["result"]["run_id"])
                self.assertEqual(operation["queue_sha256"], terminal["result"]["queue_sha256"])
                _, queue, _ = cloud_runtime._verified_queue(fixture.state, job, operation["queue_sha256"])
                self.assertEqual(queue["papers"], [])
                results = {"run_id": queue["run_id"], "profile_hash": queue["profile_hash"],
                           "model": "synthetic-test-only", "source_failures": queue["source_failures"], "results": []}
                (fixture.root / "results.json").write_text(json.dumps(results))
            elif command == "publish":
                self.assertTrue(terminal["result"]["acknowledged"])
                self.assertEqual(terminal["result"]["status"], "published")

    def test_cli_pins_child_and_rejects_arbitrary_commands(self):
        with patch.object(relay.subprocess, "Popen") as spawn:
            relay._spawn_native(BASE_ARGS, self.workspace)
            self.assertEqual(spawn.call_args.args[0], [sys.executable, "-E", "-s", "-m", "academic_radar.native_host"] + BASE_ARGS)
            self.assertEqual(spawn.call_args.kwargs["cwd"], str(relay.TRUSTED_SOURCE_ROOT))
            self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        for args in (["/bin/sh", "-c", "echo SECRET_TOKEN"], ["publish", "--url", "https://example.invalid"],
                     ["capture-head", "--hel"], ["capture-head", "--con", "/config"],
                     ["capture-head", "--config", "relative.toml", "--state", "/state", "--role", "staging", "--head-file", "/head"], []):
            with self.subTest(args=args), patch.object(relay, "_spawn_native") as spawn, context_stdout() as output:
                result = relay.main(["--workspace", str(self.workspace), "--spool", "unused", "--"] + args)
                self.assertEqual(result, 1)
                spawn.assert_not_called()
                self.assertNotIn("SECRET_TOKEN", output.getvalue())
                self.assertFalse((self.workspace / "unused").exists())

    def test_timeout_validation_and_initial_private_mode_under_open_umask(self):
        for value in (0, -1, float("inf"), float("nan"), 86401, True):
            with self.subTest(value=value), self.assertRaises(relay.RelayError):
                relay.relay(self.workspace, "unused", BASE_ARGS, timeout=value)
        previous = os.umask(0)
        try:
            with relay.PrivateSpool(self.workspace, "private") as spool:
                self.assertEqual(stat.S_IMODE(spool.path.stat().st_mode), 0o700)
        finally:
            os.umask(previous)


RelayErrorTypes = (relay.RelayError, OSError)


def context_stdout():
    import contextlib
    return contextlib.redirect_stdout(io.StringIO())


if __name__ == "__main__":
    unittest.main()

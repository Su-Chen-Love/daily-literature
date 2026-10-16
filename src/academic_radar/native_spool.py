"""Private, bounded filesystem relay for native_host; never invokes native tools.

The CLI pins the child module and accepts only that module's explicit arguments.
A separate authorized host services immutable request files. Files do not confer
permission or replace the runner's durable publication receipts.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import io
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from .native_host import MAX_MESSAGE_BYTES, MAX_REQUESTS, PROTOCOL, _json, parser as native_parser

# Deliberately fixed here: adding a child capability does not silently authorize
# a new relay operation. These are logical names, never command or URL dispatch.
OPERATIONS = frozenset({
    "site.read_run_status", "site.read_feedback", "site.read_profile_events",
    "site.read_profile", "site.stage_snapshot", "site.acknowledge_snapshot",
    "site.read_checkpoint_head", "site.commit_library_checkpoint",
    "library.create_archive", "library.materialize_archive",
})
SESSION = re.compile(r"[0-9a-f]{32}\Z")
LEAF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
TRUSTED_SOURCE_ROOT = Path(__file__).resolve().parent.parent
HASH = re.compile(r"[0-9a-f]{64}\Z")
ERROR_CODES = frozenset({
    "interrupted", "invalid_arguments", "explicit_managed_configuration_required",
    "recovery_requires_absent_state_and_external_config",
    "publication_artifacts_must_be_outside_state", "control_files_must_be_outside_checkpoint",
    "published_head_requires_recovery", "published_operation_requires_recovery",
    "resume_publication_without_reimport", "unknown_command", "publication_conflict",
    "recovery_required", "native_host_failed", "runtime_busy", "verification_failed", "operation_failed",
})


class RelayError(RuntimeError):
    """Only a fixed error code is exposed; underlying exception text is private."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _check(condition, code="invalid_protocol"):
    if not condition:
        raise RelayError(code)


def _encode(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    _check(len(raw) <= MAX_MESSAGE_BYTES, "invalid_message")
    return raw


def _decode(raw):
    _check(isinstance(raw, bytes) and 0 < len(raw) <= MAX_MESSAGE_BYTES
           and raw.endswith(b"\n") and raw.count(b"\n") == 1, "invalid_message")
    try:
        value = _json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise RelayError("invalid_message") from None
    _check(isinstance(value, dict), "invalid_message")
    return value


def _absolute_directory(path):
    # Walk with directory descriptors, not resolve(): resolving would hide links.
    raw = os.fspath(path)
    _check(isinstance(raw, str) and raw.startswith("/") and "\0" not in raw
           and ".." not in raw.split("/"), "unsafe_workspace")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in Path(raw).parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return Path(os.path.abspath(raw)), fd
    except BaseException:
        os.close(fd)
        raise


class PrivateSpool:
    """New direct child of an explicit workspace; all file access is dirfd-bound."""
    def __init__(self, workspace, name):
        _check(isinstance(name, str) and LEAF.fullmatch(name) is not None
               and name not in {".", ".."}, "unsafe_spool_name")
        self.fd = None
        self.parent_fd = None
        try:
            workspace, self.parent_fd = _absolute_directory(workspace)
            self.path, self.name = workspace / name, name
            self.parent_identity = os.fstat(self.parent_fd)
            old_mask = os.umask(0o077)
            try:
                os.mkdir(name, mode=0o700, dir_fd=self.parent_fd)
            finally:
                os.umask(old_mask)
            self.fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
            self.identity = os.fstat(self.fd)
            _check(stat.S_IMODE(self.identity.st_mode) == 0o700, "unsafe_spool")
            os.fsync(self.parent_fd)
        except BaseException:
            self.close()
            raise

    def close(self):
        for key in ("fd", "parent_fd"):
            fd = getattr(self, key, None)
            if fd is not None:
                os.close(fd)
                setattr(self, key, None)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _intact(self):
        # A live dirfd is not enough: the advertised absolute pathname must still
        # reach that same workspace through non-symlink ancestors.
        try:
            _path, fd = _absolute_directory(self.path.parent)
            try:
                current_parent = os.fstat(fd)
                _check((current_parent.st_dev, current_parent.st_ino) ==
                       (self.parent_identity.st_dev, self.parent_identity.st_ino), "unsafe_workspace")
            finally:
                os.close(fd)
        except OSError:
            raise RelayError("unsafe_workspace") from None
        current = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        _check(stat.S_ISDIR(current.st_mode) and current.st_ino == self.identity.st_ino
               and current.st_dev == self.identity.st_dev and stat.S_IMODE(current.st_mode) == 0o700,
               "unsafe_spool")

    def save(self, name, raw):
        self._intact()
        _check(LEAF.fullmatch(name) is not None, "unsafe_artifact_name")
        temporary = "." + name + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        with os.fdopen(fd, "wb") as handle:
            try:
                # Keep the inode locked until its temporary second link is gone
                # and publication is durable. Readers must never accept nlink=2.
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.write(raw)
                handle.flush()
                os.fchmod(handle.fileno(), 0o400)
                os.fsync(handle.fileno())
                # Atomic, no-clobber publication. No partial request is ever visible.
                os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
                os.unlink(temporary, dir_fd=self.fd)
                os.fsync(self.fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=self.fd)
                raise
        self._intact()
        return self.path / name

    def require_response_absent(self, number):
        self._intact()
        try:
            os.stat("response-%d.json" % number, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise RelayError("preplayed_response")

    def response(self, number):
        self._intact()
        name = "response-%d.json" % number
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise RelayError("unsafe_response") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return None  # Publication is active; the relay deadline still applies.
                raise RelayError("unsafe_response") from None
            before = os.fstat(fd)
            _check(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                   and before.st_uid == os.geteuid() and before.st_mode & 0o077 == 0,
                   "unsafe_response")
            _check(0 < before.st_size <= MAX_MESSAGE_BYTES, "invalid_message")
            raw = bytearray()
            while len(raw) <= MAX_MESSAGE_BYTES:
                chunk = os.read(fd, min(65536, MAX_MESSAGE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            raw = bytes(raw)
            after = os.fstat(fd)
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            _check(fingerprint(before) == fingerprint(after) == fingerprint(current), "response_changed")
            self._intact()
            _decode(raw)  # Never forward an exposed partial, malformed or oversized file.
            return raw
        finally:
            os.close(fd)


class _Pipe:
    def __init__(self, child, deadline):
        self.child, self.deadline = child, deadline
        self.buffer = bytearray()
        self.selector = selectors.DefaultSelector()
        os.set_blocking(child.stdout.fileno(), False)
        self.selector.register(child.stdout, selectors.EVENT_READ)

    def close(self):
        self.selector.close()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        _check(remaining > 0, "relay_timeout")
        return remaining

    def line(self, eof_ok=False):
        while True:
            position = self.buffer.find(b"\n")
            if position >= 0:
                _check(position + 1 <= MAX_MESSAGE_BYTES, "invalid_message")
                raw = bytes(self.buffer[:position + 1])
                del self.buffer[:position + 1]
                return raw
            _check(len(self.buffer) <= MAX_MESSAGE_BYTES, "invalid_message")
            events = self.selector.select(min(self.remaining(), 0.1))
            if not events:
                continue
            raw = os.read(self.child.stdout.fileno(), min(65536, MAX_MESSAGE_BYTES + 1 - len(self.buffer)))
            if not raw:
                _check(eof_ok and not self.buffer, "unexpected_eof")
                return None
            self.buffer.extend(raw)

    def send(self, raw):
        # Even a child which stops consuming stdin cannot defeat the deadline.
        fd = self.child.stdin.fileno()
        os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE)
            offset = 0
            while offset < len(raw):
                if not selector.select(min(self.remaining(), 0.1)):
                    continue
                try:
                    offset += os.write(fd, raw[offset:offset + 65536])
                except BlockingIOError:
                    continue


def _session(value, session, request_id):
    _check(value.get("protocol") == PROTOCOL and isinstance(value.get("session_id"), str)
           and SESSION.fullmatch(value["session_id"]) is not None
           and (session is None or value["session_id"] == session)
           and type(value.get("request_id")) is int and value["request_id"] == request_id)
    return value["session_id"]


def _request(value, session, number):
    _check(set(value) == {"protocol", "type", "session_id", "request_id", "operation", "arguments"}
           and value["type"] == "request" and isinstance(value["operation"], str)
           and value["operation"] in OPERATIONS and isinstance(value["arguments"], dict)
           and number <= MAX_REQUESTS)
    return _session(value, session, number)


def _response(raw, request):
    value = _decode(raw)
    _check(set(value) in ({"protocol", "type", "session_id", "request_id", "operation", "ok", "result"},
                          {"protocol", "type", "session_id", "request_id", "operation", "ok", "error"})
           and value["type"] == "response" and value["operation"] == request["operation"]
           and type(value["ok"]) is bool)
    _session(value, request["session_id"], request["request_id"])
    _check((value["ok"] is True and "result" in value) or
           (value["ok"] is False and isinstance(value.get("error"), dict)))
    if value["ok"] is False:
        # The runner needs only failure, never a remote error body or credential.
        value["error"] = {"code": "host_action_failed"}
        return _encode(value)
    return raw


def _curated_result(value, session, number):
    _check(set(value) in ({"protocol", "type", "session_id", "request_id", "ok", "result"},
                          {"protocol", "type", "session_id", "request_id", "ok", "error"})
           and value["type"] == "result" and type(value["ok"]) is bool)
    _session(value, session, number)
    result = {key: value[key] for key in ("protocol", "type", "session_id", "request_id", "ok")}
    if not value["ok"]:
        error = value.get("error")
        _check(isinstance(error, dict) and error.get("code") in ERROR_CODES)
        result["error"] = {"code": error["code"]}
        return result
    payload = value.get("result")
    _check(isinstance(payload, dict))
    curated = {}
    enumerations = {
        "status": {"captured", "restored", "already_published", "resume_publication", "published", "archived"},
        "phase": {"prepared", "awaiting_judgment", "imported", "committing", "published"},
        "next_action": {"publish_to_confirm_ack", "publish_with_existing_receipt"},
    }
    for key, choices in enumerations.items():
        if key in payload:
            _check(isinstance(payload[key], str) and payload[key] in choices)
            curated[key] = payload[key]
    for key in ("operation_id", "run_id"):
        if key in payload:
            _check(isinstance(payload[key], str) and TOKEN.fullmatch(payload[key]) is not None)
            curated[key] = payload[key]
    for key in ("checkpoint_id", "publication_id", "queue_sha256"):
        if key in payload:
            _check(isinstance(payload[key], str) and HASH.fullmatch(payload[key]) is not None)
            curated[key] = payload[key]
    for key in ("site_generation", "candidates", "imported", "source_failure_count"):
        if key in payload:
            _check(type(payload[key]) is int and 0 <= payload[key] < 2 ** 53)
            curated[key] = payload[key]
    if "collection_exit_code" in payload:
        code = payload["collection_exit_code"]
        _check(code is None or type(code) is int and -255 <= code <= 255)
        curated["collection_exit_code"] = code
    for key in ("acknowledged", "resumed", "collection_performed"):
        if key in payload:
            _check(type(payload[key]) is bool)
            curated[key] = payload[key]
    # No arbitrary paths, diagnostics, profile content or unknown nested fields.
    result["result"] = curated
    return result


def _emit(output, value, deadline=None):
    raw = _encode(value)
    try:
        fd = output.fileno()
    except (AttributeError, OSError, ValueError):
        # In-memory sinks are useful for offline fixtures; the CLI always has a
        # real descriptor and cannot block forever behind an unread stdout pipe.
        output.write(raw.decode("utf-8"))
        output.flush()
        return
    deadline = time.monotonic() + 1 if deadline is None else deadline
    was_blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    try:
        offset = 0
        with selectors.DefaultSelector() as selector:
            registered = False
            while offset < len(raw):
                try:
                    offset += os.write(fd, raw[offset:])
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    _check(remaining > 0, "output_timeout")
                    if not registered:
                        selector.register(fd, selectors.EVENT_WRITE)
                        registered = True
                    selector.select(min(remaining, 0.1))
    finally:
        os.set_blocking(fd, was_blocking)


def _announce(spool, output, value, deadline=None):
    spool._intact()
    _emit(output, value, deadline)
    spool._intact()


def _stop(child):
    # Cleanup is bounded and must finish even if the first cancellation arrives
    # during timeout/failure cleanup rather than before it. Shield installation
    # atomically on the CLI main thread, then restore its prior signal handlers.
    previous_handlers = {}
    signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    if threading.current_thread() is threading.main_thread():
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            for number in signals:
                previous_handlers[number] = signal.signal(number, signal.SIG_IGN)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    try:
        # Each pinned child has a new session; stop descendants as well.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=2)
    finally:
        for handle in (child.stdin, child.stdout):
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


def _spawn_native(arguments, workspace):
    # -E ignores PYTHONPATH/PYTHONHOME and -s disables user-site imports. The
    # fixed reviewed source root, never the request workspace, supplies -m.
    return subprocess.Popen([sys.executable, "-E", "-s", "-m", "academic_radar.native_host"] + list(arguments),
                            cwd=str(TRUSTED_SOURCE_ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)


def relay(workspace, spool_name, child_arguments, *, timeout=1800, output=None):
    """Run one native-host invocation, with no retries and a total bounded timeout.

    Spool construction/argument errors occur before a child is launched. A failed
    run retains private artifacts for inspection; it never consumes them again.
    """
    _check(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 86400,
           "invalid_arguments")
    _check(child_arguments and "--help" not in child_arguments and "-h" not in child_arguments,
           "invalid_arguments")
    # Validate the fixed child's CLI without executing any native workflow.
    try:
        native = native_parser()
        native.allow_abbrev = False
        for action in native._actions:
            if isinstance(action, argparse._SubParsersAction):
                for command in action.choices.values():
                    command.allow_abbrev = False
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            parsed = native.parse_args(child_arguments)
    except (Exception, SystemExit):
        raise RelayError("invalid_arguments") from None
    # The trusted child cwd must never reinterpret a relative caller path.
    for value in vars(parsed).values():
        if isinstance(value, Path):
            _check(value.is_absolute() and ".." not in value.parts, "invalid_arguments")
    output = output if output is not None else sys.stdout
    with PrivateSpool(workspace, spool_name) as spool:
        child, pipe, session, number = None, None, None, 0
        try:
            spool._intact()
            child = _spawn_native(child_arguments, spool.path.parent)
            pipe = _Pipe(child, time.monotonic() + timeout)
            while True:
                value = _decode(pipe.line())
                if value.get("type") == "result":
                    terminal = _curated_result(value, session, number)
                    _check(pipe.line(eof_ok=True) is None, "unexpected_output")
                    returncode = child.wait(timeout=pipe.remaining())
                    _check((terminal["ok"] and returncode == 0) or
                           (not terminal["ok"] and returncode in (1, 130)), "unexpected_exit")
                    break
                session = _request(value, session, number + 1)
                number += 1
                raw = _encode(value)
                spool.require_response_absent(number)
                request_path = spool.save("request-%d.json" % number, raw)
                _announce(spool, output, {"type": "request_ready", "protocol": PROTOCOL, "session_id": session,
                               "request_id": number, "operation": value["operation"],
                               "path": str(request_path), "byte_length": len(raw)}, pipe.deadline)
                while True:
                    pipe.remaining()
                    response = spool.response(number)
                    if response is not None:
                        break
                    _check(child.poll() is None, "unexpected_eof")
                    time.sleep(min(0.05, pipe.remaining()))
                pipe.send(_response(response, value))
        except (Exception, KeyboardInterrupt) as error:
            code = "interrupted" if isinstance(error, KeyboardInterrupt) else (
                error.code if isinstance(error, RelayError) else "relay_failed")
            terminal = {"protocol": PROTOCOL, "type": "relay_error", "session_id": session,
                        "request_id": number, "ok": False, "error": {"code": code}}
        finally:
            if pipe is not None:
                pipe.close()
            if child is not None:
                _stop(child)
        terminal_path = spool.save("result.json", _encode(terminal))
        code = 0 if terminal["ok"] else 130 if terminal["error"]["code"] == "interrupted" else 1
        report_deadline = min(pipe.deadline, time.monotonic() + 0.25) if pipe is not None else time.monotonic() + 0.25
        try:
            _announce(spool, output, {"type": "result_ready", "path": str(terminal_path), "ok": terminal["ok"],
                      "request_id": number, **({"error_code": terminal["error"]["code"]} if not terminal["ok"] else {})},
                      report_deadline)
        except RelayError as error:
            if error.code != "output_timeout":
                raise
            # The durable terminal file survives a stalled reporting channel.
            return code if code else 1
        return code


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise RelayError("invalid_arguments")


def _interrupted(_number, _frame):
    # Repeated cancellation must not interrupt the ensuing bounded child cleanup.
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, signal.SIG_IGN)
    raise KeyboardInterrupt


def main(argv=None):
    parser = _Parser(description="Private filesystem relay for the fixed academic_radar.native_host runner.")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--spool", required=True, help="New private directory name directly below workspace")
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    parser.add_argument("child_arguments", nargs=argparse.REMAINDER, help="-- followed by explicit native_host arguments")
    previous_handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    for number in previous_handlers:
        signal.signal(number, _interrupted)
    try:
        args = parser.parse_args(argv)
        child_arguments = args.child_arguments
        _check(child_arguments and child_arguments[0] == "--", "invalid_arguments")
        return relay(args.workspace, args.spool, child_arguments[1:], timeout=args.timeout_seconds)
    except (Exception, KeyboardInterrupt) as error:
        code = "interrupted" if isinstance(error, KeyboardInterrupt) else (
            error.code if isinstance(error, RelayError) else "relay_failed")
        if code != "output_timeout":
            # A closed/full destination is not a reason to retry reporting forever.
            with contextlib.suppress(Exception):
                _emit(sys.stdout, {"type": "relay_error", "ok": False, "error_code": code})
        return 130 if code == "interrupted" else 1
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())

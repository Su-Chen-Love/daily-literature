"""Bounded, credential-free JSON-lines bridge for an approved native-tool host.

This process is not an MCP client, credential source, scheduler or installation
flow. Its parent host must invoke the actual approved connector and return its
structured result. Only the existing optional research collector can use HTTP.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import secrets
import sys
from pathlib import Path

from . import cloud_runtime as runtime
from .checkpoints import CheckpointError, _absolute, _regular, restore_checkpoint, verify_checkpoint
from .library_checkpoint import (
    LibraryCheckpointAdapter, PublicationConflict, RecoveryRequired,
    _head, _publication, _save_receipt, _sha, _version,
    prepare_publication, read_receipt, verify_publication_binding,
)
from .managed_site import ManagedSiteClient
from .product import load_config, resolve_state

PROTOCOL = "academic-radar-native-host-v1"
HEAD_FORMAT = "academic-radar-native-head-v1"
MAX_MESSAGE_BYTES = 2_000_000
MAX_REQUESTS = 10_000
SITE_TOOLS = frozenset({"read_run_status", "read_feedback", "read_profile_events", "read_profile",
                        "stage_snapshot", "acknowledge_snapshot", "read_checkpoint_head",
                        "commit_library_checkpoint", "read_main_store", "write_main_store",
                        "publish_main_snapshot", "read_site_backup"})
LIBRARY_OPERATIONS = frozenset({"library.create_archive", "library.materialize_archive"})


class HostProtocolError(RuntimeError):
    """The channel is unusable. No raw native payload is included in this error."""


class RunnerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _integer(value):
    if type(value) is not int or not 0 <= value < 2 ** 53:
        raise ValueError("Invalid bounded integer")
    return value


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) <= set(required) | set(optional):
        raise ValueError("Unexpected structured fields")
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _json(raw):
    def reject_constant(_value):
        raise ValueError("Non-finite JSON value")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject_constant)
    def depth(item, level=0):
        if level > 48:
            raise ValueError("JSON nesting limit exceeded")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON value")
        if isinstance(item, dict):
            for child in item.values():
                depth(child, level + 1)
        elif isinstance(item, list):
            for child in item:
                depth(child, level + 1)
    depth(value)
    return value


def _encoded(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Host message exceeds limit")
    return raw


def normalize_library_metadata(value):
    """Accept the actual native version field or the exact normalized schema."""
    if isinstance(value, dict) and set(value) == {"library_file_id", "current_version_number", "file_id", "file_name"}:
        value = {"library_file_id": value["library_file_id"], "current_version": value["current_version_number"],
                 "file_id": value["file_id"], "file_name": value["file_name"]}
    return _version(value)


def _site_arguments(name, args):
    if name not in SITE_TOOLS:
        raise ValueError("Unsupported Site operation")
    if name in {"read_main_store", "write_main_store", "publish_main_snapshot", "read_site_backup"}:
        # Lazy import keeps the legacy runner independent of D1 initialization.
        from .d1_native_host import validate_arguments
        validate_arguments(name, args)
        return
    if name in {"read_run_status", "read_profile", "read_checkpoint_head"}:
        _object(args, ())
    elif name in {"read_feedback", "read_profile_events"}:
        _object(args, {"after"}); _integer(args["after"])
    elif name == "commit_library_checkpoint":
        _object(args, {"data"}); data = _object(args["data"], {"expected_head", "publication"})
        _head(data["expected_head"]); _publication(data["publication"])
    elif name == "acknowledge_snapshot":
        _object(args, {"data"}); data = _object(args["data"], {"generation", "cursor", "request_id", "profile_cursor"})
        _sha(data["generation"])
        for key in ("cursor", "request_id", "profile_cursor"):
            _integer(data[key])
    else:
        _object(args, {"action", "data"}); data = args["data"]
        if args["action"] == "begin":
            _object(data, {"generation", "count"}, {"manifest"})
            if not 1 <= _integer(data["count"]) <= 100_000:
                raise ValueError("Invalid snapshot count")
            if "manifest" in data and (not isinstance(data["manifest"], list) or len(data["manifest"]) != data["count"]):
                raise ValueError("Invalid snapshot manifest")
        elif args["action"] == "chunk":
            _object(data, {"generation", "records"})
            if not isinstance(data["records"], list) or not 1 <= len(data["records"]) <= 40:
                raise ValueError("Invalid snapshot chunk")
        elif args["action"] == "finish":
            _object(data, {"generation", "activate"})
            if data["activate"] is not False:
                raise ValueError("Native snapshot staging cannot activate")
        else:
            raise ValueError("Unknown snapshot action")
        _sha(data["generation"])


def _site_result(name, args, value):
    # Require a decoded structured payload, never human text or MCP envelopes.
    if (not isinstance(value, dict) or any(key in value for key in ("error", "isError", "structuredContent"))
            or ("content" in value and name != "read_profile")):
        raise ValueError("Native host did not return a decoded successful result")
    if name in {"read_main_store", "write_main_store", "publish_main_snapshot", "read_site_backup"}:
        from .d1_native_host import validate_result
        return validate_result(name, args, value)
    if name == "read_checkpoint_head":
        return _head(value)
    if name == "commit_library_checkpoint":
        _object(value, {"ok", "head"}, {"idempotent"})
        if value["ok"] is not True:
            raise ValueError("Site activation was not accepted")
        _head(value["head"])
    elif name == "acknowledge_snapshot":
        _object(value, {"ok", "acknowledged_request_id", "acknowledged_cursor", "acknowledged_profile_cursor"})
        if value["ok"] is not True:
            raise ValueError("Site acknowledgement was not accepted")
        for returned, requested in (("acknowledged_request_id", "request_id"),
                                    ("acknowledged_cursor", "cursor"),
                                    ("acknowledged_profile_cursor", "profile_cursor")):
            if _integer(value[returned]) < args["data"][requested]:
                raise ValueError("Site has not confirmed the requested acknowledgement")
    elif name in {"read_feedback", "read_profile_events"}:
        optional = {"profile_events_version", "profile_cursor", "acknowledged_profile_cursor", "request_id",
                    "acknowledged_request_id", "acknowledged_cursor", "pending", "synced_at", "active_generation"}
        _object(value, {"events", "cursor", "more"}, optional if name == "read_feedback" else ())
        maximum = 500 if name == "read_feedback" else 100
        if (not isinstance(value["events"], list) or len(value["events"]) > maximum
                or not all(isinstance(event, dict) for event in value["events"]) or type(value["more"]) is not bool):
            raise ValueError("Invalid native event page")
        _integer(value["cursor"])
    elif name == "stage_snapshot":
        action = args["action"]
        if action == "begin":
            _object(value, {"generation", "active"}, {"missing"})
            if type(value["active"]) is not bool or ("missing" in value and not isinstance(value["missing"], list)):
                raise ValueError("Invalid native staging response")
        elif action == "chunk":
            _object(value, {"accepted"})
            if _integer(value["accepted"]) != len(args["data"]["records"]):
                raise ValueError("Native staging did not accept the complete chunk")
        else:
            _object(value, {"generation", "count", "staged"})
            _integer(value["count"])
            if value["staged"] is not True:
                raise ValueError("Native staging was not confirmed")
        if "generation" in value and value["generation"] != args["data"]["generation"]:
            raise ValueError("Native staging generation mismatch")
    elif name == "read_profile":
        _object(value, {"profile_hash", "screening_preferences", "version", "generation", "profile_cursor", "pending", "updated_at", "content"})
    elif name == "read_run_status":
        _object(value, {"run", "profile_events_version", "profile_cursor", "acknowledged_profile_cursor", "request_id",
                        "acknowledged_request_id", "cursor", "acknowledged_cursor", "pending", "synced_at"}, {"active_generation"})
    return value


class JsonLineHost:
    """One in-flight request on private binary pipes; replies are session-bound.

    The fixed operation names are logical host capabilities, not arbitrary tool
    lookup, shell commands, URLs, credentials or claims of authorization.
    """
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.session_id = secrets.token_hex(16)
        self.request_id = 0
        self.broken = False
        self.in_flight = False

    def _call(self, operation, arguments):
        if self.broken or self.in_flight or self.request_id >= MAX_REQUESTS:
            raise HostProtocolError("Native host channel unavailable")
        if operation not in LIBRARY_OPERATIONS and operation not in {"site." + name for name in SITE_TOOLS}:
            raise HostProtocolError("Unsupported native host operation")
        self.in_flight = True
        try:
            self.request_id += 1
            request = {"protocol": PROTOCOL, "type": "request", "session_id": self.session_id,
                       "request_id": self.request_id, "operation": operation, "arguments": arguments}
            self.writer.write(_encoded(request)); self.writer.flush()
            raw = self.reader.readline(MAX_MESSAGE_BYTES + 1)
            if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                raise ValueError("Missing or oversized native response")
            response = _object(_json(raw), {"protocol", "type", "session_id", "request_id", "operation", "ok"}, {"result", "error"})
            if (response["protocol"] != PROTOCOL or response["type"] != "response"
                    or response["session_id"] != self.session_id or type(response["request_id"]) is not int
                    or response["request_id"] != self.request_id or response["operation"] != operation
                    or response["ok"] is not True or "error" in response or "result" not in response):
                raise ValueError("Native response rejected")
            return response["result"]
        except Exception:
            self.broken = True
            raise HostProtocolError("Native host response unavailable or rejected; inspect the host action without retrying uncertain writes") from None
        finally:
            self.in_flight = False

    def site_call(self, name, arguments):
        _site_arguments(name, arguments)
        result = self._call("site." + name, arguments)
        try:
            return _site_result(name, arguments, result)
        except Exception:
            self.broken = True
            raise HostProtocolError("Native Site returned an invalid structured result") from None

    def create_archive(self, path, operation_id, archive_sha256):
        path = _absolute(path); _regular(path); _sha(archive_sha256)
        if not runtime.OPERATION.fullmatch(operation_id):
            raise ValueError("Invalid publication operation")
        value = self._call("library.create_archive", {"path": str(path), "file_name": path.name,
            "operation_id": operation_id, "archive_sha256": archive_sha256, "archive_size": path.stat().st_size})
        try:
            return normalize_library_metadata(value)
        except Exception:
            self.broken = True
            raise HostProtocolError("Native Library metadata is invalid") from None

    def materialize_archive(self, metadata, directory):
        metadata = _version(metadata); directory = _absolute(directory)
        if not directory.is_dir() or any(directory.iterdir()):
            raise ValueError("Materialization requires an empty isolated directory")
        value = self._call("library.materialize_archive", {"metadata": metadata, "directory": str(directory)})
        try:
            _object(value, {"path"})
            if not isinstance(value["path"], str) or not Path(value["path"]).is_absolute():
                raise ValueError("Materialization must return an absolute local path")
            path = _absolute(Path(value["path"]))
            if not path.is_relative_to(directory):
                raise ValueError("Materialization escaped its isolated directory")
            _regular(path)
            return path
        except Exception:
            self.broken = True
            raise HostProtocolError("Native Library materialization is invalid") from None

    def finish(self, *, result=None, error=None):
        value = {"protocol": PROTOCOL, "type": "result", "session_id": self.session_id,
                 "request_id": self.request_id, "ok": error is None}
        value.update({"result": result} if error is None else {"error": error})
        self.writer.write(_encoded(value)); self.writer.flush()


def _read_json(path, limit=65536):
    path = _absolute(path); _regular(path)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Local control file exceeds size limit")
    return _json(raw)


def _head_record(head, state, checkpoint=None):
    return {"format": HEAD_FORMAT, "head": _head(head), "state": str(state),
            "checkpoint": str(checkpoint) if checkpoint is not None else None}


def _save_head(path, record):
    path = _absolute(path)
    if path.exists():
        if _read_json(path) != record:
            raise PublicationConflict("Captured head file already belongs to a different restore")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_receipt(path, record, create=True)


def _load_head(path, state, config):
    value = _object(_read_json(path), {"format", "head", "state", "checkpoint"})
    if value["format"] != HEAD_FORMAT or value["state"] != str(state):
        raise ValueError("Captured head is bound to another state directory")
    head = _head(value["head"])
    if head["publication"] is None:
        if value["checkpoint"] is not None:
            raise ValueError("Invalid initial head capture")
    else:
        if not isinstance(value["checkpoint"], str):
            raise ValueError("Published head must come from verified recovery")
        proof = _checkpoint_proof(Path(value["checkpoint"]), head["publication"], expected=None)
        if head["generation"] != proof["expected_head"]["generation"] + 1:
            raise ValueError("Captured publication generation does not match its checkpoint")
        # The pathname alone is not evidence that this is still the recovered
        # state. Keep its prior publication proof intact before preparing a new
        # operation; mutable paper/profile state can legitimately evolve.
        _local_operation(config, head["publication"]["operation_id"], proof)
    return head


def _checkpoint_proof(checkpoint, publication, expected):
    manifest = verify_checkpoint(checkpoint)
    if manifest["checkpoint_id"] != publication["checkpoint_id"]:
        raise ValueError("Checkpoint does not match publication receipt")
    # For recovery, the verified embedded journal supplies the original fence.
    if expected is None:
        import sqlite3
        with contextlib.closing(sqlite3.connect(
                (_absolute(checkpoint) / "payload/papers.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", ("cloud_runtime_operation:" + publication["operation_id"],)).fetchone()
        if row is None:
            raise ValueError("Recovered runtime operation is missing")
        expected = _head(_json(row[0])["expected_head"])
    proof = verify_publication_binding(checkpoint, publication["operation_id"], publication["snapshot_generation"], expected)
    for key in ("cursor", "profile_cursor", "request_id"):
        _integer(proof[key])
    return proof


def _local_operation(config, operation, proof=None):
    _, state, db = runtime._context(config, operation)
    saved = runtime._read(db, operation)
    if not saved:
        raise ValueError("Operation has not been prepared")
    if proof is not None:
        fields = ("operation_id", "run_id", "backend", "expected_head", "snapshot_generation", "queue_sha256", "results_sha256",
                  "cursor", "profile_cursor", "request_id")
        if saved.get("phase") not in {"committing", "published"} or any(saved.get(key) != proof.get(key) for key in fields):
            raise ValueError("Restored runtime journal does not match the publication")
        if runtime._meta(db, "cloud_runtime_export:" + operation) != proof["run_id"]:
            raise ValueError("Runtime operation-to-run binding changed")
        job = runtime._job(db, proof["run_id"])
        if job["status"] != "imported":
            raise ValueError("Publication requires its imported runtime job")
        runtime._verified_queue(state, job, proof["queue_sha256"])
        result_path = _absolute(Path(job["results_path"]))
        if not result_path.is_relative_to(state):
            raise ValueError("Imported results escaped runtime state")
        results = _read_json(result_path, 64 * 1024 * 1024)
        if hashlib.sha256(runtime._canonical(results).encode()).hexdigest() != proof["results_sha256"]:
            raise ValueError("Imported runtime results changed")
    return saved


class NativeHostRunner:
    def __init__(self, host):
        self.site = ManagedSiteClient(host.site_call)
        self.library = LibraryCheckpointAdapter(create_archive=host.create_archive,
            materialize_archive=host.materialize_archive, read_site_head=self.site.read_checkpoint_head,
            activate_site=self.site.activate_library_checkpoint)

    def execute(self, args):
        config, state = _absolute(args.config), _absolute(args.state)
        _regular(config)
        configured = load_config(config)
        if (configured.get("cloud_runtime", {}).get("role") != args.role
                or args.role not in {"primary", "staging"}
                or configured.get("cloud_sync", {}).get("transport") != "managed"
                or resolve_state(config, configured) != state):
            raise RunnerError("explicit_managed_configuration_required")
        if args.command != "recover":
            _regular(state / "papers.sqlite3")
        else:
            if state.exists() or config.is_relative_to(state):
                raise RunnerError("recovery_requires_absent_state_and_external_config")
        for key in ("head_file", "checkpoint", "archive", "receipt", "metadata", "results"):
            if getattr(args, key, None) is not None:
                setattr(args, key, _absolute(getattr(args, key)))
        for key in ("head_file", "checkpoint", "archive", "receipt"):
            path = getattr(args, key, None)
            if path is not None and path.is_relative_to(state):
                raise RunnerError("publication_artifacts_must_be_outside_state")
        checkpoint = getattr(args, "checkpoint", None)
        if checkpoint is not None:
            for key in ("head_file", "archive", "receipt"):
                path = getattr(args, key, None)
                if path is not None and path.is_relative_to(checkpoint):
                    raise RunnerError("control_files_must_be_outside_checkpoint")
        # One native workflow per target workspace, including before restore.
        # Core runtime/receipt locks still enforce their own existing contracts.
        state.parent.mkdir(parents=True, exist_ok=True)
        lock = state.parent / (".native-host-" + hashlib.sha256(str(state).encode()).hexdigest()[:32] + ".lock")
        from .library_checkpoint import _locked_receipt
        with _locked_receipt(lock):
            return self._execute(args, config, state)

    def _execute(self, args, config, state):
        command = args.command
        if command == "capture-head":
            head = _head(self.site.read_checkpoint_head())
            if head["publication"] is not None:
                raise RunnerError("published_head_requires_recovery")
            _save_head(args.head_file, _head_record(head, state))
            return {"status": "captured", "site_generation": 0, "head_file": str(args.head_file)}
        if command == "recover":
            result = self.library.recover_published(args.receipt, args.checkpoint)
            head = result["site_head"]
            _checkpoint_proof(args.checkpoint, head["publication"], result["expected_site_head"])
            _save_head(args.head_file, _head_record(head, state, args.checkpoint))
            restore_checkpoint(args.checkpoint, state)
            if _head(self.site.read_checkpoint_head()) != head:
                raise PublicationConflict("Site changed during restore; keep the original fence")
            return {"status": "restored", "operation_id": result["operation_id"], "checkpoint_id": result["package"]["checkpoint_id"],
                    "site_generation": head["generation"], "state": str(state), "head_file": str(args.head_file),
                    "receipt": str(args.receipt), "acknowledged": False}
        operation = args.operation
        if not runtime.OPERATION.fullmatch(operation):
            raise ValueError("Invalid operation identifier")
        if command == "prepare":
            captured = _load_head(args.head_file, state, config)
            current = _head(self.site.read_checkpoint_head())
            saved = runtime.status(config, operation)
            if current["publication"] and current["publication"]["operation_id"] == operation:
                # A scheduler retry of a restored published job must never call
                # prepare/import again: those APIs may change its journal phase.
                record = _read_json(args.head_file)
                if captured == current and record["checkpoint"]:
                    proof = _checkpoint_proof(Path(record["checkpoint"]), current["publication"], None)
                    _local_operation(config, operation, proof)
                    return {"status": "already_published", "operation_id": operation,
                            "site_generation": current["generation"], "next_action": "publish_to_confirm_ack"}
                if saved.get("phase") in {"committing", "published"}:
                    return {"status": "resume_publication", "operation_id": operation,
                            "next_action": "publish_with_existing_receipt"}
                raise RunnerError("published_operation_requires_recovery")
            if saved.get("phase") in {"committing", "published"}:
                return {"status": "resume_publication", "operation_id": operation,
                        "next_action": "publish_with_existing_receipt"}
            if current != captured:
                raise PublicationConflict("Captured Site head is stale")
            if saved.get("phase") != "not_started" and saved.get("expected_head") != captured:
                raise ValueError("Prepared operation has a different captured head")
            result = runtime.prepare(config, operation, no_collect=not args.collect, limit=args.limit,
                                     expected_head=captured, sync_client=self.site)
            from .cloud_collection import compact_report
            return {key: result[key] for key in ("operation_id", "phase", "run_id", "queue_path", "queue_sha256", "candidates", "resumed")} | {
                "collection_performed": bool(result.get("collection_performed", False)),
                "collection_exit_code": result.get("collection_exit_code"),
                "source_failure_count": len(result.get("source_failures", [])),
                "collection_report": compact_report(result.get("collection_report"))}
        if command == "import-results":
            saved = _local_operation(config, operation)
            head = _head(self.site.read_checkpoint_head())
            if (saved.get("phase") in {"committing", "published"}
                    or head["publication"] and head["publication"]["operation_id"] == operation):
                raise RunnerError("resume_publication_without_reimport")
            _read_json(args.results, 64 * 1024 * 1024)
            result = runtime.import_results(config, operation, args.results)
            return {key: result[key] for key in ("operation_id", "phase", "run_id", "imported", "resumed")}
        if command == "reconcile-upload":
            saved = read_receipt(args.receipt)
            if saved["operation_id"] != operation:
                raise ValueError("Receipt belongs to a different operation")
            proof = _checkpoint_proof(args.checkpoint, {"operation_id": operation,
                "snapshot_generation": saved["snapshot_generation"], "checkpoint_id": saved["package"]["checkpoint_id"]}, saved["expected_site_head"])
            with runtime._lock(state):
                _local_operation(config, operation, proof)
                result = self.library.reconcile_created(args.receipt, normalize_library_metadata(_read_json(args.metadata)))
            return {"status": result["phase"], "operation_id": operation, "next_action": "publish_with_existing_receipt"}
        if command != "publish":
            raise RunnerError("unknown_command")
        resumed = args.receipt.exists()
        if not resumed:
            saved = _local_operation(config, operation)
            if saved.get("phase") == "published":
                raise RunnerError("published_operation_requires_recovery")
            staged = runtime.stage_library_checkpoint(config, operation, args.checkpoint, sync_client=self.site)
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            prepare_publication(args.checkpoint, args.archive, args.receipt, operation_id=operation,
                                snapshot_generation=staged["snapshot_generation"], expected_site_head=staged["expected_site_head"])
        # Never stage again after a receipt exists, even after a lost CAS or ACK
        # response. Its immutable proof and original fence remain authoritative.
        receipt = read_receipt(args.receipt)
        if receipt["operation_id"] != operation:
            raise ValueError("Receipt belongs to a different operation")
        proof = _checkpoint_proof(args.checkpoint, {"operation_id": operation,
            "snapshot_generation": receipt["snapshot_generation"], "checkpoint_id": receipt["package"]["checkpoint_id"]}, receipt["expected_site_head"])
        with runtime._lock(state):
            saved = _local_operation(config, operation, proof)
            result = self.library.publish(args.receipt, args.archive)
            head = _head(self.site.read_checkpoint_head())
            if head != result["site_head"] or head["publication"] != result["publication"]:
                raise PublicationConflict("Authoritative Site head changed before acknowledgement")
            self.site.acknowledge_configured(config, proof["snapshot_generation"], proof["cursor"],
                                             proof["request_id"], proof["profile_cursor"])
            runtime._save(state / "papers.sqlite3", operation, {**saved, "phase": "published", "head": head,
                          "acknowledgement": {"ok": True}, "resumed": resumed})
        return {"status": "published", "operation_id": operation, "checkpoint_id": result["package"]["checkpoint_id"],
                "site_generation": head["generation"], "publication_id": result["publication"]["publication_id"],
                "acknowledged": True, "resumed": resumed}


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise RunnerError("invalid_arguments")


def parser():
    result = _Parser(description="Native host RPC runner. Requires a real approved connector host on stdin/stdout.")
    sub = result.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name in ("capture-head", "recover", "prepare", "import-results", "publish", "reconcile-upload"):
        item = sub.add_parser(name)
        for field in ("config", "state"):
            item.add_argument("--" + field, type=Path, required=True)
        item.add_argument("--role", choices=("staging", "primary"), required=True)
        if name not in {"capture-head", "recover"}:
            item.add_argument("--operation", required=True)
        if name in {"capture-head", "recover", "prepare"}:
            item.add_argument("--head-file", type=Path, required=True)
        if name in {"recover", "publish", "reconcile-upload"}:
            item.add_argument("--checkpoint", type=Path, required=True)
            item.add_argument("--receipt", type=Path, required=True)
        if name == "prepare":
            item.add_argument("--collect", action="store_true", help="Use the existing configured public-source collector; otherwise freeze existing candidates only")
            item.add_argument("--limit", type=int)
        if name == "publish":
            item.add_argument("--archive", type=Path, required=True)
        if name == "import-results":
            item.add_argument("--results", type=Path, required=True)
        if name == "reconcile-upload":
            item.add_argument("--metadata", type=Path, required=True)
    return result


class _Discard:
    def write(self, text):
        return len(text)
    def flush(self):
        pass


def main(argv=None):
    host = JsonLineHost(sys.stdin.buffer, sys.stdout.buffer)
    try:
        args = parser().parse_args(argv)
        if getattr(args, "limit", None) is not None and args.limit < 0:
            raise RunnerError("invalid_arguments")
        # The protocol pipe is captured above. Collector and library diagnostics
        # cannot leak native error bodies, private profile content or URLs into it.
        with contextlib.redirect_stdout(_Discard()), contextlib.redirect_stderr(_Discard()):
            result = NativeHostRunner(host).execute(args)
        host.finish(result=result)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        code = ("interrupted" if isinstance(error, KeyboardInterrupt) else error.code if isinstance(error, RunnerError) else
                "publication_conflict" if isinstance(error, PublicationConflict) else
                "recovery_required" if isinstance(error, RecoveryRequired) else
                "native_host_failed" if isinstance(error, HostProtocolError) else
                "runtime_busy" if isinstance(error, runtime.Busy) else
                "verification_failed" if isinstance(error, CheckpointError) else "operation_failed")
        # Never stringify an underlying exception or native tool result.
        try:
            host.finish(error={"code": code, "message": "Operation stopped safely. Check the exact host action and local receipt before retrying."})
        except (OSError, ValueError):
            pass  # The host pipe may have closed; do not print a traceback.
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())

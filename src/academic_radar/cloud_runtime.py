"""Resumable host-agent pipeline. Scratch space is never a durability promise.

The scheduler invokes prepare, has its host model judge the frozen queue, then
invokes import_results. Publishing is a separate, fenced operation after durable
checkpoint storage. No model API, scheduler, credential or service is created.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

from .product import load_config, resolve_state
from .storage import connect, utc_now, upgrade_database

OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class Busy(RuntimeError):
    """Another runner owns this restored workspace."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        h = hashlib.sha256()
        for part in iter(lambda: source.read(1024 * 1024), b""):
            h.update(part)
        return h.hexdigest()


def _queue_digest(queue: dict) -> str:
    # profile_path is deliberately relocated by portable checkpoint restore.
    return hashlib.sha256(_canonical({k: v for k, v in queue.items() if k != "profile_path"}).encode()).hexdigest()


def _context(config_path: Path, operation_id: str) -> tuple[dict, Path, Path]:
    if not OPERATION.fullmatch(operation_id):
        raise ValueError("operation_id must be a stable, path-safe scheduler key")
    config = load_config(config_path)
    role = config.get("cloud_runtime", {}).get("role")
    if role not in {"staging", "primary"}:
        raise ValueError("cloud_runtime.role must explicitly be staging or primary; backups cannot run analysis")
    state = resolve_state(config_path, config)
    db = state / "papers.sqlite3"
    if not db.is_file():
        raise FileNotFoundError("Restore and verify a full checkpoint before running")
    return config, state, db


@contextlib.contextmanager
def _lock(state: Path) -> Iterator[None]:
    import fcntl
    path = state / ".cloud-runtime.lock"
    if path.is_symlink():
        raise ValueError("Runtime lock cannot be a symlink")
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("A cloud operation is already running in this workspace") from exc
        yield


def _meta(db_path: Path, key: str) -> str | None:
    with contextlib.closing(connect(db_path)) as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def _save(db_path: Path, operation_id: str, values: dict) -> dict:
    result = {**values, "operation_id": operation_id, "updated_at": utc_now()}
    with contextlib.closing(connect(db_path)) as db, db:
        db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   ("cloud_runtime_operation:" + operation_id, _canonical(result)))
    return result


def _read(db_path: Path, operation_id: str) -> dict | None:
    raw = _meta(db_path, "cloud_runtime_operation:" + operation_id)
    return json.loads(raw) if raw else None


def _monitor():
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "paper_monitor.py",
        Path(sys.prefix) / "share" / "personal-academic-radar" / "paper_monitor.py",
    ]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise FileNotFoundError("Installed paper_monitor.py is unavailable")
    name = "_academic_radar_cloud_monitor"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the installed monitor")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _invoke(function, *args, **kwargs) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = function(*args, **kwargs)
    return code, json.loads(output.getvalue())


def _job(db_path: Path, run_id: str) -> dict:
    with contextlib.closing(connect(db_path)) as db:
        row = db.execute("SELECT * FROM agent_jobs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Cloud operation references a missing agent job")
        return dict(row)


def _verified_queue(state: Path, job: dict, expected_hash: str | None = None) -> tuple[Path, dict, str]:
    path = Path(job["queue_path"]).resolve()
    if not path.is_relative_to(state.resolve()) or not path.is_file():
        raise ValueError("Frozen queue must exist within the restored state directory")
    queue = json.loads(path.read_text(encoding="utf-8"))
    if queue.get("run_id") != job["run_id"] or queue.get("profile_hash") != job["profile_hash"]:
        raise ValueError("Frozen queue does not match its agent job")
    digest = _queue_digest(queue)
    if expected_hash is not None and digest != expected_hash:
        raise ValueError("Frozen queue changed after preparation")
    return path, queue, digest


def prepare(config_path: Path, operation_id: str, *, no_collect: bool = False, limit: int | None = None,
            expected_head: dict | None = None, sync_client=None) -> dict:
    """Pull edits, collect once and return one immutable queue on every retry."""
    config, state, db_path = _context(config_path, operation_id)
    with _lock(state):
        upgrade_database(db_path)
        saved = _read(db_path, operation_id)
        run_id = _meta(db_path, "cloud_runtime_export:" + operation_id)
        if run_id:
            job = _job(db_path, run_id)
            queue_path, queue, digest = _verified_queue(state, job, (saved or {}).get("queue_sha256"))
            if job["status"] not in {"exported", "imported"}:
                raise ValueError("Cloud operation job was abandoned or rejected; investigate before starting another")
            phase = "awaiting_judgment" if job["status"] == "exported" else "imported"
            result = _save(db_path, operation_id, {**(saved or {}), "phase": phase, "run_id": run_id,
                           "collection_report": queue.get("collection_report", (saved or {}).get("collection_report")),
                           "queue_sha256": digest, "candidates": len(queue["papers"])})
            return {**result, "queue_path": str(queue_path), "resumed": True, "collection_performed": False}
        if saved and saved.get("expected_head") != expected_head:
            raise ValueError("Cannot substitute a different head during preparation")
        mode = "offline" if no_collect else "daily"
        if saved and saved.get("collection_mode", mode) != mode:
            raise ValueError("Cannot change collection mode during a resumable operation")
        if config.get("cloud_runtime", {}).get("role") == "primary" and expected_head is None:
            raise ValueError("Primary preparation requires the head captured before checkpoint restore")
        with contextlib.closing(connect(db_path)) as db:
            active = db.execute("SELECT run_id FROM agent_jobs WHERE status='exported'").fetchall()
            if active:
                raise Busy("An exported queue is still awaiting results; resume its operation before starting another")
            pending = db.execute("SELECT key,value FROM meta WHERE key LIKE 'cloud_runtime_operation:%'").fetchall()
            if any(row["key"] != "cloud_runtime_operation:" + operation_id and json.loads(row["value"]).get("phase") == "preparing" for row in pending):
                raise Busy("Another collection is incomplete; resume its original operation")
        if config.get("cloud_sync", {}).get("transport") == "managed" and sync_client is None:
            raise ValueError("Managed Site transport requires the approved host tool bridge")
        if sync_client is not None or config.get("cloud_sync", {}).get("enabled"):
            from .cloud_sync import pull_configured
            pull = (sync_client.pull_configured(config_path) if sync_client is not None
                    else pull_configured(config_path))
            if pull.get("status") not in {"pulled", "succeeded", "unchanged", "disabled"}:
                raise Busy("Feedback/profile pull did not finish; refusing to freeze a stale queue")
        else:
            pull = {"status": "disabled"}
        saved = _save(db_path, operation_id, {**(saved or {}), "phase": "preparing", "started_at": (saved or {}).get("started_at", utc_now()),
                                     "pull_status": pull.get("status"), "expected_head": expected_head, "collection_mode": mode})
        from .cloud_collection import offline_report, run as collect_daily
        monitor = _monitor()
        if no_collect:
            saved = _save(db_path, operation_id, {**saved, "collection_report": offline_report(), "collection_performed": False})
            batch_run = None
        else:
            saved = collect_daily(config_path, operation_id, saved, monitor)
            batch_run = saved["collection_progress"]["api"]["run_id"]
            if saved["collection_progress"]["phase"] != "completed":
                raise ValueError("Daily collection stages are incomplete; queue cannot be frozen")
        code, summary = _invoke(monitor.agent_export, config_path, no_collect=True,
                                batch_run=batch_run, limit=limit, operation_id=operation_id,
                                collection_report=saved["collection_report"])
        job = _job(db_path, summary["run_id"])
        queue_path, queue, digest = _verified_queue(state, job)
        result = _save(db_path, operation_id, {**saved, "phase": "awaiting_judgment", "run_id": summary["run_id"],
                       "queue_sha256": digest, "candidates": len(queue["papers"]),
                       "expected_head": expected_head,
                       "collection_exit_code": saved.get("collection_exit_code", code), "source_failures": summary.get("source_failures", [])})
        return {**result, "queue_path": str(queue_path), "profile_path": queue.get("profile_path"),
                "deferred_candidates": queue.get("deferred_candidates", 0), "resumed": False}


def import_results(config_path: Path, operation_id: str, results_path: Path) -> dict:
    """Import completely or not at all; retries cannot duplicate judgments."""
    _, state, db_path = _context(config_path, operation_id)
    with _lock(state):
        saved = _read(db_path, operation_id)
        run_id = _meta(db_path, "cloud_runtime_export:" + operation_id)
        if not saved or not run_id:
            raise ValueError("Prepare this operation before importing")
        job = _job(db_path, run_id)
        _verified_queue(state, job, saved.get("queue_sha256"))
        incoming = json.loads(results_path.read_text(encoding="utf-8"))
        if incoming.get("run_id") != run_id:
            raise ValueError("Results belong to a different operation")
        content = _canonical(incoming).encode()
        digest = hashlib.sha256(content).hexdigest()
        if job["status"] == "imported":
            original = json.loads(Path(job["results_path"]).read_text(encoding="utf-8"))
            if hashlib.sha256(_canonical(original).encode()).hexdigest() != digest:
                raise ValueError("This operation already imported different results")
        # Preserve a canonical, state-relative artifact before committing import.
        directory = state / "agent_results"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / (operation_id + "-" + digest + ".json")
        if target.is_symlink():
            raise ValueError("Results artifact cannot be a symlink")
        if target.exists() and target.read_bytes() != content:
            raise ValueError("Refusing to replace different saved results")
        if not target.exists():
            with target.open("xb") as handle:
                handle.write(content)
                handle.flush()
                import os
                os.fsync(handle.fileno())
            target.chmod(0o600)
        if job["status"] == "imported":
            return _save(db_path, operation_id, {**saved, "phase": "imported", "results_sha256": digest, "imported": job["imported_count"],
                                                "resumed": True})
        if job["status"] != "exported":
            raise ValueError("Operation is no longer importable")
        _, summary = _invoke(_monitor().agent_import, config_path, target, sync_after=False)
        return _save(db_path, operation_id, {**saved, "phase": "imported", "results_sha256": digest, "imported": summary["imported"],
                                            "relevant": summary["relevant"], "resumed": False})


def status(config_path: Path, operation_id: str) -> dict:
    _, _, db_path = _context(config_path, operation_id)
    return _read(db_path, operation_id) or {"operation_id": operation_id, "phase": "not_started"}



def commit(config_path: Path, operation_id: str, checkpoint_path: Path, *, client=None, sync_client=None) -> dict:
    """Stage views, upload verified state, then atomically CAS head + Site views.

    The expected head is captured BEFORE restore/prepare and travels inside the
    operation journal. Never substitute a newer head to force a stale run through.
    A losing run must restore the winner and begin fresh; its immutable chunks
    are harmless and may be reclaimed only by a separately reviewed GC policy.
    """
    config, state, db_path = _context(config_path, operation_id)
    if config.get("cloud_sync", {}).get("transport") == "managed" and sync_client is None:
        raise ValueError("Managed Site transport requires the approved host tool bridge")
    from .checkpoint_transport import CheckpointConflict, from_config
    from .checkpoints import create_checkpoint, verify_checkpoint
    from .cloud_sync import stage_configured, acknowledge_configured
    if sync_client is not None:
        acknowledge_configured = sync_client.acknowledge_configured
    client = client or from_config(config_path)
    with _lock(state):
        saved = _read(db_path, operation_id)
        if not saved or saved.get("phase") not in {"imported", "committing", "published"}:
            raise ValueError("Complete the judgment import before committing")
        expected = saved.get("expected_head")
        if not isinstance(expected, dict):
            raise ValueError("Operation lacks the head captured before restore; refusing unfenced publication")
        current = client.get_head()
        already = (current.get("operation_id") == operation_id and saved.get("snapshot_generation")
                   and current.get("snapshot_generation") == saved["snapshot_generation"])
        if already:
            published = current
        else:
            if current.get("generation") != expected.get("generation") or current.get("manifest_sha256") != expected.get("manifest_sha256"):
                raise CheckpointConflict("Another run committed first; restore its head instead of overwriting it")
            if saved.get("phase") == "published":
                raise CheckpointConflict("Published operation no longer matches the cloud head")
            if saved.get("phase") != "committing":
                staged = (sync_client.stage_configured(config_path) if sync_client is not None
                          else stage_configured(config_path))
                if staged.get("status") != "staged":
                    raise RuntimeError("Site snapshot was not staged; refusing checkpoint activation")
                saved = _save(db_path, operation_id, {**saved, "phase": "committing",
                              "snapshot_generation": staged["generation"],
                              "cursor": staged.get("cursor", 0), "profile_cursor": staged.get("profile_cursor", 0),
                              "request_id": staged.get("request_id")})
            if checkpoint_path.exists():
                manifest = verify_checkpoint(checkpoint_path)
                candidate = saved.get("checkpoint_id")
                if candidate is not None and manifest.get("checkpoint_id") != candidate:
                    raise ValueError("Existing checkpoint belongs to another commit attempt")
                # Checkpoint publication can finish just before the journal
                # records its ID. Recover that window from the verified copy's
                # embedded operation, never from a filename or caller assertion.
                uri = (checkpoint_path / "payload" / "papers.sqlite3").resolve().as_uri() + "?mode=ro"
                with contextlib.closing(sqlite3.connect(uri, uri=True)) as checkpoint_db:
                    row = checkpoint_db.execute("SELECT value FROM meta WHERE key=?",
                                                ("cloud_runtime_operation:" + operation_id,)).fetchone()
                embedded = json.loads(row[0]) if row else {}
                fields = ("operation_id", "run_id", "snapshot_generation", "expected_head",
                          "queue_sha256", "results_sha256")
                if embedded.get("phase") != "committing" or any(embedded.get(key) != saved.get(key) for key in fields):
                    raise ValueError("Existing checkpoint does not match this commit operation")
            else:
                create_checkpoint(state, checkpoint_path, config_path=config_path)
                manifest = verify_checkpoint(checkpoint_path)
            saved = _save(db_path, operation_id, {**saved, "checkpoint_id": manifest["checkpoint_id"]})
            uploaded = client.upload_checkpoint(checkpoint_path)
            published = client.compare_and_swap_head(expected, uploaded["manifest_sha256"],
                                                      saved["snapshot_generation"], operation_id=operation_id)
        # This ACK does not determine durability. If interrupted or rejected,
        # the committed head is recognized on retry and only ACK is repeated.
        ack = acknowledge_configured(config_path, saved["snapshot_generation"],
                                     saved.get("cursor", 0), saved.get("request_id"), saved.get("profile_cursor", 0))
        return _save(db_path, operation_id, {**saved, "phase": "published", "head": published,
                                            "acknowledgement": ack, "resumed": bool(already)})


def stage_library_checkpoint(config_path: Path, operation_id: str, checkpoint_path: Path, *, sync_client) -> dict:
    """Bind a frozen imported operation, staged view and immutable checkpoint.

    This does not upload to Library, activate a Site view or ACK any event. Use
    LibraryCheckpointAdapter after this function returns; it validates the
    embedded journal before preparing the publication archive.
    """
    _, state, db_path = _context(config_path, operation_id)
    from .checkpoints import create_checkpoint, verify_checkpoint
    from .library_checkpoint import PublicationConflict, _head, verify_publication_binding
    if sync_client is None:
        raise ValueError("Library publication requires the approved managed Site bridge")
    with _lock(state):
        saved = _read(db_path, operation_id)
        if not saved or saved.get("phase") not in {"imported", "committing"}:
            raise ValueError("Complete the judgment import before staging a Library checkpoint")
        if saved.get("phase") == "committing" and saved.get("backend") != "library":
            raise ValueError("Operation is committing through a different checkpoint backend")
        expected = _head(saved.get("expected_head"))
        current = _head(sync_client.read_checkpoint_head())
        if current != expected:
            raise PublicationConflict("Site head changed; recover the published checkpoint instead of restaging")
        run_id = _meta(db_path, "cloud_runtime_export:" + operation_id)
        if not run_id or run_id != saved.get("run_id"):
            raise ValueError("Operation no longer matches its frozen run")
        job = _job(db_path, run_id)
        if job["status"] != "imported":
            raise ValueError("Library publication requires a completely imported job")
        _verified_queue(state, job, saved.get("queue_sha256"))
        results_path = Path(job["results_path"]).resolve()
        if not results_path.is_relative_to(state.resolve()) or not results_path.is_file():
            raise ValueError("Imported results must exist within the restored state")
        results = json.loads(results_path.read_text(encoding="utf-8"))
        if hashlib.sha256(_canonical(results).encode()).hexdigest() != saved.get("results_sha256"):
            raise ValueError("Imported results changed before checkpoint staging")
        if saved["phase"] != "committing":
            staged = sync_client.stage_configured(config_path)
            if staged.get("status") != "staged":
                raise RuntimeError("Managed Site snapshot was not staged")
            saved = _save(db_path, operation_id, {**saved, "phase": "committing", "backend": "library",
                "snapshot_generation": staged["generation"], "cursor": staged.get("cursor", 0),
                "profile_cursor": staged.get("profile_cursor", 0), "request_id": staged.get("request_id", 0)})
        checkpoint_path = Path(checkpoint_path)
        if checkpoint_path.exists():
            manifest = verify_checkpoint(checkpoint_path)
            with contextlib.closing(sqlite3.connect(
                    (checkpoint_path.resolve() / "payload/papers.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                row = db.execute("SELECT value FROM meta WHERE key=?",
                                 ("cloud_runtime_operation:" + operation_id,)).fetchone()
            embedded = json.loads(row[0]) if row else {}
            fields = ("operation_id", "phase", "backend", "run_id", "snapshot_generation", "expected_head",
                      "queue_sha256", "results_sha256", "cursor", "profile_cursor", "request_id")
            if any(embedded.get(key) != saved.get(key) for key in fields):
                raise ValueError("Existing checkpoint belongs to a different staged operation")
        else:
            create_checkpoint(state, checkpoint_path, config_path=config_path)
            manifest = verify_checkpoint(checkpoint_path)
        verify_publication_binding(checkpoint_path, operation_id=operation_id,
                                   snapshot_generation=saved["snapshot_generation"], expected_site_head=expected)
        _save(db_path, operation_id, {**saved, "checkpoint_id": manifest["checkpoint_id"]})
        return {"status": "staged", "operation_id": operation_id, "checkpoint": str(checkpoint_path),
                "checkpoint_id": manifest["checkpoint_id"], "snapshot_generation": saved["snapshot_generation"],
                "expected_site_head": expected, "cursor": saved["cursor"], "profile_cursor": saved["profile_cursor"],
                "request_id": saved["request_id"]}

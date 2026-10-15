"""Portable private state checkpoints. No network, credentials, or archive extraction.

The application writer must be paused for a whole-state checkpoint. SQLite's
online backup and an IMMEDIATE lock protect committed database state; file
fingerprints detect ordinary concurrent artifact writes. See checkpoint-format.md.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10
    import tomli as tomllib

FORMAT = "academic-radar-state"
VERSION = 1
TOKEN = "state://"
ARCHIVE_TOKEN = "archive://"
PROVENANCE_FILE = "reference-provenance.json"
DB_NAME = "papers.sqlite3"
DATA_DIRS = frozenset({"fulltexts", "agent_queue", "agent_results", "results", "digests", "official", "reports"})
PATH_FIELDS = frozenset({"stored_path", "queue_path", "results_path", "profile_path", "digest_path", "backup_path", "report_path", "config_path"})
SECRET_FILES = frozenset({".ssh", ".aws", ".git", "__pycache__", "cloud-sync.json", ".env", ".netrc", ".npmrc", "credentials.json", "id_rsa", "id_ed25519"})
SECRET_KEY = re.compile(r"(?:^|_)(?:password|passwd|secret|token|api_key|private_key|access_key|authorization|cookie)(?:$|_)", re.I)
HEX = re.compile(r"[0-9a-f]{64}\Z")


class CheckpointError(ValueError):
    """A checkpoint is unsafe, incomplete, corrupt, or unsupported."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json(raw: bytes | str) -> Any:
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise CheckpointError("Duplicate JSON key")
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(CheckpointError("Non-finite JSON value")))
    except (ValueError, UnicodeError) as error:
        raise CheckpointError("Invalid checkpoint JSON") from error


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value or ":" in value:
        raise CheckpointError("Unsafe relative path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(value).is_absolute():
        raise CheckpointError("Unsafe relative path")
    return value


def _absolute(path: Path) -> Path:
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise CheckpointError("Parent traversal is not supported in filesystem paths")
    path = Path(os.path.abspath(path))
    for ancestor in reversed((path, *path.parents)):
        if ancestor.is_symlink():
            raise CheckpointError("Symlink paths are not supported")
    return path


def _regular(path: Path) -> None:
    _absolute(path)
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise CheckpointError("Missing required checkpoint file: " + path.name) from error
    if not stat.S_ISREG(mode):
        raise CheckpointError("Checkpoint members must be regular files")


def _read(path: Path) -> bytes:
    _regular(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        result = handle.read()
        after = os.fstat(handle.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise CheckpointError("A source file changed during checkpoint creation; pause writers and retry")
    return result


def _walk(root: Path) -> dict[str, Path]:
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in names + filenames:
            item = Path(directory) / name
            if item.is_symlink():
                raise CheckpointError("Symlinks are not allowed in checkpoint trees")
        for name in filenames:
            item = Path(directory) / name
            _regular(item)
            files[_relative(item.relative_to(root).as_posix())] = item
    return files


def _secret_path(relative: str) -> bool:
    return any(part in SECRET_FILES or part.startswith(".env.")
               or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".kdbx"))
               or re.search(r"(?:^|[._-])(?:credentials?|secrets?|passwords?|tokens?|private[_-]?key|api[_-]?key)(?:$|[._-])", part, re.I)
               or SECRET_KEY.search(part.rsplit(".", 1)[0].replace("-", "_"))
               for part in relative.split("/"))


def _check_secrets(value: Any, location: str = "structured data") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            # Variable *names* are portable, credentials themselves are not.
            if SECRET_KEY.search(key) and not key.endswith("_env") and item not in (None, "", False):
                raise CheckpointError("Credential-like field is not allowed in " + location)
            _check_secrets(item, location)
    elif isinstance(value, list):
        for item in value:
            _check_secrets(item, location)


def _toml(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return str(value).lower()
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(_toml(key) + " = " + _toml(item) for key, item in value.items()) + " }"
    raise CheckpointError("Unsupported TOML value")


def _config(raw: bytes, profile: str | None = None) -> tuple[bytes, dict]:
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise CheckpointError("Invalid checkpoint configuration") from error
    _check_secrets(config, "configuration")
    config["state_dir"] = "."
    if profile is not None:
        config["profile_file"] = profile
    _relative(config.get("profile_file", "research-profile.md"))
    for section in ("cloud_sync", "delivery"):
        if section in config:
            if not isinstance(config[section], dict):
                raise CheckpointError("Invalid configuration section")
            config[section]["enabled"] = False
            config[section].pop("credentials_file", None)
    def reject_config_paths(value):
        if isinstance(value, dict):
            for item in value.values():
                reject_config_paths(item)
        elif isinstance(value, list):
            for item in value:
                reject_config_paths(item)
        elif isinstance(value, str) and (value.startswith((TOKEN, "/", "~/")) or re.match(r"^[A-Za-z]:[\\/]", value)):
            raise CheckpointError("Unsupported absolute path in configuration")
    reject_config_paths(config)
    return ("\n".join(_toml(key) + " = " + _toml(value) for key, value in config.items()) + "\n").encode(), config


def _transform_json(value: Any, path_map: Callable, key: str = "", *, context: dict | None = None, pointer: str = "") -> Any:
    context = context or {}
    if isinstance(value, dict):
        _check_secrets(value)
        return {name: _transform_json(item, path_map, name, context=context,
                pointer=pointer + "/" + name.replace("~", "~0").replace("/", "~1")) for name, item in value.items()}
    if isinstance(value, list):
        return [_transform_json(item, path_map, key, context=context, pointer=pointer + "/" + str(index)) for index, item in enumerate(value)]
    if isinstance(value, str) and value and (key in PATH_FIELDS or key.endswith("_path") or value.startswith((TOKEN, ARCHIVE_TOKEN, "/", "~/")) or re.match(r"^[A-Za-z]:[\\/]", value)):
        return path_map(value, {**context, "field": key, "pointer": pointer})
    return value


def _reference_scope(context: dict) -> str:
    if context.get("field") in {"stored_path", "profile_path", "config_path", "database"}:
        return "operational"
    if context.get("historical") is True:
        return "historical"
    if ((context.get("table") == "cleanup_audits" and context.get("status") == "preview"
         and context.get("column") in {"backup_path", "report_path"})
            or context.get("archive_scope") == "preview_cleanup"):
        return "preview_cleanup"
    return "operational"


def _historical(context: dict) -> bool:
    return _reference_scope(context) == "historical"


def _combined_scope(owners: list[str]) -> str:
    if not owners or "operational" in owners:
        return "operational"
    return "preview_cleanup" if "preview_cleanup" in owners else "historical"


def _provenance(entries: Any) -> list[dict]:
    if not isinstance(entries, list):
        raise CheckpointError("Invalid reference provenance")
    result = []
    seen = set()
    allowed = {"source", "disposition", "target", "reason", "archive_label", "sha256", "size", "id", "operation_disabled", "revalidation_required"}
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise CheckpointError("Invalid reference policy entry")
        entry = dict(raw)
        source = entry.get("source")
        if not isinstance(source, str) or not source or "\0" in source or "\\" in source or source.startswith((TOKEN, ARCHIVE_TOKEN)):
            raise CheckpointError("Reference policy requires an original filesystem path")
        if ".." in PurePosixPath(source).parts or (not source.startswith(("/", "~/")) and _relative(source) != source):
            raise CheckpointError("Unsafe reference policy source")
        if _secret_path(source):
            raise CheckpointError("Credential-like source cannot enter reference provenance")
        disposition = entry.get("disposition")
        if disposition == "relocate":
            target = _relative(entry.get("target"))
            if target == PROVENANCE_FILE or _secret_path(target):
                raise CheckpointError("Unsafe relocation target")
        elif disposition in {"archived_local", "unavailable", "not_migrated"}:
            if "target" in entry or not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
                raise CheckpointError("Archived references require an explicit reason and no target")
            if disposition == "archived_local" and (not isinstance(entry.get("archive_label"), str) or not entry["archive_label"].strip()):
                raise CheckpointError("archived_local requires an archive label")
        else:
            raise CheckpointError("Unsupported reference disposition")
        if "operation_disabled" in entry or "revalidation_required" in entry:
            if disposition != "archived_local" or entry.get("operation_disabled") is not True or entry.get("revalidation_required") is not True:
                raise CheckpointError("Preview archives require both explicit disable and revalidation flags")
        if disposition == "not_migrated" and not {"sha256", "size"}.issubset(entry):
            raise CheckpointError("PDF omission requires an explicit SHA256 and size")
        for key in ("reason", "archive_label"):
            if key in entry and (not isinstance(entry[key], str) or len(entry[key]) > 1000):
                raise CheckpointError("Invalid reference provenance description")
        if "sha256" in entry and (not isinstance(entry["sha256"], str) or not HEX.fullmatch(entry["sha256"])):
            raise CheckpointError("Invalid reference SHA256")
        if "size" in entry and (type(entry["size"]) is not int or entry["size"] < 0):
            raise CheckpointError("Invalid reference size")
        supplied = entry.pop("id", None)
        identity = hashlib.sha256(_canonical(entry)).hexdigest()
        if supplied is not None and supplied != identity:
            raise CheckpointError("Reference provenance checksum mismatch")
        if identity in seen:
            raise CheckpointError("Duplicate reference provenance")
        seen.add(identity)
        result.append({**entry, "id": identity})
    return sorted(result, key=lambda item: item["id"])


def _load_provenance(state: Path) -> tuple[list[dict], list[dict]]:
    path = state / PROVENANCE_FILE
    if not path.exists() and not path.is_symlink():
        return [], []
    payload = _json(_read(path))
    if not isinstance(payload, dict) or set(payload) not in ({"format", "version", "entries", "checksum"}, {"format", "version", "entries", "omitted_fulltexts", "checksum"}) or payload["format"] != "academic-radar-reference-provenance" or type(payload["version"]) is not int or payload["version"] != 1:
        raise CheckpointError("Invalid persisted reference provenance")
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    if hashlib.sha256(_canonical(unsigned)).hexdigest() != payload["checksum"]:
        raise CheckpointError("Persisted reference provenance checksum mismatch")
    entries = _provenance(payload["entries"])
    omitted = _omitted_fulltexts(payload.get("omitted_fulltexts", []), {entry["id"]: entry for entry in entries})
    return entries, omitted


def _write_provenance(state: Path, entries: list[dict], omitted: list[dict]) -> None:
    payload = {"format": "academic-radar-reference-provenance", "version": 1, "entries": entries, "omitted_fulltexts": omitted}
    payload["checksum"] = hashlib.sha256(_canonical(payload)).hexdigest()
    _write(state / PROVENANCE_FILE, _canonical(payload) + b"\n")


def _archive(value: str, context: dict, provenance: dict[str, dict]) -> dict:
    identity = value[len(ARCHIVE_TOKEN):]
    if not HEX.fullmatch(identity) or identity not in provenance or provenance[identity]["disposition"] not in {"archived_local", "unavailable", "not_migrated"}:
        raise CheckpointError("Unknown or invalid archived reference")
    entry = provenance[identity]
    if entry["disposition"] == "not_migrated":
        raise CheckpointError("Omitted PDFs belong only in provenance, never active registry markers")
    scope = _reference_scope(context)
    if not (scope == "historical" or (scope == "preview_cleanup" and entry["disposition"] == "archived_local"
            and entry.get("operation_disabled") is True and entry.get("revalidation_required") is True)):
        raise CheckpointError("Operational references cannot be archived or unavailable; preview cleanup requires explicit disable/revalidation")
    return entry


def _omitted_fulltexts(value: Any, provenance: dict[str, dict]) -> list[dict]:
    if not isinstance(value, list):
        raise CheckpointError("Invalid omitted fulltext audit")
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"policy_id", "row"}:
            raise CheckpointError("Invalid omitted fulltext audit entry")
        entry = provenance.get(item["policy_id"])
        row = item["row"]
        if not entry or entry["disposition"] != "not_migrated" or not isinstance(row, dict) or not {"id", "identity", "stored_path", "sha256", "size_bytes"}.issubset(row):
            raise CheckpointError("Omitted fulltext lacks explicit inventory provenance")
        if row["stored_path"] != entry["source"] or row["sha256"] != entry["sha256"] or row["size_bytes"] != entry["size"]:
            raise CheckpointError("Omitted fulltext audit disagrees with inventory")
        _check_secrets(row, "omitted fulltext audit")
        identity = hashlib.sha256(_canonical(item)).hexdigest()
        if identity in seen:
            raise CheckpointError("Duplicate omitted fulltext audit")
        seen.add(identity)
    return value


def _omit_fulltexts(path: Path, rules: dict[str, dict]) -> list[dict]:
    omissions = {source: entry for source, entry in rules.items() if entry["disposition"] == "not_migrated"}
    if not omissions:
        return []
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    audits = []
    matched = set()
    try:
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise CheckpointError("SQLite triggers are unsupported before fulltext omission")
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if any(str(foreign_key[2]).lower() == "fulltext_files" for foreign_key in db.execute("PRAGMA foreign_key_list(" + _quote(table) + ")")):
                raise CheckpointError("Fulltext omission would affect a dependent table")
        with db:
            for raw in db.execute("SELECT * FROM fulltext_files").fetchall():
                row = dict(raw)
                entry = omissions.get(row["stored_path"])
                if entry is None:
                    continue
                if row["sha256"] != entry["sha256"] or row["size_bytes"] != entry["size"]:
                    raise CheckpointError("Explicit PDF omission disagrees with SQLite checksum or size")
                audits.append({"policy_id": entry["id"], "row": row})
                db.execute("DELETE FROM fulltext_files WHERE id=?", (row["id"],))
                matched.add(entry["source"])
            if matched != set(omissions):
                raise CheckpointError("PDF omission inventory does not match an active fulltext row")
    except sqlite3.Error as error:
        raise CheckpointError("Cannot safely omit a referenced fulltext row") from error
    finally:
        db.close()
    return audits


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _database(path: Path, path_map: Callable, *, change: bool, immutable: bool = True) -> dict[str, int]:
    # immutable mode avoids creating sidecars while verifying an untrusted tree.
    db = sqlite3.connect(path if change else path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=not change)
    db.row_factory = sqlite3.Row
    try:
        if not change and not immutable:
            db.execute("BEGIN")
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise CheckpointError("SQLite triggers are unsupported in portable checkpoints")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CheckpointError("SQLite integrity check failed")
        tables = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        names = {row[0] for row in tables}
        jobs = {row["run_id"]: row["status"] for row in db.execute("SELECT run_id,status FROM agent_jobs")} if "agent_jobs" in names else {}
        operational_runs = {row[0] for row in db.execute("SELECT value FROM meta WHERE key LIKE 'cloud_runtime_export:%'")} if "meta" in names else set()
        counts = {}
        for table, sql in tables:
            if sql and "CREATE VIRTUAL TABLE" in sql.upper():
                raise CheckpointError("Virtual SQLite tables are unsupported")
            info = list(db.execute("PRAGMA table_info(" + _quote(table) + ")"))
            columns = [row[1] for row in info]
            counts[table] = db.execute("SELECT COUNT(*) FROM " + _quote(table)).fetchone()[0]
            for column in columns:
                if SECRET_KEY.search(column) and not column.endswith("_env"):
                    if db.execute("SELECT 1 FROM " + _quote(table) + " WHERE " + _quote(column) + " IS NOT NULL AND " + _quote(column) + " != '' LIMIT 1").fetchone():
                        raise CheckpointError("Credential-like SQLite column is unsupported")
            if table == "meta" and {"key", "value"}.issubset(columns):
                for key, value in db.execute('SELECT key,value FROM meta'):
                    if SECRET_KEY.search(key) and not key.endswith("_env") and value:
                        raise CheckpointError("Credential-like SQLite metadata is unsupported")
            targets = [name for name in columns if name in PATH_FIELDS or name.endswith("_path") or name.endswith("_json")]
            if not targets:
                continue
            primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            hidden = next((key for key in ("_rowid_", "rowid", "oid") if key not in columns), None)
            if hidden is None:
                raise CheckpointError("Unsupported row identity in SQLite table")
            try:
                # Ordinary SQLite primary keys may contain duplicate NULLs.
                # Prefer the actual rowid; only WITHOUT ROWID tables need PKs.
                rows = db.execute("SELECT " + hidden + " AS __checkpoint_identity__,* FROM " + _quote(table)).fetchall()
                selectors = [hidden]
                uses_rowid = True
            except sqlite3.OperationalError:
                if not primary:
                    raise CheckpointError("Unsupported row identity in SQLite table")
                rows = db.execute("SELECT * FROM " + _quote(table)).fetchall()
                selectors = primary
                uses_rowid = False
            for raw_row in rows:
                row = dict(raw_row)
                run_id = row.get("run_id")
                historical = False
                if table == "agent_jobs":
                    historical = row.get("status") in {"imported", "abandoned"} and run_id not in operational_runs
                elif table == "pipeline_runs":
                    historical = (row.get("status") in {"succeeded", "partial", "abandoned"}
                                  and jobs.get(run_id) in {None, "imported", "abandoned"} and run_id not in operational_runs)
                elif table == "cleanup_audits":
                    historical = row.get("status") in {"applied", "restored"}
                elif table == "notifications":
                    historical = bool(row.get("sent_at"))
                identity = {hidden: raw_row[0]} if uses_rowid else {key: row[key] for key in selectors}
                for column in targets:
                    old = row[column]
                    if not isinstance(old, str) or not old:
                        continue
                    context = {"table": table, "column": column, "row": identity, "status": row.get("status"), "historical": historical, "field": column}
                    if table == "fulltext_files":
                        context.update(sha256=row.get("sha256"), size=row.get("size_bytes"))
                    if column.endswith("_json"):
                        parsed = _json(old)
                        transformed = _transform_json(parsed, path_map, context=context)
                        new = _canonical(transformed).decode() if transformed != parsed else old
                    else:
                        new = path_map(old, context)
                    if change and old != new:
                        where = " AND ".join(_quote(key) + " IS ?" for key in selectors)
                        db.execute("UPDATE " + _quote(table) + " SET " + _quote(column) + "=? WHERE " + where, [new, *identity.values()])
        if change:
            db.commit()
            db.execute("PRAGMA journal_mode=DELETE")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise CheckpointError("SQLite foreign-key check failed")
        return counts
    except sqlite3.Error as error:
        raise CheckpointError("Unsupported or corrupt SQLite checkpoint") from error
    finally:
        db.close()


def _publish(stage: Path, destination: Path) -> None:
    """Atomic directory publication that cannot overwrite even a raced empty dir."""
    for directory, _, files in os.walk(stage, topdown=False):
        for name in files:
            with (Path(directory) / name).open("rb") as handle:
                os.fsync(handle.fileno())
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, os.fsencode(stage), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        result = libc.renamex_np(os.fsencode(stage), os.fsencode(destination), 4)  # RENAME_EXCL
    else:
        raise CheckpointError("Atomic no-overwrite publication requires Linux or macOS")
    if result:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError("Checkpoint destination already exists")
        raise OSError(code, os.strerror(code))
    fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _destination(path: Path) -> Path:
    path = _absolute(path)
    if path.exists():
        raise FileExistsError("Destination must be absent; existing state is never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    _absolute(path.parent)
    return path


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _result(path: Path, manifest: dict) -> dict:
    return {"checkpoint": str(path), "checkpoint_id": manifest["checkpoint_id"], "files": len(manifest["files"]), "table_counts": manifest["table_counts"], "excluded": manifest["excluded"]}


def create_checkpoint(state_dir: Path, output: Path, *, config_path: Path | None = None, reference_policy: dict | None = None) -> dict:
    """Create a complete portable checkpoint from a paused local installation.

    Operational files must be regular files beneath state_dir; explicit policies
    can relocate legacy paths, archive scoped history, or audit-only omit PDFs.
    The selected config may reside outside state; its included profile may not.
    """
    state = _absolute(state_dir)
    if not state.is_dir():
        raise CheckpointError("State directory is missing")
    output = _absolute(output)
    if output.is_relative_to(state):
        raise CheckpointError("Checkpoint output must be outside the source state")
    output = _destination(output)
    config_source = _absolute(config_path or state / "config.toml")
    raw_config = _read(config_source)
    try:
        original = tomllib.loads(raw_config.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise CheckpointError("Invalid source configuration") from error
    configured_state = Path(str(original.get("state_dir", "."))).expanduser()
    if not configured_state.is_absolute():
        configured_state = config_source.parent / configured_state
    if _absolute(configured_state) != state:
        raise CheckpointError("Configuration does not refer to the selected state directory")
    selected: dict[str, Path] = {"config.toml": config_source}
    excluded: list[dict] = []
    inherited, omitted_fulltexts = _load_provenance(state)
    if reference_policy is None:
        explicit = []
    else:
        if not isinstance(reference_policy, dict) or set(reference_policy) != {"version", "entries"} or type(reference_policy["version"]) is not int or reference_policy["version"] != 1:
            raise CheckpointError("Invalid explicit reference policy")
        explicit = _provenance(reference_policy["entries"])
        if len({entry["source"] for entry in explicit}) != len(explicit):
            raise CheckpointError("Each explicit reference policy source must be unique")
    rules = {entry["source"]: entry for entry in explicit}
    provenance = {entry["id"]: entry for entry in [*inherited, *explicit]}
    file_owners: dict[str, list[str]] = {}
    archived_sources = set()
    for entry in explicit:
        if entry["disposition"] in {"archived_local", "unavailable", "not_migrated"}:
            candidate = Path(entry["source"]).expanduser()
            if not candidate.is_absolute():
                candidate = state / candidate
            if candidate.is_relative_to(state):
                archived_sources.add(candidate.relative_to(state).as_posix())

    def portable(value: str, context: dict | None = None) -> str:
        context = context or {}
        old_archive = None
        if value.startswith(ARCHIVE_TOKEN):
            old_archive = _archive(value, context, provenance)
            rule = rules.get(old_archive["source"])
            if rule is None or rule["disposition"] != "relocate":
                return value
        else:
            rule = rules.get(value)
        if rule is not None:
            if rule["disposition"] in {"archived_local", "unavailable", "not_migrated"}:
                if Path(rule["source"]).name in {DB_NAME, PROVENANCE_FILE, "config.toml"}:
                    raise CheckpointError("Operational state files cannot be archived")
                marker = ARCHIVE_TOKEN + rule["id"]
                _archive(marker, context, provenance)
                return marker
            relative = rule["target"]
            path = _absolute(state / relative)
            _regular(path)
            raw = _read(path)
            if ("size" in rule and len(raw) != rule["size"]) or ("sha256" in rule and hashlib.sha256(raw).hexdigest() != rule["sha256"]):
                raise CheckpointError("Relocated file disagrees with explicit inventory checksum")
            if relative == DB_NAME:
                return TOKEN + relative
            selected[relative] = path
            file_owners.setdefault(relative, []).append(_reference_scope(context))
            return TOKEN + relative
        if value.startswith(TOKEN):
            raise CheckpointError("Source state contains unresolved portable paths")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = state / _relative(value)
        path = _absolute(path)
        if path == config_source:
            return TOKEN + "config.toml"
        try:
            relative = _relative(path.relative_to(state).as_posix())
        except ValueError as error:
            raise CheckpointError("Referenced files outside state are unsupported; relocate them before checkpointing") from error
        if relative == DB_NAME:
            return TOKEN + relative
        if _secret_path(relative) or relative.endswith(("-wal", "-shm", ".log", ".lock")):
            raise CheckpointError("Referenced file is excluded from checkpoints")
        _regular(path)
        selected[relative] = path
        file_owners.setdefault(relative, []).append(_reference_scope(context))
        return TOKEN + relative

    profile = portable(str(original.get("profile_file", "research-profile.md")), {"field": "profile_path"})[len(TOKEN):]
    normalized_config, _ = _config(raw_config, profile)
    source_db = state / DB_NAME
    _regular(source_db)
    stage = Path(tempfile.mkdtemp(prefix=".checkpoint-", dir=output.parent))
    payload = stage / "payload"
    payload.mkdir(mode=0o700)
    guard = None
    fingerprints = {}
    try:
        guard = sqlite3.connect(source_db, timeout=10)
        guard.execute("BEGIN IMMEDIATE")
        reader = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)
        writer = sqlite3.connect(payload / DB_NAME)
        try:
            reader.backup(writer)
            writer.execute("PRAGMA journal_mode=DELETE")
        finally:
            writer.close()
            reader.close()
        os.chmod(payload / DB_NAME, 0o600)
        omitted_fulltexts.extend(_omit_fulltexts(payload / DB_NAME, rules))
        counts = _database(payload / DB_NAME, portable, change=True)
        # Drop freed pages so old path strings/deleted content do not leak in
        # SQLite's freelist after normalization.
        compact = sqlite3.connect(payload / DB_NAME)
        try:
            compact.execute("VACUUM")
        finally:
            compact.close()
        inventories = {}
        for item in sorted(state.iterdir()):
            name = item.name
            if name in {DB_NAME, PROVENANCE_FILE} or name in {DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal"}:
                continue
            if name in DATA_DIRS:
                if item.is_symlink() or not item.is_dir():
                    raise CheckpointError("State artifact directories must be real directories")
                members = _walk(item)
                inventories[name] = set(members)
                for relative, source in members.items():
                    relative = name + "/" + relative
                    if relative in archived_sources:
                        excluded.append({"path": relative, "reason": "explicit historical reference policy"})
                    elif _secret_path(relative):
                        excluded.append({"path": relative, "reason": "credential-like filename"})
                    else:
                        selected[relative] = source
            elif name not in selected and item != config_source:
                excluded.append({"path": name, "reason": "not a supported application artifact"})
        copied = set()
        while set(selected) - copied:
            for relative in sorted(set(selected) - copied):
                source = selected[relative]
                _relative(relative)
                if _secret_path(relative):
                    raise CheckpointError("Credential-like artifact is not allowed")
                before = source.stat()
                raw = normalized_config if relative == "config.toml" else _read(source)
                if relative.endswith(".json"):
                    owners = file_owners.get(relative, [])
                    raw = _canonical(_transform_json(_json(raw), portable, context={"artifact": relative, "historical": _combined_scope(owners) == "historical", "archive_scope": _combined_scope(owners)})) + b"\n"
                _write(payload / relative, raw)
                fingerprints[source] = (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                copied.add(relative)
        for source, fingerprint in fingerprints.items():
            current = source.stat()
            if fingerprint != (current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                raise CheckpointError("Source files changed; pause writers and retry")
        for name in DATA_DIRS:
            directory = state / name
            current = set(_walk(directory)) if directory.exists() else set()
            if current != inventories.get(name, set()):
                raise CheckpointError("Artifact inventory changed; pause writers and retry")
        entries = []
        for relative, path in sorted(_walk(payload).items()):
            raw = _read(path)
            entries.append({"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        manifest = {"format": FORMAT, "version": VERSION, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "database": DB_NAME,
                    "files": entries, "table_counts": counts, "excluded": sorted(excluded, key=lambda item: item["path"]),
                    "policy": {"credentials": "excluded", "external_references": "rejected", "delivery_and_cloud_sync": "disabled_on_restore"}}
        if omitted_fulltexts:
            manifest["omitted_fulltexts"] = _omitted_fulltexts(omitted_fulltexts, provenance)
            manifest["policy"]["fulltexts"] = "explicit_not_migrated_audit_only"
        if provenance:
            manifest["reference_provenance"] = sorted(provenance.values(), key=lambda item: item["id"])
            manifest["policy"]["external_references"] = "explicit_inventory_only"
        manifest["checkpoint_id"] = hashlib.sha256(_canonical(manifest)).hexdigest()
        _write(stage / "manifest.json", _canonical(manifest) + b"\n")
        verify_checkpoint(stage)
        _publish(stage, output)
        return _result(output, manifest)
    finally:
        if guard is not None:
            guard.rollback()
            guard.close()
        if stage.exists():
            shutil.rmtree(stage)


def _reference_contexts(payload: Path, files: dict, provenance: dict[str, dict]) -> tuple[dict, dict]:
    owners: dict[str, list[str]] = {}
    edges: dict[str, set[str]] = {}

    def included(value: str) -> str:
        if not value.startswith(TOKEN):
            raise CheckpointError("Unmapped path in checkpoint")
        relative = _relative(value[len(TOKEN):])
        if relative not in files:
            raise CheckpointError("Missing referenced checkpoint artifact")
        return relative

    def database_reference(value: str, context: dict) -> str:
        if value.startswith(ARCHIVE_TOKEN):
            _archive(value, context, provenance)
        else:
            relative = included(value)
            owners.setdefault(relative, []).append(_reference_scope(context))
        return value

    counts = _database(payload / DB_NAME, database_reference, change=False)
    json_files = {}
    for relative in files:
        if relative.endswith(".json"):
            parsed = _json(_read(payload / relative))
            json_files[relative] = parsed
            def collect(value: str, context: dict) -> str:
                if value.startswith(ARCHIVE_TOKEN):
                    # Validate the identity now. Historical eligibility follows
                    # after all active-to-artifact reference edges are known.
                    _archive(value, {**context, "historical": True}, provenance)
                else:
                    edges.setdefault(relative, set()).add(included(value))
                return value
            _transform_json(parsed, collect)
    scopes = {relative: _combined_scope(owners.get(relative, [])) for relative in files}
    changed = True
    while changed:
        changed = False
        for source, targets in edges.items():
            for target in targets:
                combined = _combined_scope([scopes[target], scopes[source]])
                if combined != scopes[target]:
                    scopes[target] = combined
                    changed = True
    for relative, parsed in json_files.items():
        def validate(value: str, context: dict) -> str:
            if value.startswith(ARCHIVE_TOKEN):
                _archive(value, context, provenance)
            else:
                included(value)
            return value
        _transform_json(parsed, validate, context={"artifact": relative, "historical": scopes[relative] == "historical", "archive_scope": scopes[relative]})
    return counts, scopes


def inventory_references(state_dir: Path) -> dict:
    """Read-only DB reference inventory with row identity and archive eligibility.

    No referenced external file is opened. This does not grant exceptions and
    does not rewrite, delete, or make a historical restore operation executable.
    """
    state = _absolute(state_dir)
    path = state / DB_NAME
    _regular(path)
    entries = []
    def collect(value: str, context: dict) -> str:
        entries.append({"source": value, "archive_eligible": _historical(context), "reference_scope": _reference_scope(context), "pdf_omission_eligible": context.get("table") == "fulltext_files" and context.get("column") == "stored_path", **context})
        return value
    counts = _database(path, collect, change=False, immutable=False)
    return {"version": 1, "references": entries, "reference_count": len(entries), "unique_sources": len({entry["source"] for entry in entries}), "table_counts": counts}


def verify_checkpoint(checkpoint: Path) -> dict:
    """Verify exact file set, all checksums, SQLite, and every portable reference."""
    checkpoint = _absolute(checkpoint)
    if not checkpoint.is_dir():
        raise CheckpointError("Checkpoint directory is missing")
    tree = _walk(checkpoint)
    manifest_raw = _read(checkpoint / "manifest.json")
    if len(manifest_raw) > 16 * 1024 * 1024:
        raise CheckpointError("Checkpoint manifest is too large")
    manifest = _json(manifest_raw)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or type(manifest.get("version")) is not int or manifest.get("version") != VERSION or manifest.get("database") != DB_NAME:
        raise CheckpointError("Unsupported checkpoint format")
    identity = manifest.get("checkpoint_id")
    unsigned = {key: value for key, value in manifest.items() if key != "checkpoint_id"}
    if not isinstance(identity, str) or not HEX.fullmatch(identity) or hashlib.sha256(_canonical(unsigned)).hexdigest() != identity:
        raise CheckpointError("Checkpoint manifest checksum mismatch")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise CheckpointError("Invalid file manifest")
    files = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise CheckpointError("Invalid file manifest entry")
        relative = _relative(entry["path"])
        if relative in files or _secret_path(relative) or type(entry["size"]) is not int or entry["size"] < 0 or not isinstance(entry["sha256"], str) or not HEX.fullmatch(entry["sha256"]):
            raise CheckpointError("Invalid or duplicate file manifest entry")
        files[relative] = entry
    if set(tree) != {"manifest.json", *("payload/" + name for name in files)} or not {DB_NAME, "config.toml"}.issubset(files):
        raise CheckpointError("Missing or unexpected checkpoint file")
    for relative, entry in files.items():
        raw = _read(checkpoint / "payload" / relative)
        if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise CheckpointError("Checkpoint file checksum mismatch: " + relative)

    provenance = {entry["id"]: entry for entry in _provenance(manifest.get("reference_provenance", []))}
    _omitted_fulltexts(manifest.get("omitted_fulltexts", []), provenance)
    if PROVENANCE_FILE in files:
        raise CheckpointError("Reference provenance is reserved for validated restore metadata")
    def portable(value: str, context: dict | None = None) -> str:
        if not value.startswith(TOKEN):
            raise CheckpointError("Unmapped operational path in checkpoint")
        relative = _relative(value[len(TOKEN):])
        if relative not in files:
            raise CheckpointError("Missing referenced checkpoint artifact")
        return value

    payload = checkpoint / "payload"
    counts, _ = _reference_contexts(payload, files, provenance)
    if counts != manifest.get("table_counts"):
        raise CheckpointError("Checkpoint table counts mismatch")
    if "fulltext_files" in counts:
        pdf_db = sqlite3.connect((payload / DB_NAME).as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            for stored, digest, size in pdf_db.execute("SELECT stored_path,sha256,size_bytes FROM fulltext_files"):
                if stored.startswith(ARCHIVE_TOKEN):
                    _archive(stored, {"table": "fulltext_files", "column": "stored_path", "field": "stored_path", "sha256": digest, "size": size}, provenance)
                    continue
                relative = portable(stored)[len(TOKEN):]
                if files[relative]["sha256"] != digest or files[relative]["size"] != size:
                    raise CheckpointError("Referenced PDF checksum or size disagrees with SQLite")
        finally:
            pdf_db.close()
    raw = _read(payload / "config.toml")
    _, config = _config(raw)
    stored_config = tomllib.loads(raw.decode("utf-8"))
    if stored_config != config:
        raise CheckpointError("Checkpoint config must use portable paths and disabled delivery/sync")
    portable(TOKEN + config.get("profile_file", "research-profile.md"))
    return manifest


def restore_checkpoint(checkpoint: Path, destination: Path) -> dict:
    """Verify first, restore into staging, then atomically publish to an absent path."""
    checkpoint = _absolute(checkpoint)
    destination = _absolute(destination)
    if destination.is_relative_to(checkpoint):
        raise CheckpointError("Restore destination must be outside the immutable checkpoint")
    destination = _destination(destination)
    manifest = verify_checkpoint(checkpoint)
    stage = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    try:
        for entry in manifest["files"]:
            relative = entry["path"]
            raw = _read(checkpoint / "payload" / relative)
            # Verify the copied bytes again; never trust a mutable input tree.
            if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise CheckpointError("Checkpoint changed during restore")
            _write(stage / relative, raw)
        paths = {entry["path"] for entry in manifest["files"]}
        provenance = {entry["id"]: entry for entry in _provenance(manifest.get("reference_provenance", []))}
        _, historical_files = _reference_contexts(stage, {entry["path"]: entry for entry in manifest["files"]}, provenance)

        def restored(value: str, context: dict | None = None) -> str:
            context = context or {}
            if value.startswith(ARCHIVE_TOKEN):
                _archive(value, context, provenance)
                return value
            if not value.startswith(TOKEN):
                raise CheckpointError("Unmapped restore path")
            relative = _relative(value[len(TOKEN):])
            if relative not in paths:
                raise CheckpointError("Missing restore artifact")
            return str(destination / relative)

        counts = _database(stage / DB_NAME, restored, change=True)
        for entry in manifest["files"]:
            if entry["path"].endswith(".json"):
                path = stage / entry["path"]
                raw = _canonical(_transform_json(_json(_read(path)), restored, context={"artifact": entry["path"], "historical": historical_files[entry["path"]] == "historical", "archive_scope": historical_files[entry["path"]]})) + b"\n"
                path.write_bytes(raw)
        if provenance:
            _write_provenance(stage, sorted(provenance.values(), key=lambda item: item["id"]), manifest.get("omitted_fulltexts", []))
        _publish(stage, destination)
        return {"destination": str(destination), "checkpoint_id": manifest["checkpoint_id"], "files": len(paths), "table_counts": counts, "integrity": "ok"}
    finally:
        if stage.exists():
            shutil.rmtree(stage)

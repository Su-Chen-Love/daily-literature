"""Offline, lossless normalized packages for the private D1 main store.

This module performs no network operations. It never executes SQL from a package.
A restore requires an independently trusted schema database and an absent output
folder; complete verification precedes atomic, no-overwrite publication.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pickle
import re
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterator

from . import checkpoints
from . import sqlite_delta as delta

FORMAT = "academic-radar-d1-main-store"
VERSION = 1
CHUNK_BYTES = 128 * 1024
DEFAULT_PAGE_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 128 * 1024
MAX_FILES = 100_000
MAX_OBJECTS = 500_000
MAX_TOTAL_BYTES = 32 * 1024**3
EXTENDED_HASH_ALGORITHM = "sha256-pickle4-columns-pk-or-rowid-ordered-rows-v1"


class PackageError(ValueError):
    """Unsafe, unsupported, incomplete, corrupt, or mismatched package."""


canonical = delta._canonical

def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _error(message):
    raise PackageError(message)


def _check_object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _error("Unexpected or missing package fields")


def _hash(value):
    if type(value) is not str or not delta.HEX.fullmatch(value):
        _error("Invalid SHA-256")
    return value


def _integer(value, maximum=MAX_TOTAL_BYTES):
    if type(value) is not int or not 0 <= value <= maximum:
        _error("Invalid nonnegative integer or resource limit exceeded")
    return value


def _path(value):
    try:
        return delta._path(value)
    except delta.DeltaError as error:
        raise PackageError(str(error)) from error


def _relative(value):
    if type(value) is not str or len(value) > 1000 or re.search(r"[\x00-\x1f\x7f]", value):
        _error("Unsafe relative path")
    try:
        return checkpoints._relative(value)
    except checkpoints.CheckpointError as error:
        raise PackageError(str(error)) from error


def _new_directory(value):
    path = _path(value)
    if os.path.lexists(path) or not path.parent.is_dir():
        _error("Destination must be absent in an existing directory")
    return path


def _stamp(path):
    try:
        return delta._stamp(_path(path))
    except (OSError, delta.DeltaError) as error:
        raise PackageError("Missing or nonregular package/source file") from error


def _read(path, limit):
    before = _stamp(path)
    if before[2] > limit:
        _error("Package member exceeds byte limit")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2]:
            _error("File changed before opening")
        raw = handle.read(limit + 1)
    if len(raw) > limit or _stamp(path) != before:
        _error("File changed while reading or exceeds byte limit")
    return raw


def _json(raw):
    try:
        value = delta._bounded_json(raw)
    except delta.DeltaError as error:
        raise PackageError(str(error)) from error
    if canonical(value) + b"\n" != raw:
        _error("Package JSON must be canonical and newline terminated")
    return value


def _write(path, raw):
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(raw)


def _schema(db):
    """Same fingerprint as sqlite_delta, with explicit rowid-key fallback."""
    objects = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name,tbl_name").fetchall()
    if any(row[0] not in ("table", "index") for row in objects):
        _error("Triggers and views are unsupported")
    tables, strategies = [], {}
    for kind, name, _, sql in objects:
        if kind != "table":
            continue
        if name.startswith("sqlite_") and name != "sqlite_sequence":
            _error("Unsupported internal SQLite table")
        if not sql or not re.match(r"\s*CREATE\s+TABLE\b", sql, re.I):
            _error("Virtual or nonstandard table declarations are unsupported")
        columns = db.execute("PRAGMA table_xinfo(" + delta._quote(name) + ")").fetchall()
        if not columns or len(columns) > delta.MAX_COLUMNS or any(row[6] != 0 for row in columns):
            _error("Hidden/generated columns or oversized schema are unsupported")
        names = [row[1] for row in columns]
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if name == "sqlite_sequence":
            if names != ["name", "seq"]:
                _error("Unexpected sqlite_sequence schema")
            primary = ["name"]
        candidates = [value for value in ("rowid", "_rowid_", "oid") if value not in {col.lower() for col in names}]
        if not candidates:
            _error("All hidden rowid names are shadowed")
        rowid = candidates[0]
        try:
            db.execute("SELECT " + rowid + " FROM " + delta._quote(name) + " LIMIT 0")
        except sqlite3.OperationalError as error:
            if "no such column" not in str(error):
                raise
            rowid = None
        for index in db.execute("PRAGMA index_list(" + delta._quote(name) + ")"):
            for part in db.execute("PRAGMA index_xinfo(" + delta._quote(index[1]) + ")"):
                if part[4] not in ("BINARY", "NOCASE", "RTRIM") or (part[5] and part[1] < 0):
                    _error("Custom collations and expression indexes are unsupported")
        # A nullable PK may identify multiple rows. Use hidden rowid for the
        # entire table, even when this snapshot happens to contain no NULLs.
        rowid_alias = len(primary) == 1 and any(row[1] == primary[0] and row[2].upper() == "INTEGER" for row in columns)
        nullable = rowid is not None and any(row[5] and not row[3] for row in columns) and not rowid_alias
        strategy = "rowid" if not primary or nullable else "primary_key"
        # Valid existing sqlite_delta inventories retain their original key
        # ordering; nullable declarations alone are fine if actual keys are safe.
        if primary:
            missing = db.execute("SELECT 1 FROM " + delta._quote(name) + " WHERE " + " OR ".join(delta._quote(col) + " IS NULL" for col in primary) + " LIMIT 1").fetchone()
            duplicate = db.execute("SELECT 1 FROM " + delta._quote(name) + " GROUP BY " + ",".join(map(delta._quote, primary)) + " HAVING count(*)>1 LIMIT 1").fetchone()
            strategy = "rowid" if missing or duplicate else "primary_key"
        if strategy == "rowid" and rowid is None:
            _error("Table lacks a usable primary key and hidden rowid")
        tables.append({"name": name, "columns": names, "primary_key": primary, "rowid": rowid})
        strategies[name] = strategy
    if not tables or len(tables) > delta.MAX_TABLES:
        _error("Unsupported table count")
    metadata = {"objects": [list(row) for row in objects],
                "user_version": db.execute("PRAGMA user_version").fetchone()[0],
                "application_id": db.execute("PRAGMA application_id").fetchone()[0],
                "encoding": db.execute("PRAGMA encoding").fetchone()[0]}
    return metadata, sorted(tables, key=lambda item: item["name"]), strategies


def _rows(db, table, strategy):
    columns = ",".join(map(delta._quote, table["columns"]))
    if table["rowid"] is not None:
        columns += "," + table["rowid"]
    order = table["rowid"] if strategy == "rowid" else ",".join(map(delta._quote, table["primary_key"]))
    cursor = db.execute("SELECT " + columns + " FROM " + delta._quote(table["name"]) + " ORDER BY " + order)
    for row in cursor:
        yield (row[:-1], row[-1]) if table["rowid"] is not None else (row, None)


def _key(values, rowid, table, strategy):
    raw = [rowid] if strategy == "rowid" else [values[table["columns"].index(name)] for name in table["primary_key"]]
    return [delta.encode_scalar(value) for value in raw]


def _inventory(db, tables, strategies, schema_sha256):
    result, total = [], 0
    for table in tables:
        logical = hashlib.sha256(pickle.dumps(table["columns"], protocol=4))
        physical = hashlib.sha256(pickle.dumps(table["columns"], protocol=4))
        count = 0
        for values, rowid in _rows(db, table, strategies[table["name"]]):
            count += 1
            logical.update(pickle.dumps(tuple(values), protocol=4))
            physical.update(pickle.dumps((tuple(values), rowid), protocol=4))
        total += count
        if total > delta.MAX_ROWS:
            _error("Snapshot row limit exceeded")
        result.append(dict(table, rows=count, sha256=logical.hexdigest(), rowid_sha256=physical.hexdigest()))
    inventory = {"schema_sha256": schema_sha256, "tables": result}
    inventory["logical_sha256"] = digest(inventory)
    return inventory


class _Objects:
    def __init__(self, directory):
        self.directory = directory
        self.objects = {}
        self.chunks = {}
        self.reference_bytes = 0
        self.chunk_bytes = 0

    def add_stream(self, handle):
        whole, size, chunks = hashlib.sha256(), 0, []
        while True:
            raw = handle.read(CHUNK_BYTES)
            if not raw:
                break
            sha = hashlib.sha256(raw).hexdigest()
            whole.update(raw)
            size += len(raw)
            if size > MAX_TOTAL_BYTES:
                _error("Object exceeds byte limit")
            chunks.append({"sha256": sha, "bytes": len(raw)})
            if sha not in self.chunks:
                _write(self.directory / sha, raw)
                self.chunks[sha] = len(raw)
                self.chunk_bytes += len(raw)
        sha = whole.hexdigest()
        descriptor = {"sha256": sha, "bytes": size, "chunks": chunks}
        if sha in self.objects and self.objects[sha] != descriptor:
            _error("Content-addressed object collision")
        self.objects[sha] = descriptor
        self.reference_bytes += size
        if len(self.objects) > MAX_OBJECTS or self.chunk_bytes > MAX_TOTAL_BYTES:
            _error("Object inventory exceeds resource limits")
        return sha, size

    def add_bytes(self, raw):
        import io
        return self.add_stream(io.BytesIO(raw))

    def scalar(self, value, threshold):
        scalar = delta.encode_scalar(value)
        if type(value) in (str, bytes):
            raw = value.encode("utf-8") if type(value) is str else value
            if len(raw) >= threshold:
                sha, size = self.add_bytes(raw)
                return {"t": scalar["t"], "ref": sha, "bytes": size}
        return scalar


def _tree(root, excluded=None):
    files, directories = {}, {}
    if root is None:
        return files, directories
    if not root.is_dir():
        _error("files_root must be a directory")
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in names + filenames:
            path = _path(Path(directory) / name)
            relative = _relative(path.relative_to(root).as_posix())
            if path == excluded:
                continue
            info = path.stat()
            if stat.S_ISDIR(info.st_mode):
                directories[relative] = (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = (_stamp(path), stat.S_IMODE(info.st_mode) & 0o777)
            else:
                _error("Only regular files and directories are supported")
    if len(files) > MAX_FILES or len(directories) > MAX_FILES:
        _error("Runtime file tree exceeds entry limit")
    return files, directories


def _record(values, rowid, table, strategy, objects, threshold):
    key = _key(values, rowid, table, strategy)
    record = {"table": table["name"], "key_sha256": digest(key), "key": key,
              "values": [objects.scalar(value, threshold) for value in values],
              "rowid": None if rowid is None else delta.encode_scalar(rowid)}
    record["row_sha256"] = digest({"values": record["values"], "rowid": record["rowid"]})
    record["record_sha256"] = digest(record)
    return record


def export_package(database_path: Path, output_dir: Path, *, files_root: Path | None = None,
                   database_relative_path="papers.sqlite3", externalize_threshold=1024):
    """Stream a complete closed database and optional runtime tree into NEW output.

    No original values, file contents, or embedded paths are rewritten. Pause all
    writers first. A detected source change aborts without publishing output.
    """
    source, destination = _path(database_path), _new_directory(output_dir)
    root = None if files_root is None else _path(files_root)
    db_relative = _relative(database_relative_path)
    if type(externalize_threshold) is not int or not 1 <= externalize_threshold <= delta.MAX_SCALAR_BYTES:
        _error("Invalid scalar externalization threshold")
    if root is not None:
        if root == destination or root in destination.parents:
            _error("Package output cannot be inside files_root")
        if source != root / db_relative and (root / db_relative).exists():
            _error("Database destination collides with a runtime file")
    source_stamp = _stamp(source)
    before = _tree(root, source)
    try:
        with tempfile.TemporaryDirectory(prefix=".d1-package-", dir=destination.parent) as temporary:
            stage = Path(temporary) / "package"
            stage.mkdir(mode=0o700)
            (stage / "tables").mkdir(mode=0o700)
            (stage / "objects").mkdir(mode=0o700)
            objects = _Objects(stage / "objects")
            table_files = []
            with delta._snapshot(source) as copy, delta._open(copy) as db:
                delta._validate_database(db)
                schema, tables, strategies = _schema(db)
                inventory = _inventory(db, tables, strategies, digest(schema))
                with sqlite3.connect(Path(temporary) / "sort.sqlite3") as spool:
                    spool.execute("CREATE TABLE rows(key_sha256 TEXT PRIMARY KEY, raw BLOB NOT NULL, record_sha256 TEXT NOT NULL)")
                    for table in tables:
                        spool.execute("DELETE FROM rows")
                        for values, rowid in _rows(db, table, strategies[table["name"]]):
                            record = _record(values, rowid, table, strategies[table["name"]], objects, externalize_threshold)
                            raw = canonical(record) + b"\n"
                            if len(raw) > MAX_RECORD_BYTES:
                                _error("Normalized row exceeds record byte limit")
                            spool.execute("INSERT INTO rows VALUES(?,?,?)", (record["key_sha256"], raw, record["record_sha256"]))
                        path = "tables/" + hashlib.sha256(table["name"].encode("utf-8")).hexdigest() + ".jsonl"
                        stream_hash, size, count = hashlib.sha256(), 0, 0
                        chain = hashlib.sha256(b"").hexdigest()
                        with (stage / path).open("xb") as handle:
                            os.chmod(stage / path, 0o600)
                            for key_sha, raw, record_sha in spool.execute("SELECT key_sha256,raw,record_sha256 FROM rows ORDER BY key_sha256"):
                                handle.write(raw)
                                stream_hash.update(raw)
                                size += len(raw)
                                count += 1
                                chain = hashlib.sha256((chain + "\n" + key_sha + "\n" + record_sha).encode("ascii")).hexdigest()
                        table_files.append({"name": table["name"], "path": path, "bytes": size, "rows": count,
                                            "sha256": stream_hash.hexdigest(), "table_chain_sha256": chain,
                                            "key_kind": strategies[table["name"]]})
            files = []
            for relative, (stamp, mode) in sorted(before[0].items()):
                path = root / relative
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if (opened.st_dev, opened.st_ino) != stamp[:2]:
                        _error("Runtime file changed before opening")
                    sha, size = objects.add_stream(handle)
                if _stamp(path) != stamp:
                    _error("Runtime file changed during export")
                files.append({"path": relative, "mode": mode, "bytes": size, "sha256": sha})
            delta._sidecars(source)
            if _stamp(source) != source_stamp:
                _error("Database changed during runtime file export")
            if _tree(root, source) != before:
                _error("Runtime file tree changed during export")
            compatible = all(value == "primary_key" for value in strategies.values())
            manifest = {"format": FORMAT, "version": VERSION,
                        "hash_algorithm": delta.HASH_ALGORITHM if compatible else EXTENDED_HASH_ALGORITHM,
                        "database_path": db_relative, "database_mode": stat.S_IMODE(source.stat().st_mode) & 0o777,
                        "schema": schema, "schema_sha256": digest(schema), "inventory": inventory,
                        "tables": table_files, "files": files,
                        "directories": [{"path": name, "mode": stamp[2] & 0o777} for name, stamp in sorted(before[1].items())],
                        "objects": [objects.objects[sha] for sha in sorted(objects.objects)],
                        "statistics": {"referenced_object_bytes": objects.reference_bytes,
                                       "unique_object_bytes": sum(obj["bytes"] for obj in objects.objects.values()),
                                       "unique_chunk_bytes": sum(objects.chunks.values()),
                                       "object_count": len(objects.objects), "chunk_count": len(objects.chunks)}}
            raw = canonical(manifest) + b"\n"
            if len(raw) > MAX_MANIFEST_BYTES:
                _error("Manifest exceeds byte limit")
            _write(stage / "manifest.json", raw)
            verify_package(stage)
            delta._sidecars(source)
            if _stamp(source) != source_stamp or _tree(root, source) != before:
                _error("Source changed before package publication")
            checkpoints._publish(stage, destination)
        return {"manifest_sha256": hashlib.sha256(raw).hexdigest(), "manifest": manifest}
    except (sqlite3.Error, OSError, delta.DeltaError, checkpoints.CheckpointError) as error:
        raise PackageError("Cannot export package: " + str(error)) from error


def _validate_manifest(manifest):
    _check_object(manifest, ("format", "version", "hash_algorithm", "database_path", "database_mode", "schema", "schema_sha256", "inventory", "tables", "files", "directories", "objects", "statistics"))
    if manifest["format"] != FORMAT or type(manifest["version"]) is not int or manifest["version"] != VERSION:
        _error("Unsupported package format/version")
    if manifest["hash_algorithm"] not in (delta.HASH_ALGORITHM, EXTENDED_HASH_ALGORITHM):
        _error("Unsupported logical hash algorithm")
    _relative(manifest["database_path"])
    _integer(manifest["database_mode"], 0o777)
    schema = manifest["schema"]
    _check_object(schema, ("objects", "user_version", "application_id", "encoding"))
    if type(schema["objects"]) is not list or len(schema["objects"]) > 10_000:
        _error("Invalid schema object list")
    for obj in schema["objects"]:
        if type(obj) is not list or len(obj) != 4 or obj[0] not in ("table", "index") or any(type(v) is not str or "\0" in v for v in obj[1:3]) or (obj[3] is not None and type(obj[3]) is not str):
            _error("Unsupported schema object")
    for name in ("user_version", "application_id"):
        if type(schema[name]) is not int or not -(2**31) <= schema[name] < 2**31:
            _error("Invalid schema pragma")
    if schema["encoding"] not in ("UTF-8", "UTF-16le", "UTF-16be"):
        _error("Unsupported SQLite encoding")
    if _hash(manifest["schema_sha256"]) != digest(schema):
        _error("Schema fingerprint mismatch")
    inv = manifest["inventory"]
    _check_object(inv, ("schema_sha256", "logical_sha256", "tables"))
    if inv["schema_sha256"] != manifest["schema_sha256"] or _hash(inv["logical_sha256"]) != digest({k: v for k, v in inv.items() if k != "logical_sha256"}):
        _error("Inventory fingerprint mismatch")
    if type(inv["tables"]) is not list or not 0 < len(inv["tables"]) <= delta.MAX_TABLES:
        _error("Invalid table inventory")
    names, total = [], 0
    for table in inv["tables"]:
        _check_object(table, ("name", "columns", "primary_key", "rowid", "rows", "sha256", "rowid_sha256"))
        if type(table["name"]) is not str or not table["name"] or "\0" in table["name"]:
            _error("Invalid table name")
        names.append(table["name"])
        for field in ("columns", "primary_key"):
            values = table[field]
            if type(values) is not list or len(values) > delta.MAX_COLUMNS or any(type(v) is not str or not v or "\0" in v for v in values) or len(set(values)) != len(values):
                _error("Invalid columns or primary key")
        if not table["columns"] or not set(table["primary_key"]) <= set(table["columns"]) or table["rowid"] not in (None, "rowid", "_rowid_", "oid"):
            _error("Invalid table metadata")
        total += _integer(table["rows"], delta.MAX_ROWS)
        _hash(table["sha256"])
        _hash(table["rowid_sha256"])
    if names != sorted(set(names)) or total > delta.MAX_ROWS:
        _error("Duplicate/unordered table or total row limit exceeded")
    if type(manifest["tables"]) is not list or len(manifest["tables"]) != len(names):
        _error("Table file coverage mismatch")
    fallback = False
    for meta, table in zip(manifest["tables"], inv["tables"]):
        _check_object(meta, ("name", "path", "bytes", "rows", "sha256", "table_chain_sha256", "key_kind"))
        path = "tables/" + hashlib.sha256(table["name"].encode("utf-8")).hexdigest() + ".jsonl"
        if meta["name"] != table["name"] or meta["path"] != path or meta["rows"] != table["rows"]:
            _error("Table file metadata mismatch")
        _integer(meta["bytes"])
        _hash(meta["sha256"])
        _hash(meta["table_chain_sha256"])
        if meta["key_kind"] == "rowid":
            fallback = True
            if table["rowid"] is None:
                _error("Rowid fallback is unavailable")
        elif meta["key_kind"] != "primary_key" or not table["primary_key"]:
            _error("Invalid stable key strategy")
    if (manifest["hash_algorithm"] == EXTENDED_HASH_ALGORITHM) != fallback:
        _error("Logical hash algorithm does not match key strategies")
    objects, chunks = {}, {}
    if type(manifest["objects"]) is not list or len(manifest["objects"]) > MAX_OBJECTS:
        _error("Invalid object inventory")
    total = 0
    for descriptor in manifest["objects"]:
        _check_object(descriptor, ("sha256", "bytes", "chunks"))
        sha, size = _hash(descriptor["sha256"]), _integer(descriptor["bytes"])
        if sha in objects or type(descriptor["chunks"]) is not list:
            _error("Duplicate object or invalid chunks")
        summed = 0
        for index, chunk in enumerate(descriptor["chunks"]):
            _check_object(chunk, ("sha256", "bytes"))
            chunk_sha, length = _hash(chunk["sha256"]), _integer(chunk["bytes"], CHUNK_BYTES)
            if length == 0 or (index < len(descriptor["chunks"]) - 1 and length != CHUNK_BYTES):
                _error("Noncanonical chunk boundary")
            if chunk_sha in chunks and chunks[chunk_sha] != length:
                _error("Conflicting chunk lengths")
            chunks[chunk_sha] = length
            summed += length
        if summed != size or (size == 0 and sha != hashlib.sha256(b"").hexdigest()):
            _error("Object size or empty-object hash mismatch")
        objects[sha] = descriptor
        total += size
    if list(objects) != sorted(objects) or sum(chunks.values()) > MAX_TOTAL_BYTES or total > MAX_TOTAL_BYTES:
        _error("Unordered objects or package byte limit exceeded")
    for field in ("files", "directories"):
        if type(manifest[field]) is not list or len(manifest[field]) > MAX_FILES:
            _error("Invalid runtime tree inventory")
        paths = []
        for entry in manifest[field]:
            _check_object(entry, ("path", "mode", "bytes", "sha256") if field == "files" else ("path", "mode"))
            paths.append(_relative(entry["path"]))
            _integer(entry["mode"], 0o777)
            if field == "files":
                sha = _hash(entry["sha256"])
                _integer(entry["bytes"])
                if sha not in objects or objects[sha]["bytes"] != entry["bytes"]:
                    _error("Missing or mismatched runtime file object")
        if paths != sorted(set(paths)):
            _error("Duplicate/unordered runtime paths")
    filepaths = {entry["path"] for entry in manifest["files"]} | {manifest["database_path"]}
    dirpaths = {entry["path"] for entry in manifest["directories"]}
    if len(filepaths) != len(manifest["files"]) + 1 or filepaths & dirpaths:
        _error("Runtime path collision")
    sidecars = {manifest["database_path"] + suffix for suffix in ("-wal", "-shm", "-journal")}
    if (filepaths | dirpaths) & sidecars:
        _error("Runtime files cannot install SQLite sidecars")
    for path in filepaths | dirpaths:
        parts = path.split("/")
        if any("/".join(parts[:i]) in filepaths for i in range(1, len(parts))):
            _error("Runtime path has a file as its parent")
    stats = manifest["statistics"]
    _check_object(stats, ("referenced_object_bytes", "unique_object_bytes", "unique_chunk_bytes", "object_count", "chunk_count"))
    for value in stats.values():
        _integer(value, MAX_TOTAL_BYTES * 1000)
    if stats["unique_object_bytes"] != total or stats["unique_chunk_bytes"] != sum(chunks.values()) or stats["object_count"] != len(objects) or stats["chunk_count"] != len(chunks):
        _error("Object statistics mismatch")
    return objects, chunks


def _object_parts(package, descriptor):
    whole, size = hashlib.sha256(), 0
    for chunk in descriptor["chunks"]:
        raw = _read(package / "objects" / chunk["sha256"], CHUNK_BYTES)
        if len(raw) != chunk["bytes"] or hashlib.sha256(raw).hexdigest() != chunk["sha256"]:
            _error("Object chunk SHA-256/size mismatch")
        whole.update(raw)
        size += len(raw)
        yield raw
    if size != descriptor["bytes"] or whole.hexdigest() != descriptor["sha256"]:
        _error("Whole-object SHA-256/size mismatch")


class _Reader:
    def __init__(self, package, objects):
        from collections import OrderedDict
        self.package, self.objects = package, objects
        self.cache, self.cache_bytes = OrderedDict(), 0
        self.references = set()
        self.reference_bytes = 0

    def scalar(self, value):
        if type(value) is dict and "ref" in value:
            _check_object(value, ("t", "ref", "bytes"))
            sha = _hash(value["ref"])
            size = _integer(value["bytes"], delta.MAX_SCALAR_BYTES)
            if value["t"] not in ("text", "blob") or sha not in self.objects or self.objects[sha]["bytes"] != size:
                _error("Invalid or missing scalar object")
            self.references.add(sha)
            self.reference_bytes += size
            if sha in self.cache:
                raw = self.cache.pop(sha)
                self.cache[sha] = raw
            else:
                raw = b"".join(_object_parts(self.package, self.objects[sha]))
                if len(raw) <= 8 * 1024 * 1024:
                    while self.cache and self.cache_bytes + len(raw) > 8 * 1024 * 1024:
                        _, evicted = self.cache.popitem(last=False)
                        self.cache_bytes -= len(evicted)
                    self.cache[sha] = raw
                    self.cache_bytes += len(raw)
            try:
                return raw.decode("utf-8") if value["t"] == "text" else memoryview(raw).tobytes()
            except UnicodeError as error:
                raise PackageError("Invalid UTF-8 scalar object") from error
        try:
            return delta.decode_scalar(value)
        except delta.DeltaError as error:
            raise PackageError(str(error)) from error


def _validate_record(record, table, strategy, reader):
    _check_object(record, ("table", "key_sha256", "key", "values", "rowid", "row_sha256", "record_sha256"))
    _hash(record["row_sha256"])
    if _hash(record["record_sha256"]) != digest({k: v for k, v in record.items() if k != "record_sha256"}):
        _error("Normalized row SHA-256 mismatch")
    if record["table"] != table["name"] or type(record["values"]) is not list or len(record["values"]) != len(table["columns"]):
        _error("Row table/width mismatch")
    values = tuple(reader.scalar(value) for value in record["values"])
    try:
        rowid = None if record["rowid"] is None else delta.decode_scalar(record["rowid"])
    except delta.DeltaError as error:
        raise PackageError(str(error)) from error
    if (table["rowid"] is None and rowid is not None) or (table["rowid"] is not None and type(rowid) is not int):
        _error("Invalid hidden rowid")
    key = _key(values, rowid, table, strategy)
    if strategy == "primary_key" and any(item["t"] == "null" for item in key):
        _error("NULL primary key without rowid fallback")
    if record["key"] != key or _hash(record["key_sha256"]) != digest(key):
        _error("Stable row key mismatch")
    if digest({"values": record["values"], "rowid": record["rowid"]}) != record["row_sha256"]:
        _error("Canonical row image SHA-256 mismatch")
    return values, rowid


def _table_records(package, meta):
    path = package / meta["path"]
    before = _stamp(path)
    if before[2] != meta["bytes"]:
        _error("Table file size mismatch")
    sha, count, previous = hashlib.sha256(), 0, ""
    chain = hashlib.sha256(b"").hexdigest()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2]:
            _error("Table file changed before opening")
        while True:
            raw = handle.readline(MAX_RECORD_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_RECORD_BYTES:
                _error("Normalized row exceeds record byte limit")
            record = _json(raw)
            key = _hash(record.get("key_sha256") if type(record) is dict else None)
            record_sha = _hash(record.get("record_sha256"))
            if key <= previous:
                _error("Duplicate/unordered stable row keys")
            previous = key
            sha.update(raw)
            count += 1
            if count > meta["rows"]:
                _error("Table row count exceeded")
            chain = hashlib.sha256((chain + "\n" + key + "\n" + record_sha).encode("ascii")).hexdigest()
            yield record
    if count != meta["rows"] or sha.hexdigest() != meta["sha256"] or chain != meta["table_chain_sha256"]:
        _error("Table content hash, chain, or count mismatch")
    if _stamp(path) != before:
        _error("Table file changed while reading")


def _load_package(package_dir, expected_manifest_sha256=None):
    package = _path(package_dir)
    raw = _read(package / "manifest.json", MAX_MANIFEST_BYTES)
    sha = hashlib.sha256(raw).hexdigest()
    if expected_manifest_sha256 is not None and _hash(expected_manifest_sha256) != sha:
        _error("Manifest SHA-256 does not match trusted transfer manifest")
    manifest = _json(raw)
    objects, chunks = _validate_manifest(manifest)
    before = _tree(package)
    expected = {"manifest.json"} | {table["path"] for table in manifest["tables"]} | {"objects/" + key for key in chunks}
    if set(before[0]) != expected or set(before[1]) != {"objects", "tables"}:
        _error("Missing or unexpected package members")
    return package, manifest, sha, objects, before


def verify_package(package_dir: Path, *, expected_manifest_sha256=None):
    """Verify all canonical records, objects, references and transfer hashes.

    Full SQLite logical inventory equivalence additionally requires restore;
    individual row audit hashes and complete table coverage are verified here.
    """
    package, manifest, sha, objects, before = _load_package(package_dir, expected_manifest_sha256)
    for descriptor in objects.values():
        for _ in _object_parts(package, descriptor):
            pass
    reader = _Reader(package, objects)
    for meta, table in zip(manifest["tables"], manifest["inventory"]["tables"]):
        for record in _table_records(package, meta):
            _validate_record(record, table, meta["key_kind"], reader)
    for entry in manifest["files"]:
        reader.references.add(entry["sha256"])
        reader.reference_bytes += entry["bytes"]
    if reader.references != set(objects) or reader.reference_bytes != manifest["statistics"]["referenced_object_bytes"]:
        _error("Unreferenced objects or referenced byte total mismatch")
    if _tree(package) != before:
        _error("Package changed during verification")
    return {"manifest_sha256": sha, "manifest": manifest}


def restore_package(package_dir: Path, output_dir: Path, *, trusted_schema_database: Path,
                    expected_manifest_sha256=None):
    """Restore only into a NEW directory, using independently trusted schema SQL.

    Never supply a schema database taken from the untrusted package. An exact
    schema fingerprint must match before any schema statements are executed.
    """
    destination = _new_directory(output_dir)
    result = verify_package(package_dir, expected_manifest_sha256=expected_manifest_sha256)
    package, manifest, sha, objects, before = _load_package(package_dir, result["manifest_sha256"])
    trusted_path = _path(trusted_schema_database)
    if package == trusted_path or package in trusted_path.parents:
        _error("Trusted schema database cannot come from the untrusted package")
    try:
        with delta._snapshot(trusted_schema_database) as trusted_copy, delta._open(trusted_copy) as trusted:
            schema, tables, _ = _schema(trusted)
            if digest(schema) != manifest["schema_sha256"] or schema != manifest["schema"]:
                _error("Package schema is not the independently trusted allowlisted schema")
            if tables != [{k: table[k] for k in ("name", "columns", "primary_key", "rowid")} for table in manifest["inventory"]["tables"]]:
                _error("Untrusted table metadata differs from trusted schema")
        with tempfile.TemporaryDirectory(prefix=".d1-restore-", dir=destination.parent) as temporary:
            stage = Path(temporary) / "state"
            stage.mkdir(mode=0o700)
            for directory in manifest["directories"]:
                (stage / directory["path"]).mkdir(parents=True, exist_ok=True, mode=0o700)
            database = stage / manifest["database_path"]
            database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            db = sqlite3.connect(database, isolation_level=None)
            try:
                disable = getattr(db, "enable_load_extension", None)
                if disable is not None:
                    disable(False)
                db.execute("PRAGMA trusted_schema=OFF")
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA encoding='" + schema["encoding"] + "'")
                db.execute("PRAGMA user_version=" + str(schema["user_version"]))
                db.execute("PRAGMA application_id=" + str(schema["application_id"]))
                # SQL originates ONLY in the trusted database, never a manifest.
                for kind in ("table", "index"):
                    for obj_kind, name, _, sql in schema["objects"]:
                        if obj_kind == kind and sql is not None and name != "sqlite_sequence":
                            db.execute(sql)
                db.execute("BEGIN IMMEDIATE")
                reader = _Reader(package, objects)
                sequence = []
                for meta, table in zip(manifest["tables"], tables):
                    for record in _table_records(package, meta):
                        values, rowid = _validate_record(record, table, meta["key_kind"], reader)
                        if table["name"] == "sqlite_sequence":
                            sequence.append((table, values, rowid))
                            continue
                        _insert(db, table, values, rowid)
                if any(table["name"] == "sqlite_sequence" for table in tables):
                    db.execute('DELETE FROM "sqlite_sequence"')
                    for table, values, rowid in sequence:
                        _insert(db, table, values, rowid)
                delta._validate_database(db)
                restored_schema, restored_tables, restored_strategies = _schema(db)
                desired_strategies = {meta["name"]: meta["key_kind"] for meta in manifest["tables"]}
                if restored_schema != schema or restored_tables != tables or restored_strategies != desired_strategies:
                    _error("Restored schema or stable key strategy mismatch")
                if _inventory(db, tables, desired_strategies, digest(schema)) != manifest["inventory"]:
                    _error("Full restored logical inventory mismatch")
                db.execute("COMMIT")
                db.execute("PRAGMA foreign_keys=ON")
            finally:
                db.close()
            for entry in manifest["files"]:
                path = stage / entry["path"]
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with path.open("xb") as handle:
                    os.chmod(path, 0o600)
                    for raw in _object_parts(package, objects[entry["sha256"]]):
                        handle.write(raw)
            if _tree(package) != before:
                _error("Package changed during restore")
            # Original ordinary mode bits are restored; setuid/setgid/sticky bits
            # are deliberately excluded. Publication stays inside a private root.
            for entry in manifest["files"]:
                os.chmod(stage / entry["path"], entry["mode"])
            os.chmod(database, manifest["database_mode"])
            # _publish needs read access to fsync members, so restrictive modes
            # are only supported if they permit owner read/traversal.
            for directory in reversed(manifest["directories"]):
                os.chmod(stage / directory["path"], directory["mode"])
            checkpoints._publish(stage, destination)
        return {"manifest_sha256": sha, "logical_sha256": manifest["inventory"]["logical_sha256"],
                "tables": len(tables), "rows": sum(table["rows"] for table in manifest["inventory"]["tables"]),
                "files": len(manifest["files"]), "database_path": manifest["database_path"]}
    except (sqlite3.Error, OSError, delta.DeltaError, checkpoints.CheckpointError) as error:
        raise PackageError("Cannot restore package: " + str(error)) from error


def _insert(db, table, values, rowid):
    columns = list(table["columns"])
    if table["rowid"] is not None:
        columns.append(table["rowid"])
        values = tuple(values) + (rowid,)
    db.execute("INSERT INTO " + delta._quote(table["name"]) + " (" + ",".join(map(delta._quote, columns)) + ") VALUES (" + ",".join("?" for _ in values) + ")", values)


# Shared by the cloud working-set adapter. These helpers contain no network I/O.
ObjectStore = _Objects
ObjectReader = _Reader


def normalize_row(values, rowid, table, key_kind, objects, threshold=1024):
    """Return the canonical normalized row; collect referenced content in objects."""
    return _record(values, rowid, table, key_kind, objects, threshold)


def read_row(record, table, key_kind, reader):
    """Validate and decode one row, including its stable key and lossless hash."""
    return _validate_record(record, table, key_kind, reader)


def iter_rows(package_dir: Path, *, table_names=None, key_sha256s=None):
    """Read a selected working set; absence is NEVER a deletion instruction.

    Selection is explicit and carries no claim to be a complete table snapshot.
    A caller must retain fetched base hashes and submit only explicit mutations.
    """
    package, manifest, _, objects, before = _load_package(package_dir)
    names = {table["name"] for table in manifest["tables"]}
    selection = names if table_names is None else set(table_names)
    if not selection <= names:
        _error("Unknown working-set table")
    keys = None if key_sha256s is None else {_hash(value) for value in key_sha256s}
    reader = _Reader(package, objects)
    for meta, table in zip(manifest["tables"], manifest["inventory"]["tables"]):
        if table["name"] not in selection:
            continue
        for record in _table_records(package, meta):
            if keys is None or record["key_sha256"] in keys:
                _validate_record(record, table, meta["key_kind"], reader)
                yield record
    if _tree(package) != before:
        _error("Package changed during working-set read")


def iter_native_pages(package_dir: Path, *, max_page_bytes=DEFAULT_PAGE_BYTES, max_records=500):
    """Yield bounded JSON-native upload pages; this function never sends them.

    Order: chunks, object descriptors, manifest descriptor, then rows. The
    manifest itself is chunked, so no full runtime database or oversized manifest
    has to cross MCP in one call. Every yielded page's canonical JSON is bounded.
    """
    if type(max_page_bytes) is not int or not 1024 <= max_page_bytes <= 1024 * 1024 or type(max_records) is not int or not 1 <= max_records <= 5000:
        _error("Invalid native page limits")
    verified = verify_package(package_dir)
    package, manifest, sha, objects, before = _load_package(package_dir, verified["manifest_sha256"])
    manifest_raw = _read(package / "manifest.json", MAX_MANIFEST_BYTES)
    manifest_chunks = []
    raw_manifest_chunks = {}
    for offset in range(0, len(manifest_raw), CHUNK_BYTES):
        raw = manifest_raw[offset:offset + CHUNK_BYTES]
        chunk_sha = hashlib.sha256(raw).hexdigest()
        manifest_chunks.append({"sha256": chunk_sha, "bytes": len(raw)})
        raw_manifest_chunks[chunk_sha] = raw
    manifest_descriptor = {"sha256": sha, "bytes": len(manifest_raw), "chunks": manifest_chunks}
    def entries():
        chunk_sizes = {chunk["sha256"]: chunk["bytes"] for obj in objects.values() for chunk in obj["chunks"]}
        for chunk_sha in sorted(set(chunk_sizes) | set(raw_manifest_chunks)):
            raw = raw_manifest_chunks.get(chunk_sha)
            if raw is None:
                raw = _read(package / "objects" / chunk_sha, CHUNK_BYTES)
            if hashlib.sha256(raw).hexdigest() != chunk_sha:
                _error("Native chunk SHA-256 mismatch")
            yield "chunks", {"sha256": chunk_sha, "bytes": len(raw), "base64": base64.b64encode(raw).decode("ascii")}
        for obj in objects.values():
            yield "objects", obj
        yield "manifest", manifest_descriptor
        for meta, table in zip(manifest["tables"], manifest["inventory"]["tables"]):
            for record in _table_records(package, meta):
                yield "rows", record
    kind, records, payload_bytes = None, [], 0
    for next_kind, record in entries():
        record_bytes = len(canonical(record))
        overhead = len(canonical({"kind": next_kind, "records": []}))
        proposed = overhead + payload_bytes + record_bytes + len(records)
        if records and (kind != next_kind or len(records) >= max_records or proposed > max_page_bytes):
            yield {"kind": kind, "records": records}
            records, payload_bytes = [], 0
        if overhead + record_bytes > max_page_bytes:
            _error("Native page byte limit cannot hold one record/chunk")
        kind = next_kind
        records.append(record)
        payload_bytes += record_bytes
    if records:
        yield {"kind": kind, "records": records}
    if _tree(package) != before:
        _error("Package changed during native page generation")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("database", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("--files-root", type=Path)
    export.add_argument("--database-relative-path", default="papers.sqlite3")
    restore = commands.add_parser("restore")
    restore.add_argument("package", type=Path)
    restore.add_argument("output", type=Path)
    restore.add_argument("--trusted-schema-database", type=Path, required=True)
    restore.add_argument("--expected-manifest-sha256")
    verify = commands.add_parser("verify")
    verify.add_argument("package", type=Path)
    verify.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_package(args.database, args.output, files_root=args.files_root, database_relative_path=args.database_relative_path)
        elif args.command == "restore":
            result = restore_package(args.package, args.output, trusted_schema_database=args.trusted_schema_database, expected_manifest_sha256=args.expected_manifest_sha256)
        else:
            result = verify_package(args.package, expected_manifest_sha256=args.expected_manifest_sha256)
        if "manifest" in result:
            manifest = result.pop("manifest")
            result.update(logical_sha256=manifest["inventory"]["logical_sha256"], tables=len(manifest["tables"]), rows=sum(t["rows"] for t in manifest["tables"]), files=len(manifest["files"]), **manifest["statistics"])
        print(json.dumps(result, sort_keys=True))
        return 0
    except (PackageError, delta.DeltaError, OSError, ValueError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())

"""Lossless, database-only deltas for two closed SQLite snapshots.

No network, credentials, pickle loading, schema migration, path rewriting, or
SQL supplied by a patch. This intentionally supports a narrow SQLite subset.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import math
import os
import pickle
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
from pathlib import Path
from typing import Any

FORMAT = "academic-radar-sqlite-delta"
VERSION = 1
HASH_ALGORITHM = "sha256-pickle4-columns-ordered-rows-v1"
MAX_PATCH_BYTES = 64 * 1024 * 1024
MAX_SCALAR_BYTES = 16 * 1024 * 1024
MAX_TABLES = 256
MAX_COLUMNS = 1024
MAX_ROWS = 2_000_000
MAX_OPERATIONS = 100_000
MAX_JSON_NODES = 2_000_000
MAX_JSON_DEPTH = 32
HEX = re.compile(r"[0-9a-f]{64}\Z")
INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")


class DeltaError(ValueError):
    """A snapshot or patch is unsafe, mismatched, incomplete, or unsupported."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise DeltaError("Unexpected or missing patch fields")


def _hash(value):
    if type(value) is not str or not HEX.fullmatch(value):
        raise DeltaError("Invalid SHA-256")


def _bounded_json(raw: bytes):
    if not raw or len(raw) > MAX_PATCH_BYTES:
        raise DeltaError("Patch exceeds the byte limit or is empty")
    # Bound nesting before json.loads can recurse. Quotes/escapes are lexical,
    # including escaped backslashes; syntax itself is checked by the parser.
    depth, quoted, escaped, atom, tokens = 0, False, False, False, 0
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted, atom = True, False
            tokens += 1
        elif byte in (91, 123):
            atom = False
            tokens += 1
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise DeltaError("Patch exceeds the nesting limit")
        elif byte in (93, 125):
            atom = False
            depth -= 1
        elif byte in (9, 10, 13, 32, 44, 58):
            atom = False
        elif not atom:
            atom = True
            tokens += 1
        if tokens > MAX_JSON_NODES:
            raise DeltaError("Patch exceeds the node limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DeltaError("Duplicate JSON key")
            result[key] = value
        return result
    def integer(value):
        if len(value) > 20:
            raise DeltaError("Oversized JSON integer")
        return int(value)
    def unsupported(_):
        raise DeltaError("JSON floats and non-finite constants are not supported")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_int=integer, parse_float=unsupported,
                           parse_constant=unsupported)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DeltaError("Invalid bounded patch JSON: " + str(error)) from error
    stack, count = [value], 0
    while stack:
        item = stack.pop()
        count += 1
        if count > MAX_JSON_NODES:
            raise DeltaError("Patch exceeds the node limit")
        if type(item) is dict:
            stack.extend(item.keys())
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
        elif type(item) is str:
            try:
                if len(item.encode("utf-8")) > MAX_SCALAR_BYTES * 2:
                    raise DeltaError("Patch scalar exceeds the byte limit")
            except UnicodeError as error:
                raise DeltaError("Invalid Unicode scalar") from error
    return value


def encode_scalar(value):
    """Only primitive SQLite storage classes; REAL uses exact IEEE-754 bytes."""
    if value is None:
        return {"t": "null"}
    if type(value) is int and -(2**63) <= value < 2**63:
        return {"t": "integer", "v": str(value)}
    if type(value) is float and not math.isnan(value):
        return {"t": "real", "v": struct.pack(">d", value).hex()}
    if type(value) is str:
        try:
            raw = value.encode("utf-8")
        except UnicodeError as error:
            raise DeltaError("Unsupported SQLite text encoding") from error
        if len(raw) <= MAX_SCALAR_BYTES:
            return {"t": "text", "v": value}
    if type(value) is bytes and len(value) <= MAX_SCALAR_BYTES:
        return {"t": "blob", "v": base64.b64encode(value).decode("ascii")}
    raise DeltaError("Unsupported or oversized SQLite scalar")


def decode_scalar(value):
    if type(value) is not dict:
        raise DeltaError("Invalid typed SQLite scalar")
    kind = value.get("t")
    _object(value, ("t",) if kind == "null" else ("t", "v"))
    if kind == "null":
        return None
    raw = value["v"]
    if type(raw) is not str:
        raise DeltaError("Typed scalar payload must be a string")
    try:
        if kind == "integer" and len(raw) <= 20 and INTEGER.fullmatch(raw):
            result = int(raw)
        elif kind == "real" and re.fullmatch(r"[0-9a-f]{16}", raw):
            result = struct.unpack(">d", bytes.fromhex(raw))[0]
        elif kind == "text":
            result = raw
        elif kind == "blob" and len(raw) <= (MAX_SCALAR_BYTES + 2) // 3 * 4:
            result = base64.b64decode(raw, validate=True)
        else:
            raise DeltaError("Invalid typed SQLite scalar")
        if encode_scalar(result) != value:
            raise DeltaError("Noncanonical typed SQLite scalar")
        return result
    except (ValueError, UnicodeError) as error:
        raise DeltaError("Invalid typed SQLite scalar") from error


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _path(path):
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise DeltaError("Parent traversal is not supported")
    path = Path(os.path.abspath(path))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise DeltaError("Symlinks are not supported")
    return path


def _stamp(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise DeltaError("Input must be a regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sidecars(path):
    if any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise DeltaError("SQLite sidecars present; use a closed, checkpointed snapshot")


@contextlib.contextmanager
def _snapshot(path):
    """Copy read-only input; never open the user's file with SQLite or create WAL."""
    path = _path(path)
    _sidecars(path)
    before = _stamp(path)
    with tempfile.TemporaryDirectory(prefix="sqlite-delta-source-") as temporary:
        copy = Path(temporary) / "snapshot.sqlite3"
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source, copy.open("xb") as output:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != before[:2]:
                raise DeltaError("Source changed before snapshot")
            shutil.copyfileobj(source, output, 1024 * 1024)
        os.chmod(copy, 0o600)
        if _stamp(path) != before:
            raise DeltaError("Source changed during snapshot")
        _sidecars(path)
        yield copy
        if _stamp(path) != before:
            raise DeltaError("Source changed during delta operation")
        _sidecars(path)


@contextlib.contextmanager
def _open(path, *, writable=False):
    suffix = "?mode=rw" if writable else "?mode=ro&immutable=1"
    db = sqlite3.connect(path.as_uri() + suffix, uri=True, isolation_level=None)
    try:
        disable_extensions = getattr(db, "enable_load_extension", None)
        if disable_extensions is not None:
            disable_extensions(False)
        db.execute("PRAGMA trusted_schema=OFF")
        if writable:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
        else:
            db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def _validate_database(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise DeltaError("SQLite integrity validation failed")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DeltaError("SQLite foreign-key validation failed")


def _schema(db):
    objects = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name,tbl_name").fetchall()
    if any(row[0] not in ("table", "index") for row in objects):
        raise DeltaError("Triggers and views are unsupported")
    tables = []
    for kind, name, _, sql in objects:
        if kind != "table":
            continue
        if name.startswith("sqlite_") and name != "sqlite_sequence":
            raise DeltaError("Unsupported internal SQLite table")
        if not sql or not re.match(r"\s*CREATE\s+TABLE\b", sql, re.I):
            raise DeltaError("Virtual or nonstandard table declarations are unsupported")
        columns = db.execute("PRAGMA table_xinfo(" + _quote(name) + ")").fetchall()
        if not columns or len(columns) > MAX_COLUMNS or any(row[6] != 0 for row in columns):
            raise DeltaError("Hidden/generated columns or oversized schema are unsupported")
        names = [row[1] for row in columns]
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if name == "sqlite_sequence":
            if names != ["name", "seq"]:
                raise DeltaError("Unexpected sqlite_sequence schema")
            primary = ["name"]
        if not primary:
            raise DeltaError("Every user table needs an explicit primary key")
        # Unquoted, fixed identifiers avoid SQLite's double-quoted-string fallback.
        candidates = [value for value in ("rowid", "_rowid_", "oid") if value not in {col.lower() for col in names}]
        if not candidates:
            raise DeltaError("All hidden rowid names are shadowed")
        rowid = candidates[0]
        try:
            db.execute("SELECT " + rowid + " FROM " + _quote(name) + " LIMIT 0")
        except sqlite3.OperationalError as error:
            if "no such column" not in str(error):
                raise
            rowid = None  # WITHOUT ROWID, independent of DDL whitespace/comments.
        # SQLite primary-key/index collations must be portable built-ins.
        for index in db.execute("PRAGMA index_list(" + _quote(name) + ")"):
            for part in db.execute("PRAGMA index_xinfo(" + _quote(index[1]) + ")"):
                if part[4] not in ("BINARY", "NOCASE", "RTRIM") or (part[5] and part[1] < 0):
                    raise DeltaError("Custom collations and expression indexes are unsupported")
        tables.append({"name": name, "columns": names, "primary_key": primary, "rowid": rowid})
    if not tables or len(tables) > MAX_TABLES:
        raise DeltaError("Unsupported table count")
    metadata = {"objects": objects, "user_version": db.execute("PRAGMA user_version").fetchone()[0],
                "application_id": db.execute("PRAGMA application_id").fetchone()[0],
                "encoding": db.execute("PRAGMA encoding").fetchone()[0]}
    return _digest(metadata), sorted(tables, key=lambda item: item["name"])


def _image(values, rowid):
    return {"values": [encode_scalar(value) for value in values],
            "rowid": None if rowid is None else encode_scalar(rowid),
            "sha256": hashlib.sha256(pickle.dumps((tuple(values), rowid), protocol=4)).hexdigest()}


def _key(image, table):
    return _canonical([image["values"][table["columns"].index(name)] for name in table["primary_key"]])


def _read_table(db, table):
    name, columns, primary = table["name"], table["columns"], table["primary_key"]
    if db.execute("SELECT 1 FROM " + _quote(name) + " WHERE " + " OR ".join(_quote(col) + " IS NULL" for col in primary) + " LIMIT 1").fetchone():
        raise DeltaError("NULL primary keys are unsupported")
    if db.execute("SELECT 1 FROM " + _quote(name) + " GROUP BY " + ",".join(map(_quote, primary)) + " HAVING count(*)>1 LIMIT 1").fetchone():
        raise DeltaError("Duplicate or ambiguous primary keys are unsupported")
    select = ",".join(map(_quote, columns))
    if table["rowid"] is not None:
        select += "," + table["rowid"]
    cursor = db.execute("SELECT " + select + " FROM " + _quote(name) + " ORDER BY " + ",".join(map(_quote, primary)))
    logical = hashlib.sha256(pickle.dumps(columns, protocol=4))
    physical = hashlib.sha256(pickle.dumps(columns, protocol=4))
    rows = {}
    for row in cursor:
        values, rowid = (row[:-1], row[-1]) if table["rowid"] is not None else (row, None)
        image = _image(values, rowid)
        key = _key(image, table)
        if key in rows or len(rows) >= MAX_ROWS:
            raise DeltaError("Ambiguous primary key or row limit exceeded")
        rows[key] = image
        logical.update(pickle.dumps(tuple(values), protocol=4))
        physical.update(pickle.dumps((tuple(values), rowid), protocol=4))
    return {"rows": len(rows), "sha256": logical.hexdigest(), "rowid_sha256": physical.hexdigest()}, rows


def _inventory(db):
    schema_hash, tables = _schema(db)
    _validate_database(db)
    result, records = [], {}
    total = 0
    for table in tables:
        summary, rows = _read_table(db, table)
        total += summary["rows"]
        if total > MAX_ROWS:
            raise DeltaError("Snapshot row limit exceeded")
        result.append(dict(table, **summary))
        records[table["name"]] = rows
    inventory = {"schema_sha256": schema_hash, "tables": result}
    inventory["logical_sha256"] = _digest(inventory)
    return inventory, records


def inspect_snapshot(path: Path):
    """Return complete logical hashes/counts without row values or file paths."""
    try:
        with _snapshot(path) as copy, _open(copy) as db:
            return _inventory(db)[0]
    except (sqlite3.Error, OSError) as error:
        raise DeltaError("Cannot inspect closed snapshot: " + str(error)) from error


def _new_output(path):
    path = _path(path)
    if os.path.lexists(path) or not path.parent.is_dir():
        raise DeltaError("Destination must be absent in an existing directory")
    _sidecars(path)
    return path


def _publish(source, destination):
    # An exclusive hard link is atomic and cannot overwrite a raced-in file.
    _new_output(destination)
    os.link(source, destination, follow_symlinks=False)


def _summary(patch):
    return {"format": FORMAT, "version": VERSION, "schema_sha256": patch["schema_sha256"],
            "base_logical_sha256": patch["base"]["logical_sha256"],
            "target_logical_sha256": patch["target"]["logical_sha256"],
            "tables": [{"name": row["name"], **{kind: len(row[kind]) for kind in ("insert", "update", "delete")}} for row in patch["changes"]]}


def export_delta(baseline: Path, target: Path, output: Path, *, require_empty_tables=()):
    """Write a new bounded patch. Empty-table guards never omit data or hashes."""
    destination = _new_output(output)
    try:
        with _snapshot(baseline) as left, _snapshot(target) as right:
            with _open(left) as db:
                base, old = _inventory(db)
            with _open(right) as db:
                final, new = _inventory(db)
            if base["schema_sha256"] != final["schema_sha256"]:
                raise DeltaError("Schema changes are unsupported")
            guards = sorted(set(require_empty_tables))
            if any(type(name) is not str or name not in old or old[name] or new[name] for name in guards):
                raise DeltaError("Required empty table is missing or contains rows")
            changes = []
            for table in base["tables"]:
                name = table["name"]
                previous, current = old[name], new[name]
                changes.append({"name": name,
                    "insert": [current[key] for key in sorted(current.keys() - previous.keys())],
                    "update": [{"before": previous[key], "after": current[key]} for key in sorted(current.keys() & previous.keys()) if previous[key] != current[key]],
                    "delete": [previous[key] for key in sorted(previous.keys() - current.keys())]})
            patch = {"format": FORMAT, "version": VERSION, "hash_algorithm": HASH_ALGORITHM,
                     "schema_sha256": base["schema_sha256"], "base": base, "target": final,
                     "require_empty_tables": guards, "changes": changes}
            raw = _canonical(patch) + b"\n"
            _validate_patch(_bounded_json(raw))
        # Both source contexts have finished their unchanged-source checks.
        with tempfile.TemporaryDirectory(prefix=".sqlite-delta-", dir=destination.parent) as temporary:
            staged = Path(temporary) / "delta.json"
            staged.write_bytes(raw)
            os.chmod(staged, 0o600)
            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
            _publish(staged, destination)
        return dict(_summary(patch), patch_sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    except (sqlite3.Error, OSError) as error:
        raise DeltaError("Cannot export delta: " + str(error)) from error


def _validate_inventory(value):
    _object(value, ("schema_sha256", "logical_sha256", "tables"))
    _hash(value["schema_sha256"])
    _hash(value["logical_sha256"])
    if type(value["tables"]) is not list or not 0 < len(value["tables"]) <= MAX_TABLES:
        raise DeltaError("Invalid inventory table list")
    names, total = [], 0
    for table in value["tables"]:
        _object(table, ("name", "columns", "primary_key", "rowid", "rows", "sha256", "rowid_sha256"))
        if type(table["name"]) is not str or not table["name"] or "\0" in table["name"]:
            raise DeltaError("Invalid table name")
        names.append(table["name"])
        for key in ("columns", "primary_key"):
            cols = table[key]
            if type(cols) is not list or not 0 < len(cols) <= MAX_COLUMNS or any(type(col) is not str or not col or "\0" in col for col in cols) or len(set(cols)) != len(cols):
                raise DeltaError("Invalid columns or primary key")
        if not set(table["primary_key"]) <= set(table["columns"]) or table["rowid"] not in (None, "rowid", "_rowid_", "oid"):
            raise DeltaError("Invalid primary key or rowid")
        if type(table["rows"]) is not int or not 0 <= table["rows"] <= MAX_ROWS:
            raise DeltaError("Invalid row count")
        total += table["rows"]
        _hash(table["sha256"])
        _hash(table["rowid_sha256"])
    if names != sorted(set(names)) or total > MAX_ROWS:
        raise DeltaError("Duplicate/unordered table or row limit exceeded")
    if value["logical_sha256"] != _digest({key: item for key, item in value.items() if key != "logical_sha256"}):
        raise DeltaError("Inventory logical hash mismatch")


def _validate_image(image, table):
    _object(image, ("values", "rowid", "sha256"))
    if type(image["values"]) is not list or len(image["values"]) != len(table["columns"]):
        raise DeltaError("Invalid row width")
    values = tuple(decode_scalar(value) for value in image["values"])
    rowid = None if image["rowid"] is None else decode_scalar(image["rowid"])
    if (table["rowid"] is None and rowid is not None) or (table["rowid"] is not None and type(rowid) is not int):
        raise DeltaError("Invalid hidden rowid")
    if any(values[table["columns"].index(name)] is None for name in table["primary_key"]):
        raise DeltaError("NULL primary key in patch")
    if _image(values, rowid) != image:
        raise DeltaError("Row before/after image hash mismatch")
    return values, rowid


def _validate_patch(patch):
    _object(patch, ("format", "version", "hash_algorithm", "schema_sha256", "base", "target", "require_empty_tables", "changes"))
    if patch["format"] != FORMAT or type(patch["version"]) is not int or patch["version"] != VERSION or patch["hash_algorithm"] != HASH_ALGORITHM:
        raise DeltaError("Unsupported delta format/version/hash algorithm")
    _validate_inventory(patch["base"])
    _validate_inventory(patch["target"])
    if not patch["schema_sha256"] == patch["base"]["schema_sha256"] == patch["target"]["schema_sha256"]:
        raise DeltaError("Schema fingerprint mismatch")
    tables, targets = patch["base"]["tables"], patch["target"]["tables"]
    def metadata(table):
        return {key: table[key] for key in ("name", "columns", "primary_key", "rowid")}
    if list(map(metadata, tables)) != list(map(metadata, targets)):
        raise DeltaError("Schema metadata changed")
    guards = patch["require_empty_tables"]
    if type(guards) is not list or any(type(name) is not str for name in guards) or guards != sorted(set(guards)):
        raise DeltaError("Invalid empty-table guards")
    names = {table["name"] for table in tables}
    if not set(guards) <= names or any(table["rows"] for table in tables + targets if table["name"] in guards):
        raise DeltaError("Required empty tables must be empty in both inventories")
    changes = patch["changes"]
    if type(changes) is not list or len(changes) != len(tables):
        raise DeltaError("Patch must explicitly cover every table")
    count = 0
    for table, change in zip(tables, changes):
        _object(change, ("name", "insert", "update", "delete"))
        if change["name"] != table["name"]:
            raise DeltaError("Patch table coverage/order mismatch")
        seen = set()
        for kind in ("insert", "update", "delete"):
            if type(change[kind]) is not list:
                raise DeltaError("Operation lists must be explicit, including empty deletions")
            count += len(change[kind])
            if count > MAX_OPERATIONS:
                raise DeltaError("Patch operation limit exceeded")
            for operation in change[kind]:
                if kind == "update":
                    _object(operation, ("before", "after"))
                    _validate_image(operation["before"], table)
                    _validate_image(operation["after"], table)
                    image = operation["before"]
                    if _key(image, table) != _key(operation["after"], table) or image == operation["after"]:
                        raise DeltaError("Updates must retain their primary key and change their image")
                else:
                    image = operation
                    _validate_image(image, table)
                key = _key(image, table)
                if key in seen:
                    raise DeltaError("Duplicate/conflicting primary key operation")
                seen.add(key)


def _read_patch(path, expected_sha256):
    path = _path(path)
    before = _stamp(path)
    if before[2] > MAX_PATCH_BYTES:
        raise DeltaError("Patch exceeds the byte limit")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_PATCH_BYTES + 1)
    if _stamp(path) != before:
        raise DeltaError("Patch changed while reading")
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        _hash(expected_sha256)
        if sha != expected_sha256:
            raise DeltaError("Patch SHA-256 does not match the trusted transfer manifest")
    patch = _bounded_json(raw)
    _validate_patch(patch)
    return patch, sha


def _preconditions(patch, records):
    desired_sequence = None
    for table, change in zip(patch["base"]["tables"], patch["changes"]):
        rows = records[table["name"]]
        desired = dict(rows) if table["name"] == "sqlite_sequence" else None
        for kind in ("delete", "update"):
            for operation in change[kind]:
                image = operation["before"] if kind == "update" else operation
                key = _key(image, table)
                if rows.get(key) != image:
                    raise DeltaError("Row-level before-image precondition failed")
                if desired is not None:
                    del desired[key]
        for kind in ("insert", "update"):
            for operation in change[kind]:
                image = operation["after"] if kind == "update" else operation
                key = _key(image, table)
                if kind == "insert" and key in rows:
                    raise DeltaError("Insert primary key already exists")
                if desired is not None:
                    desired[key] = image
        if desired is not None:
            desired_sequence = desired
    return desired_sequence


def _insert(db, table, image):
    values, rowid = _validate_image(image, table)
    columns = list(table["columns"])
    if table["rowid"] is not None:
        columns.append(table["rowid"])
        values += (rowid,)
    db.execute("INSERT INTO " + _quote(table["name"]) + " (" + ",".join(map(_quote, columns)) + ") VALUES (" + ",".join("?" for _ in values) + ")", values)


def _apply(db, patch, records):
    sequence = _preconditions(patch, records)
    # Disable FK actions, not checks: cascades would mutate rows absent from the
    # patch. Validate the complete final state before COMMIT. Delete changed
    # images first so unique-value swaps, FK cycles and rowid swaps are lossless.
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("BEGIN IMMEDIATE")
    try:
        for table, change in zip(patch["base"]["tables"], patch["changes"]):
            if table["name"] == "sqlite_sequence":
                continue
            where = " AND ".join(_quote(col) + " IS ?" for col in table["primary_key"])
            for image in change["delete"] + [row["before"] for row in change["update"]]:
                values, _ = _validate_image(image, table)
                key = tuple(values[table["columns"].index(name)] for name in table["primary_key"])
                if db.execute("DELETE FROM " + _quote(table["name"]) + " WHERE " + where, key).rowcount != 1:
                    raise DeltaError("Row deletion precondition failed")
        for table, change in zip(patch["base"]["tables"], patch["changes"]):
            if table["name"] == "sqlite_sequence":
                continue
            for image in change["insert"] + [row["after"] for row in change["update"]]:
                _insert(db, table, image)
        if sequence is not None:
            table = next(table for table in patch["base"]["tables"] if table["name"] == "sqlite_sequence")
            db.execute('DELETE FROM "sqlite_sequence"')
            for image in sequence.values():
                _insert(db, table, image)
        if _inventory(db)[0] != patch["target"]:
            raise DeltaError("Full target logical hash/count/schema verification failed")
        db.execute("COMMIT")
        db.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def apply_delta(baseline: Path, patch_path: Path, destination: Path, *, expected_sha256=None):
    """Create only a NEW destination; verified inputs are never modified.

    Failure deletes the private staging copy and leaves destination absent.
    expected_sha256 should come from the separately verified transfer manifest.
    """
    destination = _new_output(destination)
    try:
        patch, sha = _read_patch(patch_path, expected_sha256)
        with tempfile.TemporaryDirectory(prefix=".sqlite-delta-", dir=destination.parent) as temporary:
            staged = Path(temporary) / "patched.sqlite3"
            with _snapshot(baseline) as copy:
                shutil.copyfile(copy, staged)
                os.chmod(staged, 0o600)
                with _open(staged, writable=True) as db:
                    actual, records = _inventory(db)
                    if actual != patch["base"]:
                        raise DeltaError("Full baseline logical hash/count/schema verification failed")
                    _apply(db, patch, records)
                with _open(staged) as db:
                    if _inventory(db)[0] != patch["target"]:
                        raise DeltaError("Closed target verification failed")
            # Source unchanged checks finish before the output becomes visible.
            _sidecars(staged)
            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
            _publish(staged, destination)
        return dict(_summary(patch), patch_sha256=sha)
    except (sqlite3.Error, OSError) as error:
        raise DeltaError("Cannot apply delta: " + str(error)) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Hash every table in a closed snapshot")
    inspect.add_argument("--db", type=Path, required=True)
    export = commands.add_parser("export", help="Create a typed patch from closed normalized snapshots")
    export.add_argument("--base", type=Path, required=True)
    export.add_argument("--target", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--require-empty-table", action="append", default=[])
    apply = commands.add_parser("apply", help="Apply to a verified baseline copy at a NEW path")
    apply.add_argument("--base", type=Path, required=True)
    apply.add_argument("--patch", type=Path, required=True)
    apply.add_argument("--destination", type=Path, required=True)
    apply.add_argument("--sha256", required=True, help="Trusted SHA-256 of transferred patch bytes")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_snapshot(args.db)
        elif args.command == "export":
            result = export_delta(args.base, args.target, args.output, require_empty_tables=args.require_empty_table)
        else:
            result = apply_delta(args.base, args.patch, args.destination, expected_sha256=args.sha256)
    except DeltaError as error:
        parser.exit(2, "sqlite-delta: " + str(error) + "\n")
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

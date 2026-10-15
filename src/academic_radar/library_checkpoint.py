"""Optional host-managed immutable Library checkpoints. No network or credentials.

Library upload and Site activation are separate operations, not a cross-service
transaction. A new immutable Library item must be retained before Site CAS.
All remote actions are injected callbacks; this module never invokes native tools.
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from typing import Callable

from .checkpoints import CheckpointError, _absolute, _destination, _publish, _regular, verify_checkpoint

FORMAT = "academic-radar-library-publication-v1"
MAX_ARCHIVE_BYTES = 16 * 1024 ** 3
MAX_MEMBERS = 10000
BLOCK = 1024 * 1024
HEX = re.compile(r"[a-f0-9]{64}\Z")
OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")


class RecoveryRequired(RuntimeError):
    """An external action has an uncertain outcome; reconcile before retrying."""


class PublicationConflict(RuntimeError):
    """The Site head changed. Its prior complete view must not be overwritten."""


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("Invalid publication digest")
    return value


def _opaque(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or any(ord(c) < 33 or ord(c) > 126 for c in value) or "/" in value or "\\" in value:
        raise ValueError("Invalid immutable Library identifier")
    return value


def _basename(value):
    if not isinstance(value, str) or not value or len(value) > 255 or value in (".", "..") or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value) or any(c in value for c in ("/", "\\", ":")):
        raise ValueError("Invalid archive filename")
    return value


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 255 or "\\" in value or ":" in value or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value) or any(p in ("", ".", "..") for p in value.split("/")):
        raise CheckpointError("Unsafe archive member path")
    if value not in ("manifest.json", "publication-intent.json") and not value.startswith("payload/"):
        raise CheckpointError("Unexpected archive member")
    return value


def _digest(path: Path, *, limit=MAX_ARCHIVE_BYTES):
    _regular(path)
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BLOCK), b""):
            size += len(chunk)
            if size > limit:
                raise CheckpointError("Checkpoint archive exceeds size limit")
            digest.update(chunk)
    return digest.hexdigest(), size


def verify_publication_binding(checkpoint: Path, operation_id: str, snapshot_generation: str, expected_site_head: dict) -> dict:
    """Prove the archive belongs to a staged, imported Library runtime operation."""
    if not isinstance(operation_id, str) or not OPERATION.fullmatch(operation_id):
        raise ValueError("Invalid publication operation")
    _sha(snapshot_generation); _head(expected_site_head)
    checkpoint = _absolute(checkpoint)
    verify_checkpoint(checkpoint)
    return _verify_operation(checkpoint, operation_id, snapshot_generation, expected_site_head)


def _verify_operation(checkpoint: Path, operation_id: str, snapshot_generation: str, expected_site_head: dict) -> dict:
    uri = (checkpoint / "payload" / "papers.sqlite3").as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT value FROM meta WHERE key=?", ("cloud_runtime_operation:" + operation_id,)).fetchone()
        if row is None:
            raise CheckpointError("Checkpoint lacks the publication runtime operation")
        saved = json.loads(row[0])
        if (not isinstance(saved, dict) or saved.get("operation_id") != operation_id or saved.get("phase") != "committing"
                or saved.get("backend") != "library" or saved.get("snapshot_generation") != snapshot_generation
                or saved.get("expected_head") != expected_site_head):
            raise CheckpointError("Checkpoint runtime operation does not match publication")
        run_id = saved.get("run_id")
        exported = db.execute("SELECT value FROM meta WHERE key=?", ("cloud_runtime_export:" + operation_id,)).fetchone()
        job = db.execute("SELECT * FROM agent_jobs WHERE run_id=?", (run_id,)).fetchone()
        if not isinstance(run_id, str) or exported is None or exported[0] != run_id or job is None or job["status"] != "imported":
            raise CheckpointError("Checkpoint publication requires its imported runtime job")
        def artifact(value):
            if not isinstance(value, str) or not value.startswith("state://"):
                raise CheckpointError("Publication queue/results must be included in the portable checkpoint")
            relative = _relative("payload/" + value[len("state://"):])
            path = checkpoint / relative
            _regular(path)
            if path.stat().st_size > 64 * 1024 * 1024:
                raise CheckpointError("Publication queue/results artifact exceeds size limit")
            return json.loads(path.read_bytes())
        queue, results = artifact(job["queue_path"]), artifact(job["results_path"])
        if not isinstance(queue, dict) or not isinstance(results, dict):
            raise CheckpointError("Invalid publication queue/results artifact")
        if (queue.get("run_id") != run_id or queue.get("profile_hash") != job["profile_hash"]
                or results.get("run_id") != run_id or results.get("profile_hash") != job["profile_hash"]):
            raise CheckpointError("Publication queue/results identity mismatch")
        queue_digest = hashlib.sha256(_canonical({key: value for key, value in queue.items() if key != "profile_path"})).hexdigest()
        results_digest = hashlib.sha256(_canonical(results)).hexdigest()
        if saved.get("queue_sha256") != queue_digest or saved.get("results_sha256") != results_digest:
            raise CheckpointError("Publication queue/results changed after import")
        return saved


def _publish_file(stage: Path, destination: Path):
    # A hard link publishes only a new pathname and fails if a raced destination
    # exists. The fully flushed staging file is on the same filesystem.
    os.link(stage, destination, follow_symlinks=False)
    stage.unlink()
    fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pack_checkpoint(checkpoint: Path, archive: Path, *, publication_intent: dict | None = None) -> dict:
    """Create a deterministic gzip/USTAR archive from a verified checkpoint."""
    checkpoint = _absolute(checkpoint)
    manifest = verify_checkpoint(checkpoint)
    archive = _absolute(archive)
    _basename(archive.name)
    if archive.is_relative_to(checkpoint):
        raise ValueError("Archive output must be outside the checkpoint")
    archive = _destination(archive)
    entries = {"payload/" + item["path"]: item for item in manifest["files"]}
    manifest_raw = (checkpoint / "manifest.json").read_bytes()
    entries["manifest.json"] = {"size": len(manifest_raw), "sha256": hashlib.sha256(manifest_raw).hexdigest()}
    intent_raw = None
    if publication_intent is not None:
        _intent(publication_intent)
        if publication_intent["checkpoint_id"] != manifest["checkpoint_id"]:
            raise ValueError("Publication intent does not match checkpoint")
        intent_raw = _canonical(publication_intent)
        if len(intent_raw) > 16384:
            raise ValueError("Publication intent too large")
        entries["publication-intent.json"] = {"size": len(intent_raw), "sha256": hashlib.sha256(intent_raw).hexdigest()}
    if len(entries) > MAX_MEMBERS:
        raise CheckpointError("Too many checkpoint archive members")
    fd, name = tempfile.mkstemp(prefix=".checkpoint-pack-", dir=archive.parent)
    stage = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            with gzip.GzipFile(fileobj=output, filename="", mode="wb", mtime=0, compresslevel=6) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT, encoding="utf-8") as tar:
                    for relative in sorted(entries):
                        _relative(relative)
                        path = checkpoint / relative
                        if relative != "publication-intent.json":
                            _regular(path)
                        expected = entries[relative]
                        info = tarfile.TarInfo(relative)
                        info.size, info.mode, info.mtime = expected["size"], 0o600, 0
                        digest = hashlib.sha256()
                        class Reader:
                            def read(self, size):
                                value = source.read(size)
                                digest.update(value)
                                return value
                        with (io.BytesIO(intent_raw) if relative == "publication-intent.json" else path.open("rb")) as source:
                            tar.addfile(info, Reader())
                            if source.read(1) or digest.hexdigest() != expected["sha256"]:
                                raise CheckpointError("Checkpoint changed during archive creation")
            output.flush()
            os.fsync(output.fileno())
        if verify_checkpoint(checkpoint)["checkpoint_id"] != manifest["checkpoint_id"]:
            raise CheckpointError("Checkpoint changed during archive creation")
        digest, size = _digest(stage)
        _publish_file(stage, archive)
        return {"archive_sha256": digest, "archive_size": size,
                "archive_file_name": archive.name, "checkpoint_id": manifest["checkpoint_id"]}
    finally:
        stage.unlink(missing_ok=True)


def unpack_checkpoint(archive: Path, destination: Path, *, expected_sha256: str, expected_checkpoint_id: str,
                      expected_size: int | None = None, max_expanded_bytes=MAX_ARCHIVE_BYTES,
                      expected_operation_id: str | None = None, expected_snapshot_generation: str | None = None) -> dict:
    """Extract only regular USTAR members, then verify before atomic publication.

    The parser reads raw 512-byte headers itself so PAX/GNU extension headers
    cannot request an unbounded metadata allocation inside tarfile.
    """
    archive = _absolute(archive)
    digest, size = _digest(archive)
    if digest != _sha(expected_sha256) or (expected_size is not None and size != expected_size):
        raise CheckpointError("Checkpoint archive checksum or size mismatch")
    _sha(expected_checkpoint_id)
    destination = _destination(destination)
    stage = Path(tempfile.mkdtemp(prefix=".checkpoint-unpack-", dir=destination.parent))
    seen = set()
    expanded = 0
    intent = None
    try:
        with gzip.open(archive, "rb") as stream:
            def read(length):
                nonlocal expanded
                value = stream.read(length)
                expanded += len(value)
                if expanded > max_expanded_bytes:
                    raise CheckpointError("Expanded archive exceeds size limit")
                return value
            while True:
                block = read(512)
                if len(block) != 512:
                    raise CheckpointError("Truncated checkpoint archive")
                if block == b"\0" * 512:
                    if read(512) != b"\0" * 512:
                        raise CheckpointError("Invalid archive terminator")
                    while tail := read(BLOCK):
                        if any(tail):
                            raise CheckpointError("Unexpected trailing archive data")
                    break
                info = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
                if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or info.linkname:
                    raise CheckpointError("Only regular archive members are allowed")
                relative = _relative(info.name)
                if relative in seen or len(seen) >= MAX_MEMBERS or info.size < 0 or info.size > max_expanded_bytes - expanded:
                    raise CheckpointError("Duplicate member or archive size limit exceeded")
                seen.add(relative)
                if relative == "publication-intent.json":
                    if info.size > 16384:
                        raise CheckpointError("Publication intent too large")
                    intent = _intent(json.loads(read(info.size)))
                    padding = (-info.size) % 512
                    if padding and read(padding) != b"\0" * padding:
                        raise CheckpointError("Invalid archive padding")
                    continue
                path = stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as output:
                    os.chmod(path, 0o600)
                    remaining = info.size
                    while remaining:
                        chunk = read(min(BLOCK, remaining))
                        if not chunk:
                            raise CheckpointError("Truncated checkpoint member")
                        output.write(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                padding = (-info.size) % 512
                if padding and read(padding) != b"\0" * padding:
                    raise CheckpointError("Invalid archive padding")
        manifest = verify_checkpoint(stage)
        if manifest["checkpoint_id"] != expected_checkpoint_id:
            raise CheckpointError("Checkpoint identity mismatch")
        if intent is not None and intent["checkpoint_id"] != manifest["checkpoint_id"]:
            raise CheckpointError("Publication intent checkpoint mismatch")
        if expected_operation_id is not None and (intent is None or intent["operation_id"] != expected_operation_id):
            raise CheckpointError("Publication intent operation mismatch")
        if expected_snapshot_generation is not None and (intent is None or intent["snapshot_generation"] != expected_snapshot_generation):
            raise CheckpointError("Publication intent snapshot mismatch")
        if intent is not None:
            _verify_operation(stage, intent["operation_id"], intent["snapshot_generation"], intent["expected_site_head"])
        _publish(stage, destination)
        return {**manifest, **({"publication_intent": intent} if intent is not None else {})}
    except (tarfile.TarError, UnicodeError, EOFError, gzip.BadGzipFile) as error:
        raise CheckpointError("Invalid checkpoint archive") from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _version(value: dict) -> dict:
    fields = {"library_file_id", "current_version", "file_id", "file_name"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Host must normalize immutable Library metadata")
    if type(value["current_version"]) is not int or not 0 <= value["current_version"] < 2 ** 53:
        raise ValueError("Invalid Library version; use the actual returned version")
    return {"library_file_id": _opaque(value["library_file_id"]), "current_version": value["current_version"],
            "file_id": _opaque(value["file_id"]), "file_name": _basename(value["file_name"])}


def _publication(value: dict) -> dict:
    fields = {"operation_id", "snapshot_generation", "archive_sha256", "archive_size", "checkpoint_id",
              "library_file_id", "current_version", "file_id", "file_name", "publication_id"}
    if not isinstance(value, dict) or set(value) != fields or not OPERATION.fullmatch(value.get("operation_id", "")):
        raise ValueError("Invalid Site publication")
    _version({key: value[key] for key in ("library_file_id", "current_version", "file_id", "file_name")})
    for key in ("snapshot_generation", "archive_sha256", "checkpoint_id"):
        _sha(value[key])
    if type(value["archive_size"]) is not int or not 0 < value["archive_size"] <= MAX_ARCHIVE_BYTES:
        raise ValueError("Invalid archive size")
    expected = hashlib.sha256(_canonical({key: item for key, item in value.items() if key != "publication_id"})).hexdigest()
    if value["publication_id"] != expected:
        raise ValueError("Invalid publication identity")
    return value


def _head(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"generation", "publication"} or type(value["generation"]) is not int or not 0 <= value["generation"] < 2 ** 53:
        raise ValueError("Invalid Site publication head")
    if value["generation"] == 0:
        if value["publication"] is not None:
            raise ValueError("Invalid empty Site publication head")
    else:
        _publication(value["publication"])
    return value


def _intent(value: dict) -> dict:
    fields = {"format", "operation_id", "snapshot_generation", "checkpoint_id", "expected_site_head"}
    if not isinstance(value, dict) or set(value) != fields or value["format"] != "academic-radar-library-intent-v1" or not OPERATION.fullmatch(value.get("operation_id", "")):
        raise ValueError("Invalid publication intent")
    _sha(value["snapshot_generation"]); _sha(value["checkpoint_id"]); _head(value["expected_site_head"])
    return value


def _save_receipt(path: Path, value: dict, *, create=False):
    path = _absolute(path)
    raw = _canonical(value) + b"\n"
    if len(raw) > 65536:
        raise ValueError("Publication receipt too large")
    fd, name = tempfile.mkstemp(prefix=".publication-receipt-", dir=path.parent)
    stage = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if create:
            _publish_file(stage, path)
        else:
            _regular(path)
            os.replace(stage, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        stage.unlink(missing_ok=True)


def read_receipt(path: Path) -> dict:
    path = _absolute(path)
    _regular(path)
    if path.stat().st_size > 65536:
        raise ValueError("Publication receipt too large")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or set(value) != {"format", "phase", "operation_id", "snapshot_generation", "expected_site_head", "package", "publication"} or value.get("format") != FORMAT or value.get("phase") not in {"prepared", "create_pending", "verify_pending", "archived", "activation_pending", "published"}:
        raise ValueError("Invalid publication receipt")
    if not OPERATION.fullmatch(value.get("operation_id", "")):
        raise ValueError("Invalid publication operation")
    _sha(value.get("snapshot_generation"))
    _head(value["expected_site_head"])
    package = value["package"]
    if set(package) != {"archive_sha256", "archive_size", "archive_file_name", "checkpoint_id"}:
        raise ValueError("Invalid publication package")
    _sha(package["archive_sha256"]); _sha(package["checkpoint_id"]); _basename(package["archive_file_name"])
    if type(package["archive_size"]) is not int or not 0 < package["archive_size"] <= MAX_ARCHIVE_BYTES:
        raise ValueError("Invalid archive size")
    if value.get("publication") is not None:
        publication = _publication(value["publication"])
        for field in ("archive_sha256", "archive_size", "checkpoint_id"):
            if publication[field] != package[field]:
                raise ValueError("Receipt publication does not match archive")
        if publication["operation_id"] != value["operation_id"] or publication["snapshot_generation"] != value["snapshot_generation"]:
            raise ValueError("Receipt publication does not match operation")
    elif value["phase"] in {"verify_pending", "archived", "activation_pending", "published"}:
        raise ValueError("Receipt is missing its immutable artifact")
    return value


@contextlib.contextmanager
def _locked_receipt(path: Path):
    import fcntl
    path = _absolute(path)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryRequired("Another publication owns this receipt") from None
        yield


def prepare_publication(checkpoint: Path, archive: Path, receipt: Path, *, operation_id: str,
                        snapshot_generation: str, expected_site_head: dict) -> dict:
    if not isinstance(operation_id, str) or not OPERATION.fullmatch(operation_id):
        raise ValueError("Invalid publication operation")
    _sha(snapshot_generation); _head(expected_site_head)
    verify_publication_binding(checkpoint, operation_id, snapshot_generation, expected_site_head)
    receipt = _absolute(receipt)
    if receipt.is_relative_to(_absolute(checkpoint)):
        raise ValueError("Receipt must be outside the checkpoint")
    receipt = _destination(receipt)
    intent = {"format": "academic-radar-library-intent-v1", "operation_id": operation_id,
              "snapshot_generation": snapshot_generation, "expected_site_head": expected_site_head,
              "checkpoint_id": verify_checkpoint(checkpoint)["checkpoint_id"]}
    archive = _absolute(archive)
    if archive.exists():
        # Recover the local crash window after archive publication but before
        # receipt publication, without accepting an unrelated existing archive.
        digest, size = _digest(archive)
        with tempfile.TemporaryDirectory(prefix=".publication-prepare-", dir=receipt.parent) as temporary:
            recovered = unpack_checkpoint(archive, Path(temporary) / "checkpoint", expected_sha256=digest,
                                          expected_checkpoint_id=intent["checkpoint_id"], expected_size=size,
                                          expected_operation_id=operation_id, expected_snapshot_generation=snapshot_generation)
        if recovered.get("publication_intent") != intent:
            raise ValueError("Existing archive belongs to a different publication intent")
        package = {"archive_sha256": digest, "archive_size": size, "archive_file_name": archive.name,
                   "checkpoint_id": intent["checkpoint_id"]}
    else:
        package = pack_checkpoint(checkpoint, archive, publication_intent=intent)
    result = {"format": FORMAT, "phase": "prepared", "operation_id": operation_id,
              "snapshot_generation": snapshot_generation, "expected_site_head": expected_site_head,
              "package": package, "publication": None}
    _save_receipt(receipt, result, create=True)
    return result


class LibraryCheckpointAdapter:
    """Inject authorized host operations; immutable Library creation only.

    create_archive(path, operation_id, archive_sha256) -> normalized Library metadata
    materialize_archive(metadata, empty_directory) -> path to those immutable bytes
    read_site_head() -> {generation, publication}
    activate_site(expected_head, publication) -> new head (one atomic Site CAS)

    The host MUST retain each created Library item unchanged and implement Site
    activation atomically with the staged view. These are capability requirements,
    not promises supplied by this Python object.
    """
    def __init__(self, *, create_archive: Callable, materialize_archive: Callable,
                 read_site_head: Callable, activate_site: Callable):
        self.create_archive = create_archive
        self.materialize_archive = materialize_archive
        self.read_site_head = read_site_head
        self.activate_site = activate_site

    def _record_archive(self, receipt: Path, saved: dict, metadata: dict) -> dict:
        version = _version(metadata)
        publication = {"operation_id": saved["operation_id"], "snapshot_generation": saved["snapshot_generation"],
                       **{key: saved["package"][key] for key in ("archive_sha256", "archive_size", "checkpoint_id")}, **version}
        publication["publication_id"] = hashlib.sha256(_canonical(publication)).hexdigest()
        _publication(publication)
        saved = {**saved, "phase": "verify_pending", "publication": publication}
        _save_receipt(receipt, saved)
        return saved

    def _materialize_verified(self, publication: dict, directory: Path) -> Path:
        metadata = {key: publication[key] for key in ("library_file_id", "current_version", "file_id", "file_name")}
        archive = _absolute(Path(self.materialize_archive(metadata, directory)))
        if not archive.is_relative_to(directory.resolve()):
            raise ValueError("Host materialization must stay in the isolated destination")
        digest, size = _digest(archive)
        if digest != publication["archive_sha256"] or size != publication["archive_size"]:
            raise CheckpointError("Immutable Library archive bytes do not match receipt")
        return archive

    def reconcile_created(self, receipt: Path, metadata: dict) -> dict:
        """Adopt an uncertain upload only after the host identifies and verifies it."""
        with _locked_receipt(receipt):
            saved = read_receipt(receipt)
            if saved["phase"] != "create_pending":
                raise ValueError("Only an uncertain create can be reconciled")
            saved = self._record_archive(receipt, saved, metadata)
            return self._verify_retained(receipt, saved)

    def _verify_retained(self, receipt: Path, saved: dict) -> dict:
        try:
            with tempfile.TemporaryDirectory(prefix=".library-verify-", dir=Path(receipt).parent) as temporary:
                root = Path(temporary)
                publication = saved["publication"]
                archive = self._materialize_verified(publication, root)
                unpack_checkpoint(archive, root / "verified", expected_sha256=publication["archive_sha256"],
                                  expected_checkpoint_id=publication["checkpoint_id"], expected_size=publication["archive_size"],
                                  expected_operation_id=publication["operation_id"], expected_snapshot_generation=publication["snapshot_generation"])
        except CheckpointError:
            raise
        except Exception:
            raise RecoveryRequired("Immutable archive read verification is incomplete; do not activate the Site") from None
        saved = {**saved, "phase": "archived"}
        _save_receipt(receipt, saved)
        return saved

    def publish(self, receipt: Path, archive: Path) -> dict:
        with _locked_receipt(receipt):
            saved = read_receipt(receipt)
            if saved["phase"] == "create_pending":
                raise RecoveryRequired("Library create outcome is unknown; identify the immutable file and reconcile it before retrying")
            if saved["phase"] == "prepared":
                digest, size = _digest(_absolute(archive))
                if digest != saved["package"]["archive_sha256"] or size != saved["package"]["archive_size"]:
                    raise CheckpointError("Local archive no longer matches publication receipt")
                # Check the fence before uploading, but only the later Site CAS
                # decides publication. Another publisher can still win meanwhile.
                if _head(self.read_site_head()) != saved["expected_site_head"]:
                    raise PublicationConflict("Site changed before upload; retain its complete view")
                saved = {**saved, "phase": "create_pending"}
                _save_receipt(receipt, saved)
                try:
                    metadata = self.create_archive(_absolute(archive), saved["operation_id"], digest)
                    saved = self._record_archive(receipt, saved, metadata)
                except Exception:
                    raise RecoveryRequired("Library create outcome is unknown; reconcile before retrying") from None
            if saved["phase"] == "verify_pending":
                saved = self._verify_retained(receipt, saved)
            publication = saved["publication"]
            current = _head(self.read_site_head())
            if current["publication"] and current["publication"]["publication_id"] == publication["publication_id"]:
                saved = {**saved, "phase": "published"}
                _save_receipt(receipt, saved)
                return {**saved, "site_head": current, "recovered": True}
            if current != saved["expected_site_head"]:
                raise PublicationConflict("Site changed; keep the staged Library artifact and do not overwrite the winner")
            if saved["phase"] == "published":
                raise PublicationConflict("Previously published Site head no longer matches")
            saved = {**saved, "phase": "activation_pending"}
            _save_receipt(receipt, saved)
            try:
                activated = _head(self.activate_site(saved["expected_site_head"], publication))
            except PublicationConflict:
                raise
            except Exception:
                raise RecoveryRequired("Site activation outcome is unknown; retry by reading its authoritative head") from None
            if activated["generation"] != saved["expected_site_head"]["generation"] + 1 or activated["publication"] != publication:
                raise RecoveryRequired("Site activation returned an unexpected head; reconcile before retrying")
            saved = {**saved, "phase": "published"}
            _save_receipt(receipt, saved)
            return {**saved, "site_head": activated, "recovered": False}

    def download_checkpoint(self, publication: dict, destination: Path) -> dict:
        publication = _publication(publication)
        destination = _destination(destination)
        with tempfile.TemporaryDirectory(prefix=".library-download-", dir=destination.parent) as temporary:
            archive = self._materialize_verified(publication, Path(temporary))
            return unpack_checkpoint(archive, destination, expected_sha256=publication["archive_sha256"],
                                     expected_checkpoint_id=publication["checkpoint_id"], expected_size=publication["archive_size"],
                                     expected_operation_id=publication["operation_id"], expected_snapshot_generation=publication["snapshot_generation"])


    def recover_published(self, receipt: Path, checkpoint_destination: Path) -> dict:
        """Rebuild a lost local receipt/checkpoint from the authoritative Site head."""
        receipt = _destination(receipt)
        current = _head(self.read_site_head())
        if current["publication"] is None:
            raise RecoveryRequired("No immutable Library checkpoint has been published")
        publication = current["publication"]
        manifest = self.download_checkpoint(publication, checkpoint_destination)
        intent = manifest["publication_intent"]
        if current["generation"] != intent["expected_site_head"]["generation"] + 1:
            raise ValueError("Recovered publication generation does not match its intent")
        if _head(self.read_site_head()) != current:
            raise PublicationConflict("Site changed during recovery; the downloaded checkpoint remains verified")
        saved = {"format": FORMAT, "phase": "published", "operation_id": publication["operation_id"],
                 "snapshot_generation": publication["snapshot_generation"], "expected_site_head": intent["expected_site_head"],
                 "package": {"archive_sha256": publication["archive_sha256"], "archive_size": publication["archive_size"],
                             "archive_file_name": publication["file_name"], "checkpoint_id": publication["checkpoint_id"]},
                 "publication": publication}
        _save_receipt(receipt, saved, create=True)
        return {**saved, "site_head": current, "recovered": True}

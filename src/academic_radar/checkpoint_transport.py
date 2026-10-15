"""Authenticated bounded-memory transport for immutable cloud checkpoints.

This module performs no discovery, enrollment, scheduling, or publication on
import. Callers explicitly provide a verified Sites origin and credentials.
The server atomically commits the checkpoint head and visible D1 snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from .cloud_sync import _NoCredentialRedirect, _verified_sites_origin, canonical

MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CHUNKS = 4096
MAX_FILES = 4096
_HASH = re.compile(r"^[a-f0-9]{64}$")


class CheckpointConflict(RuntimeError):
    """Another publisher committed first; reload state rather than overwrite it."""


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Invalid checkpoint digest")
    return value


def _safe_path(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > 1000
            or "\\" in value or ":" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("Invalid checkpoint path")
    return value


def validate_manifest(value: Any) -> dict:
    if (not isinstance(value, dict) or value.get("format") != "academic-radar-chunks-v1"
            or not isinstance(value.get("files"), list) or not 0 < len(value["files"]) <= MAX_FILES):
        raise ValueError("Invalid checkpoint transport manifest")
    _hash(value.get("checkpoint_id"))
    names: set[str] = set()
    chunks: dict[str, int] = {}
    count = 0
    for file in value["files"]:
        if not isinstance(file, dict):
            raise ValueError("Invalid checkpoint file")
        path = _safe_path(file.get("path"))
        if path in names:
            raise ValueError("Duplicate checkpoint file")
        names.add(path)
        _hash(file.get("sha256"))
        if type(file.get("size")) is not int or file["size"] < 0 or not isinstance(file.get("chunks"), list):
            raise ValueError("Invalid checkpoint file size")
        size = 0
        for chunk in file["chunks"]:
            if not isinstance(chunk, dict):
                raise ValueError("Invalid checkpoint chunk")
            digest = _hash(chunk.get("sha256"))
            length = chunk.get("size")
            count += 1
            if type(length) is not int or not 0 < length <= MAX_BLOB_BYTES or count > MAX_CHUNKS:
                raise ValueError("Invalid checkpoint chunk size")
            if digest in chunks and chunks[digest] != length:
                raise ValueError("Inconsistent checkpoint chunk size")
            chunks[digest] = length
            size += length
        if size != file["size"] or (not size and file["sha256"] != hashlib.sha256(b"").hexdigest()):
            raise ValueError("Checkpoint file size mismatch")
    if "manifest.json" not in names:
        raise ValueError("Checkpoint descriptor missing")
    for name in names:
        if any(str(parent) in names for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise ValueError("Checkpoint path collision")
    return value


def _head(value: Any) -> dict:
    if not isinstance(value, dict) or type(value.get("generation")) is not int or value["generation"] < 0:
        raise ValueError("Invalid checkpoint head")
    if value["generation"] == 0:
        if value.get("manifest_sha256") is not None or value.get("snapshot_generation") is not None:
            raise ValueError("Invalid empty checkpoint head")
    else:
        _hash(value.get("manifest_sha256"))
        _hash(value.get("snapshot_generation"))
    return value


class CheckpointClient:
    def __init__(self, endpoint: str, credentials: dict, *, timeout: float = 60, attempts: int = 3):
        self.endpoint = _verified_sites_origin(endpoint)
        if not isinstance(credentials, dict):
            raise ValueError("Invalid cloud credentials")
        self._headers = {"Accept": "application/json"}
        for field, header in (("sync_token", "Authorization"), ("sites_token", "OAI-Sites-Authorization")):
            token = credentials.get(field)
            if field == "sync_token" or token:
                if not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
                    raise ValueError("Cloud credentials require nonempty header-safe tokens")
                self._headers[header] = "Bearer " + token
        if type(attempts) is not int or not 1 <= attempts <= 5 or not 0 < timeout <= 300:
            raise ValueError("Invalid checkpoint connection policy")
        self.timeout = timeout
        self.attempts = attempts

    def _request(self, method: str, path: str, payload: bytes | None = None, *,
                 limit: int = MAX_MANIFEST_BYTES, missing_ok: bool = False) -> tuple[bytes, dict, int]:
        if not re.fullmatch(r"/api/state/(head|(?:blob|manifest)/[a-f0-9]{64})", path):
            raise ValueError("Invalid checkpoint request path")
        headers = dict(self._headers)
        if payload is not None:
            headers["Content-Type"] = "application/json" if path.endswith("/head") else "application/octet-stream"
        request = urllib.request.Request(self.endpoint + path, data=payload, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoCredentialRedirect())
        for attempt in range(self.attempts):
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    body = b"" if method == "HEAD" else response.read(limit + 1)
                    if len(body) > limit:
                        raise RuntimeError("Checkpoint response exceeds allowed size")
                    return body, dict(response.headers), response.status
            except urllib.error.HTTPError as error:
                error.close()
                if missing_ok and error.code == 404:
                    return b"", {}, 404
                if error.code == 409:
                    raise CheckpointConflict("Checkpoint changed or is incomplete; reload before publishing") from None
                if attempt + 1 < self.attempts and error.code in (408, 429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                # Never include server bodies, redirects, header values, or URLs.
                raise RuntimeError(f"Checkpoint request rejected (HTTP {error.code})") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt + 1 == self.attempts:
                    raise RuntimeError("Checkpoint connection unavailable") from None
                time.sleep(2 ** attempt)
        raise RuntimeError("Checkpoint connection unavailable")

    def _json(self, method: str, path: str, data: dict | None = None) -> dict:
        body, _, _ = self._request(method, path, None if data is None else canonical(data).encode())
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise RuntimeError("Invalid checkpoint server response") from None
        if not isinstance(result, dict):
            raise RuntimeError("Invalid checkpoint server response")
        return result

    def get_head(self) -> dict:
        return _head(self._json("GET", "/api/state/head").get("head"))

    def compare_and_swap_head(self, expected_head: dict, manifest_sha256: str, snapshot_generation: str, operation_id: str | None = None) -> dict:
        expected = _head(expected_head)
        manifest_sha256, snapshot_generation = _hash(manifest_sha256), _hash(snapshot_generation)
        if operation_id is not None and (not isinstance(operation_id, str) or not re.fullmatch(r"[-A-Za-z0-9_.:]{1,120}", operation_id)):
            raise ValueError("Invalid checkpoint operation ID")
        response = self._json("POST", "/api/state/head", {
            "expected_generation": expected["generation"],
            "expected_manifest_sha256": expected["manifest_sha256"],
            "manifest_sha256": manifest_sha256, "snapshot_generation": snapshot_generation,
            **({"operation_id": operation_id} if operation_id is not None else {}),
        })
        result = _head(response.get("head"))
        if (result["generation"] != expected["generation"] + 1
                or result["manifest_sha256"] != manifest_sha256 or result["snapshot_generation"] != snapshot_generation
                or result.get("operation_id") != operation_id):
            raise RuntimeError("Checkpoint commit returned an unexpected head")
        return result

    def _put(self, kind: str, data: bytes) -> tuple[str, bool]:
        digest = hashlib.sha256(data).hexdigest()
        path = f"/api/state/{kind}/{digest}"
        _, headers, status = self._request("HEAD", path, missing_ok=True)
        if status != 404:
            length = next((value for key, value in headers.items() if key.lower() == "content-length"), None)
            if length != str(len(data)):
                raise RuntimeError("Stored checkpoint object has an unexpected size")
            return digest, False
        self._request("PUT", path, data)
        return digest, True

    def upload_checkpoint(self, checkpoint_dir: Path) -> dict:
        from .checkpoints import verify_checkpoint
        checkpoint_dir = Path(checkpoint_dir)
        descriptor = verify_checkpoint(checkpoint_dir)
        manifest = {"format": "academic-radar-chunks-v1", "checkpoint_id": descriptor["checkpoint_id"], "files": []}
        uploaded = 0
        total = 0
        # The checkpoint verifier supplies an allowlist; do not upload unrelated
        # files that appeared in the directory after it was created.
        paths = ["manifest.json"] + ["payload/" + item["path"] for item in descriptor["files"]]
        if len(paths) > MAX_FILES:
            raise ValueError("Checkpoint exceeds maximum file count")
        for relative in sorted(paths):
            path = checkpoint_dir / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError("Checkpoint payload is not a regular file")
            digest = hashlib.sha256()
            size = 0
            parts = []
            with path.open("rb") as stream:
                while chunk := stream.read(MAX_BLOB_BYTES):
                    size += len(chunk)
                    digest.update(chunk)
                    chunk_id, created = self._put("blob", chunk)
                    uploaded += int(created)
                    parts.append({"sha256": chunk_id, "size": len(chunk)})
                    if sum(len(file["chunks"]) for file in manifest["files"]) + len(parts) > MAX_CHUNKS:
                        raise ValueError("Checkpoint exceeds maximum chunk count")
            manifest["files"].append({"path": relative, "sha256": digest.hexdigest(), "size": size, "chunks": parts})
            total += size
        # Detect changing files before exposing a manifest or moving the head.
        if verify_checkpoint(checkpoint_dir)["checkpoint_id"] != descriptor["checkpoint_id"]:
            raise ValueError("Checkpoint changed during upload")
        by_path = {file["path"]: file for file in manifest["files"]}
        for item in descriptor["files"]:
            transferred = by_path["payload/" + item["path"]]
            if transferred["size"] != item["size"] or transferred["sha256"] != item["sha256"]:
                raise ValueError("Checkpoint changed during upload")
        validate_manifest(manifest)
        raw = canonical(manifest).encode()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("Checkpoint transport manifest too large")
        manifest_id, _ = self._put("manifest", raw)
        return {"manifest_sha256": manifest_id, "checkpoint_id": descriptor["checkpoint_id"],
                "files": len(paths), "total_bytes": total, "uploaded_blobs": uploaded}

    def download_checkpoint(self, manifest_sha256: str, destination: Path) -> dict:
        from .checkpoints import _destination, _publish, verify_checkpoint
        manifest_sha256 = _hash(manifest_sha256)
        destination = _destination(Path(destination))
        raw, _, _ = self._request("GET", f"/api/state/manifest/{manifest_sha256}")
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            raise ValueError("Checkpoint manifest checksum mismatch")
        try:
            manifest = validate_manifest(json.loads(raw))
        except (ValueError, UnicodeError):
            raise ValueError("Invalid checkpoint transport manifest") from None
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".checkpoint-download-", dir=destination.parent))
        try:
            for file in manifest["files"]:
                path = temporary / file["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with path.open("xb") as stream:
                    os.chmod(path, 0o600)
                    for chunk in file["chunks"]:
                        data, _, _ = self._request("GET", f"/api/state/blob/{chunk['sha256']}", limit=MAX_BLOB_BYTES)
                        if len(data) != chunk["size"] or hashlib.sha256(data).hexdigest() != chunk["sha256"]:
                            raise ValueError("Checkpoint chunk checksum mismatch")
                        stream.write(data)
                        digest.update(data)
                        size += len(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if size != file["size"] or digest.hexdigest() != file["sha256"]:
                    raise ValueError("Checkpoint file checksum mismatch")
            descriptor = verify_checkpoint(temporary)
            if manifest.get("checkpoint_id") != descriptor["checkpoint_id"]:
                raise ValueError("Checkpoint identity mismatch")
            if destination.exists() or destination.is_symlink():
                raise ValueError("Checkpoint destination must not exist")
            _publish(temporary, destination)
            return descriptor
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def from_config(config_path: Path) -> CheckpointClient:
    """Load only the explicitly configured endpoint and credential file."""
    from .product import load_config, resolve_state
    config_path = Path(config_path).expanduser().resolve()
    config = load_config(config_path)
    settings = config.get("cloud_sync", {})
    if not settings.get("enabled"):
        raise ValueError("Cloud synchronization is disabled")
    endpoint = _verified_sites_origin(str(settings.get("endpoint", "")))
    state = resolve_state(config_path, config)
    credentials_path = Path(settings.get("credentials_file", str(state / "cloud-sync.json"))).expanduser()
    if not credentials_path.is_absolute():
        credentials_path = config_path.parent / credentials_path
    credentials = json.loads(credentials_path.read_text())
    return CheckpointClient(endpoint, credentials)

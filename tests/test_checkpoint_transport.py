import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from academic_radar.checkpoint_transport import (
    CheckpointClient, CheckpointConflict, MAX_BLOB_BYTES, MAX_MANIFEST_BYTES,
    validate_manifest,
)
from academic_radar.checkpoints import create_checkpoint, verify_checkpoint
from academic_radar.product import initialize_installation


class MemoryClient(CheckpointClient):
    def __init__(self):
        super().__init__("https://test.example.chatgpt.site", {"sync_token": "fake-token"})
        self.objects = {}
        self.requests = []
        self.head = {"generation": 0, "manifest_sha256": None, "snapshot_generation": None}

    def _request(self, method, path, payload=None, *, limit=MAX_MANIFEST_BYTES, missing_ok=False):
        self.requests.append((method, path, len(payload or b"")))
        if path == "/api/state/head":
            if method == "POST":
                value = json.loads(payload)
                if (value["manifest_sha256"] != self.head["manifest_sha256"]
                        or value["snapshot_generation"] != self.head["snapshot_generation"]
                        or value.get("operation_id") != self.head.get("operation_id")):
                    if (value["expected_generation"] != self.head["generation"]
                            or value["expected_manifest_sha256"] != self.head["manifest_sha256"]):
                        raise CheckpointConflict("Changed")
                    self.head = dict(generation=self.head["generation"] + 1,
                                     manifest_sha256=value["manifest_sha256"],
                                     snapshot_generation=value["snapshot_generation"],
                                     **({"operation_id": value["operation_id"]} if "operation_id" in value else {}))
            return json.dumps({"head": self.head}).encode(), {}, 200
        if method == "PUT":
            self.objects.setdefault(path, payload)
            return b'{"ok":true}', {}, 201
        if path not in self.objects:
            if missing_ok:
                return b"", {}, 404
            raise RuntimeError("Missing object")
        raw = self.objects[path]
        if len(raw) > limit and method != "HEAD":
            raise RuntimeError("Response too large")
        return (b"" if method == "HEAD" else raw), {"Content-Length": str(len(raw))}, 200


class CheckpointTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        initialize_installation(self.state)
        self.checkpoint = self.root / "checkpoint"
        self.client = MemoryClient()

    def create(self):
        return create_checkpoint(self.state, self.checkpoint)

    def test_roundtrip_chunks_deduplicate_and_preserve_full_checkpoint(self):
        with sqlite3.connect(self.state / "papers.sqlite3") as db:
            db.execute("CREATE TABLE large_state(data BLOB)")
            db.execute("INSERT INTO large_state(data) VALUES(zeroblob(?))", (MAX_BLOB_BYTES + 3000,))
        original = self.create()
        uploaded = self.client.upload_checkpoint(self.checkpoint)
        self.assertEqual(uploaded["checkpoint_id"], original["checkpoint_id"])
        puts = [row for row in self.client.requests if row[0] == "PUT" and "/blob/" in row[1]]
        self.assertTrue(any(row[2] == MAX_BLOB_BYTES for row in puts))
        self.assertTrue(all(row[2] <= MAX_BLOB_BYTES for row in puts))
        second = self.client.upload_checkpoint(self.checkpoint)
        self.assertEqual(second["manifest_sha256"], uploaded["manifest_sha256"])
        self.assertEqual(second["uploaded_blobs"], 0)
        destination = self.root / "downloaded"
        downloaded = self.client.download_checkpoint(uploaded["manifest_sha256"], destination)
        self.assertEqual(downloaded["checkpoint_id"], original["checkpoint_id"])
        self.assertEqual(verify_checkpoint(destination)["checkpoint_id"], original["checkpoint_id"])
        self.assertEqual((destination / "payload" / "papers.sqlite3").read_bytes(),
                         (self.checkpoint / "payload" / "papers.sqlite3").read_bytes())
        with self.assertRaises(FileExistsError):
            self.client.download_checkpoint(uploaded["manifest_sha256"], destination)

    def test_corrupt_blob_cannot_publish_partial_checkpoint(self):
        self.create()
        uploaded = self.client.upload_checkpoint(self.checkpoint)
        blob = next(path for path in self.client.objects if "/blob/" in path)
        self.client.objects[blob] = b"corrupted"
        destination = self.root / "rejected"
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.client.download_checkpoint(uploaded["manifest_sha256"], destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".checkpoint-download-*")))

    def test_corrupt_manifest_is_rejected(self):
        self.create()
        uploaded = self.client.upload_checkpoint(self.checkpoint)
        self.client.objects["/api/state/manifest/" + uploaded["manifest_sha256"]] = b"{}"
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.client.download_checkpoint(uploaded["manifest_sha256"], self.root / "invalid")

    def test_manifest_traversal_duplicates_and_file_directory_collisions(self):
        empty = hashlib.sha256(b"").hexdigest()
        descriptor = {"path": "manifest.json", "sha256": empty, "size": 0, "chunks": []}
        for path in ("../escape", "/absolute", "a\\b", "a//b", "a:stream", "a/./b", "a/../b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_manifest({"format": "academic-radar-chunks-v1", "checkpoint_id": "a" * 64, "files": [descriptor, dict(descriptor, path=path)]})
        for path in ("manifest.json", "manifest.json/child"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_manifest({"format": "academic-radar-chunks-v1", "checkpoint_id": "a" * 64, "files": [descriptor, dict(descriptor, path=path)]})

    def test_symlinked_destination_parent_is_rejected(self):
        self.create()
        uploaded = self.client.upload_checkpoint(self.checkpoint)
        link = self.root / "link"
        link.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.client.download_checkpoint(uploaded["manifest_sha256"], link / "new")

    def test_concurrent_empty_destination_is_never_overwritten(self):
        from academic_radar.checkpoints import _publish
        self.create()
        uploaded = self.client.upload_checkpoint(self.checkpoint)
        destination = self.root / "raced"
        def race(stage, target):
            target.mkdir()
            return _publish(stage, target)
        with patch("academic_radar.checkpoints._publish", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.client.download_checkpoint(uploaded["manifest_sha256"], destination)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(list(self.root.glob(".checkpoint-download-*")))

    def test_manifest_requires_checkpoint_identity(self):
        empty = hashlib.sha256(b"").hexdigest()
        manifest = {"format": "academic-radar-chunks-v1", "files": [
            {"path": "manifest.json", "sha256": empty, "size": 0, "chunks": []}]}
        for value in (None, "", "a" * 63, "../invalid"):
            with self.assertRaises(ValueError):
                validate_manifest(dict(manifest, checkpoint_id=value))

    def test_head_cas_and_operation_id(self):
        initial = self.client.get_head()
        desired = self.client.compare_and_swap_head(initial, "a" * 64, "b" * 64, "operation-1")
        self.assertEqual(desired["generation"], 1)
        self.assertEqual(desired["operation_id"], "operation-1")
        self.assertEqual(self.client.compare_and_swap_head(initial, "a" * 64, "b" * 64, "operation-1"), desired)
        with self.assertRaises(CheckpointConflict):
            self.client.compare_and_swap_head(initial, "c" * 64, "b" * 64, "stale")
        for invalid in ("a/b", "../test", "a" * 121, ""):
            with self.assertRaises(ValueError):
                self.client.compare_and_swap_head(desired, "c" * 64, "b" * 64, invalid)

    def test_invalid_origin_and_credentials_never_reach_network(self):
        for origin in ("http://test.chatgpt.site", "https://foreign.example", "https://test.chatgpt.site/path", "https://user:pass@test.chatgpt.site"):
            with self.assertRaises(ValueError):
                CheckpointClient(origin, {"sync_token": "safe-token"})
        for token in (None, "", "secret\nheader", "nonascii-\u03bb"):
            with self.assertRaisesRegex(ValueError, "header-safe") as raised:
                CheckpointClient("https://test.chatgpt.site", {"sync_token": token})
            if token:
                self.assertNotIn(token, str(raised.exception))

    def test_http_failures_do_not_echo_credentials_or_response(self):
        client = CheckpointClient("https://test.chatgpt.site", {"sync_token": "PRIVATE-TOKEN"}, attempts=1)
        error = urllib.error.HTTPError("https://PRIVATE-REDIRECT", 302, "PRIVATE-MESSAGE", {"Token": "PRIVATE-HEADER"}, io.BytesIO(b"PRIVATE-BODY"))
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(RuntimeError, "HTTP 302") as raised:
                client.get_head()
            self.assertNotIn("PRIVATE", str(raised.exception))
            handler = opener.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, 302, None, None, "https://foreign.example"))

    def test_response_read_is_bounded(self):
        client = CheckpointClient("https://test.chatgpt.site", {"sync_token": "safe-token"})
        with patch("urllib.request.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b"x" * 17
            with self.assertRaisesRegex(RuntimeError, "allowed size"):
                client._request("GET", "/api/state/blob/" + "a" * 64, limit=16)
            response.read.assert_called_once_with(17)


if __name__ == "__main__":
    unittest.main()

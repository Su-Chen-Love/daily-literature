"""Credential-free bridge to owner-authorized native Site tool calls.

The host supplies call_tool(name, arguments) -> decoded result. This module does
not install plugins, discover credentials, or perform HTTP. The actual host's
approved connector remains responsible for OAuth and owner identity delivery.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cloud_sync import _pull_remote, _save_sync_meta, _synchronize
from .product import load_config, resolve_state


class ManagedSiteClient:
    def __init__(self, call_tool):
        if not callable(call_tool):
            raise TypeError("A host-owned Site tool callback is required")
        self.call_tool = call_tool

    def call(self, name: str, arguments: dict) -> dict:
        result = self.call_tool(name, arguments)
        if not isinstance(result, dict) or result.get("isError") is True:
            raise RuntimeError("Managed Site tool did not return a successful object")
        # A native host may return the decoded payload or its standard MCP
        # envelope. Never parse human text as an authorization/result signal.
        if "structuredContent" in result:
            result = result["structuredContent"]
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError("Managed Site tool rejected the request")
        return result

    def request(self, _endpoint: str, path: str, _credentials: dict, data=None) -> dict:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("Managed Site bridge accepts only fixed application routes")
        if parsed.path in {"/api/sync/feedback", "/api/sync/profile"} and data is None:
            query = parse_qs(parsed.query, strict_parsing=True)
            if set(query) != {"after"} or len(query["after"]) != 1 or not query["after"][0].isdigit():
                raise ValueError("Invalid event cursor")
            name = "read_feedback" if parsed.path.endswith("feedback") else "read_profile_events"
            return self.call(name, {"after": int(query["after"][0])})
        if parsed.query or not isinstance(data, dict):
            raise ValueError("Unsupported managed Site request")
        if parsed.path in {"/api/sync/begin", "/api/sync/chunk", "/api/sync/finish"}:
            action = parsed.path.rsplit("/", 1)[1]
            if action == "begin" and len(json.dumps(data, ensure_ascii=False).encode()) > 600_000:
                # Incremental negotiation is optional. Fall back to complete
                # bounded chunks rather than exceed the doubled MCP response envelope limit.
                data = {key: value for key, value in data.items() if key != "manifest"}
            if action == "finish":
                if data.get("activate") is not False:
                    raise ValueError("Managed snapshots must be staged before checkpoint activation")
                data = {**data, "activate": False}
            return self.call("stage_snapshot", {"action": action, "data": data})
        if parsed.path == "/api/sync/ack":
            return self.call("acknowledge_snapshot", {"data": data})
        raise ValueError("Unsupported managed Site request")

    @staticmethod
    def _context(config_path: Path):
        config = load_config(config_path)
        if config.get("cloud_runtime", {}).get("role") not in {"staging", "primary"}:
            raise ValueError("Managed synchronization requires an explicit cloud runtime role")
        return config, resolve_state(config_path, config) / "papers.sqlite3"

    def pull_configured(self, config_path: Path) -> dict:
        config, db = self._context(config_path)
        result = _pull_remote(db, config, "", {}, request_fn=self.request)
        return {**result, "status": "pulled"}

    def stage_configured(self, config_path: Path) -> dict:
        config, db = self._context(config_path)
        return _synchronize(db, config, "", {}, activate=False, request_fn=self.request)

    def acknowledge_configured(self, config_path: Path, generation: str, cursor: int,
                               request_id: int, profile_cursor: int = 0) -> dict:
        _, db = self._context(config_path)
        response = self.call("acknowledge_snapshot", {"data": {
            "generation": generation, "cursor": cursor, "request_id": request_id,
            "profile_cursor": profile_cursor,
        }})
        if response.get("ok") is not True:
            raise ValueError("Managed acknowledgement could not be confirmed")
        _save_sync_meta(db, {"cloud_last_synced_generation": generation,
                            "cloud_acknowledged_cursor": cursor,
                            "cloud_acknowledged_request_id": request_id})
        return response

    def read_checkpoint_head(self) -> dict:
        return self.call("read_checkpoint_head", {})

    def activate_library_checkpoint(self, expected_head: dict, publication: dict) -> dict:
        result = self.call("commit_library_checkpoint", {
            "data": {"expected_head": expected_head, "publication": publication},
        })
        if result.get("ok") is not True or not isinstance(result.get("head"), dict):
            raise RuntimeError("Managed checkpoint activation could not be confirmed")
        return result["head"]

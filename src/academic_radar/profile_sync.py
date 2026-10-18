"""Validated owner-authored profile edits, durable preferences and file recovery.

The SQLite transaction is authoritative. A pending file marker survives a crash
between its commit and atomic profile-file replacement; every pull retries it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .engagement import _atomic_write
from .storage import connect, utc_now


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_preferences(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"relevance_threshold", "instructions"}:
        raise ValueError("Invalid cloud screening preferences")
    threshold, instructions = value["relevance_threshold"], value["instructions"]
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Invalid cloud relevance threshold")
    if not isinstance(instructions, str) or len(instructions) > 20000 or "\0" in instructions:
        raise ValueError("Invalid cloud screening instructions")
    return {"relevance_threshold": threshold, "instructions": instructions}


def screening_preferences(db, config: dict) -> dict:
    saved = db.execute("SELECT value FROM meta WHERE key='cloud_screening_preferences'").fetchone()
    value = json.loads(saved[0]) if saved else {
        "relevance_threshold": config.get("relevance_threshold", .7),
        "instructions": config.get("screening_instructions", ""),
    }
    return validate_preferences(value)


def effective_config(config: dict, db_path: Path) -> dict:
    """Return a copy with only the explicitly allowed future-screening settings."""
    result = {**config, "relevance_threshold": 0.7}
    if not db_path.exists():
        return result
    db = connect(db_path)
    try:
        prefs = screening_preferences(db, config)
        cursor = db.execute("SELECT value FROM meta WHERE key='cloud_profile_cursor'").fetchone()
        result.update(relevance_threshold=0.7,
                      screening_instructions=prefs["instructions"],
                      screening_preferences_version=int(cursor[0]) if cursor else 0)
        return result
    finally:
        db.close()


def _validated_event(event: Any) -> dict:
    if not isinstance(event, dict):
        raise ValueError("Invalid cloud profile event")
    required = {"seq", "base_version", "base_generation", "base_profile_hash", "base_preferences",
                "profile_hash", "content", "screening_preferences", "summary", "updated_at"}
    if not required <= set(event) or set(event) - required - {"version"}:
        raise ValueError("Invalid cloud profile event fields")
    if type(event["seq"]) is not int or event["seq"] < 1 or type(event["base_version"]) is not int or event["base_version"] != event["seq"] - 1:
        raise ValueError("Invalid cloud profile version")
    if "version" in event and (type(event["version"]) is not int or event["version"] != event["seq"]):
        raise ValueError("Invalid cloud profile version")
    for key in ("base_generation", "base_profile_hash", "profile_hash"):
        if not isinstance(event[key], str) or len(event[key]) != 64 or any(c not in "0123456789abcdef" for c in event[key]):
            raise ValueError("Invalid cloud profile checksum")
    for key, maximum in (("content", 100000), ("summary", 1000)):
        if not isinstance(event[key], str) or not event[key].strip() or len(event[key]) > maximum or "\0" in event[key]:
            raise ValueError("Invalid cloud profile " + key)
    if hashlib.sha256(event["content"].encode()).hexdigest() != event["profile_hash"]:
        raise ValueError("Cloud profile checksum mismatch")
    if not isinstance(event["updated_at"], str):
        raise ValueError("Invalid cloud profile timestamp")
    stamp = dt.datetime.fromisoformat(event["updated_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Cloud profile timestamp must include a timezone")
    return {**{key: event[key] for key in required},
            "base_preferences": validate_preferences(event["base_preferences"]),
            "screening_preferences": validate_preferences(event["screening_preferences"])}


def apply_remote_profiles(db, events: list[dict], cursor: int, config: dict) -> int:
    """Atomic event/cursor/profile/preferences commit; no filesystem or rejudge."""
    if not isinstance(events, list) or type(cursor) is not int or cursor < 0:
        raise ValueError("Invalid cloud profile page or cursor")
    changed = 0
    with db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT value FROM meta WHERE key='cloud_profile_cursor'").fetchone()
        previous = int(saved[0]) if saved else 0
        if cursor < previous:
            raise ValueError("Cloud profile cursor cannot move backwards")
        for raw in events:
            event = _validated_event(raw)
            serialized = _canonical(event)
            sequence = event["seq"]
            if sequence > cursor:
                raise ValueError("Cloud profile event exceeds cursor")
            if sequence <= previous:
                old = db.execute("SELECT event_json FROM applied_cloud_profile_events WHERE seq=?", (sequence,)).fetchone()
                if not old or old[0] != serialized:
                    raise ValueError("Cloud profile replay differs from the committed event")
                continue
            if sequence != previous + 1:
                raise ValueError("Cloud profile event sequence has a gap")
            active = db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
            content = active["content"] if active else ""
            if hashlib.sha256(content.encode()).hexdigest() != event["base_profile_hash"]:
                raise ValueError("Cloud profile conflicts with a newer local profile; no changes applied")
            if screening_preferences(db, config) != event["base_preferences"]:
                raise ValueError("Cloud preferences conflict with newer local preferences; no changes applied")
            if not db.execute("SELECT 1 FROM meta WHERE key='cloud_profile_file_pending'").fetchone():
                db.execute("INSERT INTO meta(key,value) VALUES('cloud_profile_file_base',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (hashlib.sha256(content.encode()).hexdigest(),))
            db.execute("UPDATE profile_versions SET status='superseded' WHERE status='active'")
            db.execute("""INSERT INTO profile_versions(profile_hash,content,status,source,change_summary,created_at,confirmed_at)
                VALUES(?,?,'active','cloud-owner',?,?,?) ON CONFLICT(profile_hash) DO UPDATE SET
                status='active',confirmed_at=excluded.confirmed_at""",
                (event["profile_hash"], event["content"], event["summary"], event["updated_at"], event["updated_at"]))
            db.execute("INSERT INTO applied_cloud_profile_events(seq,event_json,applied_at) VALUES(?,?,?)", (sequence, serialized, utc_now()))
            values = {"cloud_profile_cursor": str(sequence),
                      "cloud_screening_preferences": _canonical(event["screening_preferences"]),
                      "cloud_profile_file_pending": event["profile_hash"]}
            db.executemany("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())
            previous = sequence
            changed += 1
        if previous != cursor:
            raise ValueError("Cloud profile cursor skips unreceived events")
    return changed


def _reconcile_profile_file(db, profile_path: Path) -> bool:
    """Use the caller-owned connection without changing its journal mode."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("SELECT value FROM meta WHERE key='cloud_profile_file_pending'").fetchone()
        if not pending:
            return False
        active = db.execute("SELECT content,profile_hash FROM profile_versions WHERE status='active'").fetchone()
        if not active:
            raise ValueError("Pending cloud profile has no active profile")
        expected = active["content"].encode()
        actual = profile_path.read_bytes() if profile_path.exists() else None
        if active["profile_hash"] != pending[0] and actual != expected:
            raise ValueError("Profile changed while cloud file reconciliation was pending")
        base = db.execute("SELECT value FROM meta WHERE key='cloud_profile_file_base'").fetchone()
        if actual is not None and actual != expected and (not base or hashlib.sha256(actual).hexdigest() != base[0]):
            raise ValueError("Profile file changed while cloud file reconciliation was pending")
        if actual != expected:
            _atomic_write(profile_path, active["content"])
        db.execute("DELETE FROM meta WHERE key IN ('cloud_profile_file_pending','cloud_profile_file_base')")
    return True


def reconcile_profile_file(db_path: Path, profile_path: Path) -> bool:
    """Repair only a pending cloud edit, without overwriting unrelated user files."""
    db = connect(db_path)
    try:
        return _reconcile_profile_file(db, profile_path)
    finally:
        db.close()


def unified_profile_text(content: str, instructions: str) -> str:
    """Surface legacy preference text in the one owner-editable profile."""
    extra = instructions.strip()
    if not extra or extra in content:
        return content
    return content.rstrip() + "\n\n## 补充筛选偏好\n\n" + extra

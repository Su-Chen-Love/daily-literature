"""Feedback and explicitly confirmed research-profile version services."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .storage import connect, utc_now, upgrade_database


INTEREST_VALUES = {None, "interested", "not_interested"}
READING_VALUES = {"unread", "read", "read_later"}


def profile_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def seed_active_profile(db_path: Path, content: str, source: str = "initial") -> dict[str, Any]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        active = db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
        if active:
            return dict(active)
        now = utc_now()
        with db:
            db.execute(
                """INSERT INTO profile_versions(
                profile_hash,content,status,source,change_summary,created_at,confirmed_at
                ) VALUES(?,?,'active',?,'Initial confirmed profile',?,?)""",
                (profile_hash(content), content, source, now, now),
            )
        return dict(db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone())
    finally:
        db.close()


def active_profile(db_path: Path) -> dict[str, Any] | None:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        row = db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def create_profile_draft(
    db_path: Path, content: str, change_summary: str, source: str = "manual"
) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("Profile content must not be empty")
    if not change_summary.strip():
        raise ValueError("A change summary is required")
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        digest = profile_hash(content)
        existing = db.execute("SELECT * FROM profile_versions WHERE profile_hash=?", (digest,)).fetchone()
        if existing:
            return dict(existing)
        with db:
            db.execute(
                """INSERT INTO profile_versions(
                profile_hash,content,status,source,change_summary,created_at,confirmed_at
                ) VALUES(?,?,'draft',?,?,?,NULL)""",
                (digest, content, source, change_summary.strip(), utc_now()),
            )
        return dict(db.execute("SELECT * FROM profile_versions WHERE profile_hash=?", (digest,)).fetchone())
    finally:
        db.close()


def _atomic_write(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def confirm_profile(db_path: Path, version_id: int, profile_file: Path) -> dict[str, Any]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        # Remote reconciliation uses this same SQLite write lock. Keep the
        # file replacement, activation and any rollback inside its boundary.
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM meta WHERE key='cloud_profile_file_pending'").fetchone():
            raise ValueError("Reconcile the pending cloud profile before confirming a local profile")
        candidate = db.execute("SELECT * FROM profile_versions WHERE id=?", (version_id,)).fetchone()
        if not candidate:
            raise ValueError(f"Unknown profile version: {version_id}")
        if candidate["status"] == "active":
            return dict(candidate)
        if candidate["status"] not in {"draft", "superseded"}:
            raise ValueError("Only a draft or historical profile can be activated")
        previous_content = profile_file.read_bytes().decode("utf-8") if profile_file.exists() else None
        _atomic_write(profile_file, candidate["content"])
        try:
            db.execute("UPDATE profile_versions SET status='superseded' WHERE status='active'")
            db.execute("UPDATE profile_versions SET status='active',confirmed_at=? WHERE id=?", (utc_now(), version_id))
            db.execute(
                """UPDATE profile_review_runs SET status='accepted',updated_at=?
                WHERE profile_version_id=? AND status='suggested'""",
                (utc_now(), version_id),
            )
            if candidate["source"] == "web-single":
                db.execute("INSERT INTO meta(key,value) VALUES('cloud_screening_preferences',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (json.dumps({"relevance_threshold": 0.7, "instructions": ""}),))
            db.commit()
        except Exception:
            if previous_content is None:
                profile_file.unlink(missing_ok=True)
            else:
                _atomic_write(profile_file, previous_content)
            raise
        return dict(db.execute("SELECT * FROM profile_versions WHERE id=?", (version_id,)).fetchone())
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


def list_profiles(db_path: Path) -> list[dict[str, Any]]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        return [dict(row) for row in db.execute(
            "SELECT * FROM profile_versions ORDER BY created_at DESC,id DESC"
        )]
    finally:
        db.close()


def pending_profile_review(db_path: Path) -> dict[str, Any]:
    """Return preference changes not yet reviewed by the host model."""

    upgrade_database(db_path)
    db = connect(db_path)
    try:
        last = db.execute("SELECT created_at FROM profile_review_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        active = db.execute("SELECT confirmed_at FROM profile_versions WHERE status='active'").fetchone()
        boundary = max(
            str(last["created_at"] or "") if last else "",
            str(active["confirmed_at"] or "") if active else "",
        )
        events = [dict(row) for row in db.execute(
            """WITH ranked AS (
              SELECT e.rowid event_id,e.identity,e.interest,e.reason,e.created_at,
              ROW_NUMBER() OVER(PARTITION BY e.identity ORDER BY e.created_at DESC,e.rowid DESC) rn
              FROM feedback_events e
              WHERE e.interest IN ('interested','not_interested') AND e.created_at>?
            )
            SELECT e.event_id,e.identity,e.interest,e.reason,e.created_at,
            p.title,p.abstract,p.venue
            FROM ranked e JOIN papers p ON p.identity=e.identity
            WHERE e.rn=1 ORDER BY e.created_at,e.event_id""",
            (boundary,),
        )]
        # A daily review must not forget yesterday's evidence.  The unseen
        # events decide whether a review is due, while the current feedback
        # state supplies cumulative calibration context so repeated weak
        # signals can become a stable profile preference across several days.
        feedback_history = [dict(row) for row in db.execute(
            """SELECT f.identity,f.interest,f.reason,f.updated_at,
            p.title,p.abstract,p.venue,s.score,s.reasons AS recommendation_reason
            FROM paper_feedback f JOIN papers p ON p.identity=f.identity
            LEFT JOIN (
              SELECT * FROM (
                SELECT screenings.*,
                ROW_NUMBER() OVER(PARTITION BY identity ORDER BY screened_at DESC,rowid DESC) rn
                FROM screenings WHERE provider='codex-agent'
              ) WHERE rn=1
            ) s ON s.identity=f.identity
            WHERE f.interest IN ('interested','not_interested')
            ORDER BY f.updated_at DESC"""
        )]
        serialized = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest() if events else ""
        pending_suggestion = db.execute(
            """SELECT r.*,v.content,v.change_summary,v.id version_id FROM profile_review_runs r
            JOIN profile_versions v ON v.id=r.profile_version_id
            WHERE r.status='suggested' AND v.status='draft' ORDER BY r.updated_at DESC LIMIT 1"""
        ).fetchone()
        latest_review_row = db.execute(
            """SELECT r.*,v.change_summary FROM profile_review_runs r
            LEFT JOIN profile_versions v ON v.id=r.profile_version_id
            ORDER BY r.updated_at DESC,r.rowid DESC LIMIT 1"""
        ).fetchone()
        latest_review = dict(latest_review_row) if latest_review_row else None
        if latest_review:
            try:
                details = json.loads(latest_review.get("details_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                details = {}
            latest_review["reason"] = str(details.get("reason") or "")
        return {
            "needed": bool(events),
            "fingerprint": fingerprint,
            "feedback_count": len(events),
            "events": events,
            "feedback_history": feedback_history,
            "history_count": len(feedback_history),
            "pending_suggestion": dict(pending_suggestion) if pending_suggestion else None,
            "latest_review": latest_review,
        }
    finally:
        db.close()


def create_feedback_profile_suggestion(
    db_path: Path, fingerprint: str, content: str, change_summary: str,
) -> dict[str, Any]:
    review = pending_profile_review(db_path)
    if not review["needed"] or review["fingerprint"] != fingerprint:
        raise ValueError("反馈集合已经变化，请重新生成画像建议")
    version = create_profile_draft(db_path, content, change_summary, "feedback-ai")
    now = utc_now()
    db = connect(db_path)
    try:
        with db:
            db.execute(
                """INSERT INTO profile_review_runs(
                fingerprint,status,feedback_count,details_json,profile_version_id,created_at,updated_at
                ) VALUES(?,'suggested',?,?,?,?,?)""",
                (fingerprint, review["feedback_count"], json.dumps({
                    "events": review["events"], "feedback_history": review["feedback_history"],
                }, ensure_ascii=False),
                 version["id"], now, now),
            )
        return {"status": "suggested", "feedback_count": review["feedback_count"], "version": version}
    finally:
        db.close()


def record_profile_review_no_change(db_path: Path, fingerprint: str, reason: str) -> dict[str, Any]:
    review = pending_profile_review(db_path)
    if not review["needed"] or review["fingerprint"] != fingerprint:
        raise ValueError("反馈集合已经变化，请重新检查")
    now = utc_now()
    db = connect(db_path)
    try:
        with db:
            db.execute(
                """INSERT INTO profile_review_runs(
                fingerprint,status,feedback_count,details_json,profile_version_id,created_at,updated_at
                ) VALUES(?,'no_change',?,?,NULL,?,?)""",
                (fingerprint, review["feedback_count"], json.dumps({"reason": reason.strip()}, ensure_ascii=False), now, now),
            )
        return {"status": "no_change", "feedback_count": review["feedback_count"], "reason": reason.strip()}
    finally:
        db.close()


def dismiss_profile_suggestion(db_path: Path, version_id: int) -> dict[str, Any]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        version = db.execute("SELECT * FROM profile_versions WHERE id=?", (version_id,)).fetchone()
        if not version or version["status"] != "draft" or version["source"] != "feedback-ai":
            raise ValueError("只能忽略尚未处理的反馈画像建议")
        now = utc_now()
        with db:
            db.execute(
                """UPDATE profile_review_runs SET status='dismissed',profile_version_id=NULL,updated_at=?
                WHERE profile_version_id=?""",
                (now, version_id),
            )
            db.execute("DELETE FROM profile_versions WHERE id=?", (version_id,))
        return {"status": "dismissed", "version_id": version_id}
    finally:
        db.close()


def set_feedback(
    db_path: Path,
    identity: str,
    interest: str | None,
    reason: str,
    favorite: bool,
    reading_status: str,
) -> dict[str, Any]:
    if interest not in INTEREST_VALUES:
        raise ValueError(f"Invalid interest value: {interest}")
    if reading_status not in READING_VALUES:
        raise ValueError(f"Invalid reading status: {reading_status}")
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        if not db.execute("SELECT 1 FROM papers WHERE identity=?", (identity,)).fetchone():
            raise ValueError(f"Unknown paper identity: {identity}")
        prior = db.execute("SELECT * FROM paper_feedback WHERE identity=?", (identity,)).fetchone()
        normalized_reason = reason.strip() or None
        semantic_changed = (
            (prior["interest"] if prior else None) != interest
            or (prior["reason"] if prior else None) != normalized_reason
        )
        now = utc_now()
        with db:
            db.execute(
                """INSERT INTO paper_feedback(
                identity,interest,reason,favorite,reading_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                interest=excluded.interest,reason=excluded.reason,favorite=excluded.favorite,
                reading_status=excluded.reading_status,updated_at=excluded.updated_at""",
                (identity, interest, normalized_reason, int(favorite), reading_status, now, now),
            )
            if semantic_changed:
                db.execute(
                    "INSERT INTO feedback_events(identity,interest,reason,favorite,reading_status,created_at) VALUES(?,?,?,?,?,?)",
                    (identity, interest, normalized_reason, int(favorite), reading_status, now),
                )
                db.execute("UPDATE papers SET needs_rescreen=1,updated_at=? WHERE identity=?", (now, identity))
        return dict(db.execute("SELECT * FROM paper_feedback WHERE identity=?", (identity,)).fetchone())
    finally:
        db.close()


def feedback_examples(db_path: Path, per_class: int = 20) -> list[dict[str, Any]]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        examples: list[dict[str, Any]] = []
        for interest in ("interested", "not_interested"):
            rows = db.execute(
                """SELECT f.interest,f.reason,f.updated_at,p.identity,p.title,p.abstract,p.venue
                FROM paper_feedback f JOIN papers p ON p.identity=f.identity
                WHERE f.interest=? ORDER BY f.updated_at DESC LIMIT ?""",
                (interest, max(1, per_class)),
            ).fetchall()
            examples.extend(dict(row) for row in rows)
        examples.sort(key=lambda item: item["updated_at"], reverse=True)
        return examples
    finally:
        db.close()


def list_feedback(db_path: Path) -> list[dict[str, Any]]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        return [dict(row) for row in db.execute(
            """SELECT f.*,p.title,p.venue FROM paper_feedback f JOIN papers p ON p.identity=f.identity
            WHERE f.interest IS NOT NULL OR COALESCE(f.reason,'')!=''
              OR f.favorite=1 OR f.reading_status!='unread'
            ORDER BY f.updated_at DESC"""
        )]
    finally:
        db.close()


def clear_feedback(db_path: Path, identity: str) -> dict[str, Any]:
    """Clear current user-facing feedback while retaining internal recovery evidence."""

    upgrade_database(db_path)
    db = connect(db_path)
    try:
        if not db.execute("SELECT 1 FROM papers WHERE identity=?", (identity,)).fetchone():
            raise ValueError(f"Unknown paper identity: {identity}")
        prior = db.execute("SELECT * FROM paper_feedback WHERE identity=?", (identity,)).fetchone()
        had_feedback = bool(prior and (prior["interest"] is not None or prior["reason"]
                                       or prior["favorite"] or prior["reading_status"] != "unread"))
        now = utc_now()
        with db:
            # Keep the clear edit's timestamp so a delayed older cloud edit
            # cannot resurrect it. Neutral rows are omitted from user lists.
            db.execute("""INSERT INTO paper_feedback(
              identity,interest,reason,favorite,reading_status,created_at,updated_at
              ) VALUES(?,NULL,NULL,0,'unread',?,?) ON CONFLICT(identity) DO UPDATE SET
              interest=NULL,reason=NULL,favorite=0,reading_status='unread',updated_at=excluded.updated_at""",
                       (identity, now, now))
            if had_feedback:
                db.execute(
                    """INSERT INTO feedback_events(
                    identity,interest,reason,favorite,reading_status,created_at
                    ) VALUES(?,NULL,'用户清除当前反馈',0,'unread',?)""",
                    (identity, now),
                )
                if prior["interest"] is not None or prior["reason"]:
                    db.execute("UPDATE papers SET needs_rescreen=1,updated_at=? WHERE identity=?", (now, identity))
        return {"identity": identity, "cleared": had_feedback}
    finally:
        db.close()


def set_favorite(db_path: Path, identity: str, favorite: bool) -> dict[str, Any]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        prior = db.execute("SELECT * FROM paper_feedback WHERE identity=?", (identity,)).fetchone()
    finally:
        db.close()
    return set_feedback(
        db_path,
        identity,
        prior["interest"] if prior else None,
        prior["reason"] or "" if prior else "",
        favorite,
        prior["reading_status"] if prior else "unread",
    )

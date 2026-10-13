"""Opt-in application-record synchronization; SQLite and credentials stay local."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .recommendations import local_today, recommendation_days, recommendations_on
from .cloud_views import export_view_contexts
from .storage import connect, utc_now
from .product import load_config, resolve_state
from .governance import latest_scores_sql


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def public_source_url(value: str | None) -> str | None:
    """Keep public provenance links without collector contact or API credentials."""
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    # Identity is in the API path. Query parameters can carry mailto/API keys.
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _stamp(value: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def apply_remote_feedback(db: sqlite3.Connection, events: list[dict], cursor: int) -> int:
    """Commit a cursor and its events together; newer local edits win conflicts."""
    changed = 0
    if not isinstance(events, list) or type(cursor) is not int or cursor < 0:
        raise ValueError("Invalid cloud feedback page or cursor")
    with db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone()
        previous_cursor = int(saved[0]) if saved else 0
        if cursor < previous_cursor:
            raise ValueError("Cloud feedback cursor cannot move backwards")
        if not events and cursor == previous_cursor:
            return 0
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("Invalid cloud feedback event")
            if "seq" in event and (type(event["seq"]) is not int or not 0 < event["seq"] <= cursor):
                raise ValueError("Cloud feedback event sequence exceeds its cursor")
            identity = event["identity"]
            if not db.execute("SELECT 1 FROM papers WHERE identity=?", (identity,)).fetchone():
                raise ValueError("Cloud feedback refers to an unknown paper; cursor was not advanced")
            if event.get("interest") not in (None, "interested", "not_interested"):
                raise ValueError("Invalid cloud interest")
            if event.get("reading_status") not in ("unread", "read", "read_later"):
                raise ValueError("Invalid cloud reading status")
            if type(event.get("favorite")) is not int or event["favorite"] not in (0, 1):
                raise ValueError("Invalid cloud favorite")
            reason = str(event.get("reason") or "").strip() or None
            if event.get("interest") and not reason:
                raise ValueError("Cloud preference requires a reason")
            stamp = event["updated_at"]
            if not isinstance(stamp, str):
                raise ValueError("Invalid cloud feedback timestamp")
            remote_stamp = _stamp(stamp)
            previous = db.execute("SELECT * FROM paper_feedback WHERE identity=?", (identity,)).fetchone()
            if previous and _stamp(previous["updated_at"]) >= remote_stamp:
                continue
            semantic = (previous["interest"] if previous else None) != event["interest"] or (previous["reason"] if previous else None) != reason
            values = (identity, event["interest"], reason, event["favorite"], event["reading_status"], stamp, stamp)
            db.execute("""INSERT INTO paper_feedback(identity,interest,reason,favorite,reading_status,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET interest=excluded.interest,
              reason=excluded.reason,favorite=excluded.favorite,reading_status=excluded.reading_status,updated_at=excluded.updated_at""", values)
            if semantic:
                db.execute("INSERT INTO feedback_events(identity,interest,reason,favorite,reading_status,created_at) VALUES(?,?,?,?,?,?)", values[:6])
                db.execute("UPDATE papers SET needs_rescreen=1 WHERE identity=?", (identity,))
            changed += 1
        db.execute("INSERT INTO meta(key,value) VALUES('cloud_feedback_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(cursor),))
    return changed


def snapshot_records(db_path: Path, config: dict) -> list[dict]:
    db = connect(db_path)
    records: list[dict] = []
    def append(kind: str, key: str, value: Any) -> None:
        data = canonical(value)
        records.append({"kind": kind, "key": key, "data": data, "checksum": hashlib.sha256(data.encode()).hexdigest()})
    try:
        db.execute("BEGIN")
        profile = db.execute("SELECT content,profile_hash,confirmed_at FROM profile_versions WHERE status='active'").fetchone()
        scores = latest_scores_sql()
        query = f"""SELECT p.identity,p.doi,p.title,p.abstract,p.venue,p.published,p.published_precision,p.url,p.authors_json,
          p.abstract_source,p.abstract_source_url,p.abstract_retrieved_at,p.eligibility_status,p.publication_type,
          EXISTS(SELECT 1 FROM fulltext_files ft WHERE ft.identity=p.identity) fulltext_count,
          (SELECT ft.id FROM fulltext_files ft WHERE ft.identity=p.identity ORDER BY ft.imported_at DESC,ft.id DESC LIMIT 1) fulltext_id,
          p.needs_rescreen,s.score,s.reasons,s.confidence,s.themes_json,s.reasoning_json,s.score_dimensions_json,s.rubric_version
          FROM papers p LEFT JOIN ({scores}) s ON s.identity=p.identity AND s.rn=1 ORDER BY p.identity"""
        papers = [dict(row) for row in db.execute(query)]
        for paper in papers:
            paper["abstract_source_url"] = public_source_url(paper["abstract_source_url"])
            paper["url"] = public_source_url(paper["url"])
            append("paper", paper["identity"], paper)
        timezone = config.get("timezone", "Asia/Shanghai")
        days = recommendation_days(db, timezone)
        for day in days:
            history, batches, incomplete = recommendations_on(db, dt.date.fromisoformat(day), timezone)
            allowed = {"identity", "score", "reasons", "confidence", "themes_json", "reasoning_json", "score_dimensions_json", "rubric_version", "evidence_source"}
            append("history", day, {"papers": [{k: p[k] for k in allowed if k in p} for p in history], "batches": batches, "incomplete": incomplete})
        # Today follows the same latest completed run as the
        # local web app. Historical days retain their immutable daily union.
        latest = db.execute("""SELECT run_id,imported_at FROM agent_jobs WHERE status='imported'
          ORDER BY imported_at DESC LIMIT 1""").fetchone()
        current: list[dict] = []
        day = local_today(timezone)
        is_today = bool(latest and _stamp(latest["imported_at"]).astimezone(ZoneInfo(timezone)).date() == day)
        if is_today:
            current = [dict(row) for row in db.execute("""SELECT p.identity,s.score,s.reasons,s.confidence,
              s.themes_json,s.reasoning_json,s.score_dimensions_json,s.rubric_version FROM run_papers rp
              JOIN papers p ON p.identity=rp.identity JOIN screenings s ON s.identity=p.identity AND s.run_id=rp.run_id
              WHERE rp.run_id=? AND s.provider='codex-agent' AND p.eligibility_status='eligible'
              AND s.score>=? AND (rp.role='selected' OR (rp.role='selected_new' AND NOT EXISTS(
                SELECT 1 FROM run_papers chosen WHERE chosen.run_id=rp.run_id AND chosen.identity=rp.identity AND chosen.role='selected')))
              ORDER BY p.identity""", (latest["run_id"], config.get("relevance_threshold", .70)))]
        append("history", "current", {"day": day.isoformat(), "papers": current, "batches": int(is_today), "incomplete": False})
        for feedback in db.execute("SELECT * FROM paper_feedback ORDER BY identity"):
            append("feedback", feedback["identity"], dict(feedback))
        sources = []
        for source in config.get("sources", []):
            item = {key: source[key] for key in ("name", "type", "issn", "official_issues_url") if key in source}
            if "official_issues_url" in item:
                item["official_issues_url"] = public_source_url(item["official_issues_url"])
            health = db.execute("SELECT status,last_success_at,last_error,updated_at FROM source_health WHERE source=?", (source["name"],)).fetchone()
            item["health"] = dict(health) if health else {"status": "unknown"}
            if item["health"].get("last_error"):
                item["health"]["last_error"] = "最近采集失败，详细信息保存在本地日志"
            refresh = db.execute("""SELECT checked_at,detail FROM official_issue_checks WHERE source_name=?
              AND status='succeeded' AND issue_key LIKE 'metadata-latest-two-as-of-%'
              ORDER BY checked_at DESC LIMIT 1""", (source["name"],)).fetchone()
            if refresh:
                try:
                    evidence = json.loads(refresh["detail"])
                    if evidence.get("phase") == "latest_two_metadata_refresh":
                        item["issue_check"] = {"checked_at": refresh["checked_at"], "as_of_date": evidence["as_of_date"],
                            "mode": "publisher_deposited_metadata", "issue_keys": evidence["issue_keys"],
                            "uncertain_date": any(i.get("uncertain_date_count", 0) for i in evidence.get("issues", []))}
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
            sources.append(item)
        append("private", "profile", dict(profile) if profile else {})
        totals = dict(db.execute("SELECT COUNT(*) total,SUM(eligibility_status='eligible') eligible,SUM(COALESCE(abstract,'')='') missing_abstracts FROM papers").fetchone())
        latest_import = db.execute("SELECT MAX(imported_at) t FROM agent_jobs WHERE status='imported'").fetchone()[0]
        append("meta", "overview", {"sources": sources, "counts": totals, "days": days, "timezone": timezone,
                                   "threshold": config.get("relevance_threshold", 0.70), "last_import": latest_import})
        for page, context in export_view_contexts(db, config, db_path.parent).items():
            append("meta", "ui:" + page, context)
        return sorted(records, key=lambda record: (record["kind"], record["key"]))
    finally:
        db.close()


def _verified_sites_origin(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".chatgpt.site")
            or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Cloud synchronization requires a verified HTTPS Sites origin")
    return "https://" + parsed.hostname


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Never forward bearer credentials to a redirect destination.
        return None


def request_json(endpoint: str, path: str, credentials: dict, data: dict | None = None) -> dict:
    endpoint = _verified_sites_origin(endpoint)
    for key in ("sync_token", "sites_token"):
        value = credentials.get(key)
        if key == "sync_token" or value:
            if not isinstance(value, str) or not value or any(not 33 <= ord(c) <= 126 for c in value):
                # urllib's invalid-header error contains the header value.
                # Reject malformed credentials before it can expose a token.
                raise ValueError("Cloud credentials require nonempty header-safe tokens")
    headers = {"Authorization": "Bearer " + credentials["sync_token"], "Accept": "application/json"}
    if credentials.get("sites_token"):
        headers["OAI-Sites-Authorization"] = "Bearer " + credentials["sites_token"]
    body = None
    if data is not None:
        body = canonical(data).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(endpoint.rstrip("/") + path, data=body, headers=headers)
    opener = urllib.request.build_opener(_NoCredentialRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=45) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if attempt < 2 and error.code in (408, 429, 500, 502, 503, 504):
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                if 0 <= delay <= 20:
                    time.sleep(delay)
                    continue
            # Never log headers, credential material, redirects or server bodies.
            raise RuntimeError(f"Cloud sync request rejected (HTTP {error.code}, path {path.split('?')[0]})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise RuntimeError("Cloud sync connection unavailable; the previous complete snapshot remains active") from None
            time.sleep(2 ** attempt)
    raise RuntimeError("Cloud sync connection unavailable")


def sync_configured(config_path: Path) -> dict:
    config = load_config(config_path)
    settings = config.get("cloud_sync", {})
    if not settings.get("enabled"):
        return {"status": "disabled"}
    endpoint = _verified_sites_origin(str(settings.get("endpoint", "")))
    state = resolve_state(config_path, config)
    credentials_path = Path(settings.get("credentials_file", str(state / "cloud-sync.json"))).expanduser()
    if not credentials_path.is_absolute():
        credentials_path = config_path.expanduser().resolve().parent / credentials_path
    credentials = json.loads(credentials_path.read_text())
    lock_path = state / ".cloud-sync.lock"
    # Do not race the daily import, the periodic service or a manual retry.
    with lock_path.open("a") as lock:
        import fcntl
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        try:
            result = _synchronize(state / "papers.sqlite3", config, endpoint, credentials)
            result["synced_at"] = utc_now()
            _save_status(state, result)
            return result
        except Exception as error:
            _save_status(state, {"status": "failed", "checked_at": utc_now(), "error": type(error).__name__ + ": " + str(error)[:300]})
            raise


def _save_status(state: Path, result: dict) -> None:
    path = state / "cloud-sync-status.json"
    handle, name = tempfile.mkstemp(prefix=".cloud-sync-status-", suffix=".tmp", dir=state)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(canonical(result))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_sync_configured(config_path: Path) -> dict:
    """Run synchronization for a UI without returning credential/error details."""
    try:
        return sync_configured(config_path)
    except Exception:
        # Web responses must not surface connection exceptions, local paths
        # or credentials, including failures before the sync lock was opened.
        result = {"status": "failed", "checked_at": utc_now(),
                  "error": "Cloud synchronization failed; retry from the local update page."}
        try:
            config = load_config(config_path)
            state = resolve_state(config_path, config)
            _save_status(state, result)
        except Exception:
            pass
        return result


_background_lock = threading.Lock()
_background_threads: dict[str, threading.Thread] = {}
_background_pending: dict[str, bool] = {}


def background_sync_state(config_path: Path) -> dict:
    """Expose only whether this web process has an in-flight sync thread."""
    key = str(Path(config_path).expanduser().resolve())
    with _background_lock:
        worker = _background_threads.get(key)
        return {"running": bool(worker and worker.is_alive())}


def background_sync(config_path: Path) -> dict:
    """Queue a bounded background sync after a local edit or manual request.

    Repeated requests in the same web process share one in-flight thread. The
    existing file lock also serializes it against scheduled/CLI processes.
    """
    config_path = Path(config_path).expanduser().resolve()
    key = str(config_path)
    with _background_lock:
        previous = _background_threads.get(key)
        if previous and previous.is_alive():
            # An edit may arrive after the running sync's snapshot. Remember
            # it and take one fresh snapshot before the shared worker exits.
            _background_pending[key] = True
            return {"status": "busy"}

        def run() -> None:
            try:
                while True:
                    safe_sync_configured(config_path)
                    with _background_lock:
                        if _background_pending.pop(key, False):
                            continue
                        _background_threads.pop(key, None)
                        return
            finally:
                with _background_lock:
                    # A request can start a replacement after the old worker
                    # removes itself but before this finalizer runs.
                    if _background_threads.get(key) is threading.current_thread():
                        _background_threads.pop(key, None)
                        _background_pending.pop(key, None)

        worker = threading.Thread(target=run, name="radar-cloud-sync", daemon=True)
        _background_pending.pop(key, None)
        _background_threads[key] = worker
        worker.start()
    return {"status": "queued"}


def _save_sync_meta(db_path: Path, values: dict[str, str | int]) -> None:
    db = connect(db_path)
    try:
        with db:
            db.executemany("""INSERT INTO meta(key,value) VALUES(?,?)
              ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                           [(key, str(value)) for key, value in values.items()])
    finally:
        db.close()


def _synchronize(db_path: Path, config: dict, endpoint: str, credentials: dict) -> dict:
    db = connect(db_path)
    pulled = 0
    remote_generation = None
    remote_synced_at = None
    request_id = None
    remote_acknowledged_request = None
    remote_acknowledged_cursor = None
    try:
        saved = dict(db.execute("""SELECT key,value FROM meta WHERE key IN
          ('cloud_feedback_cursor','cloud_last_synced_generation',
           'cloud_acknowledged_request_id','cloud_acknowledged_cursor')"""))
        cursor = int(saved.get("cloud_feedback_cursor", "0"))
        while True:
            response = request_json(endpoint, f"/api/sync/feedback?after={cursor}", credentials)
            if response.get("more") and (type(response.get("cursor")) is not int or response["cursor"] <= cursor):
                raise ValueError("Cloud feedback pagination did not advance its cursor")
            pulled += apply_remote_feedback(db, response["events"], response["cursor"])
            cursor = response["cursor"]
            # The last page is the freshest view of the active snapshot. Older
            # servers omit these fields and retain begin/manifest negotiation.
            remote_generation = response.get("active_generation")
            remote_synced_at = response.get("synced_at")
            for field in ("acknowledged_request_id", "acknowledged_cursor"):
                if field in response and (type(response[field]) is not int or response[field] < 0):
                    raise ValueError("Invalid cloud synchronization acknowledgement state")
            remote_acknowledged_request = response.get("acknowledged_request_id")
            remote_acknowledged_cursor = response.get("acknowledged_cursor")
            if "request_id" in response:
                if type(response["request_id"]) is not int or response["request_id"] < 0:
                    raise ValueError("Invalid cloud synchronization request ID")
                request_id = max(request_id or 0, response["request_id"])
            if not response.get("more"):
                break
    finally:
        db.close()
    records = snapshot_records(db_path, config)
    fingerprint = "\n".join(f"{r['kind']}:{r['key']}:{r['checksum']}" for r in records)
    generation = hashlib.sha256(fingerprint.encode()).hexdigest()
    base = {"generation": generation, "records": len(records), "feedback_imported": pulled}
    if isinstance(remote_synced_at, str):
        base["remote_synced_at"] = remote_synced_at

    def complete(result: dict) -> dict:
        # Record verified activation even when acknowledging a UI request
        # subsequently fails. The next run can retry that ACK without upload.
        if saved.get("cloud_last_synced_generation") != generation:
            _save_sync_meta(db_path, {"cloud_last_synced_generation": generation})
        # Remote acknowledgements are authoritative; an ACK pointer may have
        # been restored independently of the local checkpoint. Older servers
        # omit them and continue using the durable local ACK checkpoint.
        acknowledged_request = (remote_acknowledged_request if remote_acknowledged_request is not None
                                else int(saved.get("cloud_acknowledged_request_id", "0")))
        acknowledged_cursor = (remote_acknowledged_cursor if remote_acknowledged_cursor is not None
                               else int(saved.get("cloud_acknowledged_cursor", "0")))
        if request_id is not None and (request_id > acknowledged_request or cursor > acknowledged_cursor):
            acknowledged = request_json(endpoint, "/api/sync/ack", credentials,
                                        {"request_id": request_id, "cursor": cursor,
                                         "generation": generation})
            if acknowledged.get("ok") is not True:
                raise ValueError("Cloud synchronization acknowledgement could not be confirmed")
            _save_sync_meta(db_path, {"cloud_acknowledged_request_id": request_id,
                                     "cloud_acknowledged_cursor": cursor})
            result["request_acknowledged"] = request_id
        return result

    if generation == saved.get("cloud_last_synced_generation") == remote_generation:
        return complete({"status": "unchanged", "uploaded_records": 0, **base})
    manifest = [{k: row[k] for k in ("kind", "key", "checksum")} for row in records]
    opened = request_json(endpoint, "/api/sync/begin", credentials, {"generation": generation, "count": len(records), "manifest": manifest})
    if opened.get("active"):
        return complete({"status": "unchanged", "uploaded_records": 0, **base})
    pending = records
    if "missing" in opened:
        missing = {(row["kind"], row["key"]) for row in opened["missing"]}
        pending = [row for row in records if (row["kind"], row["key"]) in missing]
    chunk: list[dict] = []
    size = 0
    for row in pending:
        row_size = len(canonical(row).encode())
        if row_size > 1500000:
            raise ValueError("Cloud snapshot record exceeds the safe upload size")
        if chunk and (len(chunk) == 40 or size + row_size > 1500000):
            request_json(endpoint, "/api/sync/chunk", credentials, {"generation": generation, "records": chunk})
            chunk, size = [], 0
        chunk.append(row)
        size += row_size
    if chunk:
        request_json(endpoint, "/api/sync/chunk", credentials, {"generation": generation, "records": chunk})
    completed = request_json(endpoint, "/api/sync/finish", credentials, {"generation": generation})
    if completed.get("generation") != generation or completed.get("count") != len(records):
        raise ValueError("Cloud snapshot verification failed; remote activation could not be confirmed")
    return complete({"status": "succeeded", "uploaded_records": len(pending), **base})

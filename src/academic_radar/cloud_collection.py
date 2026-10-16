"""Recoverable daily collection stages before a cloud queue may be frozen.

API health, issue evidence and enrichment are independent outcomes. The existing
collectors, strict official preview/import and raw-abstract precedence own their
semantics; this module only orders, journals and reports those operations.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import time
from pathlib import Path

from . import cloud_runtime as runtime
from . import official
from .collection_network import collection_network_run
from .checkpoints import _absolute, _regular, _relative
from .product import _atomic_write, load_config, resolve_state
from .storage import connect, utc_now


def offline_report():
    return {"version": 1, "status": "not_exercised", "api": {"status": "not_exercised"},
            "official": {"status": "not_exercised", "sources": []},
            "enrichment": {"status": "not_exercised"}}


def compact_report(report):
    """Do not put native response/error bodies or profile content in RPC output."""
    report = report or offline_report()
    sources = report["official"].get("sources", [])
    return {"status": report["status"],
            "api": {key: value for key, value in report["api"].items()
                    if key in {"status", "source_count", "healthy", "degraded", "failed", "collected", "new"}},
            "official": {"status": report["official"]["status"],
                         **{status: sum(item["status"] == status for item in sources) for status in
                            ("succeeded", "partial", "failed", "unsupported", "deferred", "not_applicable")},
                         "official_page_sources": sum("official_page" in item.get("evidence_types", []) for item in sources),
                         "publisher_deposited_metadata_sources": sum("publisher_deposited_metadata" in item.get("evidence_types", []) for item in sources)},
            "enrichment": {key: value for key, value in report["enrichment"].items()
                           if key in {"status", "checked", "deferred", "budget_exhausted", "updated", "type_updated", "unresolved"}}}


def latest_report(db):
    """Only allowlisted aggregate fields leave local runtime evidence files."""
    row = db.execute("""SELECT pr.details_json FROM agent_jobs aj
        JOIN pipeline_runs pr ON pr.run_id=aj.run_id
        ORDER BY aj.created_at DESC LIMIT 1""").fetchone()
    report = json.loads(row[0]).get("collection_report") if row else None
    return compact_report(report) if report else None


def coverage_detail(report):
    return ("API=" + report["api"]["status"] + "; official=" + report["official"]["status"]
            + "; enrichment=" + report["enrichment"]["status"])


def _official_status(sources):
    statuses = {item["status"] for item in sources} - {"not_applicable"}
    if not statuses:
        return "not_applicable"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "partial"


@collection_network_run
def run(config_path: Path, operation_id: str, saved: dict, monitor, *, checkpoint=None, hydrate_official=None) -> dict:
    """Resume one original head/config/date/batch, with no queue side effects.

    Optional native-D1 hooks run only after a local durable stage save, or before
    applying an official package. A checkpoint failure propagates; the caller
    must recover from its last authoritative revision. A collection interrupted
    before its durable boundary may replay idempotent upserts, not claim exactly
    once network collection. Defaults retain the original checkpoint behavior.
    """
    config = load_config(config_path)
    state = resolve_state(config_path, config)
    db_path = state / "papers.sqlite3"
    portable_config = dict(config, state_dir=".")
    profile = Path(str(config.get("profile_file", "research-profile.md")))
    if profile.is_absolute():
        portable_config["profile_file"] = profile.relative_to(state).as_posix()
    fingerprint = hashlib.sha256(runtime._canonical(portable_config).encode()).hexdigest()

    def artifact(relative):
        path = _absolute(state / _relative(relative))
        if not path.is_relative_to(state):
            raise ValueError("Collection artifact must remain within state")
        return path

    def evidence_hash(path):
        # Checkpoint restore canonicalizes JSON whitespace. Hash semantic JSON
        # (which has no installation paths) rather than its serialization.
        return hashlib.sha256(runtime._canonical(json.loads(path.read_text())).encode()).hexdigest()
    performed = False
    progress = saved.get("collection_progress")
    if progress is None:
        progress = {"version": 1, "config_sha256": fingerprint, "api": None, "official": None, "enrichment": None}
    elif progress.get("version") != 1 or progress.get("config_sha256") != fingerprint:
        raise ValueError("Collection configuration changed during a resumable operation")

    def persist(phase):
        nonlocal saved
        progress["phase"] = phase
        saved = runtime._save(db_path, operation_id, {**saved, "collection_progress": progress})
        if checkpoint is not None:
            checkpoint(saved)

    if progress["api"] is None:
        persist("api_collecting")
        code, api = runtime._invoke(monitor.collect_only, config_path, operation_id=operation_id)
        performed = bool(api.get("collection_performed", True))
        with contextlib.closing(connect(db_path)) as db:
            batch = db.execute("SELECT status,details_json FROM pipeline_runs WHERE run_id=? AND kind='collection'", (api["run_id"],)).fetchone()
            if batch is None or batch["status"] not in {"succeeded", "partial", "failed"}:
                raise ValueError("API collection has not durably completed")
            if json.loads(batch["details_json"]).get("operation_id") != operation_id:
                raise ValueError("API batch belongs to another cloud operation")
            health = [dict(row) for row in db.execute("SELECT source,status,count FROM source_runs WHERE run_id=? ORDER BY source", (api["run_id"],))]
        progress["api"] = {"status": batch["status"], "run_id": api["run_id"], "exit_code": code,
                           "collected": api["collected"], "new": api["new"], "source_count": len(config.get("sources", [])),
                           "healthy": sum(row["status"] == "ok" for row in health),
                           "degraded": sum(row["status"] == "degraded" for row in health),
                           "failed": sum(row["status"] == "failed" for row in health), "sources": health}
        persist("api_completed")

    directory = _absolute(state / "official" / ("cloud-" + operation_id))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if progress["official"] is None:
        budget = official.official_collection_budget(config)
        plan = official.build_official_plan(db_path, config)
        # Oldest attempted source goes first. Deferred/unattempted journals do
        # not starve behind the same slow early sources on every daily run.
        with contextlib.closing(connect(db_path)) as db:
            attempts = {row["key"][len("cloud_official_last_attempt:"):]: row["value"]
                        for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'cloud_official_last_attempt:%'")}
        plan["sources"].sort(key=lambda item: attempts.get(item["source_name"], ""))
        plan_path = _absolute(directory / "plan.json")
        _atomic_write(plan_path, runtime._canonical(plan))
        progress["official"] = {"plan_file": plan_path.relative_to(state).as_posix(), "plan_sha256": evidence_hash(plan_path),
            "deadline_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=budget["time_budget_seconds"])).isoformat(),
            "max_sources": budget["max_sources"], "sources": [
                {"source_name": item["source_name"], "phase": "pending"} for item in plan["sources"]]}
        persist("official_planned")
    stage = progress["official"]
    plan_path = artifact(stage["plan_file"]); _regular(plan_path)
    if evidence_hash(plan_path) != stage["plan_sha256"]:
        raise ValueError("Frozen official plan changed")
    plan = json.loads(plan_path.read_text())
    planned = {item["source_name"]: item for item in plan["sources"]}

    def conclude(entry, status, reason_code, *, details=None):
        source = planned[entry["source_name"]]
        if status in {"failed", "deferred", "unsupported"}:
            # This distinct operation marker never overwrites an issue success.
            official.record_official_failure(db_path, config, entry["source_name"],
                "cloud-latest-two-" + operation_id, source["issues_url"],
                reason_code + (": " + str(details)[:1000] if details else ""))
        entry.update(phase="completed", status=status, reason_code=reason_code)
        persist("official_collecting")

    for index, entry in enumerate(stage["sources"]):
        if entry["phase"] == "completed":
            continue
        output = _absolute(directory / (str(index) + ".json"))
        if entry["phase"] in {"pending", "collecting"}:
            remaining = (dt.datetime.fromisoformat(stage["deadline_at"]) - dt.datetime.now(dt.timezone.utc)).total_seconds()
            if index >= stage["max_sources"] or remaining <= 0:
                conclude(entry, "deferred", "official_budget_exhausted")
                continue
            entry["phase"] = "collecting"; persist("official_collecting")
            with contextlib.closing(connect(db_path)) as db, db:
                db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           ("cloud_official_last_attempt:" + entry["source_name"], utc_now()))
            performed = True
            official.collect_supported_official(db_path, config, output, entry["source_name"],
                plan=plan, deadline=time.monotonic() + remaining)
            _regular(output)
            entry.update(phase="collected", package_file=output.relative_to(state).as_posix(), package_sha256=evidence_hash(output))
            persist("official_collecting")
        output = artifact(entry["package_file"]); _regular(output)
        if evidence_hash(output) != entry["package_sha256"]:
            raise ValueError("Collected official evidence changed before import")
        package = json.loads(output.read_text())
        if not isinstance(package, dict) or package.get("as_of_date") != plan["as_of_date"]:
            raise ValueError("Official collection did not preserve the frozen plan date")
        for field in ("sources", "failures", "unsupported", "deferred"):
            items = package.get(field, [])
            if (not isinstance(items, list) or len(items) > 1
                    or any(not isinstance(item, dict) or item.get("source_name") != entry["source_name"] for item in items)):
                raise ValueError("Official package does not match its source-bound stage")
        terminal = next(((field, status) for field, status in
                         (("failures", "failed"), ("unsupported", "unsupported"), ("deferred", "deferred")) if package.get(field)), None)
        if terminal:
            field, status = terminal
            conclude(entry, status, "official_" + field, details=package[field][0].get("reason"))
            continue
        if len(package.get("sources", [])) != 1:
            conclude(entry, "failed", "official_evidence_missing")
            continue
        # Re-preview after an interruption. Existing checked issues still pass
        # the original full evidence validation; they are never blindly trusted.
        if hydrate_official is not None:
            hydrate_official(package, operation_id, index)
        preview = official.preview_official_import(db_path, config, output)
        preview_path = _absolute(directory / (str(index) + "-preview.json"))
        _atomic_write(preview_path, runtime._canonical(preview))
        entry["preview_file"] = preview_path.relative_to(state).as_posix()
        if preview["errors"]:
            entry["validation_errors"] = len(preview["errors"])
            conclude(entry, "failed", "official_preview_rejected")
            continue
        entry["phase"] = "validated"; persist("official_importing")
        applied = official.apply_official_import(db_path, preview,
            idempotency_key=operation_id + ".official." + str(index), package_sha256=entry["package_sha256"])
        issues = package["sources"][0]["issues"]
        entry.update(issues=len(issues), papers=sum(len(issue["papers"]) for issue in issues),
                     evidence_types=sorted({issue.get("issue_evidence_type", "official_page") for issue in issues}),
                     imported=applied)
        conclude(entry, "succeeded" if len(issues) == 2 else "partial",
                 "latest_two_validated" if len(issues) == 2 else "incomplete_issue_coverage")
    persist("official_completed")

    if progress["enrichment"] is None:
        persist("enrichment_running")
        from .profile_sync import effective_config
        performed = True
        enriched = monitor.run_enrichment(db_path, effective_config(config, db_path), limit=500)
        if enriched.get("status") not in {"succeeded", "partial", "failed"}:
            raise ValueError("Enrichment has not returned a terminal outcome")
        progress["enrichment"] = {key: enriched[key] for key in
            ("status", "checked", "deferred", "budget_exhausted", "updated", "type_updated", "unresolved") if key in enriched}
        persist("enrichment_completed")

    sources = [{key: value for key, value in entry.items() if key in
               {"source_name", "status", "reason_code", "issues", "papers", "evidence_types", "imported", "validation_errors"}}
               for entry in stage["sources"]]
    configured = {item["name"]: item for item in config.get("sources", [])}
    for fallback in plan["api_fallback"]:
        source = configured[fallback["source_name"]]
        sources.append({"source_name": fallback["source_name"], "status":
                        "unsupported" if source.get("type") == "crossref" and source.get("issn") else "not_applicable",
                        "reason_code": "no_verified_issue_mapping"})
    official_report = {"status": _official_status(sources), "as_of_date": plan["as_of_date"], "sources": sources}
    status = "succeeded"
    if progress["api"]["status"] != "succeeded" or official_report["status"] not in {"succeeded", "not_applicable"} or progress["enrichment"]["status"] != "succeeded":
        status = "partial"
    if progress["api"]["status"] == "failed" and not any(item["status"] in {"succeeded", "partial"} for item in sources):
        status = "failed"
    report = {"version": 1, "status": status, "api": progress["api"], "official": official_report, "enrichment": progress["enrichment"]}
    progress["phase"] = "completed"
    saved = runtime._save(db_path, operation_id, {**saved, "collection_progress": progress,
        "collection_report": report, "collection_exit_code": progress["api"]["exit_code"]})
    if checkpoint is not None:
        checkpoint(saved)
    return {**saved, "collection_performed": performed}

"""Read-only contexts for the shared local and hosted page templates.

The caller owns the SQLite connection and its snapshot transaction.  Exporting
views never opens another connection, migrates a database, runs a collector, or
copies installation configuration.  Paper cards are supplied by the hosted
renderer from the separately synchronized paper and history records.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

from .governance import latest_scores_sql
from .official import resolve_official_source
from .operations import recommendation_freshness
from .product import screening_coverage
from .recommendations import local_today, recommendation_days
from .storage import latest_schema_version


def _rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(row) for row in db.execute(sql, args)]


def _row(db: sqlite3.Connection, sql: str, args: tuple = ()) -> dict | None:
    value = db.execute(sql, args).fetchone()
    return dict(value) if value is not None else None


def _public_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return None


def _error_summary(value: Any, noun: str) -> str | None:
    """Collector errors may embed URLs, credentials, or installation paths."""
    if not value:
        return None
    match = re.search(r"\bHTTP\s+(\d{3})\b", str(value), re.I)
    return f"{noun}失败（HTTP {match.group(1)}），等待重试" if match else f"{noun}尚未完成，等待重试"


def _json_object(value: Any) -> dict:
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _quality(db: sqlite3.Connection, threshold: float) -> tuple[dict, dict]:
    """The same recommendation and feedback metrics used by the local views."""
    counts = {row["eligibility_status"]: row["n"] for row in db.execute(
        "SELECT eligibility_status,COUNT(*) n FROM papers GROUP BY eligibility_status"
    )}
    latest = latest_scores_sql()
    aggregate = _row(db, f"""SELECT COUNT(*) scored,
      SUM(CASE WHEN s.score<? THEN 1 ELSE 0 END) below_threshold,
      SUM(CASE WHEN s.score>=? THEN 1 ELSE 0 END) visible,
      SUM(CASE WHEN s.score>=? AND COALESCE(p.abstract,'')<>'' THEN 1 ELSE 0 END) abstracts
      FROM papers p JOIN ({latest}) s ON p.identity=s.identity
      WHERE p.eligibility_status='eligible'""", (threshold, threshold, threshold)) or {}
    visible = int(aggregate.get("visible") or 0)
    abstracts = int(aggregate.get("abstracts") or 0)
    quality = {
        "total": sum(counts.values()), "eligible": int(counts.get("eligible", 0)),
        "excluded": int(counts.get("excluded", 0)), "quarantine": int(counts.get("quarantine", 0)),
        "below_threshold": int(aggregate.get("below_threshold") or 0), "visible": visible,
        "exclusion_reasons": {row["reason"]: row["n"] for row in db.execute("""SELECT
          COALESCE(exclusion_reason,'未分类') reason,COUNT(*) n FROM papers
          WHERE eligibility_status<>'eligible' GROUP BY COALESCE(exclusion_reason,'未分类')""")},
        "abstracts": abstracts, "missing_abstracts": visible - abstracts,
        "abstract_percent": round(abstracts / visible * 100, 1) if visible else 0.0,
    }
    metrics_row = _row(db, f"""SELECT COUNT(*) rated,
      SUM(f.interest='interested') positive,SUM(f.interest='not_interested') negative,
      SUM(s.score>=? AND f.interest='interested') true_positive,
      SUM(s.score>=? AND f.interest='not_interested') false_positive,
      SUM(s.score<? AND f.interest='interested') false_negative,
      SUM((s.score>=? AND f.interest='interested') OR (s.score<? AND f.interest='not_interested')) agreed
      FROM paper_feedback f JOIN ({latest}) s ON s.identity=f.identity
      WHERE f.interest IN ('interested','not_interested')""",
      (threshold, threshold, threshold, threshold, threshold)) or {}
    metrics = {key: int(value or 0) for key, value in metrics_row.items()}
    recommended = metrics["true_positive"] + metrics["false_positive"]
    metrics.update({
        "agreement_percent": round(metrics["agreed"] / metrics["rated"] * 100, 1) if metrics["rated"] else None,
        "precision_percent": round(metrics["true_positive"] / recommended * 100, 1) if recommended else None,
        "recall_percent": round(metrics["true_positive"] / metrics["positive"] * 100, 1) if metrics["positive"] else None,
    })
    return quality, metrics


def _issue_evidence(detail: str) -> dict:
    raw = _json_object(detail)
    if raw.get("phase") != "latest_two_metadata_refresh":
        return {}
    evidence = {key: raw[key] for key in ("phase", "as_of_date", "evidence_type")
                if isinstance(raw.get(key), str)}
    evidence["issue_keys"] = [str(value) for value in raw.get("issue_keys", [])
                              if isinstance(value, str)] if isinstance(raw.get("issue_keys"), list) else []
    allowed = {"issue_key", "article_count", "abstract_count", "missing_abstract_count",
               "published", "published_precision", "published_precisions", "published_dates",
               "date_precision", "uncertain_date_count"}
    issues = raw.get("issues")
    evidence["issues"] = []
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict):
            continue
        item = {key: value for key, value in issue.items() if key in allowed}
        for key in ("issue_url", "evidence_url"):
            if issue.get(key):
                item[key] = _public_url(issue[key])
        evidence["issues"].append(item)
    return evidence


def _sources(db: sqlite3.Connection, configured: list[dict]) -> list[dict]:
    latest = {item["source"]: item for item in _rows(db, """SELECT * FROM (
      SELECT sr.*,ROW_NUMBER() OVER(PARTITION BY source ORDER BY finished_at DESC,rowid DESC) rn
      FROM source_runs sr) WHERE rn=1""")}
    official_counts = {item["source_name"]: item for item in _rows(db, """SELECT source_name,
      COUNT(*) issue_count,MAX(checked_at) last_checked_at,SUM(article_count) article_count
      FROM official_issue_checks WHERE status='succeeded'
      AND issue_key NOT LIKE 'metadata-latest-two-as-of-%' GROUP BY source_name""")}
    official_latest = {item["source_name"]: item for item in _rows(db, """SELECT * FROM (
      SELECT source_name,issue_key,status,detail,checked_at,
      ROW_NUMBER() OVER(PARTITION BY source_name ORDER BY checked_at DESC,rowid DESC) rn
      FROM official_issue_checks) WHERE rn=1""")}
    output = []
    source_fields = ("name", "type", "required", "issn", "openalex_id", "query_container",
                     "container_title_contains", "official_status", "official_provider",
                     "official_issues_url", "official_feed_url", "rows_per_page", "max_pages_per_source")
    for configured_source in configured:
        name = configured_source["name"]
        source = {key: configured_source[key] for key in source_fields if key in configured_source}
        for key in ("official_issues_url", "official_feed_url"):
            if key in source:
                source[key] = _public_url(source[key])
        observed = _rows(db, """SELECT DISTINCT p.identity,p.published,p.abstract,p.first_seen
          FROM observations o JOIN papers p ON p.identity=o.identity
          WHERE o.source=? OR o.source LIKE ? ESCAPE '\\'""",
          (name, name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + " / %"))
        abstracts = sum(bool(item["abstract"]) for item in observed)
        dates = [item["published"] for item in observed if item["published"]]
        seen = [item["first_seen"] for item in observed if item["first_seen"]]
        source["coverage"] = {
            "paper_count": len(observed), "abstract_count": abstracts,
            "missing_abstracts": len(observed) - abstracts,
            "abstract_percent": round(abstracts / len(observed) * 100, 1) if observed else 0,
            "oldest_published": min(dates, default=""), "newest_published": max(dates, default=""),
            "first_seen": min(seen, default=""), "last_seen": max(seen, default=""),
        }
        previous = latest.get(name)
        source["latest"] = ({key: previous[key] for key in ("run_id", "source", "status", "count", "finished_at")}
                            if previous else None)
        if source["latest"] is not None:
            source["latest"]["error"] = _error_summary(previous["error"], "来源采集")
        source["official_check"] = official_counts.get(name)
        issue = official_latest.get(name)
        source["official_latest"] = ({key: issue[key] for key in ("source_name", "issue_key", "status", "checked_at")}
                                     if issue else None)
        if source["official_latest"] is not None:
            source["official_latest"]["detail"] = _error_summary(issue["detail"], "卷期核验") if issue["status"] == "failed" else ""
            source["official_latest"]["evidence"] = _issue_evidence(issue["detail"])
        output.append(source)
    return output


def _profile(db: sqlite3.Connection) -> dict:
    versions = _rows(db, """SELECT id,profile_hash,content,status,source,change_summary,created_at,confirmed_at,
      ROW_NUMBER() OVER(ORDER BY created_at,id) version_number FROM profile_versions
      WHERE NOT(source='feedback-ai' AND status='superseded') ORDER BY created_at DESC,id DESC""")
    active = next((item for item in versions if item["status"] == "active"), None)
    last = _row(db, "SELECT created_at FROM profile_review_runs ORDER BY created_at DESC LIMIT 1")
    boundary = max(str(last["created_at"] or "") if last else "", str(active["confirmed_at"] or "") if active else "")
    unseen = int(db.execute("""SELECT COUNT(*) FROM (SELECT e.identity FROM feedback_events e
      JOIN papers p ON p.identity=e.identity WHERE e.interest IN ('interested','not_interested')
      AND e.created_at>? GROUP BY e.identity)""", (boundary,)).fetchone()[0])
    history_count = int(db.execute("SELECT COUNT(*) FROM paper_feedback WHERE interest IN ('interested','not_interested')").fetchone()[0])
    suggestion = _row(db, """SELECT r.status,r.feedback_count,r.created_at,r.updated_at,
      v.content,v.change_summary,v.id version_id FROM profile_review_runs r
      JOIN profile_versions v ON v.id=r.profile_version_id
      WHERE r.status='suggested' AND v.status='draft' ORDER BY r.updated_at DESC LIMIT 1""")
    latest = _row(db, """SELECT r.status,r.feedback_count,r.created_at,r.updated_at,r.details_json,
      r.profile_version_id,v.change_summary FROM profile_review_runs r
      LEFT JOIN profile_versions v ON v.id=r.profile_version_id ORDER BY r.updated_at DESC,r.rowid DESC LIMIT 1""")
    if latest:
        latest["reason"] = str(_json_object(latest.pop("details_json")).get("reason") or "")
    return {"versions": versions, "active": active, "profile_review": {
        "needed": bool(unseen), "feedback_count": unseen, "history_count": history_count,
        "pending_suggestion": suggestion, "latest_review": latest,
    }}


def _run_records(db: sqlite3.Connection) -> dict:
    # Diagnostic bodies and queue/output paths are deliberately never exported.
    return {
        "pipeline_runs": _rows(db, """SELECT run_id,kind,status,profile_version_id,started_at,finished_at,
          collected_count,candidate_count,relevant_count FROM pipeline_runs ORDER BY started_at DESC LIMIT 20"""),
        "agent_jobs": _rows(db, """SELECT run_id,status,exported_count,imported_count,created_at,imported_at
          FROM agent_jobs ORDER BY created_at DESC LIMIT 20"""),
        "task_runs": _rows(db, """SELECT task_id,task_type,status,total_count,completed_count,
          success_count,failure_count,created_at,started_at,finished_at FROM task_runs ORDER BY created_at DESC LIMIT 20"""),
    }


def _status(db: sqlite3.Connection, config: dict, quality: dict,
            metrics: dict, source_items: list[dict], profile: dict, freshness: dict) -> dict:
    checks = []

    def check(name: str, ok: bool, detail: str, level: str = "warning", action: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "level": level, "action": action})

    integrity = "ok" if db.execute("PRAGMA quick_check").fetchone()[0] == "ok" else "failed"
    schema = [int(row[0]) for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
    latest_schema = latest_schema_version()
    db_state = {"exists": True, "integrity": integrity, "schema_version": max(schema, default=0),
                "latest_schema_version": latest_schema, "schema_current": schema == list(range(1, latest_schema + 1))}
    check("database_integrity", integrity == "ok", "数据库完整性正常" if integrity == "ok" else "数据库需要修复", "error")
    active = profile["active"]
    # Host filesystem/service checks are runtime-local.  The shared snapshot
    # validates the authoritative confirmed profile without reading a second
    # changing resource outside this SQLite transaction.
    profile_matches = bool(active) and active["profile_hash"] == hashlib.sha256(active["content"].encode()).hexdigest()
    check("confirmed_profile", profile_matches, f"已确认版本={active['id'] if active else '无'}", "error", "在研究兴趣页确认当前画像")
    names = {source["name"] for source in source_items}
    health = {item["source"]: item for item in _rows(db, "SELECT source,status,last_success_at FROM source_health") if item["source"] in names}
    missing = sorted(names - set(health))
    failed = sorted(name for name, item in health.items() if item["status"] == "failed")
    degraded = sorted(name for name, item in health.items() if item["status"] == "degraded")
    check("source_coverage", not missing, "全部来源已有运行记录" if not missing else "尚无运行记录：" + "、".join(missing), action="执行数据库更新建立来源健康记录")
    check("source_runs", not failed, "最近运行无失败来源" if not failed else "最近失败来源：" + "、".join(failed), "error", "重试失败来源；已有文献继续保留")
    if degraded:
        check("source_degradation", False, "部分提供商降级：" + "、".join(degraded), action="稍后重试并检查元数据提供商网络状态")
    incomplete = [f"{source['name']}（{(source['official_check'] or {}).get('issue_count', 0)}/2 期）"
                  for source in source_items if resolve_official_source(source) and (source["official_check"] or {}).get("issue_count", 0) < 2]
    check("official_issue_coverage", not incomplete, "各出版来源历史上至少记录过两期；最新性、日期精度和核验方式见卷期审计" if not incomplete else "官网两期核验尚未完成：" + "、".join(incomplete), action="继续核验尚未完成的最新卷期")
    issue_failed = [source["name"] for source in source_items if source["official_latest"] and source["official_latest"]["status"] == "failed"]
    check("official_issue_failures", not issue_failed, "最近卷期核验无失败；部分通过出版商元数据复核" if not issue_failed else "官网待重试：" + "、".join(issue_failed), action="重试失败卷期；失败不影响已保存的数据")
    job = _row(db, "SELECT run_id,status FROM agent_jobs ORDER BY created_at DESC LIMIT 1")
    check("latest_semantic_job", bool(job) and job["status"] == "imported", f"最近任务状态={job['status'] if job else '尚未运行'}", action="完成待判断队列并原子导入")
    check("recommendation_freshness", not freshness["overdue"] and not freshness["pending_collection"], "最近完整更新：" + freshness["last_import"] + ("；存在尚未完成评分导入的采集" if freshness["pending_collection"] else ""), action="执行今日完整更新；本地定时任务需要电脑开机且 Codex 运行")
    coverage = screening_coverage(db)
    pending = coverage["pending"]
    check("semantic_coverage", pending == 0,
          f"可筛选论文={coverage['eligible']}；已判断={coverage['judged']}；待首次判断={pending}；已有判断保留",
          action="仅判断从未评分的新文献；不自动重评已有判断")
    check("abstract_coverage", quality["abstract_percent"] >= 70 or quality["visible"] == 0,
          f"{quality['abstract_percent']}%（{quality['abstracts']}/{quality['visible']}）", action="补全可追溯的原始摘要")
    needed = sorted((item for item in checks if not item["ok"]), key=lambda item: ({"error": 0, "warning": 1}.get(item["level"], 2), item["name"]))
    situations = []
    if quality["missing_abstracts"]:
        situations.append(f"正式文献库仍缺 {quality['missing_abstracts']} 篇摘要")
    situations.append("部分监测来源尚未成功更新" if any(not item["ok"] and item["name"].startswith(("source_", "official_issue_")) for item in checks) else "所有已配置来源都有运行记录")
    situations.append(f"有 {pending} 篇新文献待首次判断" if pending else "现有可筛选论文均已完成相关性判断")
    summary = "当前：" + "；".join(situations) + "。点击“更新数据库”复制针对这些情况生成的 Codex 任务。"
    # A portable task is actionable from either page without publishing machine paths.
    prompt = "\n\n".join([
        "请更新个人学术助手。使用本项目已配置的本地数据库和已确认研究兴趣，不使用独立模型 API。",
        "当前诊断：" + "；".join(situations) + "。",
        "先同步并合并云端反馈；采集所有已配置来源最近14天数据，核验已出版的最新两期，补全可追溯的原始摘要。官网或接口失败时保留已有数据并说明真实限制。",
        "结合完整研究兴趣与累计反馈检查画像是否需要建议；只有本人确认才能启用新画像。仅导出从未由 Codex 判断过的新文献，逐篇按当前标准判断并原子导入，每篇恰好一条结果。已有判断不因画像、反馈、摘要或量表更新自动重评；没有新文献时导入完整的空队列。",
        "推荐理由先讲清研究问题和动机，再说明作者做了什么，最后说明有证据支持的核心成果或价值。区分论文结果与拟议迁移，证据缺失必须明确，不推测未提供的结果。",
        "完成后运行安装与数据检查；有内容变化时同步云端，报告新增论文、补全摘要、推荐结果、来源失败及剩余待处理项。",
    ])
    return {"db_state": db_state, "quality": quality, "recommendation_quality": metrics,
            "checks": needed, "all_checks": checks, "healthy_count": len(checks) - len(needed),
            "pending_screening": pending, "update_summary": summary, "update_prompt": prompt,
            **_run_records(db)}


def export_view_contexts(db: sqlite3.Connection, config: dict, state: Path) -> dict[str, dict]:
    """Return all six page contexts from the caller's consistent read snapshot.

    No request/CSRF/installation path is included.  The hosted renderer adds
    its request, per-request CSRF, static URL helper, live cloud feedback, and
    selected/paginated paper-card records.  It also overlays an explicitly
    requested history date on the default-yesterday context.
    """
    if not db.in_transaction:
        raise ValueError("View export requires the caller's SQLite snapshot transaction")
    threshold = float(config.get("relevance_threshold", .70))
    timezone = str(config.get("timezone", "Asia/Shanghai"))
    quality, metrics = _quality(db, threshold)
    profile = _profile(db)
    sources = _sources(db, config.get("sources", []))
    freshness = recommendation_freshness(db, timezone)
    yesterday = local_today(timezone) - dt.timedelta(days=1)
    active = profile["active"]
    latest_job = _row(db, """SELECT run_id,profile_hash,status,exported_count,imported_count,created_at,imported_at
      FROM agent_jobs WHERE status='imported' ORDER BY imported_at DESC LIMIT 1""")
    latest_run = _row(db, """SELECT run_id,kind,status,started_at,finished_at,collected_count,candidate_count,
      relevant_count FROM pipeline_runs WHERE run_id=?""", (latest_job["run_id"],)) if latest_job else None
    totals = _row(db, """SELECT COUNT(*) papers,
      (SELECT COUNT(*) FROM paper_feedback WHERE favorite=1) favorites,
      (SELECT COUNT(*) FROM paper_feedback WHERE reading_status='read_later') read_later,
      (SELECT COUNT(*) FROM papers WHERE eligibility_status='excluded') excluded
      FROM papers WHERE eligibility_status='eligible'""")
    current_feedback = "(f.interest IS NOT NULL OR COALESCE(f.reason,'')<>'' OR f.favorite=1 OR f.reading_status<>'unread')"
    feedback_stats = _row(db, f"""SELECT COUNT(*) total,SUM(interest='interested') interested,
      SUM(interest='not_interested') not_interested,SUM(favorite) favorites,
      SUM(reading_status='read') was_read FROM paper_feedback f WHERE {current_feedback}""") or {}
    feedback_stats = {key: int(value or 0) for key, value in feedback_stats.items()}
    feedback_stats["event_count"] = int(db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0])
    feedback_items = _rows(db, f"""SELECT f.*,p.title,p.venue,p.abstract,p.doi,p.published,p.url,s.score,s.reasons
      FROM paper_feedback f JOIN papers p ON p.identity=f.identity
      LEFT JOIN ({latest_scores_sql()}) s ON s.identity=f.identity WHERE {current_feedback} ORDER BY f.updated_at DESC""")
    for item in feedback_items:
        item["url"] = _public_url(item["url"])
    common = {"display_timezone": "北京时间" if timezone == "Asia/Shanghai" else timezone,
              "timezone": timezone, "threshold": threshold}
    result = {
        "today": {"papers": [], "history_papers": [], "history_runs": 0, "history_incomplete": False,
                  "history_days": [day for day in recommendation_days(db, timezone) if day <= yesterday.isoformat()],
                  "history_date": yesterday.isoformat(), "yesterday": yesterday.isoformat(),
                  "history_previous": (yesterday-dt.timedelta(days=1)).isoformat(), "history_next": None,
                  "latest_job": latest_job, "latest_run": latest_run, "totals": totals,
                  "active_profile": active, "freshness": freshness},
        "library": {"papers": [], "q": "", "interest": "", "reading": "", "favorite": "", "sort": "score_desc",
                    "page_no": 1, "total": quality["visible"], "has_next": quality["visible"] > 24,
                    "previous_query": "", "next_query": "page_no=2&sort=score_desc"},
        "sources": {"sources": sources, "quality": quality},
        "profile": profile,
        "feedback": {"items": feedback_items, "stats": feedback_stats, "interest": "", "favorite": "", "sort": "updated"},
        "status": _status(db, config, quality, metrics, sources, profile, freshness),
    }
    return {page: {"page": page, **common, **values} for page, values in result.items()}

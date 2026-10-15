"""Publication-type governance and recoverable cleanup audits."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .storage import backup_database, connect, utc_now


ALLOWED_TYPES = {"Journal Article", "Conference Paper"}

RAW_TYPE_MAP = {
    "journal-article": ("Journal Article", "eligible", "期刊论文"),
    "research-article": ("Journal Article", "eligible", "期刊论文"),
    "original-article": ("Journal Article", "eligible", "期刊论文"),
    "review-article": ("Journal Article", "eligible", "期刊论文"),
    "proceedings-article": ("Conference Paper", "eligible", "会议论文"),
    "conference-paper": ("Conference Paper", "eligible", "会议论文"),
    "conference": ("Conference Paper", "eligible", "会议论文"),
    "editorial": ("Editorial", "excluded", "编辑性内容"),
    "paratext": ("Front/Back Matter", "excluded", "前置或后置材料"),
    "correction": ("Correction", "excluded", "更正或勘误"),
    "corrigendum": ("Correction", "excluded", "更正或勘误"),
    "erratum": ("Correction", "excluded", "更正或勘误"),
    "letter": ("Letter", "excluded", "来信或短评"),
    "correspondence": ("Letter", "excluded", "来信或短评"),
    "comment": ("Comment", "excluded", "评论性内容"),
    "commentary": ("Comment", "excluded", "评论性内容"),
    "news-and-views": ("News", "excluded", "新闻或评论性内容"),
    "news-views": ("News", "excluded", "新闻或评论性内容"),
    "research-briefing": ("Comment", "excluded", "研究简报或评论性内容"),
    "news": ("News", "excluded", "新闻或公告"),
    "book-review": ("Book Review", "excluded", "书评"),
    "posted-content": ("Other", "excluded", "非正式发表内容"),
    "reference-entry": ("Other", "excluded", "参考条目"),
    "journal-issue": ("Front/Back Matter", "excluded", "整期期刊材料"),
    "journal-volume": ("Front/Back Matter", "excluded", "整卷期刊材料"),
    "proceedings": ("Front/Back Matter", "excluded", "整本会议录"),
}

CORRECTION_TITLE_PATTERN = r"^\s*(?:corrigendum|erratum)\b|^\s*(?:(?:author|publisher)\s+)?correction(?:\s*$|\s*[:\-–—]|\s+to\b)"

NEGATIVE_TITLE_RULES = [
    (r"(?:^|[;:\-–—]\s*)\s*(editorial\s+board|editorial|editor['’]s?\s+note)\b", "Editorial", "编辑性内容"),
    (r"\bextended\s+abstracts?\b", "Extended Abstract", "扩展摘要"),
    (CORRECTION_TITLE_PATTERN, "Correction", "更正或勘误"),
    (r"^\s*(letter\s+to\s+the\s+editor|comment\s+on)\b", "Letter", "来信或评论"),
    (r"(?:^|[;:\-–—]\s*)\s*(?:a\s+)?commentary\s+on\b", "Comment", "评论性内容"),
    (r"^\s*(news|announcement)\b", "News", "新闻或公告"),
    (r"\bbook\s+review\b", "Book Review", "书评"),
    (r"\bcall\s+for\s+papers\b", "Call for Papers", "征稿通知"),
    (r"^\s*(front\s+matter|back\s+matter)\b", "Front/Back Matter", "前置或后置材料"),
    (r"^\s*in\s+memoriam\b|^\s*in\s+memory\s+of\s+.+\b(?:18|19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}\s*$", "Memorial", "纪念或悼念材料"),
]


def publication_decision(
    title: str,
    venue: str = "",
    raw_type: str = "",
    source_name: str = "",
    source_kind: str = "",
) -> dict[str, Any]:
    """Return a conservative, evidence-bearing publication decision."""

    raw = re.sub(r"[^a-z0-9]+", "-", (raw_type or "").strip().lower()).strip("-")
    evidence: list[dict[str, str]] = []
    if raw:
        evidence.append({"kind": "metadata_type", "source": source_name or "metadata", "value": raw_type})

    negative = None
    combined = (title or "").strip()
    for pattern, normalized, reason in NEGATIVE_TITLE_RULES:
        if re.search(pattern, combined, flags=re.IGNORECASE):
            negative = (normalized, reason, pattern)
            evidence.append({"kind": "title_rule", "source": "local-rule", "value": pattern})
            break

    mapped = RAW_TYPE_MAP.get(raw)
    if negative:
        normalized, reason, _ = negative
        return {
            "publication_type": normalized,
            "eligibility_status": "excluded",
            "exclusion_reason": reason,
            "evidence": evidence,
        }
    if mapped:
        normalized, status, reason = mapped
        return {
            "publication_type": normalized,
            "eligibility_status": status,
            "exclusion_reason": None if status == "eligible" else reason,
            "evidence": evidence,
        }

    # OpenAlex historically used the broad value "article". It is only a
    # positive signal when the hosting source type independently identifies a
    # journal or conference.
    if raw == "article" and source_kind.lower() in {"journal", "journals"}:
        evidence.append({"kind": "source_type", "source": source_name or "metadata", "value": source_kind})
        return {
            "publication_type": "Journal Article",
            "eligibility_status": "eligible",
            "exclusion_reason": None,
            "evidence": evidence,
        }
    if raw == "article" and source_kind.lower() in {"conference", "proceedings"}:
        evidence.append({"kind": "source_type", "source": source_name or "metadata", "value": source_kind})
        return {
            "publication_type": "Conference Paper",
            "eligibility_status": "eligible",
            "exclusion_reason": None,
            "evidence": evidence,
        }

    return {
        "publication_type": "Unknown",
        "eligibility_status": "quarantine",
        "exclusion_reason": "出版类型证据不足，等待核查",
        "evidence": evidence,
    }


def should_preserve_publication_metadata(
    prior_source: str,
    prior_status: str,
    incoming_source: str,
    incoming_status: str,
) -> bool:
    """Keep stronger type evidence without freezing an erroneous exclusion.

    Publisher classifications are more specific than registry article types.
    Crossref's DOI deposit takes precedence over OpenAlex's inferred type.
    Equal or stronger new evidence may correct an old excluded classification.
    """

    if prior_status == "quarantine":
        return False
    if incoming_status == "quarantine":
        return True
    prior = (prior_source or "").strip().lower()
    incoming = (incoming_source or "").strip().lower()
    publisher_sources = {"publisher-official", "elsevier-official-api", "publisher-deposited-metadata"}
    if prior in publisher_sources and incoming not in publisher_sources:
        return True
    return prior == "crossref" and incoming == "openalex"


def latest_scores_sql() -> str:
    return """SELECT * FROM (
      SELECT s.*,ROW_NUMBER() OVER(PARTITION BY s.identity ORDER BY s.screened_at DESC,s.rowid DESC) rn
      FROM screenings s WHERE s.provider='codex-agent'
    ) WHERE rn=1"""


def source_kind_from_evidence(value: str) -> str:
    """Recover the independently observed host kind for repeatable audits."""

    try:
        evidence = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(evidence, list):
        return ""
    for item in evidence:
        if isinstance(item, dict) and item.get("kind") == "source_type":
            return str(item.get("value") or "")
    return ""


def _cleanup_snapshot(db: sqlite3.Connection) -> tuple[list[dict[str, Any]], str]:
    """Bind a preview to all paper data, including user-supplied abstracts."""

    papers = [dict(row) for row in db.execute("SELECT * FROM papers ORDER BY identity")]
    payload = json.dumps(papers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return papers, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _review_fingerprint(reviews: list[dict[str, Any]]) -> str:
    payload = json.dumps(reviews, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cleanup_items(
    papers: list[dict[str, Any]],
    publication_reviews: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Apply explicitly reviewed records; never infer types from abstracts."""

    by_identity = {paper["identity"]: paper for paper in papers}
    reviews: dict[str, dict[str, Any]] = {}
    for review in publication_reviews or []:
        if not isinstance(review, dict):
            raise ValueError("出版类型核查记录必须为对象")
        identity = review.get("identity")
        paper = by_identity.get(identity)
        if not paper or identity in reviews:
            raise ValueError("出版类型核查记录身份不存在或重复")
        if review.get("doi") != paper["doi"] or review.get("title") != paper["title"]:
            raise ValueError("出版类型核查记录的 DOI 或标题不匹配")
        if review.get("source") not in {"crossref", "publisher-official"}:
            raise ValueError("出版类型核查须使用 Crossref 或出版商原始证据")
        source_url = str(review.get("source_url") or "")
        if not re.match(r"^https://[^/\s?#]+/", source_url):
            raise ValueError("出版类型核查缺少原始 HTTPS 来源地址")
        try:
            verified_at = dt.datetime.fromisoformat(str(review.get("verified_at") or ""))
            if verified_at.tzinfo is None:
                raise ValueError
        except ValueError:
            raise ValueError("出版类型核查缺少有时区的核验时间") from None
        reviewed = publication_decision(
            paper["title"], paper["venue"] or "", str(review.get("raw_type") or ""),
            review["source"], str(review.get("source_kind") or ""),
        )
        if reviewed["eligibility_status"] != "eligible":
            raise ValueError("出版类型恢复证据未确认合格研究条目")
        reviews[identity] = review

    items = []
    for paper in papers:
        raw_type = paper["publication_type_raw"] or ""
        source = paper["publication_type_source"] or ""
        kind = source_kind_from_evidence(paper["publication_type_evidence_json"])
        review = reviews.get(paper["identity"])
        if review:
            raw_type, source = review["raw_type"], review["source"]
            kind = str(review.get("source_kind") or "")
        decision = publication_decision(paper["title"], paper["venue"] or "", raw_type, source, kind)
        if review:
            decision["evidence"].extend([
                {"kind": "metadata_conflict", "source": paper["publication_type_source"] or "metadata",
                 "value": paper["publication_type_raw"] or "", "prior_status": paper["eligibility_status"]},
                {"kind": "verified_record", "source": source, "value": raw_type,
                 "source_url": review["source_url"], "verified_at": review["verified_at"]},
            ])
        else:
            # Source conflicts remain part of the audit on later cleanups.
            try:
                evidence = json.loads(paper["publication_type_evidence_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                evidence = []
            if isinstance(evidence, list):
                decision["evidence"].extend(item for item in evidence if isinstance(item, dict)
                                            and item.get("kind") in {"metadata_conflict", "verified_record"})
        items.append({
            "identity": paper["identity"], "doi": paper["doi"] or "",
            "publication_type_raw": raw_type, "publication_type_source": source,
            "decision": decision,
        })
    return items


def _verified_cleanup_backup(path: Path) -> str:
    path = path.expanduser().resolve()
    wal = path.with_name(path.name + "-wal")
    # Older backups retain a WAL-mode header but are complete single-file
    # snapshots. Immutable reading avoids requiring writable sidecars for
    # those archives; a populated WAL must still be read normally.
    suffix = "?mode=ro" if wal.exists() and wal.stat().st_size else "?mode=ro&immutable=1"
    check = sqlite3.connect(path.as_uri() + suffix, uri=True)
    check.row_factory = sqlite3.Row
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("清洗备份完整性检查失败")
        return _cleanup_snapshot(check)[1]
    finally:
        check.close()


def governance_stats(db_path: Path, threshold: float = 0.70) -> dict[str, Any]:
    db = connect(db_path)
    try:
        total = int(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        status_counts = {
            row["eligibility_status"]: int(row["count"])
            for row in db.execute(
                "SELECT eligibility_status,COUNT(*) count FROM papers GROUP BY eligibility_status"
            )
        }
        reasons = {
            row["reason"]: int(row["count"])
            for row in db.execute(
                """SELECT COALESCE(exclusion_reason,'未分类') reason,COUNT(*) count
                FROM papers WHERE eligibility_status<>'eligible'
                GROUP BY COALESCE(exclusion_reason,'未分类') ORDER BY count DESC"""
            )
        }
        latest = latest_scores_sql()
        below = int(
            db.execute(
                f"""SELECT COUNT(*) FROM papers p JOIN ({latest}) s ON s.identity=p.identity
                WHERE p.eligibility_status='eligible' AND s.score<?""",
                (threshold,),
            ).fetchone()[0]
        )
        eligible = int(status_counts.get("eligible", 0))
        visible = int(
            db.execute(
                f"""SELECT COUNT(*) FROM papers p JOIN ({latest}) s ON s.identity=p.identity
                WHERE p.eligibility_status='eligible' AND s.score>=?""",
                (threshold,),
            ).fetchone()[0]
        )
        abstract_row = db.execute(
            f"""SELECT COUNT(*) total,SUM(CASE WHEN COALESCE(p.abstract,'')<>'' THEN 1 ELSE 0 END) abstracts
            FROM papers p JOIN ({latest}) s ON s.identity=p.identity
            WHERE p.eligibility_status='eligible' AND s.score>=?""",
            (threshold,),
        ).fetchone()
        abstract_total = int(abstract_row["total"] or 0)
        abstract_count = int(abstract_row["abstracts"] or 0)
        return {
            "total": total,
            "eligible": eligible,
            "excluded": int(status_counts.get("excluded", 0)),
            "quarantine": int(status_counts.get("quarantine", 0)),
            "below_threshold": below,
            "visible": visible,
            "exclusion_reasons": reasons,
            "abstracts": abstract_count,
            "missing_abstracts": abstract_total - abstract_count,
            "abstract_percent": round(abstract_count / abstract_total * 100, 1) if abstract_total else 0.0,
        }
    finally:
        db.close()


def recommendation_feedback_metrics(db_path: Path, threshold: float = 0.70) -> dict[str, Any]:
    """Compare the latest recommendation with explicit user judgments."""

    db = connect(db_path)
    try:
        latest = latest_scores_sql()
        row = db.execute(
            f"""SELECT COUNT(*) rated,
            SUM(CASE WHEN f.interest='interested' THEN 1 ELSE 0 END) positive,
            SUM(CASE WHEN f.interest='not_interested' THEN 1 ELSE 0 END) negative,
            SUM(CASE WHEN s.score>=? AND f.interest='interested' THEN 1 ELSE 0 END) true_positive,
            SUM(CASE WHEN s.score>=? AND f.interest='not_interested' THEN 1 ELSE 0 END) false_positive,
            SUM(CASE WHEN s.score<? AND f.interest='interested' THEN 1 ELSE 0 END) false_negative,
            SUM(CASE WHEN (s.score>=? AND f.interest='interested') OR
                           (s.score<? AND f.interest='not_interested') THEN 1 ELSE 0 END) agreed
            FROM paper_feedback f JOIN ({latest}) s ON s.identity=f.identity
            WHERE f.interest IN ('interested','not_interested')""",
            (threshold, threshold, threshold, threshold, threshold),
        ).fetchone()
        values = {key: int(row[key] or 0) for key in row.keys()}
        recommended_rated = values["true_positive"] + values["false_positive"]
        return {
            **values,
            "agreement_percent": round(values["agreed"] / values["rated"] * 100, 1) if values["rated"] else None,
            "precision_percent": round(values["true_positive"] / recommended_rated * 100, 1) if recommended_rated else None,
            "recall_percent": round(values["true_positive"] / values["positive"] * 100, 1) if values["positive"] else None,
        }
    finally:
        db.close()


def preview_cleanup(
    db_path: Path,
    state_dir: Path,
    threshold: float,
    backup_path: Path | None = None,
    publication_reviews: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Preview changes without editing papers and record a recoverable audit."""

    state = state_dir.expanduser().resolve()
    audit_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    backup = backup_path or state / "backups" / f"cleanup-{audit_id}.sqlite3"
    if not backup.exists():
        backup_database(db_path, backup)
    backup_fingerprint = _verified_cleanup_backup(backup)
    before = governance_stats(db_path, threshold)
    db = connect(db_path)
    try:
        papers, fingerprint = _cleanup_snapshot(db)
        if fingerprint != backup_fingerprint:
            raise ValueError("清洗备份与当前论文数据不一致，请重新创建备份和预览")
        reviews = publication_reviews or []
        items = _cleanup_items(papers, reviews)
        reason_counts: Counter[str] = Counter()
        for item in items:
            decision = item["decision"]
            if decision["eligibility_status"] != "eligible":
                reason_counts[decision["exclusion_reason"] or "未分类"] += 1
        report = {
            "audit_id": audit_id,
            "created_at": utc_now(),
            "status": "preview",
            "snapshot_version": 1,
            "paper_fingerprint": fingerprint,
            "publication_reviews": reviews,
            "database": str(db_path.expanduser().resolve()),
            "backup": str(backup.expanduser().resolve()),
            "integrity": "ok",
            "threshold": threshold,
            "before": before,
            "planned": {
                "eligible": sum(1 for item in items if item["decision"]["eligibility_status"] == "eligible"),
                "excluded": sum(1 for item in items if item["decision"]["eligibility_status"] == "excluded"),
                "quarantine": sum(1 for item in items if item["decision"]["eligibility_status"] == "quarantine"),
                "reasons": dict(reason_counts),
            },
            "items": items,
            "restore": f"academic-radar db restore --backup {backup} --db {db_path} --replace",
        }
    finally:
        db.close()
    reports = state / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"cleanup-preview-{audit_id}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    db = connect(db_path)
    try:
        with db:
            db.execute(
                """INSERT INTO cleanup_audits(
                audit_id,status,backup_path,report_path,before_json,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (audit_id, "preview", str(backup), str(report_path),
                 json.dumps(dict(before, _publication_reviews_fingerprint=_review_fingerprint(reviews)), ensure_ascii=False),
                 report["created_at"]),
            )
    finally:
        db.close()
    return report | {"report_path": str(report_path)}


def apply_cleanup_preview(db_path: Path, state_dir: Path, report_path: Path) -> dict[str, Any]:
    def require_available(value):
        # pathlib collapses archive:// to archive:/; neither is a local file.
        if str(value).startswith(("archive:", "state:")):
            raise ValueError("历史清洗附件仅在原存档中；请先取回并校验备份，再重新生成预览")
    require_available(report_path)
    report = json.loads(report_path.expanduser().read_text(encoding="utf-8"))
    if report.get("status") != "preview":
        raise ValueError("清洗报告不是可应用的预览")
    if report.get("snapshot_version") != 1 or not report.get("paper_fingerprint"):
        raise ValueError("清洗预览缺少快照校验，请重新生成预览")
    require_available(report.get("database"))
    require_available(report.get("backup"))
    # An edited report cannot reactivate a deliberately disabled local-only
    # preview. Check its authoritative audit before touching any backup file.
    check = connect(db_path)
    try:
        existing = check.execute("SELECT backup_path,report_path FROM cleanup_audits WHERE audit_id=?",
                                 (report.get("audit_id"),)).fetchone()
        if existing:
            require_available(existing["backup_path"])
            require_available(existing["report_path"])
    finally:
        check.close()
    if Path(report["database"]).expanduser().resolve() != db_path.expanduser().resolve():
        raise ValueError("清洗预览属于其他数据库")
    backup = Path(report["backup"])
    if not backup.exists():
        raise FileNotFoundError("清洗预览对应的备份不存在")
    if _verified_cleanup_backup(backup) != report["paper_fingerprint"]:
        raise ValueError("清洗备份与预览快照不一致")
    db = connect(db_path)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            audit = db.execute(
                "SELECT status,backup_path,before_json FROM cleanup_audits WHERE audit_id=?",
                (report["audit_id"],),
            ).fetchone()
            if not audit or audit["status"] != "preview":
                raise ValueError("清洗预览不存在或已经应用，请重新生成预览")
            require_available(audit["backup_path"])
            if Path(audit["backup_path"]).expanduser().resolve() != backup.expanduser().resolve():
                raise ValueError("清洗预览与审计记录的备份不一致")
            reviews = report.get("publication_reviews", [])
            audit_review_hash = json.loads(audit["before_json"]).get("_publication_reviews_fingerprint")
            if audit_review_hash != _review_fingerprint(reviews) and (audit_review_hash or reviews):
                raise ValueError("出版类型核查证据在预览后改变，请重新生成预览")
            papers, fingerprint = _cleanup_snapshot(db)
            if fingerprint != report["paper_fingerprint"]:
                raise ValueError("论文数据已在预览后改变，请重新生成清洗预览")
            if report["items"] != _cleanup_items(papers, reviews):
                raise ValueError("清洗决策与当前规则不一致，请重新生成预览")
            # Feedback and other private history can change without editing
            # papers. Preserve their latest state immediately before applying.
            apply_backup = Path(state_dir).expanduser().resolve() / "backups" / f"pre-cleanup-apply-{report['audit_id']}.sqlite3"
            backup_database(db_path, apply_backup)
            report["preview_backup"] = str(backup.expanduser().resolve())
            report["backup"] = str(apply_backup)
            report["restore"] = f"academic-radar db restore --backup {apply_backup} --db {db_path} --replace"
            previous = {paper["identity"]: paper for paper in papers}
            applied_at = utc_now()
            for item in report["items"]:
                decision = item["decision"]
                prior = previous[item["identity"]]
                changed = any(prior[key] != decision[key] for key in (
                    "publication_type", "eligibility_status", "exclusion_reason",
                )) or any(prior[key] != item[key] for key in ("publication_type_raw", "publication_type_source"))
                db.execute(
                    """UPDATE papers SET publication_type=?,eligibility_status=?,exclusion_reason=?,
                    publication_type_evidence_json=?,publication_type_raw=?,publication_type_source=?,
                    needs_rescreen=CASE WHEN ? THEN 1 ELSE needs_rescreen END,
                    updated_at=CASE WHEN ? THEN ? ELSE updated_at END WHERE identity=?""",
                    (
                        decision["publication_type"], decision["eligibility_status"],
                        decision.get("exclusion_reason"), json.dumps(decision.get("evidence", []), ensure_ascii=False),
                        item["publication_type_raw"], item["publication_type_source"],
                        changed, changed, applied_at, item["identity"],
                    ),
                )
            db.execute(
                "UPDATE cleanup_audits SET status='applied',applied_at=?,backup_path=? WHERE audit_id=? AND status='preview'",
                (applied_at, str(apply_backup), report["audit_id"]),
            )
    finally:
        db.close()
    after = governance_stats(db_path, float(report["threshold"]))
    report["status"] = "applied"
    report["applied_at"] = applied_at
    report["after"] = after
    applied_path = Path(state_dir).expanduser().resolve() / "reports" / f"cleanup-applied-{report['audit_id']}.json"
    applied_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    db = connect(db_path)
    try:
        with db:
            db.execute(
                "UPDATE cleanup_audits SET report_path=?,after_json=? WHERE audit_id=?",
                (str(applied_path), json.dumps(after, ensure_ascii=False), report["audit_id"]),
            )
    finally:
        db.close()
    return report | {"report_path": str(applied_path)}

"""User-facing product helpers for setup, source discovery, and paper assets."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .engagement import seed_active_profile
from .governance import CORRECTION_TITLE_PATTERN, publication_decision
from .storage import backup_database, connect, database_status, latest_schema_version, upgrade_database, utc_now


DEFAULT_STATE_DIR = Path("~/.local/share/personal-academic-radar")
MAX_PDF_BYTES = 50 * 1024 * 1024
DEFAULT_PROFILE = """# Research interest profile

## Core problem

Describe the research problem this radar should prioritize.

## High-priority themes

- Human-AI collaboration with concrete decision support value.
- Preference elicitation, mixed-initiative interaction, and interactive optimization.
- Methods or evaluations that can transfer to the user's active research.

## Methodological interests

- Empirical studies with clear tasks, measures, and operational settings.
- Systems that expose uncertainty, clarification, grounding, or calibration.

## Relevance boundaries

- Venue match alone is not enough.
- Generic editorials, announcements, board notes, and book reviews are low priority.

## Feedback history

Record false positives and false negatives here as the radar improves.
"""


LOW_PRIORITY_PATTERNS = [
    (r"\beditorial\b", "editorial item"),
    (r"\beditors?'?\s+note\b", "editor note"),
    (r"\beditorial\s+board\b", "editorial board item"),
    (CORRECTION_TITLE_PATTERN, "correction or erratum"),
    (r"\bannouncement\b", "announcement"),
    (r"\bbook\s+review\b", "book review"),
    (r"\bcall\s+for\s+papers\b", "call for papers"),
    (r"\bfront\s+matter\b|\bback\s+matter\b", "front/back matter"),
]


def asset_text(name: str, fallback: str) -> str:
    # Installed wheels carry their own copy.  The repository-level copy remains
    # useful for people reading or adapting the examples before installation.
    packaged = Path(__file__).with_name("assets") / name
    repository = Path(__file__).resolve().parents[2] / "assets" / name
    for path in (packaged, repository):
        if path.exists():
            return path.read_text(encoding="utf-8")
    return fallback


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def load_config(path: Path) -> dict[str, Any]:
    with path.expanduser().open("rb") as handle:
        return tomllib.load(handle)


def migrate_legacy_model_config(path: Path) -> str | None:
    """Remove obsolete direct-model settings after preserving the full file."""

    path = path.expanduser().resolve()
    if not path.exists():
        return None
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    kept: list[str] = []
    in_llm = False
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[llm]":
            in_llm = True
            removed = True
            continue
        if in_llm and stripped.startswith("[") and stripped.endswith("]"):
            in_llm = False
        if not in_llm:
            kept.append(line)
    if not removed:
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.backup-pre-v0.8-{stamp}")
    shutil.copy2(path, backup)
    _atomic_write(path, "".join(kept))
    return str(backup)


def resolve_state(config_path: Path, config: dict[str, Any]) -> Path:
    raw = Path(str(config["state_dir"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (config_path.parent / raw).resolve()


def initialize_installation(state_dir: Path = DEFAULT_STATE_DIR, config_path: Path | None = None) -> dict[str, Any]:
    state = state_dir.expanduser().resolve()
    config = (config_path.expanduser().resolve() if config_path else state / "config.toml")
    state.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    preserved: list[str] = []

    legacy_config_backup = None
    if config.exists():
        preserved.append(str(config))
        legacy_config_backup = migrate_legacy_model_config(config)
    else:
        text = asset_text("config.example.toml", 'state_dir = "."\nprofile_file = "research-profile.md"\n')
        if config.parent == state:
            text = re.sub(r'^state_dir\s*=.*$', 'state_dir = "."', text, count=1, flags=re.M)
        else:
            text = re.sub(r'^state_dir\s*=.*$', f"state_dir = {json.dumps(str(state))}", text, count=1, flags=re.M)
        _atomic_write(config, text)
        created.append(str(config))

    cfg = load_config(config)
    resolved_state = resolve_state(config, cfg)
    resolved_state.mkdir(parents=True, exist_ok=True)
    profile = resolved_state / cfg.get("profile_file", "research-profile.md")
    if profile.exists():
        preserved.append(str(profile))
    else:
        _atomic_write(profile, asset_text("research-profile.example.md", DEFAULT_PROFILE))
        created.append(str(profile))

    db_path = resolved_state / "papers.sqlite3"
    pre_upgrade_backup = None
    if db_path.exists():
        status = database_status(db_path)
        if not status.get("schema_current", False):
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_path = resolved_state / "backups" / f"pre-upgrade-v{status.get('schema_version', 0)}-{stamp}.sqlite3"
            backup_database(db_path, backup_path)
            pre_upgrade_backup = str(backup_path)
    upgrade = upgrade_database(db_path)
    metadata_repaired = repair_product_metadata(db_path)
    active = seed_active_profile(db_path, profile.read_text(encoding="utf-8"), "init")
    return {
        "ok": True,
        "state_dir": str(resolved_state),
        "config": str(config),
        "database": str(db_path),
        "profile": str(profile),
        "created": created,
        "preserved": preserved,
        "schema_version": upgrade["schema_version"],
        "active_profile_id": active["id"],
        "pre_upgrade_backup": pre_upgrade_backup,
        "legacy_config_backup": legacy_config_backup,
        "metadata_repaired": metadata_repaired,
    }


def classify_low_priority(title: str, venue: str = "") -> tuple[bool, str]:
    text = (title or "").lower()
    for pattern, reason in LOW_PRIORITY_PATTERNS:
        if re.search(pattern, text):
            return True, reason
    return False, ""


def repair_product_metadata(db_path: Path) -> dict[str, int]:
    """Backfill user-facing metadata introduced after early local releases."""

    database = connect(db_path)
    abstract_sources = 0
    low_priority = 0
    confidence_capped = 0
    try:
        paper_rows = database.execute(
            "SELECT identity,title,venue,abstract,abstract_source,low_priority,low_priority_reason FROM papers"
        ).fetchall()
        with database:
            for paper in paper_rows:
                updates: dict[str, Any] = {}
                if paper["abstract_source"] in {None, "", "unknown"}:
                    updates["abstract_source"] = "existing" if (paper["abstract"] or "").strip() else "missing"
                    abstract_sources += 1
                is_low, reason = classify_low_priority(paper["title"], paper["venue"] or "")
                if bool(paper["low_priority"]) != is_low or (paper["low_priority_reason"] or "") != reason:
                    updates["low_priority"] = int(is_low)
                    updates["low_priority_reason"] = reason
                    low_priority += 1
                if updates:
                    assignments = ",".join(f"{column}=?" for column in updates)
                    database.execute(
                        f"UPDATE papers SET {assignments} WHERE identity=?",
                        (*updates.values(), paper["identity"]),
                    )
            cursor = database.execute(
                """UPDATE screenings SET confidence=0.5
                WHERE identity IN (SELECT identity FROM papers WHERE COALESCE(abstract,'')='')
                  AND (confidence IS NULL OR confidence>0.5)"""
            )
            confidence_capped = max(0, cursor.rowcount)
        return {
            "abstract_sources": abstract_sources,
            "low_priority": low_priority,
            "confidence_capped": confidence_capped,
        }
    finally:
        database.close()


def abstract_source_for(abstract: str, current: str = "") -> str:
    if abstract:
        return current if current and current not in {"missing", "unknown"} else "metadata"
    return "missing"


APA_YEAR_RE = re.compile(r"\((?P<year>(?:19|20)\d{2}[a-z]?|n\.d\.)\)\.?", re.IGNORECASE)
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def _normalize_doi(value: str) -> str:
    value = urllib.parse.unquote((value or "").strip().lower())
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.rstrip(".,;)]} ")


def _normalized_paper_title(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").casefold(), flags=re.UNICODE)


def manual_identity_for_title(db: Any, title: str) -> str | None:
    """Return the stable identity of an exact title-only manual record."""

    normalized = _normalized_paper_title(title)
    if not normalized:
        return None
    for candidate in db.execute(
        """SELECT identity,title FROM papers
        WHERE manual_citation IS NOT NULL AND identity LIKE 'title:%'"""
    ):
        if _normalized_paper_title(candidate["title"]) == normalized:
            return str(candidate["identity"])
    return None


def parse_apa_citation(value: str) -> dict[str, Any]:
    """Extract stable paper metadata from a Google Scholar-style APA citation."""

    citation = re.sub(r"\s+", " ", value or "").strip()
    if len(citation) < 20:
        raise ValueError("APA 引用过短，请粘贴 Google Scholar 提供的完整引用")
    if len(citation) > 5000:
        raise ValueError("APA 引用超过 5000 个字符，请检查是否误粘贴了正文")
    year_match = APA_YEAR_RE.search(citation)
    if not year_match:
        raise ValueError("无法识别 APA 引用中的年份，请保留类似“(2026).”的部分")
    author_text = citation[:year_match.start()].strip().rstrip(". ")
    remainder = citation[year_match.end():].strip()
    separator = re.search(r"[.!?]\s+(?=\w)", remainder, flags=re.UNICODE)
    if not separator:
        raise ValueError("无法区分论文题名和期刊名，请粘贴完整 APA 引用")
    title = remainder[:separator.end() - 1].strip().rstrip(".")
    publication = remainder[separator.end():].strip()
    venue = publication.split(",", 1)[0].strip().rstrip(".")
    if len(title) < 5 or len(venue) < 2:
        raise ValueError("APA 引用中的论文题名或期刊名无法可靠识别")
    doi_match = DOI_RE.search(citation)
    doi = _normalize_doi(doi_match.group(0)) if doi_match else ""
    urls = re.findall(r"https?://[^\s]+", citation)
    url = urls[-1].rstrip(".,;)]}") if urls else ("https://doi.org/" + doi if doi else "")
    year = year_match.group("year")
    published = year[:4] if year[:4].isdigit() else ""
    conference = bool(re.search(r"\b(?:proceedings|conference|symposium|workshop)\b", venue, re.IGNORECASE))
    return {
        "citation": citation,
        "title": title,
        "authors": [author_text] if author_text else [],
        "venue": venue,
        "published": published,
        "doi": doi,
        "url": url,
        "publication_type_raw": "proceedings-article" if conference else "journal-article",
    }


def add_manual_paper(db_path: Path, apa_citation: str, abstract: str) -> dict[str, Any]:
    """Idempotently add a user-supplied paper and queue it for semantic screening."""

    metadata = parse_apa_citation(apa_citation)
    abstract_text = re.sub(r"\s+", " ", abstract or "").strip()
    if len(abstract_text) < 80:
        raise ValueError("摘要至少需要 80 个字符，请粘贴完整摘要而不是搜索片段")
    if len(abstract_text) > 50_000:
        raise ValueError("摘要超过 50000 个字符，请检查是否误粘贴了全文")
    normalized_title = _normalized_paper_title(metadata["title"])
    identity = "doi:" + metadata["doi"] if metadata["doi"] else "title:" + hashlib.sha256(normalized_title.encode()).hexdigest()
    now = utc_now()
    db = connect(db_path)
    try:
        existing = db.execute("SELECT * FROM papers WHERE identity=?", (identity,)).fetchone()
        if not existing:
            for candidate in db.execute("SELECT * FROM papers"):
                if _normalized_paper_title(candidate["title"]) == normalized_title:
                    existing = candidate
                    identity = candidate["identity"]
                    break
        if existing:
            abstract_changed = not (existing["abstract"] or "").strip()
            unjudged = db.execute(
                "SELECT 1 FROM screenings WHERE identity=? AND provider='codex-agent' LIMIT 1",
                (identity,),
            ).fetchone() is None
            with db:
                db.execute(
                    """UPDATE papers SET manual_citation=COALESCE(manual_citation,?),
                    doi=CASE WHEN COALESCE(doi,'')='' AND ?<>'' THEN ? ELSE doi END,
                    url=CASE WHEN COALESCE(url,'')='' AND ?<>'' THEN ? ELSE url END,
                    abstract=CASE WHEN COALESCE(abstract,'')='' THEN ? ELSE abstract END,
                    abstract_source=CASE WHEN COALESCE(abstract,'')='' THEN 'user-provided' ELSE abstract_source END,
                    abstract_retrieved_at=CASE WHEN COALESCE(abstract,'')='' THEN ? ELSE abstract_retrieved_at END,
                    abstract_failure_reason=CASE WHEN COALESCE(abstract,'')='' THEN NULL ELSE abstract_failure_reason END,
                    needs_rescreen=1,
                    updated_at=? WHERE identity=?""",
                    (
                        metadata["citation"], metadata["doi"], metadata["doi"],
                        metadata["url"], metadata["url"], abstract_text, now, now, identity,
                    ),
                )
                db.execute(
                    "INSERT OR IGNORE INTO observations(identity,source,observed_at) VALUES(?,? ,?)",
                    (identity, "manual-apa", now),
                )
            return {
                "identity": identity, "title": existing["title"], "created": False,
                "abstract_updated": abstract_changed, "queued_for_screening": unjudged,
            }
        decision = publication_decision(
            metadata["title"], metadata["venue"], metadata["publication_type_raw"], "manual-apa",
            "conference" if metadata["publication_type_raw"] == "proceedings-article" else "journal",
        )
        low_priority, low_priority_reason = classify_low_priority(metadata["title"], metadata["venue"])
        with db:
            db.execute(
                """INSERT INTO papers(
                identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at,
                abstract_source,low_priority,low_priority_reason,publication_type,publication_type_raw,
                publication_type_source,publication_type_evidence_json,eligibility_status,exclusion_reason,
                abstract_retrieved_at,needs_rescreen,manual_citation
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity, metadata["doi"] or None, metadata["title"], abstract_text, metadata["venue"],
                    metadata["published"], metadata["url"], json.dumps(metadata["authors"], ensure_ascii=False),
                    now, now, "user-provided", int(low_priority), low_priority_reason or None,
                    decision["publication_type"], metadata["publication_type_raw"], "manual-apa",
                    json.dumps(decision["evidence"], ensure_ascii=False), decision["eligibility_status"],
                    decision.get("exclusion_reason"), now, 1, metadata["citation"],
                ),
            )
            db.execute(
                "INSERT INTO observations(identity,source,observed_at) VALUES(?,?,?)",
                (identity, "manual-apa", now),
            )
        return {"identity": identity, "title": metadata["title"], "created": True, "abstract_updated": True, "queued_for_screening": True}
    finally:
        db.close()


def add_manual_abstract(db_path: Path, identity: str, abstract: str, source_url: str = "") -> dict[str, Any]:
    """Fill missing evidence without scheduling an already judged paper again."""

    abstract_text = re.sub(r"\s+", " ", abstract or "").strip()
    if len(abstract_text) < 80:
        raise ValueError("摘要至少需要 80 个字符，请粘贴完整摘要而不是搜索片段")
    if len(abstract_text) > 50_000:
        raise ValueError("摘要超过 50000 个字符，请检查是否误粘贴了全文")
    url = (source_url or "").strip()
    if url and urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("摘要来源需要是 http 或 https 链接")
    now = utc_now()
    db = connect(db_path)
    try:
        paper = db.execute("SELECT * FROM papers WHERE identity=?", (identity,)).fetchone()
        if not paper:
            raise ValueError("没有找到这篇文献")
        if (paper["abstract"] or "").strip():
            raise ValueError("这篇文献已经有摘要，无需重复添加")
        unjudged = db.execute(
            "SELECT 1 FROM screenings WHERE identity=? AND provider='codex-agent' LIMIT 1",
            (identity,),
        ).fetchone() is None
        with db:
            db.execute(
                """UPDATE papers SET abstract=?,abstract_source='user-provided',
                abstract_source_url=?,
                abstract_retrieved_at=?,abstract_failure_reason=NULL,needs_rescreen=1,updated_at=?
                WHERE identity=?""",
                (abstract_text, url or paper["url"] or None, now, now, identity),
            )
            db.execute(
                """INSERT INTO abstract_attempts(
                task_id,identity,provider,status,source_url,evidence_type,detail,attempted_at
                ) VALUES(?,?,'manual','found',?,'official_page_manual','用户核验并补充完整摘要',?)""",
                ("manual-" + now, identity, url or paper["url"] or None, now),
            )
        # Keep the evidence-change marker for explicit manual re-evaluation;
        # automatic screening only consumes never-judged papers.
        return {"identity": identity, "title": paper["title"], "needs_rescreen": True,
                "queued_for_screening": unjudged}
    finally:
        db.close()


def source_coverage(db_path: Path, configured_sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    db = connect(db_path)
    try:
        output: dict[str, dict[str, Any]] = {}
        for source in configured_sources:
            name = source["name"]
            rows = db.execute(
                """SELECT DISTINCT p.identity,p.published,p.abstract,p.first_seen FROM observations o
                JOIN papers p ON p.identity=o.identity
                WHERE o.source=? OR o.source LIKE ? ESCAPE '\\'""",
                (name, name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + " / %"),
            ).fetchall()
            count = len(rows)
            abstracts = sum(1 for item in rows if item["abstract"])
            dates = [item["published"] for item in rows if item["published"]]
            first_seen = [item["first_seen"] for item in rows if item["first_seen"]]
            output[name] = {
                "paper_count": count,
                "abstract_count": abstracts,
                "missing_abstracts": count - abstracts,
                "abstract_percent": round((abstracts / count * 100), 1) if count else 0,
                "oldest_published": min(dates) if dates else "",
                "newest_published": max(dates) if dates else "",
                "first_seen": min(first_seen) if first_seen else "",
                "last_seen": max(first_seen) if first_seen else "",
            }
        return output
    finally:
        db.close()


def overall_quality(db_path: Path) -> dict[str, Any]:
    db = connect(db_path)
    try:
        row = db.execute(
            """SELECT COUNT(*) papers,
            SUM(CASE WHEN COALESCE(abstract,'')<>'' THEN 1 ELSE 0 END) abstracts,
            SUM(COALESCE(low_priority,0)) low_priority
            FROM papers"""
        ).fetchone()
        papers = int(row["papers"] or 0)
        abstracts = int(row["abstracts"] or 0)
        return {
            "papers": papers,
            "abstracts": abstracts,
            "abstract_percent": round(abstracts / papers * 100, 1) if papers else 0,
            "low_priority": int(row["low_priority"] or 0),
            "missing_abstracts": papers - abstracts,
        }
    finally:
        db.close()


def screening_coverage(db: Any) -> dict[str, int]:
    """Count first judgments across all profiles and rubric versions."""
    values = db.execute("""SELECT COUNT(*) eligible,
        COALESCE(SUM(EXISTS(SELECT 1 FROM screenings s
          WHERE s.identity=p.identity AND s.provider='codex-agent')),0) judged
        FROM papers p WHERE p.eligibility_status='eligible'""").fetchone()
    eligible, judged = int(values["eligible"]), int(values["judged"])
    return {"eligible": eligible, "judged": judged, "pending": eligible - judged}


def safe_slug(value: str, fallback: str = "paper") -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return (slug[:80] or fallback).strip("-")


def _filename_component(value: str, fallback: str, limit: int) -> str:
    """Keep readable Unicode names while removing filesystem-reserved characters."""

    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:limit].strip(" .") or fallback)


def fulltext_filename(paper: Any) -> str:
    """Build the stable, human-readable local name: author + year + title."""

    try:
        authors = json.loads(paper["authors_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        authors = []
    author_names = [_filename_component(str(name), "", 42) for name in authors if str(name).strip()]
    author_label = "、".join(author_names[:3]) + ("等" if len(author_names) > 3 else "")
    year_match = re.search(r"\b(\d{4})\b", str(paper["published"] or ""))
    year = year_match.group(1) if year_match else "年份未知"
    title = _filename_component(str(paper["title"] or ""), "未命名文献", 150)
    return f"{_filename_component(author_label, '作者未知', 90)}_{year}_{title}.pdf"


def import_fulltext(db_path: Path, state_dir: Path, identity: str, original_name: str, content: bytes) -> dict[str, Any]:
    if not content.startswith(b"%PDF"):
        raise ValueError("文件不是有效的 PDF，请重新选择")
    if len(content) > MAX_PDF_BYTES:
        raise ValueError("PDF 文件超过 50 MB，请先压缩后再导入")
    db = connect(db_path)
    try:
        paper = db.execute("SELECT * FROM papers WHERE identity=?", (identity,)).fetchone()
        if not paper:
            raise ValueError(f"Unknown paper identity: {identity}")
        digest = hashlib.sha256(content).hexdigest()
        existing = db.execute("""SELECT * FROM fulltext_files WHERE identity=? AND sha256=?
          ORDER BY imported_at DESC,id DESC LIMIT 1""", (identity, digest)).fetchone()
        if existing:
            return dict(existing) | {"deduplicated": True, "updated": False}
        prior = db.execute("SELECT * FROM fulltext_files WHERE identity=?", (identity,)).fetchall()
        filename = fulltext_filename(paper)
        directory = state_dir.expanduser().resolve() / "fulltexts"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        fd, temp_name = tempfile.mkstemp(prefix=filename + ".", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        now = utc_now()
        with db:
            db.execute("DELETE FROM fulltext_files WHERE identity=?", (identity,))
            db.execute(
                """INSERT INTO fulltext_files(identity,stored_path,original_name,sha256,size_bytes,imported_at)
                VALUES(?,?,?,?,?,?)""",
                (identity, str(target), original_name or "paper.pdf", digest, len(content), now),
            )
        for item in prior:
            old_path = Path(item["stored_path"])
            if old_path == target:
                continue
            still_referenced = db.execute(
                "SELECT 1 FROM fulltext_files WHERE stored_path=? LIMIT 1", (str(old_path),)
            ).fetchone()
            if not still_referenced:
                old_path.unlink(missing_ok=True)
        row = db.execute("SELECT * FROM fulltext_files WHERE identity=?", (identity,)).fetchone()
        return dict(row) | {"deduplicated": False, "updated": bool(prior)}
    finally:
        db.close()


def source_candidates(query: str, user_agent: str = "PersonalAcademicRadar/0.8") -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return []
    candidates: dict[str, dict[str, Any]] = {}
    providers_ok = 0

    def add(item: dict[str, Any]) -> None:
        name = item.get("name", "").strip()
        issn = item.get("issn", "").strip()
        openalex_id = item.get("openalex_id", "").strip()
        if not name or not (issn or openalex_id):
            return
        key = issn or openalex_id or name.lower()
        merged = {**candidates.get(key, {}), **item}
        merged["candidate_id"] = hashlib.sha256(
            f"{merged.get('name','')}|{merged.get('issn','')}|{merged.get('openalex_id','')}".encode()
        ).hexdigest()[:16]
        candidates[key] = merged

    crossref_url = "https://api.crossref.org/journals?" + urllib.parse.urlencode({"query": query, "rows": 5})
    try:
        request = urllib.request.Request(crossref_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        providers_ok += 1
        for item in data.get("message", {}).get("items", []):
            issns = item.get("ISSN") or []
            add({
                "name": item.get("title", ""),
                "issn": issns[0] if issns else "",
                "issns": issns,
                "publisher": item.get("publisher", ""),
                "source_type": "期刊",
                "config_type": "crossref",
                "match_basis": "Crossref 期刊名称与 ISSN",
            })
    except Exception:
        pass

    openalex_url = "https://api.openalex.org/sources?" + urllib.parse.urlencode({"search": query, "per_page": 8})
    try:
        request = urllib.request.Request(openalex_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        providers_ok += 1
        for item in data.get("results", []):
            issn = (item.get("issn") or [item.get("issn_l") or ""])[0]
            raw_type = str(item.get("type") or "").lower()
            if raw_type not in {"journal", "conference"}:
                continue
            add({
                "name": item.get("display_name", ""),
                "issn": issn or item.get("issn_l", ""),
                "issns": item.get("issn") or [],
                "publisher": item.get("host_organization_name", ""),
                "openalex_id": str(item.get("id", "")).rsplit("/", 1)[-1],
                "source_type": "会议" if raw_type == "conference" else "期刊",
                "config_type": "crossref" if issn and raw_type == "journal" else "openalex",
                "match_basis": "OpenAlex 已验证来源类型与名称匹配",
            })
    except Exception:
        pass

    if not providers_ok:
        raise RuntimeError("Crossref 和 OpenAlex 当前都无法响应，请检查网络后重试")
    return list(candidates.values())[:8]


def publication_date_label(value: str | None, precision: str = "unknown") -> str:
    """Display only the granularity actually supplied by publication metadata."""
    if not value:
        return "日期未知"
    if precision == "month":
        return value[:7]
    if precision == "year":
        return value[:4]
    return value


def human_time(value: str | None) -> str:
    if not value:
        return "尚无记录"
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    local = parsed.astimezone()
    return local.strftime("%Y-%m-%d %H:%M")

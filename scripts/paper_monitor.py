#!/usr/bin/env python3
"""Idempotent academic paper monitor. Python 3.9+, standard library only."""
from __future__ import annotations

import argparse, datetime as dt, email.message, email.utils, hashlib, html, json, os, random, re
import smtplib, sqlite3, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

PROJECT_SRC=Path(__file__).resolve().parents[1]/"src"
if PROJECT_SRC.exists() and str(PROJECT_SRC) not in sys.path: sys.path.insert(0,str(PROJECT_SRC))
from academic_radar.enrichment import enrich_abstracts as run_enrichment, clean_abstract
from academic_radar.governance import (
    publication_decision, should_preserve_publication_metadata, source_kind_from_evidence,
)
from academic_radar.product import abstract_source_for, classify_low_priority, manual_identity_for_title, publication_date_label
from academic_radar.storage import latest_schema_version, upgrade_database
from academic_radar.recommendations import (
    snapshot_run, SCREENING_SCHEMA_VERSION, SCREENING_RUBRIC_VERSION,
    SCREENING_DIMENSIONS, REASONING_MIN_LENGTHS,
    RECOMMENDATION_REASON_MIN_LENGTH, RECOMMENDATION_REASON_MAX_LENGTH,
    evaluation_policy, unit_number, validate_judgment_evidence, calibrated_score, validate_study_summary,
)

VERSION = "0.11.0"

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10 compatibility without dependencies
    tomllib = None

def _toml_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return json.loads(raw)
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner: return []
        parts = re.split(r',(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', inner)
        return [_toml_value(x) for x in parts]
    if raw in ("true", "false"): return raw == "true"
    try: return float(raw) if "." in raw else int(raw)
    except ValueError: raise ValueError(f"Unsupported TOML value: {raw}")

def _toml_load_fallback(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}; current = root
    for line_no, line in enumerate(text.splitlines(), 1):
        line = re.sub(r'\s+#.*$', '', line).strip()
        if not line: continue
        if line.startswith("[[") and line.endswith("]] ".strip()):
            key=line[2:-2].strip(); root.setdefault(key,[]).append({}); current=root[key][-1]; continue
        if line.startswith("[") and line.endswith("]"):
            key=line[1:-1].strip(); current=root.setdefault(key,{}); continue
        if "=" not in line: raise ValueError(f"Invalid TOML at line {line_no}")
        key,raw=line.split("=",1); current[key.strip()]=_toml_value(raw)
    return root

@dataclass
class Paper:
    identity: str; doi: str; title: str; abstract: str; venue: str
    published: str; url: str; authors: list[str]; source: str
    abstract_source: str = ""; low_priority: bool = False; low_priority_reason: str = ""
    publication_type_raw: str = ""; publication_type_source: str = ""; source_kind: str = ""
    publication_type: str = ""
    abstract_source_url: str = ""
    published_precision: str = "unknown"


class CollectionIncomplete(RuntimeError):
    """Keep collected records while reporting that the bounded scan was incomplete."""

    def __init__(self, provider: str, papers: list[Paper], detail: str) -> None:
        super().__init__(f"{provider} collection incomplete: {detail}")
        self.papers = papers

class AutoClosingConnection(sqlite3.Connection):
    """Close runner connections during exception unwinding as a final safeguard."""
    def __del__(self) -> None:
        try: self.close()
        except Exception: pass

def clean_text(value: Any) -> str:
    if not value: return ""
    if isinstance(value, list): value = " ".join(str(x) for x in value)
    value = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()

def normalize_doi(value: str) -> str:
    value = urllib.parse.unquote((value or "").strip().lower())
    value = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.rstrip(".,; ")

def identity(doi: str, title: str) -> str:
    doi = normalize_doi(doi)
    if doi: return "doi:" + doi
    norm = re.sub(r"[^a-z0-9]+", "", title.lower())
    return "title:" + hashlib.sha256(norm.encode()).hexdigest()

def date_parts_with_precision(item: dict[str, Any]) -> tuple[str, str]:
    for key in ("published-online", "published-print", "published", "created"):
        parts = item.get(key, {}).get("date-parts", [[]])[0]
        if parts:
            vals = list(parts) + [1, 1]
            try:
                value = dt.date(int(vals[0]), int(vals[1]), int(vals[2])).isoformat()
                return value, "day" if len(parts) >= 3 else "month" if len(parts) == 2 else "year"
            except (ValueError, TypeError): pass
    return "", "unknown"

def date_parts(item: dict[str, Any]) -> str:
    return date_parts_with_precision(item)[0]

def request_json(url: str, headers: dict[str,str], timeout: int, retries: int, backoff: float,
                 payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    hdr = {**headers, **({"Content-Type":"application/json"} if data else {})}
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            status = getattr(exc, "code", None)
            transient = status in (408, 429, 500, 502, 503, 504) or status is None
            if attempt >= retries or not transient: raise
            retry_after = getattr(exc, "headers", {}).get("Retry-After") if getattr(exc, "headers", None) else None
            delay = None
            if retry_after:
                try: delay = max(0.0,float(retry_after))
                except ValueError:
                    try:
                        retry_at=email.utils.parsedate_to_datetime(retry_after)
                        delay=max(0.0,(retry_at-dt.datetime.now(dt.timezone.utc)).total_seconds())
                    except (TypeError,ValueError): pass
            if delay is None: delay = backoff * 2**attempt + random.uniform(0, max(0.1,backoff))
            time.sleep(min(delay, 60))
    raise RuntimeError("unreachable")

def config_load(path: Path) -> dict[str, Any]:
    if tomllib:
        with path.open("rb") as f: cfg = tomllib.load(f)
    else:
        cfg = _toml_load_fallback(path.read_text(encoding="utf-8"))
    required = ("state_dir", "profile_file", "sources")
    missing = [k for k in required if k not in cfg]
    if missing: raise ValueError("Missing config fields: " + ", ".join(missing))
    if not isinstance(cfg["sources"], list) or not cfg["sources"]: raise ValueError("sources must be a non-empty list")
    return cfg

def resolve_state(cfg: dict[str,Any], config_path: Path) -> Path:
    raw = Path(os.path.expandvars(os.path.expanduser(cfg["state_dir"])))
    return raw if raw.is_absolute() else (config_path.parent / raw).resolve()

def db_open(path: Path) -> sqlite3.Connection:
    upgrade_database(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, factory=AutoClosingConnection)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS papers(
      identity TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL, abstract TEXT, venue TEXT,
      published TEXT, url TEXT, authors_json TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
      abstract_source TEXT NOT NULL DEFAULT 'unknown',
      low_priority INTEGER NOT NULL DEFAULT 0 CHECK(low_priority IN (0,1)),
      low_priority_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS observations(
      identity TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
      PRIMARY KEY(identity, source), FOREIGN KEY(identity) REFERENCES papers(identity)
    );
    CREATE TABLE IF NOT EXISTS screenings(
      identity TEXT NOT NULL, profile_hash TEXT NOT NULL, provider TEXT NOT NULL, model TEXT,
      relevant INTEGER NOT NULL, score REAL NOT NULL, reasons TEXT, themes_json TEXT,
      confidence REAL, screened_at TEXT NOT NULL, PRIMARY KEY(identity, profile_hash, provider, model)
    );
    CREATE TABLE IF NOT EXISTS notifications(
      identity TEXT PRIMARY KEY, sent_at TEXT NOT NULL, digest_path TEXT
    );
    CREATE TABLE IF NOT EXISTS source_runs(
      run_id TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, count INTEGER NOT NULL,
      error TEXT, finished_at TEXT NOT NULL, since TEXT, PRIMARY KEY(run_id, source)
    );
    CREATE TABLE IF NOT EXISTS source_health(
      source TEXT PRIMARY KEY, status TEXT NOT NULL, consecutive_failures INTEGER NOT NULL DEFAULT 0,
      last_success_at TEXT, last_failure_at TEXT, last_error TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pipeline_runs(
      run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, profile_version_id INTEGER,
      started_at TEXT NOT NULL, finished_at TEXT, collected_count INTEGER NOT NULL DEFAULT 0,
      candidate_count INTEGER NOT NULL DEFAULT 0, relevant_count INTEGER NOT NULL DEFAULT 0,
      error_summary TEXT, details_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS profile_versions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, profile_hash TEXT NOT NULL UNIQUE, content TEXT NOT NULL,
      status TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', change_summary TEXT,
      created_at TEXT NOT NULL, confirmed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS agent_jobs(
      run_id TEXT PRIMARY KEY, profile_hash TEXT NOT NULL, status TEXT NOT NULL, queue_path TEXT,
      results_path TEXT, exported_count INTEGER NOT NULL DEFAULT 0, imported_count INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, imported_at TEXT
    );
    CREATE TABLE IF NOT EXISTS fulltext_files(
      id INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL, stored_path TEXT NOT NULL UNIQUE,
      original_name TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL,
      imported_at TEXT NOT NULL, FOREIGN KEY(identity) REFERENCES papers(identity) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
    CREATE INDEX IF NOT EXISTS idx_papers_seen ON papers(first_seen);
    CREATE INDEX IF NOT EXISTS idx_fulltext_identity ON fulltext_files(identity, imported_at DESC);
    """)
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)", (str(latest_schema_version()),))
    db.commit(); return db

def crossref_collect(source: dict[str,Any], cfg: dict[str,Any], since: str) -> list[Paper]:
    c = cfg.get("collection", {}); rows = max(1,min(1000,int(source.get("rows_per_page",c.get("rows_per_page",c.get("rows_per_source",80))))))
    max_pages=max(1,int(source.get("max_pages_per_source",c.get("max_pages_per_source",3))))
    base = "https://api.crossref.org"
    params: dict[str, str] = {"rows":str(rows), "select":"DOI,title,abstract,container-title,published-online,published-print,published,created,URL,author,type,ISSN"}
    filters = [f"from-created-date:{since}"]
    if source["type"] == "crossref":
        url = f"{base}/journals/{urllib.parse.quote(source['issn'])}/works"
    else:
        url = f"{base}/works"; filters.extend(["prefix:10.1145", "type:proceedings-article"])
        params["query.container-title"] = source["query_container"]
    params["filter"] = ",".join(filters); params["sort"]="created"; params["order"]="desc"; params["cursor"]="*"
    result=[]; seen=set(); cursors={"*"}; downloaded=0
    for page in range(max_pages):
        data = request_json(url+"?"+urllib.parse.urlencode(params), {"User-Agent":cfg.get("user_agent","ResearchPaperMonitor/1.0")},
                            int(c.get("timeout_seconds",30)), int(c.get("max_retries",3)), float(c.get("backoff_seconds",2)))
        message=data.get("message",{}); items=message.get("items")
        if not isinstance(items,list):
            raise CollectionIncomplete("Crossref",result,"response has no valid items array")
        downloaded += len(items)
        for item in items:
            venue = clean_text(item.get("container-title"))
            if source["type"] == "crossref-query":
                low = venue.lower()
                expected=source.get("container_title_contains","chi conference on human factors in computing systems").lower()
                excluded=[x.lower() for x in source.get("exclude_container_contains",["extended abstracts"])]
                if expected not in low or any(x in low for x in excluded): continue
            title=clean_text(item.get("title")); doi=normalize_doi(item.get("DOI",""))
            if not title: continue
            ident=identity(doi,title)
            if ident in seen: continue
            seen.add(ident)
            authors=[clean_text(" ".join(filter(None,(a.get("given"),a.get("family"))))) for a in item.get("author",[])]
            abstract=clean_abstract(item.get("abstract"))
            raw_type=str(item.get("type") or "")
            decision=publication_decision(title,venue or source["name"],raw_type,"crossref")
            low=decision["eligibility_status"]!="eligible"; low_reason=decision.get("exclusion_reason") or ""
            result.append(Paper(ident,doi,title,abstract,venue or source["name"],
                                date_parts(item),item.get("URL","") or ("https://doi.org/"+doi if doi else ""),authors,source["name"],
                                abstract_source_for(abstract,"crossref"),low,low_reason,raw_type,"crossref","",
                                decision.get("publication_type") or "",
                                "https://api.crossref.org/works/"+urllib.parse.quote(doi,safe="") if abstract and doi else "",
                                date_parts_with_precision(item)[1]))
        next_cursor=message.get("next-cursor")
        total=message.get("total-results")
        if not next_cursor or len(items)<rows or (isinstance(total,int) and downloaded>=total): break
        if next_cursor in cursors:
            raise CollectionIncomplete("Crossref",result,"pagination cursor did not advance")
        if page+1==max_pages:
            raise CollectionIncomplete("Crossref",result,f"page limit {max_pages} reached after {downloaded} records; more results remain")
        cursors.add(next_cursor)
        params["cursor"]=next_cursor
    return result

def openalex_abstract(doi: str, cfg: dict[str,Any]) -> str:
    if not doi or not cfg.get("collection",{}).get("openalex_fallback",True): return ""
    c=cfg.get("collection",{}); url="https://api.openalex.org/works/https://doi.org/"+urllib.parse.quote(doi,safe="")
    mail = re.search(r"mailto:([^\s;)]+)", cfg.get("user_agent",""))
    if mail: url += "?mailto=" + urllib.parse.quote(mail.group(1))
    try: data=request_json(url,{"User-Agent":cfg.get("user_agent","")},int(c.get("timeout_seconds",30)),1,1)
    except Exception: return ""
    if normalize_doi(str(data.get("doi") or ""))!=normalize_doi(doi): return ""
    inv=data.get("abstract_inverted_index") or {}; words=[]
    for word, positions in inv.items():
        for pos in positions: words.append((pos,word))
    return clean_abstract(" ".join(word for _,word in sorted(words)))

def inverted_abstract(data: dict[str,Any]) -> str:
    words=[]
    for word,positions in (data.get("abstract_inverted_index") or {}).items():
        words.extend((pos,word) for pos in positions)
    return clean_abstract(" ".join(word for _,word in sorted(words)))

def openalex_collect(source: dict[str,Any], cfg: dict[str,Any], since: str) -> list[Paper]:
    source_id=source.get("openalex_id")
    if not source_id or (source.get("type")!="openalex" and not cfg.get("collection",{}).get("openalex_fallback",True)): return []
    c=cfg.get("collection",{}); rows=max(1,min(100,int(source.get("rows_per_page",c.get("rows_per_page",c.get("rows_per_source",80))))))
    max_pages=max(1,int(source.get("max_pages_per_source",c.get("max_pages_per_source",3))))
    params={"filter":f"primary_location.source.id:{source_id},from_publication_date:{since}",
            "sort":"publication_date:desc","per-page":str(rows),"cursor":"*"}
    mail=re.search(r"mailto:([^\s;)]+)",cfg.get("user_agent",""))
    if mail: params["mailto"]=mail.group(1)
    result=[]; seen=set(); cursors={"*"}; downloaded=0
    for page in range(max_pages):
        data=request_json("https://api.openalex.org/works?"+urllib.parse.urlencode(params),
          {"User-Agent":cfg.get("user_agent","")},int(c.get("timeout_seconds",30)),int(c.get("max_retries",3)),float(c.get("backoff_seconds",2)))
        items=data.get("results")
        if not isinstance(items,list):
            raise CollectionIncomplete("OpenAlex",result,"response has no valid results array")
        downloaded += len(items)
        for item in items:
            title=clean_text(item.get("title")); doi=normalize_doi(item.get("doi",""))
            if not title: continue
            ident=identity(doi,title)
            if ident in seen: continue
            seen.add(ident)
            loc=item.get("primary_location") or {}; src=loc.get("source") or {}
            authors=[clean_text(x.get("author",{}).get("display_name")) for x in item.get("authorships",[])]
            abstract=inverted_abstract(item)
            venue=clean_text(src.get("display_name")) or source["name"]
            raw_type=str(item.get("type_crossref") or item.get("type") or "")
            source_kind=str(src.get("type") or "")
            decision=publication_decision(title,venue,raw_type,"openalex",source_kind)
            low=decision["eligibility_status"]!="eligible"; low_reason=decision.get("exclusion_reason") or ""
            result.append(Paper(ident,doi,title,abstract,venue,
              item.get("publication_date","") or "",loc.get("landing_page_url","") or ("https://doi.org/"+doi if doi else ""),authors,source["name"]+" / OpenAlex",
              abstract_source_for(abstract,"openalex"),low,low_reason,raw_type,"openalex",source_kind,
              decision.get("publication_type") or "",
              "https://api.openalex.org/works/"+str(item.get("id") or "").rsplit("/",1)[-1] if abstract and item.get("id") else "",
              "day" if item.get("publication_date") else "unknown"))
        next_cursor=(data.get("meta") or {}).get("next_cursor")
        total=(data.get("meta") or {}).get("count")
        if not next_cursor or len(items)<rows or (isinstance(total,int) and downloaded>=total): break
        if next_cursor in cursors:
            raise CollectionIncomplete("OpenAlex",result,"pagination cursor did not advance")
        if page+1==max_pages:
            raise CollectionIncomplete("OpenAlex",result,f"page limit {max_pages} reached after {downloaded} records; more results remain")
        cursors.add(next_cursor)
        params["cursor"]=next_cursor
    return result

def extract_json(text: str) -> dict[str,Any]:
    match=re.search(r"\{.*\}",text,re.S)
    if not match: raise ValueError("model returned no JSON object")
    value=json.loads(match.group(0)); required={"relevant","score","reasons","matched_themes","confidence"}
    if not required.issubset(value): raise ValueError("model JSON missing fields")
    value["score"]=max(0,min(1,float(value["score"]))); value["confidence"]=max(0,min(1,float(value["confidence"])))
    value["relevant"]=bool(value["relevant"]); value["reasons"]=clean_text(value["reasons"])
    value["matched_themes"]=[clean_text(x) for x in value["matched_themes"]][:8]
    return value


def legacy_reason_text(reasoning: dict[str,str]) -> str:
    """Render the audit fields for queues created before reader-facing summaries."""

    return (
        f"论文证据：{reasoning['evidence_summary']}；"
        f"画像关联：{reasoning['profile_connection']}；"
        f"可迁移价值：{reasoning['transfer_value']}；"
        f"边界与不确定性：{reasoning['limitations']}"
    )


def recommendation_reason(item: dict[str,Any], reasoning: dict[str,str], required: bool) -> str:
    """Validate a natural reader-facing synthesis separately from audit fields."""

    value = clean_text(item.get("recommendation_reason"))
    if not value:
        if required:
            raise ValueError("recommendation_reason is required")
        return legacy_reason_text(reasoning)
    if len(value) < RECOMMENDATION_REASON_MIN_LENGTH:
        raise ValueError(
            f"recommendation_reason is too shallow (minimum {RECOMMENDATION_REASON_MIN_LENGTH} characters)"
        )
    if len(value) > RECOMMENDATION_REASON_MAX_LENGTH:
        raise ValueError(
            f"recommendation_reason is too long (maximum {RECOMMENDATION_REASON_MAX_LENGTH} characters)"
        )
    if value in reasoning.values():
        raise ValueError("recommendation_reason must synthesize rather than repeat one audit field")
    if any(label in value for label in ("论文证据：", "画像关联：", "可迁移价值：", "边界与不确定性：")):
        raise ValueError("recommendation_reason must be natural prose, not concatenated audit labels")
    return value


def structured_judgment(item: dict[str,Any], paper: sqlite3.Row, threshold: float,
                        schema_version: int=SCREENING_SCHEMA_VERSION) -> dict[str,Any]:
    """Validate evidence-bearing reasoning and calculate the authoritative score."""

    reasoning = item.get("reasoning")
    dimensions = item.get("score_dimensions")
    if not isinstance(reasoning, dict) or not isinstance(dimensions, dict):
        raise ValueError("structured results require reasoning and score_dimensions objects")
    required_reasoning = ("evidence_summary", "profile_connection", "transfer_value", "limitations")
    missing = [key for key in required_reasoning if key not in reasoning]
    if missing:
        raise ValueError("structured reasoning missing fields: " + ", ".join(missing))
    clean_reasoning = {key: clean_text(reasoning.get(key)) for key in required_reasoning}
    minimums = REASONING_MIN_LENGTHS if schema_version >= 4 else {key: 8 for key in required_reasoning}
    too_short = [
        f"{key}<{minimums[key]}" for key,value in clean_reasoning.items()
        if len(value) < minimums[key]
    ]
    if too_short:
        raise ValueError("structured reasoning is too shallow: " + ", ".join(too_short))
    required_dimensions = {*SCREENING_DIMENSIONS, "boundary_penalty"}
    if not required_dimensions.issubset(dimensions):
        raise ValueError("score_dimensions missing fields")
    clean_dimensions = {
        key: unit_number(dimensions[key],key) if schema_version>=5
        else max(0.0,min(1.0,float(dimensions[key]))) for key in required_dimensions
    }
    score = sum(clean_dimensions[key] * weight for key,weight in SCREENING_DIMENSIONS.items())
    score -= 0.35 * clean_dimensions["boundary_penalty"]
    score = max(0.0, min(1.0, round(score, 4)))
    abstract_missing = not (paper["abstract"] or "").strip()
    if abstract_missing:
        if not re.search(r"摘要.{0,4}(缺失|不可得|未找到)|abstract.{0,8}(missing|unavailable)", clean_reasoning["evidence_summary"], re.I):
            raise ValueError("missing-abstract reasoning must explicitly identify the evidence limitation")
        clean_dimensions["evidence_quality"] = min(clean_dimensions["evidence_quality"], 0.25)
        score = sum(clean_dimensions[key] * weight for key,weight in SCREENING_DIMENSIONS.items())
        score -= 0.35 * clean_dimensions["boundary_penalty"]
        score = min(0.69, max(0.0, round(score, 4)))
    themes = item.get("matched_themes")
    if not isinstance(themes, list):
        raise ValueError("matched_themes must be a list")
    confidence = unit_number(item.get("confidence",0),"confidence") if schema_version>=5 else max(0.0,min(1.0,float(item.get("confidence",0))))
    if abstract_missing:
        confidence = min(confidence, 0.5)
    reasons = recommendation_reason(item, clean_reasoning, required=schema_version >= 4)
    study_summary = validate_study_summary(item)
    if study_summary is not None:
        clean_reasoning["study_summary"] = study_summary
    if schema_version>=5:
        quality=validate_judgment_evidence(item,paper)
        score=calibrated_score(clean_dimensions,quality["recommendation_type"],abstract_missing)
        clean_reasoning.update(quality)
    return {
        "relevant": score >= threshold,
        "score": score,
        "reasons": reasons,
        "matched_themes": [clean_text(value) for value in themes if clean_text(value)][:8],
        "confidence": confidence,
        "reasoning": clean_reasoning,
        "score_dimensions": clean_dimensions,
        "rubric_version": SCREENING_RUBRIC_VERSION if schema_version>=5 else ("evidence-v2" if schema_version>=4 else "evidence-v1"),
    }

def upsert(db: sqlite3.Connection, p: Paper, now: str) -> bool:
    exists=db.execute("SELECT 1 FROM papers WHERE identity=?",(p.identity,)).fetchone() is not None
    if not exists and p.doi:
        # A Google Scholar APA citation does not always include a DOI.  Keep
        # the title-hash identity stable when a later provider resolves that
        # same manually added paper, instead of creating a second row.
        manual_identity=manual_identity_for_title(db,p.title)
        if manual_identity:
            p.identity=manual_identity; exists=True
    if not p.low_priority:
        p.low_priority,p.low_priority_reason=classify_low_priority(p.title,p.venue)
    p.abstract_source=abstract_source_for(p.abstract,p.abstract_source)
    decision=publication_decision(p.title,p.venue,p.publication_type_raw,p.publication_type_source,p.source_kind)
    incoming_publication_raw=p.publication_type_raw
    incoming_publication_source=p.publication_type_source
    incoming_publication_status=decision['eligibility_status']
    prior=db.execute("SELECT * FROM papers WHERE identity=?", (p.identity,)).fetchone()
    if prior and should_preserve_publication_metadata(
        prior['publication_type_source'], prior['eligibility_status'],
        p.publication_type_source, decision['eligibility_status'],
    ):
        decision=publication_decision(
            p.title,p.venue,prior['publication_type_raw'] or '',prior['publication_type_source'] or '',
            source_kind_from_evidence(prior['publication_type_evidence_json']),
        )
        p.publication_type_raw=prior['publication_type_raw'] or ''
        p.publication_type_source=prior['publication_type_source'] or ''
    if prior:
        prior_evidence=json.loads(prior['publication_type_evidence_json'] or '[]')
        if isinstance(prior_evidence,list):
            decision['evidence'].extend(item for item in prior_evidence if isinstance(item,dict)
              and item.get('kind') in {'metadata_conflict','verified_record'} and item not in decision['evidence'])
        if (prior['publication_type_raw'],prior['publication_type_source']) != (p.publication_type_raw,p.publication_type_source):
            decision['evidence'].append({'kind':'metadata_conflict','source':prior['publication_type_source'] or 'metadata',
              'value':prior['publication_type_raw'] or '', 'prior_status':prior['eligibility_status']})
        elif should_preserve_publication_metadata(
            prior['publication_type_source'],prior['eligibility_status'],
            incoming_publication_source, incoming_publication_status,
        ):
            conflict={'kind':'metadata_conflict','source':incoming_publication_source or 'metadata',
              'value':incoming_publication_raw, 'prior_status':incoming_publication_status}
            if conflict not in decision['evidence']:decision['evidence'].append(conflict)
    db.execute("""INSERT INTO papers(
      identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at,
      abstract_source,low_priority,low_priority_reason,publication_type,publication_type_raw,
      publication_type_source,publication_type_evidence_json,eligibility_status,exclusion_reason,needs_rescreen,
      abstract_source_url,abstract_retrieved_at,published_precision
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
      doi=CASE WHEN excluded.doi<>'' THEN excluded.doi ELSE papers.doi END,
      title=excluded.title, abstract=CASE WHEN length(excluded.abstract)>length(papers.abstract) THEN excluded.abstract ELSE papers.abstract END,
      abstract_source=CASE WHEN length(excluded.abstract)>length(papers.abstract) THEN excluded.abstract_source ELSE papers.abstract_source END,
      abstract_source_url=CASE WHEN length(excluded.abstract)>length(papers.abstract) THEN excluded.abstract_source_url ELSE papers.abstract_source_url END,
      abstract_retrieved_at=CASE WHEN length(excluded.abstract)>length(papers.abstract) THEN excluded.abstract_retrieved_at ELSE papers.abstract_retrieved_at END,
      venue=excluded.venue, published=CASE WHEN excluded.published<>'' THEN excluded.published ELSE papers.published END,
      published_precision=CASE WHEN excluded.published='' THEN papers.published_precision
        WHEN excluded.published_precision<>'unknown' THEN excluded.published_precision
        WHEN excluded.published=papers.published THEN papers.published_precision ELSE 'unknown' END,
      url=excluded.url, authors_json=excluded.authors_json,
      low_priority=excluded.low_priority, low_priority_reason=excluded.low_priority_reason,
      publication_type=excluded.publication_type,publication_type_raw=excluded.publication_type_raw,
      publication_type_source=excluded.publication_type_source,
      publication_type_evidence_json=excluded.publication_type_evidence_json,
      eligibility_status=excluded.eligibility_status,exclusion_reason=excluded.exclusion_reason,
      needs_rescreen=CASE WHEN length(excluded.abstract)>length(papers.abstract)
        OR excluded.eligibility_status<>papers.eligibility_status OR excluded.publication_type<>papers.publication_type
        THEN 1 ELSE papers.needs_rescreen END,
      updated_at=excluded.updated_at""",
      (p.identity,p.doi,p.title,p.abstract,p.venue,p.published,p.url,json.dumps(p.authors,ensure_ascii=False),now,now,
       p.abstract_source,int(p.low_priority),p.low_priority_reason or None,decision["publication_type"],
       p.publication_type_raw or None,p.publication_type_source or None,json.dumps(decision["evidence"],ensure_ascii=False),
       decision["eligibility_status"],decision.get("exclusion_reason"),0,
       p.abstract_source_url or None,now if p.abstract else None,p.published_precision))
    db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?)",(p.identity,p.source,now)); return not exists

def render_digest(papers: list[tuple[Paper,dict[str,Any]]], failures: list[dict[str,str]], run_id: str) -> tuple[str,str]:
    md=[f"# Research paper digest — {run_id[:10]}","",f"Relevant new papers: **{len(papers)}**",""]
    for p,s in papers:
        md += [f"## [{p.title}]({p.url})",f"- Venue: {p.venue}",f"- Published: {publication_date_label(p.published,p.published_precision)}",
               f"- Relevance: {s['score']:.2f} ({s['reasons']})","",p.abstract or "*Abstract unavailable.*",""]
    if not papers: md += ["No new papers met the relevance threshold.",""]
    if failures:
        md += ["## Source warnings",""]+[f"- {x['source']}: {x['error']}" for x in failures]+[""]
    markdown="\n".join(md)
    body=[f"<h1>Research paper digest — {html.escape(run_id[:10])}</h1><p>Relevant new papers: <b>{len(papers)}</b></p>"]
    for p,s in papers:
        body += [f'<h2><a href="{html.escape(p.url)}">{html.escape(p.title)}</a></h2>',
                 f"<p><b>Venue:</b> {html.escape(p.venue)}<br><b>Published:</b> {html.escape(publication_date_label(p.published,p.published_precision))}<br><b>Relevance:</b> {s['score']:.2f} — {html.escape(s['reasons'])}</p>",
                 f"<p>{html.escape(p.abstract or 'Abstract unavailable.')}</p>"]
    if not papers: body.append("<p>No new papers met the relevance threshold.</p>")
    if failures: body.append("<h2>Source warnings</h2><ul>"+"".join(f"<li>{html.escape(x['source'])}: {html.escape(x['error'])}</li>" for x in failures)+"</ul>")
    return markdown,"\n".join(body)

def send_email(cfg: dict[str,Any], subject: str, text: str, html_body: str) -> None:
    d=cfg["delivery"]; user=os.getenv(d["username_env"]); password=os.getenv(d["password_env"])
    if not user or not password: raise RuntimeError("SMTP credentials are missing from environment")
    msg=email.message.EmailMessage(); msg["Subject"]=subject; msg["From"]=d["from_address"]; msg["To"]=", ".join(d["to_addresses"])
    msg.set_content(text); msg.add_alternative(html_body,subtype="html")
    with smtplib.SMTP_SSL(d["smtp_host"],int(d.get("smtp_port",465)),context=ssl.create_default_context(),timeout=30) as s:
        s.login(user,password); s.send_message(msg)

def doctor(config_path: Path) -> int:
    try:
        cfg=config_load(config_path); state=resolve_state(cfg,config_path); profile=state/cfg["profile_file"]
        issues=[]
        if sys.version_info < (3,9): issues.append("Python 3.9+ required")
        if not profile.exists(): issues.append(f"Profile not found: {profile}")
        for i,s in enumerate(cfg["sources"]):
            if s.get("type") not in ("crossref","crossref-query","openalex"): issues.append(f"sources[{i}] has unsupported type")
            if s.get("type")=="crossref" and not s.get("issn"): issues.append(f"sources[{i}] needs issn")
        print(json.dumps({"ok":not issues,"version":VERSION,"state_dir":str(state),"issues":issues},ensure_ascii=False,indent=2))
        return 0 if not issues else 2
    except Exception as e: print(json.dumps({"ok":False,"error":str(e)},ensure_ascii=False)); return 2

def row_to_paper(row: sqlite3.Row) -> Paper:
    return Paper(row["identity"],row["doi"] or "",row["title"],row["abstract"] or "",row["venue"] or "",
      row["published"] or "",row["url"] or "",json.loads(row["authors_json"] or "[]"),"database",
      row["abstract_source"] if "abstract_source" in row.keys() else "",
      bool(row["low_priority"]) if "low_priority" in row.keys() else False,
      row["low_priority_reason"] if "low_priority_reason" in row.keys() else "",
      row["publication_type_raw"] if "publication_type_raw" in row.keys() else "",
      row["publication_type_source"] if "publication_type_source" in row.keys() else "",
      "",
      row["publication_type"] if "publication_type" in row.keys() else "",
      row["abstract_source_url"] if "abstract_source_url" in row.keys() else "",
      row["published_precision"] if "published_precision" in row.keys() else "unknown")

def confirmed_profile(db: sqlite3.Connection, profile_path: Path) -> sqlite3.Row:
    # Hash the exact bytes written at profile confirmation. ``read_text``
    # performs universal-newline conversion and would otherwise make a valid
    # CRLF profile appear different on the next scheduled run.
    raw=profile_path.read_bytes(); content=raw.decode("utf-8"); digest=hashlib.sha256(raw).hexdigest()
    active=db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
    if not active:
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        with db:
            db.execute("""INSERT INTO profile_versions(
              profile_hash,content,status,source,change_summary,created_at,confirmed_at
            ) VALUES(?,?,'active','legacy_import','Imported existing profile',?,?)""",(digest,content,now,now))
        active=db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
    if active["profile_hash"]!=digest:
        raise ValueError("research-profile.md differs from the confirmed active version; create and confirm a profile draft")
    return active

def feedback_snapshot(db: sqlite3.Connection, per_class: int=20) -> list[dict[str,Any]]:
    examples=[]
    for interest in ("interested","not_interested"):
        rows=db.execute("""SELECT f.interest,f.reason,f.updated_at,p.identity,p.title,p.abstract,p.venue
          FROM paper_feedback f JOIN papers p ON p.identity=f.identity
          WHERE f.interest=? ORDER BY f.updated_at DESC LIMIT ?""",(interest,max(1,per_class))).fetchall()
        examples.extend(dict(row) for row in rows)
    examples.sort(key=lambda x:x["updated_at"],reverse=True)
    return examples

def update_source_health(db: sqlite3.Connection, source: str, status: str, now: str, error: str="") -> None:
    prior=db.execute("SELECT * FROM source_health WHERE source=?",(source,)).fetchone()
    failures=(int(prior["consecutive_failures"]) if prior else 0) + (1 if status=="failed" else 0)
    if status!="failed": failures=0
    # A fallback success does not prove that the other provider's window was
    # covered. Keep the previous complete checkpoint for recovery next time.
    last_success=now if status=="healthy" else (prior["last_success_at"] if prior else None)
    last_failure=now if status in ("degraded","failed") else (prior["last_failure_at"] if prior else None)
    db.execute("""INSERT INTO source_health VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
      status=excluded.status, consecutive_failures=excluded.consecutive_failures,
      last_success_at=excluded.last_success_at, last_failure_at=excluded.last_failure_at,
      last_error=excluded.last_error, updated_at=excluded.updated_at""",
      (source,status,failures,last_success,last_failure,error or None,now))

def enrich_missing_abstracts(papers: list[Paper], cfg: dict[str,Any], db: sqlite3.Connection | None=None) -> None:
    grouped: dict[str,list[Paper]]={}
    for paper in papers:
        paper.abstract=clean_abstract(paper.abstract)
        grouped.setdefault(paper.identity,[]).append(paper)
    for group in grouped.values():
        origin=max(group,key=lambda paper:len(paper.abstract))
        abstract=origin.abstract
        source=origin.abstract_source
        source_url=origin.abstract_source_url
        if not abstract and db is not None:
            prior=db.execute("SELECT abstract,abstract_source,abstract_source_url FROM papers WHERE identity=?",(group[0].identity,)).fetchone()
            abstract=clean_abstract(prior[0]) if prior else ""
            source=(prior[1] or "existing") if abstract else ""
            source_url=(prior[2] or "") if abstract else ""
        if not abstract:
            abstract=openalex_abstract(group[0].doi,cfg)
            source="openalex-doi" if abstract else ""
            source_url="https://api.openalex.org/works/https://doi.org/"+urllib.parse.quote(group[0].doi,safe="") if abstract else ""
        if abstract:
            for paper in group:
                if not paper.abstract:
                    paper.abstract=abstract
                    paper.abstract_source=source
                    paper.abstract_source_url=source_url
        for paper in group:
            if not paper.low_priority:
                paper.low_priority,paper.low_priority_reason=classify_low_priority(paper.title,paper.venue)
            paper.abstract_source=abstract_source_for(paper.abstract,paper.abstract_source)

def collect_into_db(cfg: dict[str,Any], db: sqlite3.Connection, now: str, run_id: str) -> tuple[list[Paper],list[Paper],list[dict[str,str]]]:
    default_since=(dt.date.today()-dt.timedelta(days=int(cfg.get("lookback_days",14)))).isoformat()
    failures=[]; collected=[]
    for source in cfg["sources"]:
        since = default_since
        previous = db.execute("SELECT last_success_at FROM source_health WHERE source=?", (source["name"],)).fetchone()
        if previous and previous[0]:
            try:
                recovery_since = (dt.date.fromisoformat(previous[0][:10]) - dt.timedelta(days=1)).isoformat()
                since = min(since, recovery_since)
            except ValueError:
                pass
        papers=[]; errors=[]; attempted=0; succeeded=0
        if source.get("type") in ("crossref","crossref-query"):
            attempted += 1
            try: papers.extend(crossref_collect(source,cfg,since)); succeeded += 1
            except Exception as e:
                if isinstance(e,CollectionIncomplete): papers.extend(e.papers)
                errors.append("Crossref: "+f"{type(e).__name__}: {str(e)[:200]}")
        if source.get("openalex_id") and (source.get("type")=="openalex" or cfg.get("collection",{}).get("openalex_fallback",True)):
            attempted += 1
            try: papers.extend(openalex_collect(source,cfg,since)); succeeded += 1
            except Exception as e:
                if isinstance(e,CollectionIncomplete): papers.extend(e.papers)
                errors.append("OpenAlex: "+f"{type(e).__name__}: {str(e)[:200]}")
        if not attempted:
            errors.append("No usable collection adapter is enabled for this source")
        if papers: enrich_missing_abstracts(papers,cfg,db)
        collected.extend(papers)
        status="healthy" if attempted and succeeded==attempted else ("degraded" if succeeded or papers else "failed")
        err="; ".join(errors)
        unique_count=len({paper.identity for paper in papers})
        db.execute("""INSERT OR REPLACE INTO source_runs(run_id,source,status,count,error,finished_at,since)
                   VALUES(?,?,?,?,?,?,?)""",
                   (run_id,source["name"],"ok" if status=="healthy" else status,unique_count,err,now,since))
        update_source_health(db,source["name"],status,now,err)
        if errors: failures.append({"source":source["name"],"status":status,"error":err})
        db.commit()
    new=[]
    for p in collected:
        with db:
            if upsert(db,p,now): new.append(p)
    unique_collected=list({paper.identity:paper for paper in collected}.values())
    unique_new=list({paper.identity:paper for paper in new}.values())
    return unique_collected,unique_new,failures

def collect_only(config_path: Path) -> int:
    """Collect the 14-day API window before official issue checks and queue freezing."""

    cfg=config_load(config_path); state=resolve_state(cfg,config_path); state.mkdir(parents=True,exist_ok=True)
    now=dt.datetime.now(dt.timezone.utc).isoformat(); run_id="collect-"+now.replace(":","-")
    db=db_open(state/"papers.sqlite3")
    collected,new,failures=collect_into_db(cfg,db,now,run_id)
    required={s["name"] for s in cfg["sources"] if s.get("required",True)}
    failed_required={x["source"] for x in failures if x.get("status")=="failed" and x["source"] in required}
    status="partial" if failures else "succeeded"
    if required and failed_required==required: status="failed"
    summary={
        "run_id":run_id,
        "started_at":now,
        "collected":len(collected),
        "new":len(new),
        "new_identities":[paper.identity for paper in new],
        "source_failures":failures,
        "next_step":"Run official two-issue imports, then agent-export --no-collect --batch-run " + run_id,
    }
    with db:
        db.execute("""INSERT OR REPLACE INTO pipeline_runs(
          run_id,kind,status,started_at,finished_at,collected_count,candidate_count,relevant_count,error_summary,details_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (run_id,"collection",status,now,dt.datetime.now(dt.timezone.utc).isoformat(),len(collected),0,0,
         "; ".join(x["error"] for x in failures) or None,json.dumps(summary,ensure_ascii=False)))
        for paper in collected:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'collected')",(run_id,paper.identity))
        for paper in new:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'new')",(run_id,paper.identity))
    db.close()
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 1 if required and failed_required==required else 0


def agent_export(config_path: Path, rescreen: bool=False, no_collect: bool=False,
                 batch_run: str | None=None, limit: int | None=None) -> int:
    cfg=config_load(config_path); state=resolve_state(cfg,config_path); state.mkdir(parents=True,exist_ok=True)
    profile_path=state/cfg["profile_file"]
    now=dt.datetime.now(dt.timezone.utc).isoformat(); run_id=now.replace(":","-"); db=db_open(state/"papers.sqlite3")
    active=confirmed_profile(db,profile_path); phash=active["profile_hash"]
    with db:
        stale=[row[0] for row in db.execute("SELECT run_id FROM agent_jobs WHERE status='exported'")]
        db.execute("UPDATE agent_jobs SET status='abandoned' WHERE status='exported'")
        if stale:
            marks=",".join("?" for _ in stale)
            db.execute(f"UPDATE pipeline_runs SET status='abandoned',finished_at=? WHERE run_id IN ({marks})",
                       (now,*stale))
    enrichment: dict[str,Any] | None = None
    if batch_run and not no_collect:
        raise ValueError("--batch-run requires --no-collect")
    if no_collect:
        collected,new,failures=[],[],[]
        if batch_run:
            batch=db.execute("SELECT * FROM pipeline_runs WHERE run_id=? AND kind='collection'",(batch_run,)).fetchone()
            if not batch:
                raise ValueError(f"Unknown collection batch: {batch_run}")
            details=json.loads(batch["details_json"] or "{}")
            failures=list(details.get("source_failures") or [])
            previous=db.execute(
                """SELECT imported_at FROM agent_jobs WHERE status='imported' AND imported_at<?
                ORDER BY imported_at DESC LIMIT 1""", (batch["started_at"],)
            ).fetchone()
            # Use the previous completed judgment as the daily boundary.  This
            # keeps genuinely new papers visible even if collection is retried
            # before the one authoritative export is frozen.
            if previous and previous["imported_at"]:
                fresh=db.execute("SELECT * FROM papers WHERE first_seen>? ORDER BY first_seen",(previous["imported_at"],)).fetchall()
            else:
                fresh=db.execute("SELECT * FROM papers WHERE first_seen>=? ORDER BY first_seen",(batch["started_at"],)).fetchall()
            collected=[row_to_paper(row) for row in fresh]
            new=list(collected)
    else:
        collected,new,failures=collect_into_db(cfg,db,now,run_id)
        # Complete traceable metadata before freezing the one authoritative
        # queue so this run retains its `new` identities for Today's Radar.
        enrichment=run_enrichment(state/"papers.sqlite3",cfg,limit=500)
    # Freeze evidence, calibration examples and the event watermark together.
    # A later cloud pull may carry an old UTC timestamp but still be new to
    # this host, so timestamps alone cannot detect feedback missing at export.
    db.execute("BEGIN IMMEDIATE")
    if rescreen:
        rows=db.execute("SELECT * FROM papers WHERE eligibility_status='eligible' ORDER BY first_seen").fetchall()
    else:
        # Daily judgment is once per identity. Changed evidence, feedback,
        # profiles, or rubrics do not authorize repeating an existing judgment;
        # only an explicit, user-requested --rescreen may do that.
        rows=db.execute("""SELECT p.* FROM papers p
          WHERE p.eligibility_status='eligible' AND NOT EXISTS(
            SELECT 1 FROM screenings s
            WHERE s.identity=p.identity AND s.provider='codex-agent')
          ORDER BY p.first_seen DESC,p.identity""").fetchall()
    total_candidates=len(rows)
    batch_limit=max(1,int(limit if limit is not None else cfg.get("max_candidates",120)))
    rows=rows[:batch_limit]
    papers=[asdict(row_to_paper(row)) for row in rows]
    queue_dir=state/"agent_queue"; queue_dir.mkdir(exist_ok=True)
    queue_path=queue_dir/f"{run_id.replace('+','_')}.json"
    examples=feedback_snapshot(db,int(cfg.get("feedback_examples_per_class",20)))
    payload={"schema_version":SCREENING_SCHEMA_VERSION,"run_id":run_id,"profile_hash":phash,"profile_version_id":active["id"],
             "profile_confirmed_at":active["confirmed_at"],"profile_path":str(profile_path),
             "feedback_examples":examples,"feedback_event_cursor":db.execute("SELECT COALESCE(MAX(id),0) FROM feedback_events").fetchone()[0],
             "threshold":float(cfg.get("relevance_threshold",0.70)),
             "papers":papers,"source_failures":failures,"collection_run_id":batch_run,
             "total_candidates":total_candidates,"deferred_candidates":total_candidates-len(papers),
             "evaluation_policy":evaluation_policy()}
    queue_path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    summary={"run_id":run_id,"collected":len(collected),"new":len(new),"candidates":len(papers),
             "queue_path":str(queue_path),"profile_path":str(profile_path),"source_failures":failures,
             "collection_run_id":batch_run,"total_candidates":total_candidates,
             "deferred_candidates":total_candidates-len(papers),"batch_limit":batch_limit}
    if enrichment is not None: summary["enrichment"]=enrichment
    run_status="running"  # An exported queue is not a completed recommendation update.
    profile_row=db.execute("SELECT id FROM profile_versions WHERE profile_hash=? AND status='active'",(phash,)).fetchone()
    with db:
        db.execute("""INSERT OR REPLACE INTO pipeline_runs(
          run_id,kind,status,profile_version_id,started_at,finished_at,collected_count,candidate_count,relevant_count,error_summary,details_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id,"agent-export",run_status,profile_row[0] if profile_row else None,now,dt.datetime.now(dt.timezone.utc).isoformat(),len(collected),len(papers),0,
         "; ".join(x["error"] for x in failures) or None,json.dumps(summary,ensure_ascii=False)))
        db.execute("""INSERT OR REPLACE INTO agent_jobs(
          run_id,profile_hash,status,queue_path,results_path,exported_count,imported_count,created_at,imported_at,
          profile_version_id,feedback_snapshot_json
        ) VALUES(?,?,'exported',?,NULL,?,0,?,NULL,?,?)""",
        (run_id,phash,str(queue_path),len(papers),now,active["id"],json.dumps(examples,ensure_ascii=False)))
        for paper in collected:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'collected')",(run_id,paper.identity))
        for paper in new:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'new')",(run_id,paper.identity))
        for paper in papers:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'candidate')",(run_id,paper["identity"]))
    required={s["name"] for s in cfg["sources"] if s.get("required",True)}
    failed_required={x["source"] for x in failures if x.get("status")=="failed" and x["source"] in required}
    db.close()
    print(json.dumps(summary,ensure_ascii=False,indent=2)); return 1 if required and failed_required==required else 0

def agent_import(config_path: Path, results_path: Path) -> int:
    cfg=config_load(config_path); state=resolve_state(cfg,config_path); db=db_open(state/"papers.sqlite3")
    active=confirmed_profile(db,state/cfg["profile_file"]); phash=active["profile_hash"]
    data=json.loads(results_path.read_text(encoding="utf-8"))
    if data.get("profile_hash") != phash: raise ValueError("Result profile_hash does not match the current research profile")
    results=data.get("results");
    if not isinstance(results,list): raise ValueError("results must be a list")
    now=dt.datetime.now(dt.timezone.utc).isoformat(); selected=[]; imported=0
    run_id=data.get("run_id") or now.replace(":","-")
    job=db.execute("SELECT * FROM agent_jobs WHERE run_id=?",(run_id,)).fetchone()
    if not job: raise ValueError("Results do not belong to an exported agent job")
    if job["status"]!="exported": raise ValueError(f"Agent job is not importable: {job['status']}")
    if job["profile_hash"]!=phash: raise ValueError("Agent job profile does not match the active profile")
    identities=[str(item.get("identity","")) for item in results]
    if len(identities)!=len(set(identities)): raise ValueError("results contain duplicate paper identities")
    queue_path=Path(job["queue_path"])
    if not queue_path.exists(): raise FileNotFoundError(f"Agent queue not found: {queue_path}")
    queue=json.loads(queue_path.read_text(encoding="utf-8"))
    if queue.get("run_id")!=run_id or queue.get("profile_hash")!=phash:
        raise ValueError("Agent queue metadata does not match the result job")
    if data.get("source_failures",[])!=queue.get("source_failures",[]):
        raise ValueError("Result source_failures do not match the exported queue")
    expected={paper["identity"] for paper in queue.get("papers",[])}
    frozen_papers={paper["identity"]:paper for paper in queue.get("papers",[])}
    feedback_cursor=queue.get("feedback_event_cursor")
    if type(feedback_cursor) is not int or feedback_cursor<0:feedback_cursor=None
    received=set(identities)
    if received != expected:
        missing=sorted(expected-received); extra=sorted(received-expected)
        raise ValueError(f"Results must cover the complete queue (missing={len(missing)}, extra={len(extra)})")
    threshold=float(cfg.get("relevance_threshold",0.70))
    validated=[]; validation_errors=[]
    queue_schema_version=int(queue.get("schema_version", 1))
    for item in results:
        identity_value=str(item.get("identity","") or "")
        row=db.execute("SELECT * FROM papers WHERE identity=?",(identity_value,)).fetchone()
        if not row:
            validation_errors.append(f"{identity_value or '<missing identity>'}: unknown paper identity")
            continue
        try:
            if queue_schema_version >= 3:
                result=structured_judgment(item,row,threshold,queue_schema_version)
            else:
                result=extract_json(json.dumps(item,ensure_ascii=False)); result["relevant"]=result["score"]>=threshold
                if not (row["abstract"] or "").strip():
                    result["confidence"]=min(result["confidence"],0.5)
                result["reasoning"]={}
                result["score_dimensions"]={}
                result["rubric_version"]="legacy"
        except (TypeError, ValueError) as exc:
            validation_errors.append(f"{identity_value}: {exc}")
            continue
        validated.append((identity_value,row,result))
        if result["relevant"]: selected.append((row_to_paper(row),result))
    if validation_errors:
        db.close()
        raise ValueError("Agent result validation failed:\n- " + "\n- ".join(validation_errors))
    snapshot=job["feedback_snapshot_json"]; version_id=job["profile_version_id"]
    model=str(data.get("model","")).strip()
    if not model: raise ValueError("results must identify the actual model")
    digest_dir=state/"digests"; digest_dir.mkdir(exist_ok=True)
    markdown,_=render_digest(selected,data.get("source_failures",[]),run_id)
    digest_path=digest_dir/f"{str(run_id).replace('+','_')}-agent.md"
    digest_tmp=digest_path.with_name(f".{digest_path.name}.{os.getpid()}.tmp")
    digest_tmp.write_text(markdown,encoding="utf-8")
    digest_replaced=False
    try:
        with db:
            for identity_value,_,result in validated:
                db.execute("""INSERT OR REPLACE INTO screenings(
              identity,profile_hash,provider,model,relevant,score,reasons,themes_json,confidence,screened_at,
              profile_version_id,feedback_snapshot_json,run_id,reasoning_json,
              score_dimensions_json,rubric_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (identity_value,phash,"codex-agent",model,int(result["relevant"]),result["score"],
                   result["reasons"],json.dumps(result["matched_themes"],ensure_ascii=False),result["confidence"],now,
                   version_id,snapshot,run_id,json.dumps(result["reasoning"],ensure_ascii=False),
                   json.dumps(result["score_dimensions"],ensure_ascii=False),result["rubric_version"]))
                frozen=frozen_papers[identity_value]
                db.execute("""UPDATE papers SET needs_rescreen=CASE
                  WHEN COALESCE(title,'')<>? OR COALESCE(abstract,'')<>? OR EXISTS(
                    SELECT 1 FROM feedback_events f WHERE f.identity=papers.identity
                    AND ((? IS NOT NULL AND f.id>?) OR (? IS NULL
                      AND julianday(f.created_at)>julianday(?)))) THEN 1 ELSE 0 END WHERE identity=?""",
                  (frozen.get('title') or '',frozen.get('abstract') or '',feedback_cursor,feedback_cursor,
                   feedback_cursor,job['created_at'],identity_value))
                if result["relevant"]:
                    db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'selected')",(run_id,identity_value))
                    if db.execute("SELECT 1 FROM run_papers WHERE run_id=? AND identity=? AND role='new'",(run_id,identity_value)).fetchone():
                        db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'selected_new')",(run_id,identity_value))
            snapshot_run(db, run_id)
            imported=len(validated)
            db.execute("""UPDATE pipeline_runs SET status=?,finished_at=?,relevant_count=?,details_json=?
              WHERE run_id=?""",("partial" if queue.get("source_failures") else "succeeded",now,len(selected),json.dumps({"results_path":str(results_path),"digest_path":str(digest_path),"collection_run_id":queue.get("collection_run_id")},ensure_ascii=False),run_id))
            db.execute("""UPDATE agent_jobs SET status='imported',results_path=?,imported_count=?,imported_at=?
              WHERE run_id=?""",(str(results_path),imported,now,run_id))
            os.replace(digest_tmp,digest_path); digest_replaced=True
    except Exception:
        if digest_tmp.exists(): digest_tmp.unlink()
        if digest_replaced and digest_path.exists(): digest_path.unlink()
        raise
    db.close()
    summary={"run_id":run_id,"imported":imported,"relevant":len(selected),"digest_path":str(digest_path)}
    try:
        from academic_radar.cloud_sync import sync_configured
        summary["cloud_sync"]=sync_configured(config_path)
    except ImportError:
        if cfg.get("cloud_sync",{}).get("enabled"):
            summary["cloud_sync"]={"status":"failed","error":"Cloud synchronization module is unavailable; local judgment import remains committed"}
        else:
            summary["cloud_sync"]={"status":"skipped","reason":"Cloud synchronization is not configured"}
    except Exception as exc:
        summary["cloud_sync"]={"status":"failed","error":f"{type(exc).__name__}: {str(exc)[:300]}","local_import_committed":True}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 1 if summary["cloud_sync"].get("status")=="failed" else 0

def backfill_history(config_path: Path) -> int:
    """Recover overwritten judgments only from their matching frozen import artifacts."""
    cfg=config_load(config_path); state=resolve_state(cfg,config_path)
    db=db_open(state/"papers.sqlite3")
    recovered=0; unavailable=0
    try:
        jobs=db.execute("""SELECT aj.*,pr.relevant_count FROM agent_jobs aj
          LEFT JOIN pipeline_runs pr ON pr.run_id=aj.run_id
          WHERE aj.status='imported' AND (
            EXISTS(SELECT 1 FROM recommendation_snapshots rs WHERE rs.run_id=aj.run_id AND rs.evidence_source='unavailable')
            OR (pr.relevant_count>0 AND NOT EXISTS(
              SELECT 1 FROM recommendation_snapshots rs WHERE rs.run_id=aj.run_id)))""").fetchall()
        for job in jobs:
            try:
                data=json.loads(Path(job['results_path']).read_text(encoding='utf-8'))
                queue=json.loads(Path(job['queue_path']).read_text(encoding='utf-8'))
                if any(artifact.get('run_id')!=job['run_id'] or artifact.get('profile_hash')!=job['profile_hash']
                       for artifact in (data,queue)):
                    continue
                originals={p['identity']:p for p in queue['papers']}
                items={p['identity']:p for p in data['results']}
                if len(items)!=len(data['results']):
                    continue
            except (OSError,ValueError,KeyError,TypeError):
                continue
            with db:
                # Very old imports predate run_papers. Reconstruct membership
                # only when the complete frozen queue and recorded count agree.
                if not db.execute('SELECT 1 FROM recommendation_snapshots WHERE run_id=?',(job['run_id'],)).fetchone():
                    try:
                        threshold=float(queue['threshold']); schema=int(queue.get('schema_version',1))
                        if set(items)!=set(originals):
                            continue
                        judgments={identity:(structured_judgment(item,originals[identity],threshold,schema)
                          if schema>=3 else extract_json(json.dumps(item,ensure_ascii=False))) for identity,item in items.items()}
                        selected_ids=[identity for identity,result in judgments.items() if result['score']>=threshold]
                        if len(selected_ids)!=job['relevant_count']:
                            continue
                        for identity in selected_ids:
                            db.execute("""INSERT OR IGNORE INTO recommendation_snapshots(run_id,identity)
                              SELECT ?,identity FROM papers WHERE identity=?""",(job['run_id'],identity))
                    except (ValueError,KeyError,TypeError):
                        continue
                missing=db.execute("SELECT identity FROM recommendation_snapshots WHERE run_id=? AND evidence_source='unavailable'",
                                   (job['run_id'],)).fetchall()
                for saved in missing:
                    identity_value=saved['identity']
                    try:
                        item=items[identity_value]; original=originals[identity_value]
                        schema=int(queue.get('schema_version',1))
                        if schema>=3:
                            result=structured_judgment(item,original,0.70,schema)
                        else:
                            result=extract_json(json.dumps(item,ensure_ascii=False))
                            if not (original.get('abstract') or '').strip():
                                result['confidence']=min(result['confidence'],0.5)
                        db.execute("""UPDATE recommendation_snapshots SET score=?,reasons=?,confidence=?,themes_json=?,
                          screened_at=?,reasoning_json=?,score_dimensions_json=?,rubric_version=?,evidence_source='import-result'
                          WHERE run_id=? AND identity=? AND evidence_source='unavailable'""",
                          (result['score'],result['reasons'],result['confidence'],json.dumps(result['matched_themes'],ensure_ascii=False),
                           job['imported_at'],json.dumps(result.get('reasoning',{}),ensure_ascii=False),
                           json.dumps(result.get('score_dimensions',{})),result.get('rubric_version','legacy'),job['run_id'],identity_value))
                        recovered+=1
                    except (ValueError,KeyError,TypeError):
                        continue
        unavailable=db.execute("SELECT COUNT(*) FROM recommendation_snapshots WHERE evidence_source='unavailable'").fetchone()[0]
    finally:
        db.close()
    print(json.dumps({'recovered':recovered,'unavailable':unavailable}))
    return 0


def enrich_abstracts(config_path: Path, limit: int=100) -> int:
    """Run the traceable multi-provider metadata enrichment pipeline."""
    cfg=config_load(config_path); state=resolve_state(cfg,config_path)
    result=run_enrichment(state/"papers.sqlite3",cfg,limit=limit)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--version",action="version",version=VERSION)
    sub=parser.add_subparsers(dest="command",required=True)
    for name in ("doctor","collect-only","agent-export","agent-import","enrich-abstracts","backfill-history"):
        p=sub.add_parser(name); p.add_argument("--config",required=True,type=Path)
        if name=="agent-export":
            p.add_argument("--rescreen",action="store_true",help="manual, user-requested re-evaluation of stored eligible papers; default exports only identities never judged by Codex")
            p.add_argument("--no-collect",action="store_true",help="export from the database without network collection")
            p.add_argument("--batch-run",help="collection run to carry into this frozen queue")
            p.add_argument("--limit",type=int,help="maximum candidates in one complete frozen queue (config max_candidates, default 120)")
        elif name=="agent-import":
            p.add_argument("--results",required=True,type=Path)
        elif name=="enrich-abstracts":
            p.add_argument("--limit",type=int,default=100)
    a=parser.parse_args()
    try:
        if a.command=="doctor": return doctor(a.config)
        if a.command=="collect-only": return collect_only(a.config)
        if a.command=="agent-export": return agent_export(a.config,a.rescreen,a.no_collect,a.batch_run,a.limit)
        if a.command=="agent-import": return agent_import(a.config,a.results)
        if a.command=="enrich-abstracts": return enrich_abstracts(a.config,a.limit)
        if a.command=="backfill-history": return backfill_history(a.config)
        raise ValueError("Unsupported command")
    except Exception as e: print(json.dumps({"ok":False,"error":f"{type(e).__name__}: {e}"},ensure_ascii=False),file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())

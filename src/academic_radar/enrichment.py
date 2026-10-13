"""Traceable, non-generative abstract enrichment and manual evidence import."""

from __future__ import annotations

import csv
import datetime as dt
import html
from html.parser import HTMLParser
import io
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from .governance import publication_decision, should_preserve_publication_metadata
from .storage import connect, utc_now


PROVIDER_LABELS = {
    "local": "本地同 DOI 记录",
    "crossref": "Crossref",
    "openalex": "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
    "europe_pmc": "Europe PMC",
    "pubmed": "PubMed",
    "elsevier": "Elsevier 官方元数据",
    "publisher": "出版商官方页面",
}
MANUAL_EVIDENCE_TYPES = {
    "crossref_metadata", "openalex_metadata", "semantic_scholar_record",
    "europe_pmc_record", "pubmed_record", "publisher_metadata", "official_page",
    "author_manuscript", "elsevier_api_record",
}


def clean_abstract(value: Any) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text=re.sub(r"\s+", " ", html.unescape(text)).strip()
    if text.lower().rstrip(".") in {
        "international audience","national audience","no abstract available",
        "abstract not available","no abstract","no abstract provided",
    }:
        return ""
    return text


def inverted_abstract(value: dict[str, list[int]] | None) -> str:
    words: list[tuple[int, str]] = []
    for word, positions in (value or {}).items():
        words.extend((int(position), word) for position in positions)
    return " ".join(word for _, word in sorted(words))


def _mailto(user_agent: str) -> str:
    match = re.search(r"mailto:([^\s;)]+)", user_agent or "")
    return match.group(1) if match else ""


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _title_matches(expected: str, actual: str) -> bool:
    left, right = _normalized_title(expected), _normalized_title(actual)
    if not left or not right:
        return False
    return left == right or (len(left) > 24 and (left in right or right in left))


class OfficialMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        item = {key.lower(): value or "" for key, value in attrs}
        name = (item.get("name") or item.get("property") or "").lower()
        content = item.get("content", "").strip()
        if name and content and name not in self.values:
            self.values[name] = content


class ProviderTemporarilyUnavailable(RuntimeError):
    """Stop repeated calls after a provider's retries have been exhausted."""


class EnrichmentBudgetExceeded(TimeoutError):
    """Defer unfinished enrichment without blocking semantic import."""


class MetadataClient:
    def __init__(self, config: dict[str, Any]) -> None:
        collection = config.get("collection", {})
        self.user_agent = str(config.get("user_agent", "PersonalAcademicRadar/0.8"))
        self.timeout = int(collection.get("timeout_seconds", 30))
        self.retries = min(3, max(0, int(collection.get("max_retries", 2))))
        self.last_request: dict[str, float] = {}
        self.provider_backoff_until: dict[str, float] = {}
        self.semantic_scholar_cache: dict[str, dict[str, Any] | None] | None = None
        self.deadline: float | None = None
        key_env = str(collection.get("elsevier_api_key_env", "ELSEVIER_API_KEY"))
        self.elsevier_api_key = os.environ.get(key_env, "") if key_env else ""
        self.minimum_interval = {
            "crossref": 0.12,
            "openalex": 0.12,
            "semantic_scholar": 1.05,
            "europe_pmc": 0.35,
            "pubmed": 0.36,
            "elsevier": 0.35,
            "publisher": 0.5,
        }

    def request(
        self, provider: str, url: str, *, max_bytes: int = 2_000_000, data: bytes | None = None,
        content_type: str = "", accept: str = "application/json, application/xml, text/html;q=0.8",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str, str]:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise EnrichmentBudgetExceeded("摘要补全时间预算已到，剩余项留待后续运行")
        backoff = self.provider_backoff_until.get(provider, 0.0) - time.monotonic()
        if backoff > 0:
            raise ProviderTemporarilyUnavailable(
                f"{provider} 暂停本轮后续请求，保留此前失败证据（约 {int(backoff)} 秒）"
            )
        elapsed = time.monotonic() - self.last_request.get(provider, 0.0)
        delay = self.minimum_interval.get(provider, 0.2) - elapsed
        if delay > 0:
            time.sleep(delay)
        headers = {"User-Agent": self.user_agent, "Accept": accept}
        headers.update(extra_headers or {})
        if content_type:
            headers["Content-Type"] = content_type
        for attempt in range(self.retries + 1):
            remaining = self.deadline - time.monotonic() if self.deadline is not None else self.timeout
            if remaining <= 0:
                raise EnrichmentBudgetExceeded("摘要补全时间预算已到，剩余项留待后续运行")
            try:
                request = urllib.request.Request(url, headers=headers, data=data)
                with urllib.request.urlopen(request, timeout=min(self.timeout, remaining)) as response:
                    self.last_request[provider] = time.monotonic()
                    content_type = response.headers.get("Content-Type", "")
                    data = response.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise ValueError("响应超过安全大小限制")
                    return data, response.geturl(), content_type
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                self.last_request[provider] = time.monotonic()
                status = getattr(exc, "code", None)
                transient = status in {408, 429, 500, 502, 503, 504} or status is None
                retry_after = getattr(exc, "headers", {}).get("Retry-After") if getattr(exc, "headers", None) else None
                try:
                    wait = float(retry_after) if retry_after else 1.5 * (2 ** attempt)
                except ValueError:
                    wait = 1.5 * (2 ** attempt)
                if attempt >= self.retries or not transient:
                    if status == 429:
                        # A single enrichment run can contain hundreds of papers.
                        # After retries are exhausted, do not repeat the same
                        # rejected call for every remaining record.
                        self.provider_backoff_until[provider] = max(
                            self.provider_backoff_until.get(provider, 0.0),
                            time.monotonic() + max(600.0, wait),
                        )
                    elif provider!="publisher" and (
                        status in {500,502,503,504} or
                        (status is None and isinstance(exc,(urllib.error.URLError,TimeoutError)))
                    ):
                        # Central API transport failures should not spend the
                        # entire daily budget retrying the same outage for each
                        # DOI. Publisher requests may target different hosts.
                        self.provider_backoff_until[provider]=max(
                            self.provider_backoff_until.get(provider,0.0),
                            time.monotonic()+300.0,
                        )
                    raise
                remaining = self.deadline - time.monotonic() if self.deadline is not None else 30.0
                time.sleep(min(30.0, max(0.0, remaining), max(0.0, wait)))
        raise RuntimeError("metadata request failed")

    def json(self, provider: str, url: str) -> tuple[dict[str, Any], str]:
        data, final_url, _ = self.request(provider, url)
        return json.loads(data.decode("utf-8")), final_url


def lookup_local(db: sqlite3.Connection, paper: dict[str, Any], _: MetadataClient) -> dict[str, Any] | None:
    doi = (paper.get("doi") or "").strip().lower()
    if not doi:
        return None
    row = db.execute(
        """SELECT identity,abstract,abstract_source,abstract_source_url,publication_type_raw,
        publication_type_source FROM papers WHERE lower(doi)=? AND identity<>?
        AND COALESCE(abstract,'')<>'' ORDER BY length(abstract) DESC LIMIT 1""",
        (doi, paper["identity"]),
    ).fetchone()
    if not row:
        return None
    return {
        "abstract": row["abstract"],
        "source_name": "local-same-doi",
        "source_url": row["abstract_source_url"] or paper.get("url") or "",
        "evidence_type": "local_same_doi",
        "publication_type_raw": row["publication_type_raw"] or "",
        "publication_type_source": row["publication_type_source"] or "local-same-doi",
    }


def lookup_crossref(_: sqlite3.Connection, paper: dict[str, Any], client: MetadataClient) -> dict[str, Any] | None:
    doi = (paper.get("doi") or "").strip()
    if not doi:
        return None
    params = {}
    if _mailto(client.user_agent):
        params["mailto"] = _mailto(client.user_agent)
    url = "https://api.crossref.org/v1/works/" + urllib.parse.quote(doi, safe="")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    payload, final_url = client.json("crossref", url)
    item = payload.get("message") or {}
    returned_doi = str(item.get("DOI") or "").lower()
    if returned_doi != doi.lower():
        return None
    titles = item.get("title") or []
    actual_title = " ".join(titles) if isinstance(titles, list) else str(titles)
    if actual_title and not _title_matches(paper.get("title", ""), actual_title):
        return None
    return {
        "abstract": clean_abstract(item.get("abstract")),
        "source_name": "crossref",
        "source_url": final_url,
        "evidence_type": "crossref_metadata",
        "publication_type_raw": str(item.get("type") or ""),
        "publication_type_source": "crossref",
        "source_kind": "",
    }


def lookup_openalex(_: sqlite3.Connection, paper: dict[str, Any], client: MetadataClient) -> dict[str, Any] | None:
    doi = (paper.get("doi") or "").strip()
    if not doi:
        return None
    identifier = "doi:" + doi
    params = {"select": "id,doi,title,abstract_inverted_index,type,type_crossref,primary_location"}
    if _mailto(client.user_agent):
        params["mailto"] = _mailto(client.user_agent)
    url = "https://api.openalex.org/works/" + urllib.parse.quote(identifier, safe=":") + "?" + urllib.parse.urlencode(params)
    item, final_url = client.json("openalex", url)
    if str(item.get("doi") or "").lower().removeprefix("https://doi.org/") != doi.lower():
        return None
    actual_title = str(item.get("title") or "")
    if actual_title and not _title_matches(paper.get("title", ""), actual_title):
        return None
    location = item.get("primary_location") or {}
    source = location.get("source") or {}
    raw = str(item.get("type_crossref") or item.get("type") or "")
    return {
        "abstract": inverted_abstract(item.get("abstract_inverted_index")),
        "source_name": "openalex",
        "source_url": final_url,
        "evidence_type": "openalex_metadata",
        "publication_type_raw": raw,
        "publication_type_source": "openalex",
        "source_kind": str(source.get("type") or ""),
    }


def lookup_semantic_scholar(_: sqlite3.Connection, paper: dict[str, Any], client: MetadataClient) -> dict[str, Any] | None:
    doi = (paper.get("doi") or "").strip()
    if not doi:
        return None
    params = {"fields": "title,abstract,url,externalIds,publicationTypes,venue,year"}
    if client.semantic_scholar_cache is not None and doi.lower() in client.semantic_scholar_cache:
        item = client.semantic_scholar_cache[doi.lower()]
        if not item:
            return None
        final_url = "https://api.semanticscholar.org/graph/v1/paper/batch?" + urllib.parse.urlencode(params)
    else:
        identifier = urllib.parse.quote("DOI:" + doi, safe=":")
        url = f"https://api.semanticscholar.org/graph/v1/paper/{identifier}?{urllib.parse.urlencode(params)}"
        item, final_url = client.json("semantic_scholar", url)
    if not _title_matches(paper.get("title", ""), str(item.get("title") or "")):
        return None
    publication_types = item.get("publicationTypes") or []
    raw_type = publication_types[0] if len(publication_types) == 1 else ""
    raw_map = {"JournalArticle": "journal-article", "Conference": "conference-paper"}
    return {
        "abstract": clean_abstract(item.get("abstract")),
        "source_name": "semantic-scholar",
        "source_url": final_url,
        "evidence_type": "semantic_scholar_record",
        "publication_type_raw": raw_map.get(str(raw_type), str(raw_type)),
        "publication_type_source": "semantic-scholar",
    }


def prime_semantic_scholar_batch(client: MetadataClient, papers: list[dict[str, Any]]) -> None:
    """Fetch DOI records in one official batch request to avoid shared-pool 429s."""

    dois = list(dict.fromkeys(str(paper.get("doi") or "").strip().lower() for paper in papers))
    dois = [doi for doi in dois if doi][:500]
    if not dois:
        client.semantic_scholar_cache = {}
        return
    params = {"fields": "title,abstract,url,externalIds,publicationTypes,venue,year"}
    url = "https://api.semanticscholar.org/graph/v1/paper/batch?" + urllib.parse.urlencode(params)
    body = json.dumps({"ids": ["DOI:" + doi for doi in dois]}).encode("utf-8")
    raw, _, _ = client.request(
        "semantic_scholar", url, data=body, content_type="application/json", max_bytes=8_000_000,
    )
    items = json.loads(raw.decode("utf-8"))
    if not isinstance(items, list) or len(items) != len(dois):
        raise ValueError("Semantic Scholar 批量响应数量不匹配")
    client.semantic_scholar_cache = {doi: item if isinstance(item, dict) else None for doi, item in zip(dois, items)}


def lookup_europe_pmc(_: sqlite3.Connection, paper: dict[str, Any], client: MetadataClient) -> dict[str, Any] | None:
    doi = (paper.get("doi") or "").strip()
    if not doi:
        return None
    params = {"query": f'DOI:"{doi}"', "format": "json", "resultType": "core", "pageSize": "3"}
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(params)
    payload, final_url = client.json("europe_pmc", url)
    for item in (payload.get("resultList") or {}).get("result", []):
        if str(item.get("doi") or "").lower() != doi.lower():
            continue
        if item.get("title") and not _title_matches(paper.get("title", ""), str(item["title"])):
            continue
        types = item.get("pubTypeList", {}).get("pubType", []) if isinstance(item.get("pubTypeList"), dict) else []
        raw_type = "journal-article" if "Journal Article" in types else ""
        return {
            "abstract": clean_abstract(item.get("abstractText")),
            "source_name": "europe-pmc",
            "source_url": final_url,
            "evidence_type": "europe_pmc_record",
            "publication_type_raw": raw_type,
            "publication_type_source": "europe-pmc",
        }
    return None


def lookup_pubmed(_: sqlite3.Connection, paper: dict[str, Any], client: MetadataClient) -> dict[str, Any] | None:
    doi = (paper.get("doi") or "").strip()
    if not doi:
        return None
    common = {"tool": "personal-academic-radar"}
    if _mailto(client.user_agent):
        common["email"] = _mailto(client.user_agent)
    search_params = {**common, "db": "pubmed", "term": f"{doi}[doi]", "retmax": "2"}
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(search_params)
    raw, _, _ = client.request("pubmed", search_url)
    ids = [node.text or "" for node in ET.fromstring(raw).findall(".//Id")]
    if len(ids) != 1:
        return None
    fetch_params = {**common, "db": "pubmed", "id": ids[0], "retmode": "xml"}
    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(fetch_params)
    raw, final_url, _ = client.request("pubmed", fetch_url)
    root = ET.fromstring(raw)
    returned_dois = ["".join(node.itertext()).strip() for node in root.findall(".//ArticleId[@IdType='doi']")]
    if not any(value.lower() == doi.lower() for value in returned_dois):
        return None
    title = " ".join("".join(node.itertext()) for node in root.findall(".//ArticleTitle"))
    if title and not _title_matches(paper.get("title", ""), title):
        return None
    parts = []
    for node in root.findall(".//Abstract/AbstractText"):
        label = node.attrib.get("Label", "").strip()
        body = "".join(node.itertext()).strip()
        parts.append((label + ": " if label else "") + body)
    publication_types = ["".join(node.itertext()).strip() for node in root.findall(".//PublicationType")]
    raw_type = "journal-article" if "Journal Article" in publication_types else ""
    return {
        "abstract": clean_abstract(" ".join(parts)),
        "source_name": "pubmed",
        "source_url": final_url,
        "evidence_type": "pubmed_record",
        "publication_type_raw": raw_type,
        "publication_type_source": "pubmed",
    }


def lookup_publisher(_: sqlite3.Connection, paper: dict[str, Any], client: MetadataClient) -> dict[str, Any] | None:
    target = str(paper.get("url") or "").strip()
    if not target and paper.get("doi"):
        target = "https://doi.org/" + str(paper["doi"])
    if not target.startswith(("http://", "https://")):
        return None
    raw, final_url, content_type = client.request("publisher", target, accept="text/html,application/xhtml+xml")
    if "html" not in content_type.lower():
        return None
    parser = OfficialMetaParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    actual_title = parser.values.get("citation_title") or parser.values.get("dc.title") or ""
    if not _title_matches(paper.get("title", ""), actual_title):
        return None
    abstract = ""
    evidence_name = ""
    for name in ("citation_abstract", "prism.abstract", "eprints.abstract"):
        value = clean_abstract(parser.values.get(name))
        if len(value) > len(abstract):
            abstract, evidence_name = value, name
    raw_type = (
        parser.values.get("citation_article_type") or parser.values.get("prism.section")
        or parser.values.get("dc.type") or ""
    ).strip()
    if not abstract and not raw_type:
        return None
    return {
        "abstract": abstract,
        "source_name": "publisher-official",
        "source_url": final_url,
        "evidence_type": "publisher_metadata:" + evidence_name,
        "publication_type_raw": raw_type,
        "publication_type_source": "publisher-official",
        "source_kind": "journal",
    }


def lookup_elsevier(_: sqlite3.Connection, paper: dict[str, Any], client: MetadataClient) -> dict[str, Any] | None:
    """Use Elsevier's official metadata API when the configured key is available."""

    doi = str(paper.get("doi") or "").strip().lower()
    if not doi.startswith("10.1016/") or not client.elsevier_api_key:
        return None
    url = (
        "https://api.elsevier.com/content/article/doi/" +
        urllib.parse.quote(doi, safe="") + "?httpAccept=application%2Fjson"
    )
    raw, final_url, _ = client.request(
        "elsevier", url, accept="application/json",
        extra_headers={"X-ELS-APIKey": client.elsevier_api_key}, max_bytes=8_000_000,
    )
    payload = json.loads(raw.decode("utf-8"))
    response = payload.get("full-text-retrieval-response") or {}
    core = response.get("coredata") or {}
    actual_doi = str(core.get("prism:doi") or "").lower()
    actual_title = str(core.get("dc:title") or "")
    if actual_doi != doi or not _title_matches(str(paper.get("title") or ""), actual_title):
        return None
    abstract = clean_abstract(core.get("dc:description") or core.get("prism:abstract"))
    if not abstract:
        return None
    return {
        "abstract": abstract,
        "source_name": "elsevier-official-api",
        "source_url": final_url,
        "evidence_type": "elsevier_api_record",
        "publication_type_raw": "journal-article",
        "publication_type_source": "elsevier-official-api",
        "source_kind": "journal",
    }


PROVIDERS: list[tuple[str, Callable[[sqlite3.Connection, dict[str, Any], MetadataClient], dict[str, Any] | None]]] = [
    ("local", lookup_local),
    ("crossref", lookup_crossref),
    ("openalex", lookup_openalex),
    ("semantic_scholar", lookup_semantic_scholar),
    ("europe_pmc", lookup_europe_pmc),
    ("pubmed", lookup_pubmed),
    ("elsevier", lookup_elsevier),
    ("publisher", lookup_publisher),
]


def _record_attempt(
    db: sqlite3.Connection,
    task_id: str,
    identity: str,
    provider: str,
    status: str,
    source_url: str = "",
    evidence_type: str = "",
    detail: str = "",
) -> None:
    db.execute(
        """INSERT INTO abstract_attempts(
        task_id,identity,provider,status,source_url,evidence_type,detail,attempted_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (task_id, identity, provider, status, source_url or None, evidence_type or None, detail[:500] or None, utc_now()),
    )


def _start_task(db: sqlite3.Connection, task_type: str, total: int) -> str:
    task_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    now = utc_now()
    try:
        with db:
            db.execute(
                """INSERT INTO task_runs(
                task_id,task_type,status,total_count,created_at,started_at,message
                ) VALUES(?,?,'running',?,?,?,'正在处理')""",
                (task_id, task_type, total, now, now),
            )
    except sqlite3.IntegrityError as exc:
        raise RuntimeError("同类任务已经在运行，请等待完成后再试") from exc
    return task_id


def _finish_task(db: sqlite3.Connection, task_id: str, status: str, details: dict[str, Any]) -> None:
    message = {
        "succeeded": "任务已完成",
        "partial": "任务部分完成，可稍后重试失败项",
        "failed": "任务失败，现有数据未被覆盖",
    }[status]
    with db:
        db.execute(
            """UPDATE task_runs SET status=?,completed_count=?,success_count=?,failure_count=?,
            message=?,details_json=?,finished_at=? WHERE task_id=?""",
            (
                status, details.get("checked", 0), details.get("updated", 0), details.get("unresolved", 0),
                message, json.dumps(details, ensure_ascii=False), utc_now(), task_id,
            ),
        )


def enrich_abstracts(
    db_path: Path,
    config: dict[str, Any],
    *,
    limit: int = 500,
    retry: bool = False,
    include_type_unknown: bool = True,
) -> dict[str, Any]:
    """Enrich missing abstracts and type evidence without generating text."""

    db = connect(db_path)
    client = MetadataClient(config)
    client.deadline = time.monotonic() + max(1.0, float(config.get("enrichment", {}).get("time_budget_seconds", 180)))
    try:
        where = "eligibility_status<>'excluded' AND COALESCE(abstract,'')=''"
        if include_type_unknown:
            where = "eligibility_status<>'excluded' AND (COALESCE(abstract,'')='' OR publication_type='Unknown' OR eligibility_status='quarantine')"
        paper_rows = [dict(row) for row in db.execute(
            f"""SELECT * FROM papers WHERE {where} ORDER BY
            COALESCE((SELECT MAX(attempted_at) FROM abstract_attempts a WHERE a.identity=papers.identity),'') ASC,
            first_seen DESC LIMIT ?""", (max(1, limit),)
        )]
        task_id = _start_task(db, "abstract_enrichment", len(paper_rows))
        if any(lookup is lookup_semantic_scholar for _, lookup in PROVIDERS):
            try:
                prime_semantic_scholar_batch(client, paper_rows)
            except Exception:
                # The ordinary per-paper path remains available and records its own evidence.
                client.semantic_scholar_cache = None
        updated = 0
        type_updated = 0
        unresolved: list[dict[str, str]] = []
        provider_found: dict[str, int] = {}
        checked = 0
        budget_exhausted = False
        for index, paper in enumerate(paper_rows, 1):
            if time.monotonic() >= client.deadline:
                budget_exhausted = True
                break
            abstract_found = bool((paper.get("abstract") or "").strip())
            type_found = paper.get("publication_type") != "Unknown" and paper.get("eligibility_status") != "quarantine"
            failures: list[str] = []
            for provider, lookup in PROVIDERS:
                if time.monotonic() >= client.deadline:
                    budget_exhausted = True
                    break
                if (abstract_found and type_found) or paper.get("eligibility_status")=="excluded":
                    break
                if not retry:
                    recent = db.execute(
                        """SELECT status,attempted_at FROM abstract_attempts
                        WHERE identity=? AND provider=? AND status IN ('not_found','failed')
                        ORDER BY attempted_at DESC LIMIT 1""",
                        (paper["identity"], provider),
                    ).fetchone()
                    if recent:
                        try:
                            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(recent["attempted_at"])
                        except ValueError:
                            age = dt.timedelta(days=1)
                        if age < dt.timedelta(hours=24):
                            _record_attempt(db, task_id, paper["identity"], provider, "skipped", detail="二十四小时失败缓存")
                            db.commit()
                            continue
                try:
                    result = lookup(db, paper, client)
                except EnrichmentBudgetExceeded:
                    budget_exhausted = True
                    break
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {str(exc)[:300]}"
                    if isinstance(exc,urllib.error.HTTPError) and exc.code==404:
                        _record_attempt(db,task_id,paper["identity"],provider,"not_found",detail="精确标识未收录（HTTP 404）")
                        db.commit()
                        continue
                    failures.append(f"{PROVIDER_LABELS[provider]}：{detail}")
                    _record_attempt(db, task_id, paper["identity"], provider, "failed", detail=detail)
                    db.commit()
                    continue
                if not result:
                    _record_attempt(db, task_id, paper["identity"], provider, "not_found")
                    db.commit()
                    continue
                result_abstract = clean_abstract(result.get("abstract"))
                raw_type = str(result.get("publication_type_raw") or "")
                decision = publication_decision(
                    paper.get("title", ""), paper.get("venue", ""), raw_type,
                    str(result.get("publication_type_source") or provider), str(result.get("source_kind") or ""),
                ) if raw_type else None
                changed = False
                with db:
                    if result_abstract and not abstract_found:
                        db.execute(
                            """UPDATE papers SET abstract=?,abstract_source=?,abstract_source_url=?,
                            abstract_retrieved_at=?,abstract_failure_reason=NULL,needs_rescreen=1,updated_at=?
                            WHERE identity=? AND COALESCE(abstract,'')=''""",
                            (
                                result_abstract, result["source_name"], result.get("source_url") or None,
                                utc_now(), utc_now(), paper["identity"],
                            ),
                        )
                        paper["abstract"] = result_abstract
                        abstract_found = True
                        updated += 1
                        changed = True
                        provider_found[provider] = provider_found.get(provider, 0) + 1
                    result_source = str(result.get("publication_type_source") or provider)
                    preserve_type = decision and should_preserve_publication_metadata(
                        str(paper.get("publication_type_source") or ""), str(paper.get("eligibility_status") or "quarantine"),
                        result_source, decision["eligibility_status"],
                    )
                    prior_evidence = json.loads(paper.get("publication_type_evidence_json") or "[]")
                    if not isinstance(prior_evidence, list):
                        prior_evidence = []
                    if decision and not preserve_type:
                        decision["evidence"].extend(item for item in prior_evidence if isinstance(item, dict)
                          and item.get("kind") in {"metadata_conflict", "verified_record"} and item not in decision["evidence"])
                        if paper.get("publication_type_raw") and (
                            paper.get("publication_type_raw"), paper.get("publication_type_source")
                        ) != (raw_type, result_source):
                            decision["evidence"].append({"kind": "metadata_conflict",
                              "source": paper.get("publication_type_source") or "metadata",
                              "value": paper.get("publication_type_raw") or "", "prior_status": paper["eligibility_status"]})
                        db.execute(
                            """UPDATE papers SET publication_type=?,publication_type_raw=?,
                            publication_type_source=?,publication_type_evidence_json=?,eligibility_status=?,
                            exclusion_reason=?,needs_rescreen=CASE WHEN eligibility_status<>? OR publication_type<>?
                            THEN 1 ELSE needs_rescreen END WHERE identity=?""",
                            (
                                decision["publication_type"], raw_type,
                                result_source,
                                json.dumps(decision["evidence"], ensure_ascii=False),
                                decision["eligibility_status"], decision.get("exclusion_reason"),
                                decision["eligibility_status"], decision["publication_type"], paper["identity"],
                            ),
                        )
                        paper["publication_type"] = decision["publication_type"]
                        paper["publication_type_raw"] = raw_type
                        paper["publication_type_source"] = result_source
                        paper["publication_type_evidence_json"] = json.dumps(decision["evidence"], ensure_ascii=False)
                        paper["eligibility_status"] = decision["eligibility_status"]
                        type_found = decision["eligibility_status"] != "quarantine"
                        type_updated += 1
                        changed = True
                    elif preserve_type:
                        conflict = {"kind": "metadata_conflict", "source": result_source,
                                    "value": raw_type, "prior_status": decision["eligibility_status"]}
                        if conflict not in prior_evidence:
                            prior_evidence.append(conflict)
                            paper["publication_type_evidence_json"] = json.dumps(prior_evidence, ensure_ascii=False)
                            db.execute("UPDATE papers SET publication_type_evidence_json=? WHERE identity=?",
                                       (paper["publication_type_evidence_json"], paper["identity"]))
                    _record_attempt(
                        db, task_id, paper["identity"], provider,
                        "found" if changed else "not_found", result.get("source_url", ""),
                        result.get("evidence_type", ""), "摘要或出版类型证据已采用" if changed else (
                            "较弱类型证据已记录为来源冲突，未覆盖当前类型" if preserve_type else "记录无可用新证据"),
                    )
                if changed and abstract_found and type_found:
                    break
            if budget_exhausted:
                break
            checked = index
            if not abstract_found and paper.get("eligibility_status")!="excluded":
                reason = "；".join(failures[-3:]) if failures else "所有公开渠道均未返回可核验的原始摘要"
                with db:
                    db.execute("UPDATE papers SET abstract_failure_reason=? WHERE identity=?", (reason, paper["identity"]))
                unresolved.append({"identity": paper["identity"], "doi": paper.get("doi") or "", "reason": reason})
            with db:
                db.execute(
                    "UPDATE task_runs SET completed_count=?,success_count=?,failure_count=?,message=? WHERE task_id=?",
                    (index, updated, len(unresolved), f"已处理 {index}/{len(paper_rows)}", task_id),
                )
        details = {
            "task_id": task_id,
            "checked": checked,
            "deferred": len(paper_rows) - checked,
            "budget_exhausted": budget_exhausted,
            "updated": updated,
            "type_updated": type_updated,
            "unresolved": len(unresolved),
            "provider_found": provider_found,
            "unresolved_items": unresolved,
            "evidence_updated": updated,
        }
        status = "succeeded" if not unresolved else ("partial" if updated or type_updated else "failed")
        if budget_exhausted:
            status = "partial"
        _finish_task(db, task_id, status, details)
        return details | {"status": status}
    except Exception:
        if "task_id" in locals():
            details = {"checked": 0, "updated": 0, "unresolved": len(paper_rows) if "paper_rows" in locals() else 0}
            _finish_task(db, task_id, "failed", details)
        raise
    finally:
        db.close()


def export_missing_task_package(db_path: Path, output: Path) -> dict[str, Any]:
    db = connect(db_path)
    try:
        items = []
        for row in db.execute(
            """SELECT identity,doi,title,authors_json,venue,published,abstract_failure_reason
            FROM papers WHERE COALESCE(abstract,'')='' AND eligibility_status<>'excluded' ORDER BY first_seen"""
        ):
            try:
                authors = json.loads(row["authors_json"] or "[]")
            except json.JSONDecodeError:
                authors = []
            query = f'"{row["title"]}"' + (f' DOI {row["doi"]}' if row["doi"] else f' "{row["venue"] or ""}"')
            items.append({
                "identity": row["identity"], "doi": row["doi"] or "", "title": row["title"],
                "authors": authors, "venue": row["venue"] or "", "year": (row["published"] or "")[:4],
                "suggested_query": query, "last_failure": row["abstract_failure_reason"] or "尚未尝试",
            })
    finally:
        db.close()
    payload = {
        "schema_version": 1,
        "created_at": utc_now(),
        "rules": "只接受可追溯的原始摘要；禁止把搜索片段、模型总结或改写文本作为摘要。",
        "required_import_fields": [
            "identity", "abstract", "source_name", "source_url", "retrieved_at", "evidence_type"
        ],
        "codex_prompt": (
            "逐项使用 DOI、精确标题和期刊名检索官方元数据或出版商页面。只抄录明确标注为 Abstract 的原文；"
            "记录直接来源 URL 和证据类型。找不到就写失败原因，绝不总结、改写或补写摘要。"
        ),
        "items": items,
    }
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"output": str(output), "count": len(items), "created_at": payload["created_at"]}


def _load_manual_rows(path: Path) -> list[dict[str, Any]]:
    text = path.expanduser().read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    value = json.loads(text)
    if isinstance(value, dict):
        value = value.get("results", value.get("items"))
    if not isinstance(value, list):
        raise ValueError("JSON 必须是记录数组，或包含 results/items 数组")
    return [dict(row) for row in value]


def preview_manual_import(db_path: Path, path: Path) -> dict[str, Any]:
    required = {"identity", "abstract", "source_name", "source_url", "retrieved_at", "evidence_type"}
    db = connect(db_path)
    accepted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for index, item in enumerate(_load_manual_rows(path), 1):
            missing = sorted(required - set(item))
            identity = str(item.get("identity") or "").strip()
            if missing:
                errors.append({"row": index, "identity": identity, "reason": "缺少字段：" + "、".join(missing)})
                continue
            if identity in seen:
                errors.append({"row": index, "identity": identity, "reason": "任务包内 identity 重复"})
                continue
            seen.add(identity)
            paper = db.execute("SELECT identity,doi,abstract FROM papers WHERE identity=?", (identity,)).fetchone()
            if not paper:
                errors.append({"row": index, "identity": identity, "reason": "identity 不存在于当前文献库"})
                continue
            abstract = clean_abstract(item["abstract"])
            if len(abstract) < 80 or abstract.endswith(("...", "…")):
                errors.append({"row": index, "identity": identity, "reason": "摘要明显过短或被截断"})
                continue
            url = str(item["source_url"]).strip()
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append({"row": index, "identity": identity, "reason": "来源 URL 无效"})
                continue
            try:
                dt.datetime.fromisoformat(str(item["retrieved_at"]).replace("Z", "+00:00"))
            except ValueError:
                errors.append({"row": index, "identity": identity, "reason": "retrieved_at 不是 ISO 8601 时间"})
                continue
            if str(item["evidence_type"]) not in MANUAL_EVIDENCE_TYPES:
                errors.append({"row": index, "identity": identity, "reason": "evidence_type 不在允许列表"})
                continue
            if paper["abstract"] and len(paper["abstract"]) >= len(abstract):
                skipped.append({"row": index, "identity": identity, "reason": "已有摘要更长或相同"})
                continue
            accepted.append({**item, "identity": identity, "abstract": abstract, "source_url": url})
    finally:
        db.close()
    return {
        "source": str(path.expanduser().resolve()),
        "accepted": accepted,
        "skipped": skipped,
        "errors": errors,
        "counts": {"success": len(accepted), "skipped": len(skipped), "failed": len(errors)},
    }


def apply_manual_import(db_path: Path, preview: dict[str, Any]) -> dict[str, Any]:
    db = connect(db_path)
    updated = 0
    try:
        with db:
            for item in preview["accepted"]:
                cursor = db.execute(
                    """UPDATE papers SET abstract=?,abstract_source=?,abstract_source_url=?,
                    abstract_retrieved_at=?,abstract_failure_reason=NULL,needs_rescreen=1,updated_at=?
                    WHERE identity=? AND length(COALESCE(abstract,''))<length(?)""",
                    (
                        item["abstract"], str(item["source_name"]), item["source_url"], item["retrieved_at"],
                        utc_now(), item["identity"], item["abstract"],
                    ),
                )
                updated += max(0, cursor.rowcount)
                _record_attempt(
                    db, "manual-import", item["identity"], "manual", "found", item["source_url"],
                    str(item["evidence_type"]), "经严格导入校验",
                )
    finally:
        db.close()
    return {"updated": updated, "skipped": len(preview["skipped"]), "failed": len(preview["errors"])}

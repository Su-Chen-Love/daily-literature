"""Local-first web application for the Personal Academic Assistant."""

from __future__ import annotations

import datetime as dt
from email.parser import BytesParser
from email.policy import default as email_policy
import json
import os
import re
import secrets
import sqlite3
import sys
import urllib.parse
import urllib.request
import tempfile
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10
    import tomli as tomllib  # type: ignore

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .cloud_sync import background_sync, background_sync_state
from .engagement import (
    clear_feedback,
    confirm_profile,
    create_profile_draft,
    dismiss_profile_suggestion,
    pending_profile_review,
    set_favorite,
    set_feedback,
)
from .governance import governance_stats, latest_scores_sql, recommendation_feedback_metrics
from .official import configure_official_source, resolve_official_source
from .operations import recommendation_freshness, verify_installation
from .recommendations import local_today, recommendation_days, recommendations_on
from .product import (
    MAX_PDF_BYTES,
    add_manual_abstract,
    add_manual_paper,
    human_time,
    publication_date_label,
    import_fulltext,
    initialize_installation,
    source_candidates,
    source_coverage,
    screening_coverage,
)
from .storage import connect, database_status, upgrade_database, utc_now


PACKAGE_DIR = Path(__file__).resolve().parent


def monitor_runner_path(state: Path) -> Path:
    candidates = [
        PACKAGE_DIR.parents[1] / "scripts" / "paper_monitor.py",
        Path(sys.prefix) / "share" / "personal-academic-radar" / "paper_monitor.py",
        state / "run" / "paper_monitor.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("采集运行器未安装；请重新运行 academic-radar setup")


def load_config(path: Path) -> dict[str, Any]:
    with path.expanduser().open("rb") as handle:
        return tomllib.load(handle)


def resolve_state(config_path: Path, config: dict[str, Any]) -> Path:
    raw = Path(str(config["state_dir"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (config_path.parent / raw).resolve()


def rows(db: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(query, parameters)]


def row(db: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    value = db.execute(query, parameters).fetchone()
    return dict(value) if value else None


async def form_data(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type:
        raise HTTPException(415, "Expected a form submission")
    parsed = urllib.parse.parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


async def json_data(request: Request) -> dict[str, Any]:
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "Expected JSON")
    value = await request.json()
    if not isinstance(value, dict):
        raise ValueError("请求内容必须是对象")
    return value


async def multipart_data(request: Request) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(415, "Expected a multipart form submission")
    length = request.headers.get("content-length")
    if length and int(length) > MAX_PDF_BYTES + 1024 * 1024:
        raise ValueError("上传内容超过 50 MB 限制，请先压缩 PDF")
    raw = await request.body()
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw
    )
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8")
    return fields, files


def safe_return(value: str | None, default: str = "/") -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else default


def normalize_source(data: dict[str, str]) -> dict[str, Any]:
    source_type=data.get("type","")
    source={"name":data.get("name","").strip(),"type":source_type,"required":True}
    if not source["name"]: raise ValueError("来源名称不能为空")
    if source_type=="crossref":
        compact=re.sub(r"[^0-9Xx]","",data.get("issn","").strip())
        issn=(compact[:4]+"-"+compact[4:].upper()) if len(compact)==8 else ""
        if not issn: raise ValueError("期刊来源需要 ISSN")
        if not re.fullmatch(r"\d{4}-\d{3}[\dX]",issn):
            raise ValueError("ISSN 格式应为 1234-5678 或 1234-567X")
        source["issn"]=issn
        if data.get("openalex_id","").strip(): source["openalex_id"]=data["openalex_id"].strip()
    elif source_type=="openalex":
        openalex_id=data.get("openalex_id","").strip()
        if not re.fullmatch(r"S\d+",openalex_id,re.I):
            raise ValueError("来源缺少可验证的 OpenAlex ID")
        source["openalex_id"]=openalex_id.upper()
    elif source_type=="crossref-query":
        query=data.get("query_container","").strip()
        if not query: raise ValueError("会议来源需要出版物名称查询")
        source["query_container"]=query
        source["container_title_contains"]=data.get("container_title_contains","").strip() or query
        source["exclude_container_contains"]=["extended abstracts"]
    else: raise ValueError("不支持的来源类型")
    return source


def preview_source(source: dict[str, Any], user_agent: str, lookback_days: int) -> dict[str, Any]:
    since=(dt.date.today()-dt.timedelta(days=lookback_days)).isoformat()
    params={"rows":"5","select":"DOI,title,container-title,published-online,published-print,created,URL",
            "filter":f"from-created-date:{since}","sort":"created","order":"desc"}
    if source["type"]=="openalex":
        params={"filter":f"primary_location.source.id:{source['openalex_id']},from_publication_date:{since}",
                "sort":"publication_date:desc","per_page":"5"}
        request=urllib.request.Request("https://api.openalex.org/works?"+urllib.parse.urlencode(params),headers={"User-Agent":user_agent})
        with urllib.request.urlopen(request,timeout=20) as response:
            message=json.loads(response.read().decode("utf-8"))
        samples=[]
        for item in message.get("results",[]):
            title=str(item.get("title") or "").strip()
            location=item.get("primary_location") or {}; host=location.get("source") or {}
            if title: samples.append({"title":title,"venue":host.get("display_name") or source["name"],
                                      "doi":str(item.get("doi") or "").replace("https://doi.org/","")})
        return {"source":source,"total_results":int((message.get("meta") or {}).get("count",0)),"samples":samples,"since":since}
    if source["type"]=="crossref":
        endpoint="https://api.crossref.org/journals/"+urllib.parse.quote(source["issn"])+"/works"
    else:
        endpoint="https://api.crossref.org/works"; params["query.container-title"]=source["query_container"]
        params["filter"] += ",prefix:10.1145,type:proceedings-article"
    request=urllib.request.Request(endpoint+"?"+urllib.parse.urlencode(params),headers={"User-Agent":user_agent})
    with urllib.request.urlopen(request,timeout=20) as response:
        message=json.loads(response.read().decode("utf-8")).get("message",{})
    samples=[]
    for item in message.get("items",[]):
        title=" ".join(item.get("title") or []).strip(); venue=" ".join(item.get("container-title") or []).strip()
        if source["type"]=="crossref-query":
            if source["container_title_contains"].lower() not in venue.lower(): continue
            if any(value.lower() in venue.lower() for value in source["exclude_container_contains"]): continue
        if title: samples.append({"title":title,"venue":venue,"doi":item.get("DOI","")})
    return {"source":source,"total_results":int(message.get("total-results",0)),"samples":samples,"since":since}


def _toml_text(value: Any) -> str:
    if isinstance(value,bool): return "true" if value else "false"
    if isinstance(value,(int,float)): return str(value)
    if isinstance(value,list): return "["+", ".join(_toml_text(item) for item in value)+"]"
    return json.dumps(str(value),ensure_ascii=False)


def write_sources(config_path: Path, sources: list[dict[str, Any]]) -> Path:
    original=config_path.read_text(encoding="utf-8")
    blocks=[]
    order=(
        "name","type","required","issn","openalex_id","query_container","container_title_contains",
        "exclude_container_contains","official_status","official_provider","official_issues_url","official_feed_url",
        "rows_per_page","max_pages_per_source",
    )
    for source in sources:
        lines=["[[sources]]"]+[f"{key} = {_toml_text(source[key])}" for key in order if key in source]
        blocks.append("\n".join(lines))
    kept: list[str] = []
    insert_at: int | None = None
    in_source = False
    for line in original.splitlines(keepends=True):
        stripped = line.strip()
        is_table = stripped.startswith("[") and stripped.endswith("]")
        if stripped == "[[sources]]":
            if insert_at is None:
                insert_at = len(kept)
            in_source = True
            continue
        if is_table and in_source:
            in_source = False
        if not in_source:
            kept.append(line)
    if insert_at is None:
        insert_at = len(kept)
    source_text = "\n\n".join(blocks) + "\n"
    if insert_at and kept[insert_at - 1].strip():
        source_text = "\n" + source_text
    if insert_at < len(kept) and kept[insert_at].strip():
        source_text += "\n"
    kept.insert(insert_at, source_text)
    content="".join(kept)
    # Validate the complete replacement before either the backup or live file
    # can be changed.
    tomllib.loads(content)
    stamp=dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup=config_path.with_name(f"{config_path.name}.backup-{stamp}")
    backup.write_text(original,encoding="utf-8")
    fd,name=tempfile.mkstemp(prefix=config_path.name+".",suffix=".tmp",dir=config_path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(name,config_path)
    finally: Path(name).unlink(missing_ok=True)
    return backup


def create_app(config_path: Path) -> FastAPI:
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path)
    state = resolve_state(config_path, config)
    db_path = state / "papers.sqlite3"
    # Opening the app is also a safe compatibility repair: missing migrations
    # and a legacy profile ledger are restored without replacing user files.
    initialize_installation(state, config_path)
    config = load_config(config_path)

    app = FastAPI(title="Personal Academic Assistant", docs_url=None, redoc_url=None)
    app.state.config_path = config_path
    app.state.config = config
    app.state.state = state
    app.state.db_path = db_path
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.pending_sources = {}
    app.state.source_candidates = {}
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["human_time"] = human_time
    templates.env.filters["publication_date"] = publication_date_label
    def authors_label(value: str) -> str:
        try: names=json.loads(value or "[]")
        except (json.JSONDecodeError,TypeError): names=[]
        return "、".join(str(item) for item in names if item) or "作者信息未提供"
    templates.env.filters["authors"] = authors_label
    def list_label(value: str) -> str:
        try: items=json.loads(value or "[]")
        except (json.JSONDecodeError,TypeError): items=[]
        return "、".join(str(item) for item in items if item)
    templates.env.filters["list_label"] = list_label
    templates.env.filters["status_label"] = lambda value: {
        "active": "已启用", "draft": "草稿", "superseded": "已停用",
        "succeeded": "成功", "partial": "部分完成", "failed": "失败",
        "running": "运行中", "exported": "待导入", "imported": "已导入",
        "abandoned": "已放弃", "rejected": "已拒绝", "healthy": "健康",
        "degraded": "降级", "unknown": "尚无记录", "ok": "成功",
        "eligible": "符合收录范围", "excluded": "已排除", "quarantine": "待核查",
        "queued": "等待中",
    }.get(str(value), str(value) if value else "尚无记录")
    templates.env.filters["check_label"] = lambda value: {
        "database_integrity": "数据库完整性",
        "confirmed_profile": "已确认研究画像",
        "source_coverage": "来源运行覆盖",
        "source_runs": "最近来源运行",
        "source_degradation": "来源降级",
        "official_issue_coverage": "官网卷期覆盖",
        "official_issue_failures": "官网核验失败",
        "latest_semantic_job": "最近一次 Codex 判断",
        "semantic_coverage": "相关性判断覆盖",
        "abstract_coverage": "摘要覆盖",
        "web_service": "后台网页服务",
        "cloud_sync": "云端数据同步",
        "recommendation_freshness": "推荐更新时效",
    }.get(str(value), str(value))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    def queue_cloud_sync() -> dict[str, Any]:
        if not config.get("cloud_sync", {}).get("enabled"):
            return {"status": "disabled"}
        return background_sync(config_path)

    def public_sync_status() -> dict[str, Any]:
        """Only stable diagnostics and counters may reach a browser response."""
        enabled = bool(config.get("cloud_sync", {}).get("enabled"))
        value: dict[str, Any] = {}
        try:
            loaded = json.loads((state / "cloud-sync-status.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                value = loaded
        except (OSError, ValueError):
            pass
        statuses = {"succeeded", "unchanged", "failed", "busy", "queued", "disabled"}
        status = value.get("status") if value.get("status") in statuses else "unknown"
        running = bool(background_sync_state(config_path).get("running")) if enabled else False
        if enabled and (state / ".cloud-sync.lock").exists():
            import fcntl
            try:
                with (state / ".cloud-sync.lock").open("r") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        running = True
            except OSError:
                pass
        result: dict[str, Any] = {"status": "running" if running else (status if enabled else "disabled"),
                                  "running": running, "enabled": enabled}
        for key in ("records", "uploaded_records", "feedback_imported", "request_acknowledged"):
            if type(value.get(key)) is int and value[key] >= 0:
                result[key] = value[key]
        for key in ("synced_at", "checked_at", "remote_synced_at"):
            if isinstance(value.get(key), str):
                try:
                    dt.datetime.fromisoformat(value[key].replace("Z", "+00:00"))
                except ValueError:
                    continue
                result[key] = value[key]
        if isinstance(value.get("generation"), str) and re.fullmatch(r"[0-9a-f]{64}", value["generation"]):
            result["generation"] = value["generation"]
        result["pending"] = enabled and (running or status not in {"succeeded", "unchanged"})
        result["message"] = ("同步正在进行；有变化时会更新云端。" if running else
            "当前安装尚未配置云端同步。" if not enabled else
            "两端同步已完成；暂无待同步更新。" if status in {"succeeded", "unchanged"} else
            "同步等待执行或重试。")
        if status == "failed" and not running:
            result["message"] = "云端同步尚未完成，请稍后重试；已保存的本地和云端数据继续保留。"
        return result

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response=await call_next(request)
        response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["X-Frame-Options"]="DENY"
        return response

    def context(request: Request, page: str, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "page": page,
            "csrf_token": app.state.csrf_token,
            "state_path": str(state),
            "cloud_url": str(config.get("cloud_sync", {}).get("endpoint", "")),
            "cloud_sync_status": public_sync_status(),
            "site_mode": "local",
            **values,
        }

    def error_response(request: Request, status_code: int, title: str, message: str, action: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html",
            context(request, "error", title=title, message=message, action=action),
            status_code=status_code,
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> HTMLResponse:
        return error_response(request, 400, "这次操作没有保存", str(exc), "返回上一页检查输入后重试。")

    @app.exception_handler(sqlite3.DatabaseError)
    async def database_error_handler(request: Request, exc: sqlite3.DatabaseError) -> HTMLResponse:
        return error_response(
            request,
            503,
            "本地数据库暂时无法读取",
            "数据仍保存在本机，没有被删除。",
            f"运行 academic-radar verify --config {config_path}；若仍失败，请先备份数据库。",
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> HTMLResponse:
        return error_response(
            request,
            500,
            "页面暂时无法完成请求",
            "系统保留了现有数据。请不要删除数据库来尝试修复。",
            f"运行 academic-radar verify --config {config_path}，并查看本地服务错误日志。",
        )

    def validate_csrf(data: dict[str, str]) -> None:
        if not secrets.compare_digest(data.get("csrf_token", ""), app.state.csrf_token):
            raise HTTPException(403, "Invalid form token")

    def validate_json_csrf(request: Request) -> None:
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), app.state.csrf_token):
            raise HTTPException(403, "Invalid request token")

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        status = database_status(db_path)
        return {"ok": status.get("integrity") == "ok", "schema_version": status.get("schema_version")}

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, history_date: str = "") -> HTMLResponse:
        timezone = str(config.get("timezone", "Asia/Shanghai"))
        yesterday = local_today(timezone) - dt.timedelta(days=1)
        try:
            selected_day = dt.date.fromisoformat(history_date) if history_date else yesterday
        except ValueError:
            raise HTTPException(400, "请选择有效的历史日期（YYYY-MM-DD）")
        if selected_day > yesterday:
            raise HTTPException(400, "历史推荐日期应早于今天")
        db = connect(db_path)
        try:
            active = row(db, "SELECT * FROM profile_versions WHERE status='active'")
            latest_job = row(db,"SELECT * FROM agent_jobs WHERE status='imported' ORDER BY imported_at DESC LIMIT 1")
            freshness = recommendation_freshness(db, str(config.get("timezone", "Asia/Shanghai")))
            run_id = latest_job["run_id"] if latest_job else ""
            threshold=float(config.get("relevance_threshold",0.70))
            papers = rows(db, """SELECT p.*,s.score,s.reasons,s.confidence,s.screened_at,s.themes_json,
              s.reasoning_json,s.score_dimensions_json,s.rubric_version,
              f.interest,f.reason AS feedback_reason,COALESCE(f.favorite,0) favorite,
              COALESCE(f.reading_status,'unread') reading_status,
              EXISTS(SELECT 1 FROM fulltext_files ft WHERE ft.identity=p.identity) fulltext_count,
              (SELECT ft.id FROM fulltext_files ft WHERE ft.identity=p.identity
                ORDER BY ft.imported_at DESC,ft.id DESC LIMIT 1) fulltext_id
              FROM run_papers rp JOIN papers p ON p.identity=rp.identity
              JOIN screenings s ON s.identity=p.identity AND s.run_id=rp.run_id AND s.provider='codex-agent'
              LEFT JOIN paper_feedback f ON f.identity=p.identity
              -- The latest agent run is the authoritative recommendation set.
              -- Use the run's saved decisions even after a profile change.
              -- Ordinary daily runs contain only first judgments; an explicit
              -- manual rescreen can still produce a completed run.
              WHERE rp.run_id=? AND (
                rp.role='selected' OR (
                  rp.role='selected_new' AND NOT EXISTS(
                    SELECT 1 FROM run_papers selected_rp
                    WHERE selected_rp.run_id=rp.run_id
                      AND selected_rp.identity=rp.identity
                      AND selected_rp.role='selected'
                  )
                )
              )
                AND s.score>=? AND p.eligibility_status='eligible'
              ORDER BY CASE WHEN f.interest IS NULL THEN 0 ELSE 1 END,
                CASE f.interest WHEN 'interested' THEN 0 WHEN 'not_interested' THEN 1 ELSE 0 END,
                s.score DESC,p.published DESC,p.identity ASC""", (run_id,threshold))
            if not freshness["is_today"]:
                papers = []
            history_papers, history_runs, history_incomplete = recommendations_on(db, selected_day, timezone)
            history_days = [day for day in recommendation_days(db, timezone) if day <= yesterday.isoformat()]
            latest_run = row(db, "SELECT * FROM pipeline_runs WHERE run_id=?",(run_id,)) if run_id else None
            totals = row(db, """SELECT COUNT(*) papers,
              (SELECT COUNT(*) FROM paper_feedback WHERE favorite=1) favorites,
              (SELECT COUNT(*) FROM paper_feedback WHERE reading_status='read_later') read_later,
              (SELECT COUNT(*) FROM papers WHERE eligibility_status='excluded') excluded
              FROM papers WHERE eligibility_status='eligible'""")
            return templates.TemplateResponse(request,"today.html",context(request,"today",papers=papers,
                history_papers=history_papers,history_runs=history_runs,history_incomplete=history_incomplete,history_days=history_days,
                history_date=selected_day.isoformat(),yesterday=yesterday.isoformat(),
                history_previous=(selected_day-dt.timedelta(days=1)).isoformat() if selected_day > dt.date.min else None,
                history_next=(selected_day+dt.timedelta(days=1)).isoformat() if selected_day < yesterday else None,
                display_timezone="北京时间" if timezone == "Asia/Shanghai" else timezone,latest_run=latest_run,
                latest_job=latest_job,totals=totals,active_profile=active,freshness=freshness))
        finally:
            db.close()

    @app.get("/library", response_class=HTMLResponse)
    def library(request: Request, q: str = "", interest: str = "", reading: str = "", favorite: str = "",
                sort: str = "score_desc", page_no: int = 1) -> HTMLResponse:
        page_no=max(1,page_no); limit=24; offset=(page_no-1)*limit
        clauses=["p.eligibility_status='eligible'","s.score>=?"]; parameters: list[Any]=[float(config.get("relevance_threshold",0.70))]
        if q:
            clauses.append("(p.title LIKE ? OR p.abstract LIKE ? OR p.venue LIKE ?)")
            value=f"%{q}%"; parameters.extend([value,value,value])
        if interest in ("interested","not_interested"):
            clauses.append("f.interest=?"); parameters.append(interest)
        if reading in ("unread","read","read_later"):
            clauses.append("COALESCE(f.reading_status,'unread')=?"); parameters.append(reading)
        if favorite == "yes":
            clauses.append("COALESCE(f.favorite,0)=1")
        where=" AND ".join(clauses)
        order_by={
            "score_desc":"s.score DESC,p.published DESC",
            "score_asc":"s.score ASC,p.published DESC",
            "date_desc":"p.published DESC,s.score DESC",
            "date_asc":"p.published ASC,s.score DESC",
            "title_asc":"p.title COLLATE NOCASE ASC",
        }.get(sort,"s.score DESC,p.published DESC")
        if sort not in {"score_desc","score_asc","date_desc","date_asc","title_asc"}: sort="score_desc"
        db=connect(db_path)
        try:
            latest=latest_scores_sql()
            query=f"""SELECT p.*,s.score,s.relevant,s.reasons,s.confidence,s.themes_json,
              s.reasoning_json,s.score_dimensions_json,s.rubric_version,
              f.interest,f.reason AS feedback_reason,
              COALESCE(f.favorite,0) favorite,COALESCE(f.reading_status,'unread') reading_status,
              EXISTS(SELECT 1 FROM fulltext_files ft WHERE ft.identity=p.identity) fulltext_count,
              (SELECT ft.id FROM fulltext_files ft WHERE ft.identity=p.identity
                ORDER BY ft.imported_at DESC,ft.id DESC LIMIT 1) fulltext_id
              FROM papers p JOIN ({latest}) s ON s.identity=p.identity
              LEFT JOIN paper_feedback f ON f.identity=p.identity WHERE {where}
              ORDER BY {order_by},p.identity ASC LIMIT ? OFFSET ?"""
            paper_rows=rows(db,query,tuple(parameters+[limit,offset]))
            count=row(db,f"""SELECT COUNT(*) count FROM papers p JOIN ({latest}) s ON s.identity=p.identity
              LEFT JOIN paper_feedback f ON f.identity=p.identity WHERE {where}""",
              tuple(parameters))["count"]
            base_params={"q":q,"interest":interest,"reading":reading,"favorite":favorite,"sort":sort}
            previous_query=urllib.parse.urlencode({**base_params,"page_no":page_no-1})
            next_query=urllib.parse.urlencode({**base_params,"page_no":page_no+1})
            return templates.TemplateResponse(request,"library.html",context(request,"library",papers=paper_rows,
                q=q,interest=interest,reading=reading,favorite=favorite,sort=sort,page_no=page_no,total=count,
                has_next=offset+limit<count,previous_query=previous_query,next_query=next_query))
        finally: db.close()

    def render_sources(
        request: Request,
    ) -> HTMLResponse:
        db=connect(db_path)
        try:
            latest={item["source"]:item for item in rows(db,"""SELECT sr.* FROM source_runs sr JOIN (
              SELECT source,MAX(finished_at) finished_at FROM source_runs GROUP BY source
            ) x ON x.source=sr.source AND x.finished_at=sr.finished_at""")}
            items=[]
            coverage=source_coverage(db_path,config.get("sources",[]))
            official_counts={item["source_name"]:dict(item) for item in rows(db,"""SELECT source_name,
              COUNT(*) issue_count,MAX(checked_at) last_checked_at,
              SUM(article_count) article_count
              FROM official_issue_checks WHERE status='succeeded' AND issue_key NOT LIKE 'metadata-latest-two-as-of-%' GROUP BY source_name""")}
            official_latest={item["source_name"]:dict(item) for item in rows(db,"""SELECT * FROM (
              SELECT source_name,issue_key,status,detail,checked_at,
              ROW_NUMBER() OVER(PARTITION BY source_name ORDER BY checked_at DESC) rank
              FROM official_issue_checks
            ) WHERE rank=1""")}
            for source in config.get("sources",[]):
                items.append({**source,"latest":latest.get(source["name"]),
                    "coverage":coverage.get(source["name"],{}),
                    "official_check":official_counts.get(source["name"]),
                    "official_latest":official_latest.get(source["name"])})
            return templates.TemplateResponse(request,"sources.html",context(request,"sources",sources=items,
                quality=governance_stats(db_path,float(config.get("relevance_threshold",0.70)))))
        finally: db.close()

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request) -> HTMLResponse:
        return render_sources(request)

    @app.get("/api/sources/search")
    def source_search(q: str = "") -> JSONResponse:
        query=q.strip()
        if len(query)<2:
            return JSONResponse({"items":[],"message":"请输入至少 2 个字符"})
        try:
            candidates=source_candidates(query,config.get("user_agent","PersonalAcademicRadar/0.8"))
        except RuntimeError as exc:
            return JSONResponse({"error":str(exc),"retryable":True},status_code=503)
        configured=config.get("sources",[])
        for candidate in candidates:
            probe={
                "name":candidate.get("name",""), "type":candidate.get("config_type",""),
                "issn":candidate.get("issn",""), "openalex_id":candidate.get("openalex_id",""),
            }
            official=resolve_official_source(probe)
            candidate["official_status"]="verified" if official else "api_fallback"
            candidate["official_provider"]=official.get("provider","") if official else ""
            candidate["added"]=any(
                item.get("name","").casefold()==candidate.get("name","").casefold()
                or (candidate.get("issn") and item.get("issn")==candidate.get("issn"))
                or (candidate.get("openalex_id") and item.get("openalex_id")==candidate.get("openalex_id"))
                for item in configured
            )
            app.state.source_candidates[candidate["candidate_id"]]=candidate
        return JSONResponse({"items":candidates,"message":"" if candidates else "没有找到可验证的来源"})

    @app.post("/api/sources/preview")
    async def source_preview_api(request: Request) -> JSONResponse:
        validate_json_csrf(request); data=await json_data(request)
        candidate=app.state.source_candidates.get(str(data.get("candidate_id","")))
        if not candidate:
            return JSONResponse({"error":"候选已过期，请重新搜索"},status_code=409)
        source_data={"name":candidate.get("name",""),"type":candidate.get("config_type",""),
                     "issn":candidate.get("issn",""),"openalex_id":candidate.get("openalex_id","")}
        source=configure_official_source(normalize_source(source_data))
        if any(
            item.get("name","").casefold()==source["name"].casefold()
            or (source.get("issn") and item.get("issn")==source.get("issn"))
            or (source.get("openalex_id") and item.get("openalex_id")==source.get("openalex_id"))
            for item in config.get("sources",[])
        ):
            return JSONResponse({"error":"这个来源已经添加"},status_code=409)
        try:
            result=preview_source(source,config.get("user_agent","PersonalAcademicRadar/0.8"),int(config.get("lookback_days",14)))
        except Exception as exc:
            code=getattr(exc,"code",None)
            message=f"元数据服务暂时返回 HTTP {code}" if code else "暂时无法获取真实作品预览"
            return JSONResponse({"error":message,"retryable":True},status_code=503)
        token=secrets.token_urlsafe(24); app.state.pending_sources[token]=source
        return JSONResponse({**result,"token":token})

    @app.post("/api/sources/confirm")
    async def source_confirm_api(request: Request) -> JSONResponse:
        validate_json_csrf(request); data=await json_data(request)
        source=app.state.pending_sources.pop(str(data.get("token","")),None)
        if not source:
            return JSONResponse({"error":"预览已过期，请重新搜索"},status_code=409)
        new_sources=[*config.get("sources",[]),source]
        backup=write_sources(config_path,new_sources)
        config["sources"]=new_sources
        return JSONResponse({"ok":True,"message":"来源已添加，配置备份已保存","backup":str(backup)})

    @app.post("/api/sources/remove")
    async def source_remove_api(request: Request) -> JSONResponse:
        validate_json_csrf(request); data=await json_data(request)
        name=str(data.get("name","")).strip(); current=list(config.get("sources",[]))
        if len(current)<=1:
            return JSONResponse({"error":"至少需要保留一个有效来源"},status_code=409)
        kept=[item for item in current if item.get("name")!=name]
        if len(kept)==len(current):
            return JSONResponse({"error":"没有找到这个来源"},status_code=404)
        try:
            backup=write_sources(config_path,kept)
        except Exception as exc:
            return JSONResponse({"error":"移除失败，原配置已保留","detail":type(exc).__name__},status_code=500)
        config["sources"]=kept
        return JSONResponse({"ok":True,"message":"已停止未来监测；历史论文、收藏、反馈和 PDF 均保留",
                             "backup":str(backup)})

    @app.get("/profile", response_class=HTMLResponse)
    def profile(request: Request) -> HTMLResponse:
        db=connect(db_path)
        try:
            versions=rows(db,"""SELECT profile_versions.*,
              ROW_NUMBER() OVER (ORDER BY created_at,id) AS version_number
              FROM profile_versions
              WHERE NOT (source='feedback-ai' AND status='superseded')
              ORDER BY created_at DESC,id DESC""")
            active=next((item for item in versions if item["status"]=="active"),None)
            return templates.TemplateResponse(request,"profile.html",context(request,"profile",versions=versions,
                active=active,profile_review=pending_profile_review(db_path)))
        finally: db.close()

    @app.post("/profile/draft")
    async def profile_draft(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        create_profile_draft(db_path,data.get("content",""),data.get("summary",""),"web")
        return RedirectResponse("/profile",303)

    @app.post("/profile/confirm")
    async def profile_confirm(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        confirm_profile(db_path,int(data["version_id"]),state/config["profile_file"])
        return RedirectResponse("/profile",303)

    @app.post("/profile/dismiss")
    async def profile_dismiss(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        dismiss_profile_suggestion(db_path,int(data["version_id"]))
        return RedirectResponse("/profile",303)

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback(request: Request, interest: str = "", favorite: str = "", sort: str = "updated") -> HTMLResponse:
        db=connect(db_path)
        try:
            clauses=["(f.interest IS NOT NULL OR COALESCE(f.reason,'')<>'' OR f.favorite=1 OR f.reading_status<>'unread')"]; params=[]
            if interest in {"interested","not_interested"}:
                clauses.append("f.interest=?"); params.append(interest)
            if favorite=="yes": clauses.append("f.favorite=1")
            order="COALESCE(s.score,-1) DESC" if sort=="score" else "f.updated_at DESC"
            latest="""SELECT * FROM (SELECT s.*,ROW_NUMBER() OVER(PARTITION BY identity ORDER BY screened_at DESC) rn
              FROM screenings s WHERE provider='codex-agent') WHERE rn=1"""
            items=rows(db,f"""SELECT f.*,p.title,p.venue,p.abstract,p.url,p.doi,p.published,s.score,s.reasons
              FROM paper_feedback f JOIN papers p ON p.identity=f.identity
              LEFT JOIN ({latest}) s ON s.identity=p.identity WHERE {' AND '.join(clauses)} ORDER BY {order}""",tuple(params))
            stats=row(db,"""SELECT COUNT(*) total,SUM(interest='interested') interested,
              SUM(interest='not_interested') not_interested,SUM(favorite) favorites,
              SUM(reading_status='read') was_read FROM paper_feedback
              WHERE interest IS NOT NULL OR COALESCE(reason,'')<>'' OR favorite=1 OR reading_status<>'unread'""")
            return templates.TemplateResponse(request,"feedback.html",context(request,"feedback",items=items,stats=stats,
                interest=interest,favorite=favorite,sort=sort))
        finally: db.close()

    @app.post("/feedback")
    async def update_feedback(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        interest=data.get("interest") or None
        set_feedback(db_path,data["identity"],interest,data.get("reason",""),data.get("favorite")=="on",
                     data.get("reading_status","unread"))
        queue_cloud_sync()
        return RedirectResponse(safe_return(data.get("return_to"),"/library"),303)

    @app.post("/api/feedback")
    async def feedback_api(request: Request) -> JSONResponse:
        validate_json_csrf(request); data=await json_data(request)
        interest=data.get("interest") or None
        result=set_feedback(
            db_path,
            str(data.get("identity", "")),
            interest,
            str(data.get("reason", "")),
            bool(data.get("favorite")),
            str(data.get("reading_status", "unread")),
        )
        labels={"interested":"已完成 · 感兴趣", "not_interested":"已完成 · 不感兴趣"}
        queue_cloud_sync()
        return JSONResponse({"ok":True,"interest":result["interest"],"favorite":bool(result["favorite"]),
                             "reading_status":result["reading_status"],
                             "status_label":labels.get(result["interest"], ""),
                             "message":labels.get(result["interest"], "反馈已保存")})

    @app.post("/feedback/clear")
    async def remove_feedback(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        clear_feedback(db_path,data["identity"])
        queue_cloud_sync()
        return RedirectResponse(safe_return(data.get("return_to"),"/feedback"),303)

    @app.post("/api/favorite")
    async def favorite_api(request: Request) -> JSONResponse:
        validate_json_csrf(request); data=await json_data(request)
        result=set_favorite(db_path,str(data.get("identity","")),bool(data.get("favorite")))
        queue_cloud_sync()
        return JSONResponse({"ok":True,"favorite":bool(result["favorite"]),
                             "message":"已收藏到本地文献库" if result["favorite"] else "已取消收藏"})

    @app.post("/api/sync")
    async def synchronize_api(request: Request) -> JSONResponse:
        validate_json_csrf(request)
        result = queue_cloud_sync()
        status = result.get("status", "queued")
        message = {"queued": "已安排同步；只有内容变化时才上传。", "busy": "同步正在进行。",
                   "disabled": "当前安装尚未配置云端同步。"}.get(status, "已安排同步。")
        return JSONResponse({"ok": status in {"queued", "busy"}, "status": status, "message": message})

    @app.get("/api/sync/status")
    def synchronize_status_api() -> JSONResponse:
        return JSONResponse(public_sync_status())

    @app.post("/api/papers/manual")
    async def manual_paper_api(request: Request) -> JSONResponse:
        validate_json_csrf(request); data=await json_data(request)
        try:
            result=add_manual_paper(db_path,str(data.get("apa_citation","")),str(data.get("abstract","")))
        except ValueError as exc:
            return JSONResponse({"error":str(exc)},status_code=400)
        if result["created"]:
            message=f"已添加《{result['title']}》，将在下次更新中自动评分"
            status_code=201
        elif result["abstract_updated"]:
            next_step="将在下次更新中首次评分" if result["queued_for_screening"] else "已有评分保留"
            message=f"文献已存在；已补充摘要，{next_step}：《{result['title']}》"
            status_code=200
        else:
            message=f"文献已存在，未创建重复条目：《{result['title']}》"
            status_code=200
        return JSONResponse({"ok":True,"message":message,**result},status_code=status_code)

    @app.post("/api/papers/abstract")
    async def manual_abstract_api(request: Request) -> JSONResponse:
        validate_json_csrf(request); data=await json_data(request)
        try:
            result=add_manual_abstract(
                db_path,str(data.get("identity","")),str(data.get("abstract","")),
                str(data.get("source_url","")),
            )
        except ValueError as exc:
            return JSONResponse({"error":str(exc)},status_code=400)
        next_step="将在下次更新中首次评分" if result["queued_for_screening"] else "已有评分保留"
        return JSONResponse({"ok":True,"message":f"已补充《{result['title']}》的摘要，{next_step}",**result})

    @app.post("/fulltext")
    async def upload_fulltext(request: Request) -> RedirectResponse:
        data,files=await multipart_data(request); validate_csrf(data)
        filename,content=files.get("pdf",(None,b""))
        if not filename or not content:
            raise ValueError("请选择 PDF 文件")
        import_fulltext(db_path,state,data["identity"],filename,content)
        return RedirectResponse(safe_return(data.get("return_to"),"/library"),303)

    def current_fulltext(identity: str) -> dict[str, Any]:
        database = connect(db_path)
        try:
            file = row(database, """SELECT * FROM fulltext_files WHERE identity=?
              ORDER BY imported_at DESC,id DESC LIMIT 1""", (identity,))
        finally:
            database.close()
        if not file:
            raise HTTPException(404, "尚未导入这篇文献的全文 PDF")
        path = Path(file["stored_path"]).resolve()
        fulltext_root = (state / "fulltexts").resolve()
        if fulltext_root not in path.parents or not path.is_file():
            raise HTTPException(404, "已记录的全文文件不可用")
        return file

    @app.get("/fulltext/file")
    def fulltext_file(identity: str) -> FileResponse:
        file = current_fulltext(identity)
        return FileResponse(
            file["stored_path"], media_type="application/pdf", filename=Path(file["stored_path"]).name,
            content_disposition_type="inline",
        )

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request) -> HTMLResponse:
        db=connect(db_path)
        try:
            db_state=database_status(db_path)
            verification=verify_installation(config_path)
            check_order={"error":0,"warning":1}
            all_checks=sorted(
                (check for check in verification["checks"] if check["name"] != "schema_version"),
                key=lambda check: (check["ok"], check_order.get(check["level"], 2), check["name"]),
            )
            source_checks=[check for check in all_checks if check["name"] in {
                "source_coverage","source_runs","source_degradation",
                "official_issue_coverage","official_issue_failures",
            }]
            checks=[check for check in all_checks if not check["ok"]]
            missing_abstracts=int(verification["governance"]["missing_abstracts"])
            pending_count=screening_coverage(db)["pending"]
            situations=[]
            if missing_abstracts:
                situations.append(f"正式文献库仍缺 {missing_abstracts} 篇摘要")
            if any(not check["ok"] for check in source_checks):
                situations.append("部分监测来源尚未成功更新")
            else:
                situations.append("所有已配置来源都有运行记录")
            if pending_count:
                situations.append(f"有 {pending_count} 篇新文献待首次判断")
            else:
                situations.append("现有可筛选论文均已完成相关性判断")
            update_summary="当前："+"；".join(situations)+"。点击“更新数据库”复制针对这些情况生成的 Codex 任务。"
            prompt_steps=[
                "请更新“个人学术助手”的本地数据库，并在完成后报告结果。不要使用独立模型 API。",
                "1. 阅读此项目的 SKILL.md 与 references/profile-guidance.md，遵守完整队列、原子导入和私有数据边界。",
                f"2. 阅读研究兴趣：{state / str(config.get('profile_file', 'research-profile.md'))}。",
                "3. 当前诊断："+"；".join(situations)+"。",
            ]
            if missing_abstracts:
                prompt_steps.append(
                    f"4. 先运行：academic-radar abstracts enrich --config {config_path} --retry。只保存可追溯的原始摘要，不生成或改写摘要。"
                )
            else:
                prompt_steps.append("4. 当前没有已识别的缺失摘要；跳过摘要补全，继续更新来源。")
            prompt_steps.extend([
                f"5. 运行：python3 {monitor_runner_path(state)} collect-only --config {config_path}。记录返回的 run_id；这一步只执行所有来源的 14 天 API 采集。",
                f"6. 运行：academic-radar official plan --config {config_path} --output {state / 'official' / 'plan-latest.json'}。按计划逐个打开出版商官网，只核验截至计划日期已经出版的最近两期；未来卷期不能占用两期名额。逐篇复制官网明确标注的完整原始摘要，按 DOI 去重，再用 official import 先预览、后 --apply 原子导入。已成功核验的相同卷期可以跳过；官网无法访问或尚无适配时保留 14 天 API 结果并报告来源。",
                f"7. 再运行摘要补全，并执行 academic-radar profile review --db {db_path}。只有发现未审阅的新反馈时才提出完整画像建议并保存；没有必要修改时用 profile no-change 记录结论，不能静默改动已激活画像。",
                f"8. 运行：python3 {monitor_runner_path(state)} agent-export --config {config_path} --no-collect --batch-run <第 5 步 run_id>。仅导出从未由 Codex 判断过的新文献；已有判断不因画像、反馈、摘要或量表更新自动重评，不使用 --rescreen。",
                f"9. 阅读 {state / 'agent_queue'} 中最新的 JSON 队列。逐篇按已激活研究兴趣和反馈判断；每篇必须恰好有一条结果。",
                "10. 将严格 results JSON 保存到 agent-results 目录，保留队列的 run_id、profile_hash、source_failures。"
                "按 evaluation_policy 为每篇提供 identity、reasoning（论文证据、画像关联、可迁移价值、局限）、"
                "score_dimensions、matched_themes、confidence 和自然中文 recommendation_reason；后者必须综合独特证据、"
                "精确画像机制、具体可迁移内容与决定性局限，不能机械拼接字段标签或复述题名。最终分数由导入程序计算。"
                "即使队列为空，也写入覆盖完整队列的空 results 数组。",
                f"11. 运行：python3 {monitor_runner_path(state)} agent-import --config {config_path} --results <结果 JSON 路径>。",
                f"12. 最后运行：academic-radar verify --config {config_path}，报告 API 采集数、官网卷期与论文数、补全摘要数、判断数、达到 70 分的入选论文、来源失败和仍需处理的项目。",
            ])
            return templates.TemplateResponse(request,"status.html",context(request,"status",
                db_state=db_state,quality=verification["governance"],
                recommendation_quality=recommendation_feedback_metrics(
                    db_path,float(config.get("relevance_threshold",0.70))
                ),checks=checks,healthy_count=len(all_checks)-len(checks),
                update_summary=update_summary,update_prompt="\n\n".join(prompt_steps)))
        finally: db.close()

    return app

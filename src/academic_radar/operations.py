"""Operational verification and background-service management."""

from __future__ import annotations

import hashlib
import datetime as dt
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .governance import governance_stats
from .official import resolve_official_source
from .product import DEFAULT_STATE_DIR, initialize_installation, overall_quality, screening_coverage
from .storage import connect, database_status, latest_schema_version, migrate_state, upgrade_database


WEB_SERVICE_LABEL = "com.personal-academic-radar.web"
SYNC_SERVICE_LABEL = "com.personal-academic-radar.sync"


def install_sync_service(config_path: Path, interval: int = 300) -> dict[str, Any]:
    """Run the deterministic synchronizer without creating a second AI task."""
    if sys.platform != "darwin":
        raise ValueError("Automatic cloud sync service installation currently supports macOS")
    config_path = config_path.expanduser().resolve()
    config = _load_config(config_path)
    if not config.get("cloud_sync", {}).get("enabled"):
        raise ValueError("Configure and verify cloud_sync before installing its service")
    if interval < 60:
        raise ValueError("Synchronization interval must be at least 60 seconds")
    state = _state_for_config(config_path)
    payload = {
        "Label": SYNC_SERVICE_LABEL,
        "ProgramArguments": [sys.executable, "-m", "academic_radar.cli", "sync", "--config", str(config_path)],
        "RunAtLoad": True, "StartInterval": interval,
        "ProcessType": "Background",
        "StandardOutPath": str(state / "sync.stdout.log"),
        "StandardErrorPath": str(state / "sync.stderr.log"),
    }
    path = Path.home() / "Library/LaunchAgents" / f"{SYNC_SERVICE_LABEL}.plist"
    import plistlib
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(plistlib.dumps(payload))
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    target = f"gui/{os.getuid()}/{SYNC_SERVICE_LABEL}"
    subprocess.run(["launchctl", "bootout", target], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Cloud synchronization service could not be loaded")
    return {"installed": True, "label": SYNC_SERVICE_LABEL, "interval_seconds": interval, "plist": str(path)}


def recommendation_freshness(database: Any, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    """Distinguish a completed import from collection and from historical results."""
    latest = database.execute("""SELECT imported_at FROM agent_jobs
        WHERE status='imported'
        ORDER BY imported_at DESC LIMIT 1""").fetchone()
    now = dt.datetime.now(ZoneInfo(timezone))
    imported = None
    if latest and latest["imported_at"]:
        try:
            imported = dt.datetime.fromisoformat(latest["imported_at"].replace("Z", "+00:00"))
            if imported.tzinfo is None:
                imported = imported.replace(tzinfo=dt.timezone.utc)
            imported = imported.astimezone(now.tzinfo)
        except ValueError:
            pass
    collection = database.execute("SELECT started_at FROM pipeline_runs WHERE kind='collection' ORDER BY started_at DESC LIMIT 1").fetchone()
    pending_collection = bool(collection and (not latest or collection["started_at"] > latest["imported_at"]))
    return {
        "is_today": bool(imported and imported.date() == now.date()),
        "overdue": not imported or now - imported > dt.timedelta(hours=36),
        "last_import": imported.strftime("%Y-%m-%d %H:%M") if imported else "尚无完整更新",
        "pending_collection": pending_collection,
    }


def _web_healthy(port: int = 8765) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _load_config(config_path: Path) -> dict[str, Any]:
    with config_path.expanduser().resolve().open("rb") as handle:
        return tomllib.load(handle)


def _state_for_config(config_path: Path) -> Path:
    config_path = config_path.expanduser().resolve()
    config = _load_config(config_path)
    raw = Path(str(config["state_dir"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (config_path.parent / raw).resolve()


def web_service_status(config_path: Path | None = None, port: int = 8765) -> dict[str, Any]:
    loaded = False
    process = ""
    if sys.platform == "darwin":
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{WEB_SERVICE_LABEL}"],
            capture_output=True,
            text=True,
        )
        loaded = result.returncode == 0
        process = result.stdout[:4000] if loaded else ""
    plist_path = Path.home() / "Library/LaunchAgents" / f"{WEB_SERVICE_LABEL}.plist"
    service_config: Path | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None
    service_port = port
    if plist_path.exists():
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
            arguments = [str(value) for value in payload.get("ProgramArguments", [])]
            if "--config" in arguments:
                service_config = Path(arguments[arguments.index("--config") + 1]).expanduser().resolve()
            if "--port" in arguments:
                service_port = int(arguments[arguments.index("--port") + 1])
            stdout_log = payload.get("StandardOutPath")
            stderr_log = payload.get("StandardErrorPath")
        except (OSError, ValueError, IndexError, plistlib.InvalidFileException):
            pass
    requested_config = config_path.expanduser().resolve() if config_path else None
    matches_config = (
        not loaded or requested_config is None or service_config is None or service_config == requested_config
    )
    endpoint_healthy = _web_healthy(service_port)
    healthy = endpoint_healthy and matches_config
    state_match = re.search(r"\bstate = ([^\n]+)", process)
    pid_match = re.search(r"\bpid = (\d+)", process)
    exit_match = re.search(r"\blast exit code = ([^\n]+)", process)
    state_dir = _state_for_config(config_path) if config_path else None
    if loaded and matches_config:
        mode = "background"
        message = "后台服务已加载并会在登录后自动启动。" if healthy else "后台服务已加载，但网页未响应。"
    elif loaded:
        mode = "other-config"
        message = "后台服务正在运行另一份配置；当前检查的 state 不会由它自动恢复。"
    elif healthy:
        mode = "manual"
        message = "网页正在手动运行；电脑重启后需要重新启动。"
    else:
        mode = "stopped"
        message = "网页当前未运行。"
    return {
        "service": WEB_SERVICE_LABEL,
        "platform": sys.platform,
        "mode": mode,
        "loaded": loaded,
        "healthy": healthy,
        "endpoint_healthy": endpoint_healthy,
        "matches_config": matches_config,
        "message": message,
        "state": state_match.group(1).strip() if state_match else None,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "last_exit": exit_match.group(1).strip() if exit_match else None,
        "requested_config": str(requested_config) if requested_config else None,
        "service_config": str(service_config) if service_config else None,
        "stdout_log": str(stdout_log or (state_dir / "web.stdout.log" if state_dir else "")) or None,
        "stderr_log": str(stderr_log or (state_dir / "web.stderr.log" if state_dir else "")) or None,
    }


def verify_installation(config_path: Path) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    config = _load_config(config_path)
    state = _state_for_config(config_path)
    db_path = state / "papers.sqlite3"
    upgrade_database(db_path)
    checks: list[dict[str, Any]] = []
    recommendations: list[str] = []

    def check(name: str, ok: bool, detail: str, level: str = "error", action: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "level": level, "detail": detail, "action": action})
        if not ok and action and action not in recommendations:
            recommendations.append(action)

    status = database_status(db_path)
    check("database_integrity", status.get("integrity") == "ok", str(status.get("integrity")))
    check(
        "schema_version",
        status.get("schema_version") == latest_schema_version(),
        f"当前 v{status.get('schema_version')}，程序需要 v{latest_schema_version()}",
        action=f"运行 academic-radar db upgrade --db {db_path}",
    )
    profile_file = state / str(config.get("profile_file", "research-profile.md"))
    database = connect(db_path)
    try:
        active = database.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
        digest = hashlib.sha256(profile_file.read_bytes()).hexdigest() if profile_file.exists() else ""
        profile_ok = bool(active) and active["profile_hash"] == digest
        check(
            "confirmed_profile",
            profile_ok,
            f"已确认版本={active['id'] if active else '无'}；画像文件={'存在' if profile_file.exists() else '缺失'}",
            action=f"运行 academic-radar init --state {state} --config {config_path}",
        )

        configured = {source["name"] for source in config.get("sources", [])}
        source_rows = database.execute(
            "SELECT source,status,last_success_at,last_error FROM source_health"
        ).fetchall()
        source_rows = [item for item in source_rows if item["source"] in configured]
        observed = {item["source"] for item in source_rows}
        missing_sources = sorted(configured - observed)
        check(
            "source_coverage",
            not missing_sources,
            "全部来源已有运行记录" if not missing_sources else "尚无运行记录：" + "、".join(missing_sources),
            "warning",
            "运行一次 collect-only 以建立来源健康记录",
        )
        failed_sources = [item["source"] for item in source_rows if item["status"] == "failed"]
        degraded_sources = [item["source"] for item in source_rows if item["status"] == "degraded"]
        check(
            "source_runs",
            not failed_sources,
            "最近运行无失败来源" if not failed_sources else "最近失败来源：" + "、".join(failed_sources),
            action="查看最近来源运行并重试；单一来源失败不会删除已有论文",
        )
        if degraded_sources:
            check(
                "source_degradation",
                False,
                "部分提供商降级：" + "、".join(degraded_sources),
                "warning",
                "稍后重试并检查 Crossref/OpenAlex 网络状态",
            )

        official_sources = [
            source["name"] for source in config.get("sources", [])
            if resolve_official_source(source)
        ]
        official_success = {
            row["source_name"]: int(row["count"])
            for row in database.execute(
                """SELECT source_name,COUNT(*) count FROM official_issue_checks
                WHERE status='succeeded' AND issue_key NOT LIKE 'metadata-latest-two-as-of-%' GROUP BY source_name"""
            )
        }
        incomplete_official = [
            f"{name}（{official_success.get(name, 0)}/2 期）"
            for name in official_sources if official_success.get(name, 0) < 2
        ]
        check(
            "official_issue_coverage",
            not incomplete_official,
            "各出版来源历史上至少记录过两期；最新性、日期精度和核验方式见本轮卷期审计" if not incomplete_official
            else "官网两期核验尚未完成：" + "、".join(incomplete_official),
            "warning",
            "运行 official plan/collect-supported，并继续核验未完成的官网卷期",
        )
        latest_official_failures = [dict(row) for row in database.execute(
            """SELECT source_name,issue_key,detail FROM (
              SELECT source_name,issue_key,status,detail,checked_at,
              ROW_NUMBER() OVER(PARTITION BY source_name ORDER BY checked_at DESC) rank
              FROM official_issue_checks
            ) WHERE rank=1 AND status='failed'"""
        )]
        check(
            "official_issue_failures",
            not latest_official_failures,
            "最近卷期核验无失败；部分通过出版商元数据复核" if not latest_official_failures else
            "官网待重试：" + "；".join(
                f"{item['source_name']} {item['issue_key']}（{item['detail']}）"
                for item in latest_official_failures
            ),
            "warning",
            "查看 official status，并重试失败卷期；失败不影响已保存的 API 数据",
        )

        latest_job = database.execute(
            "SELECT * FROM agent_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        check(
            "latest_semantic_job",
            bool(latest_job) and latest_job["status"] == "imported",
            f"最近任务={latest_job['run_id'] if latest_job else '无'}；状态={latest_job['status'] if latest_job else '尚未运行'}",
            "warning",
            "先运行 collect-only 与官网卷期核验，再导出完整队列并运行 agent-import",
        )
        freshness = recommendation_freshness(database, str(config.get("timezone", "Asia/Shanghai")))
        check(
            "recommendation_freshness",
            not freshness["overdue"] and not freshness["pending_collection"],
            f"最近完整更新：{freshness['last_import']}" + ("；存在尚未完成评分导入的采集" if freshness["pending_collection"] else ""),
            "warning",
            "完成今日 collect-only、官网核验和 agent-import；本地定时任务需要电脑开机且 Codex 保持运行",
        )

        from .cloud_collection import latest_report, coverage_detail
        collection_report = latest_report(database)
        if collection_report:
            check("daily_collection_coverage", collection_report["status"] == "succeeded",
                  coverage_detail(collection_report), "warning",
                  "查看本轮分阶段覆盖；未执行、失败或预算延期不代表完整采集")

        coverage = screening_coverage(database)
        check(
            "semantic_coverage",
            coverage["pending"] == 0,
            f"可筛选论文={coverage['eligible']}；已判断={coverage['judged']}；待首次判断={coverage['pending']}；已有判断保留",
            "warning",
            "运行 agent-export --no-collect/agent-import，仅判断从未评分的新文献；不自动重评已有判断",
        )
        quality = governance_stats(db_path, float(config.get("relevance_threshold", 0.70)))
        abstract_ok = quality["abstract_percent"] >= 70 or quality["visible"] == 0
        check(
            "abstract_coverage",
            abstract_ok,
            f"{quality['abstract_percent']}%（{quality['abstracts']}/{quality['visible']}）",
            "warning",
            f"运行 python scripts/paper_monitor.py enrich-abstracts --config {config_path}",
        )
    finally:
        database.close()

    service = web_service_status(config_path)
    if config.get("cloud_sync", {}).get("enabled"):
        import json
        try:
            sync = json.loads((state / "cloud-sync-status.json").read_text())
            stamp = dt.datetime.fromisoformat(sync.get("synced_at", ""))
            recent = dt.datetime.now(dt.timezone.utc) - stamp.astimezone(dt.timezone.utc) < dt.timedelta(minutes=15)
            sync_ok = sync.get("status") in {"succeeded", "unchanged"} and recent
        except (OSError, ValueError, TypeError):
            sync, sync_ok = {}, False
        check("cloud_sync", sync_ok, "云端同步正常" if sync_ok else "云端同步失败或超过15分钟未校验", "warning",
              f"运行 academic-radar sync --config {config_path} 并检查本地 sync.stderr.log")
    service_action = (
        "已有后台服务使用另一份配置；确认要切换后再运行 service install-web"
        if service["mode"] == "other-config"
        else f"如需开机恢复，运行 academic-radar service install-web --config {config_path}"
    )
    check(
        "web_service",
        service["mode"] == "background" and service["healthy"],
        service["message"],
        "warning",
        service_action,
    )
    blocking = [item for item in checks if not item["ok"] and item["level"] == "error"]
    return {
        "ok": not blocking,
        "state_dir": str(state),
        "checks": checks,
        "recommendations": recommendations,
        "quality": overall_quality(db_path),
        "database": status,
        "service": service,
        "governance": governance_stats(db_path, float(config.get("relevance_threshold", 0.70))),
    }


def init_installation(state: Path = DEFAULT_STATE_DIR, config: Path | None = None) -> dict[str, Any]:
    result = initialize_installation(state, config)
    verification = verify_installation(Path(result["config"]))
    result["verify"] = verification
    result["ok"] = verification["ok"]
    return result


def setup_installation(
    state: Path = DEFAULT_STATE_DIR,
    config: Path | None = None,
    *,
    source_state: Path | None = None,
    install_service: bool = True,
    port: int = 8765,
) -> dict[str, Any]:
    """One-command, recoverable local setup for non-technical users."""

    destination = state.expanduser().resolve()
    migration = None
    detected_source = source_state.expanduser().resolve() if source_state else None
    if detected_source and detected_source != destination:
        destination_empty = not destination.exists() or not any(destination.iterdir())
        if destination_empty:
            migration = migrate_state(detected_source, destination)
    initialized = init_installation(destination, config)
    config_path = Path(initialized["config"])
    service: dict[str, Any]
    if install_service and sys.platform == "darwin":
        service = install_web_service(config_path, port=port)
    else:
        service = web_service_status(config_path, port)
        service["manual_boundary"] = (
            "此版本只自动安装 macOS 用户级后台服务；Linux 与 Windows 请按部署文档启动本地服务。"
            if sys.platform != "darwin" else "已按要求跳过后台服务安装。"
        )
    verification = verify_installation(config_path)
    return {
        "ok": initialized["ok"] and verification["ok"] and (not install_service or sys.platform != "darwin" or service.get("healthy", False)),
        "local_url": f"http://127.0.0.1:{port}",
        "state_dir": initialized["state_dir"],
        "config": initialized["config"],
        "database": initialized["database"],
        "migration": migration,
        "initialization": initialized,
        "verification": verification,
        "service": service,
        "logs": {"stdout": service.get("stdout_log"), "stderr": service.get("stderr_log")},
    }


def install_web_service(
    config_path: Path,
    *,
    launch_agents_dir: Path | None = None,
    python_executable: Path | None = None,
    activate: bool = True,
    port: int = 8765,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    state = _state_for_config(config_path)
    state.mkdir(parents=True, exist_ok=True)
    launch_agents = (launch_agents_dir or Path.home() / "Library/LaunchAgents").expanduser().resolve()
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{WEB_SERVICE_LABEL}.plist"
    # Keep the virtual-environment path instead of resolving its symlink to the
    # system interpreter, which may not have the package installed.
    python_path = (python_executable or Path(sys.executable)).expanduser().absolute()
    if not python_path.exists():
        raise FileNotFoundError(python_path)
    payload = {
        "Label": WEB_SERVICE_LABEL,
        "ProgramArguments": [
            str(python_path), "-m", "academic_radar.cli", "web", "--config", str(config_path),
            "--port", str(port),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(state / "web.stdout.log"),
        "StandardErrorPath": str(state / "web.stderr.log"),
    }
    fd, name = tempfile.mkstemp(prefix=plist_path.name + ".", suffix=".tmp", dir=launch_agents)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(payload, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, plist_path)
    finally:
        Path(name).unlink(missing_ok=True)
    loaded = False
    archived_logs: list[str] = []
    if activate:
        if sys.platform != "darwin":
            raise RuntimeError("自动安装后台服务目前仅支持 macOS；Linux/Windows 请参考部署文档")
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain + "/" + WEB_SERVICE_LABEL],
            capture_output=True,
            text=True,
        )
        # launchd may acknowledge bootout before the old label is fully gone.
        # Wait briefly so an immediate reinstall does not fail with a generic
        # bootstrap I/O error.
        for _ in range(20):
            gone = subprocess.run(
                ["launchctl", "print", domain + "/" + WEB_SERVICE_LABEL],
                capture_output=True,
                text=True,
            ).returncode != 0
            if gone:
                break
            time.sleep(0.1)
        archive_dir = state / "logs" / "service-archive"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for log_name in ("web.stdout.log", "web.stderr.log"):
            log_path = state / log_name
            if log_path.exists() and log_path.stat().st_size:
                archive_dir.mkdir(parents=True, exist_ok=True)
                archived = archive_dir / f"{log_name}.{stamp}"
                os.replace(log_path, archived)
                archived_logs.append(str(archived))
        result = None
        for _ in range(5):
            result = subprocess.run(
                ["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True, text=True
            )
            if result.returncode == 0:
                break
            time.sleep(0.25)
        assert result is not None
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "launchctl 未提供详细原因"
            raise RuntimeError(
                "后台服务加载失败（" + detail[:200] + "）；请查看 " + str(state / "web.stderr.log")
            )
        loaded = True
        for _ in range(20):
            if _web_healthy(port):
                break
            time.sleep(0.25)
        if not _web_healthy(port):
            raise RuntimeError("后台服务已加载但网页未响应；请查看 " + str(state / "web.stderr.log"))
    return {
        "service": WEB_SERVICE_LABEL,
        "plist": str(plist_path),
        "loaded": loaded,
        "healthy": _web_healthy(port) if loaded else None,
        "config": str(config_path),
        "stdout_log": str(state / "web.stdout.log"),
        "stderr_log": str(state / "web.stderr.log"),
        "archived_logs": archived_logs,
    }


def uninstall_web_service(*, launch_agents_dir: Path | None = None) -> dict[str, Any]:
    launch_agents = (launch_agents_dir or Path.home() / "Library/LaunchAgents").expanduser().resolve()
    plist_path = launch_agents / f"{WEB_SERVICE_LABEL}.plist"
    unloaded = False
    if sys.platform == "darwin":
        result = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{WEB_SERVICE_LABEL}"],
            capture_output=True,
            text=True,
        )
        unloaded = result.returncode == 0
    plist_path.unlink(missing_ok=True)
    return {
        "service": WEB_SERVICE_LABEL,
        "plist": str(plist_path),
        "unloaded": unloaded,
        "removed": not plist_path.exists(),
    }


def restart_web_service(config_path: Path | None = None, port: int = 8765) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("自动重启后台服务目前仅支持 macOS")
    current = web_service_status(config_path, port)
    if config_path and not current["matches_config"]:
        raise RuntimeError("后台服务正在使用另一份配置；如需切换，请明确运行 service install-web")
    if not current["loaded"]:
        if not config_path:
            raise RuntimeError("后台服务尚未安装；请提供 --config 进行安装")
        return install_web_service(config_path, port=port)
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{WEB_SERVICE_LABEL}"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("后台服务重启失败；请查看服务错误日志")
    for _ in range(20):
        if _web_healthy(port):
            break
        time.sleep(0.25)
    return web_service_status(config_path, port)

"""Command-line administration for Personal Academic Radar."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from .storage import backup_database, database_status, migrate_state, restore_database, upgrade_database
from .engagement import (
    confirm_profile,
    create_feedback_profile_suggestion,
    create_profile_draft,
    dismiss_profile_suggestion,
    feedback_examples,
    list_feedback,
    list_profiles,
    pending_profile_review,
    record_profile_review_no_change,
    set_feedback,
)
from .operations import (
    init_installation,
    install_web_service,
    restart_web_service,
    setup_installation,
    uninstall_web_service,
    verify_installation,
    web_service_status,
)
from .enrichment import apply_manual_import, enrich_abstracts, export_missing_task_package, preview_manual_import
from .governance import apply_cleanup_preview, preview_cleanup
from .official import (
    apply_official_import,
    collect_supported_official,
    official_status,
    preview_official_import,
    record_official_failure,
    write_official_plan,
)
from .product import load_config, resolve_state


def _default_backup(db: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return db.expanduser().resolve().with_name(f"{db.stem}.backup-{stamp}{db.suffix}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="academic-radar")
    groups = root.add_subparsers(dest="group", required=True)

    init = groups.add_parser("init", help="create or repair a local private state directory")
    init.add_argument("--state", type=Path, default=Path("~/.local/share/personal-academic-radar"))
    init.add_argument("--config", type=Path)

    setup_command = groups.add_parser("setup", help="initialize, verify, and start the private local application")
    setup_command.add_argument("--state", type=Path, default=Path("~/.local/share/personal-academic-radar"))
    setup_command.add_argument("--config", type=Path)
    setup_command.add_argument("--from-state", type=Path)
    setup_command.add_argument("--no-service", action="store_true")
    setup_command.add_argument("--port", default=8765, type=int)

    db = groups.add_parser("db", help="manage the SQLite database")
    db_commands = db.add_subparsers(dest="command", required=True)
    for name in ("status", "upgrade"):
        command = db_commands.add_parser(name)
        command.add_argument("--db", required=True, type=Path)
    backup = db_commands.add_parser("backup")
    backup.add_argument("--db", required=True, type=Path)
    backup.add_argument("--output", type=Path)
    restore = db_commands.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--db", required=True, type=Path)
    restore.add_argument("--replace", action="store_true")

    state = groups.add_parser("state", help="manage the external state directory")
    state_commands = state.add_subparsers(dest="command", required=True)
    migrate = state_commands.add_parser("migrate")
    migrate.add_argument("--from", dest="source", required=True, type=Path)
    migrate.add_argument("--to", dest="destination", required=True, type=Path)
    migrate.add_argument("--merge", action="store_true")

    feedback = groups.add_parser("feedback", help="record and inspect paper feedback")
    feedback_commands = feedback.add_subparsers(dest="command", required=True)
    feedback_set = feedback_commands.add_parser("set")
    feedback_set.add_argument("--db", required=True, type=Path)
    feedback_set.add_argument("--identity", required=True)
    feedback_set.add_argument("--interest", choices=("interested", "not_interested", "none"), default="none")
    feedback_set.add_argument("--reason", default="")
    feedback_set.add_argument("--favorite", action="store_true")
    feedback_set.add_argument("--reading-status", choices=("unread", "read", "read_later"), default="unread")
    for name in ("list", "examples"):
        command = feedback_commands.add_parser(name)
        command.add_argument("--db", required=True, type=Path)

    profile = groups.add_parser("profile", help="manage confirmed research-profile versions")
    profile_commands = profile.add_subparsers(dest="command", required=True)
    profile_list = profile_commands.add_parser("list")
    profile_list.add_argument("--db", required=True, type=Path)
    draft = profile_commands.add_parser("draft")
    draft.add_argument("--db", required=True, type=Path)
    draft.add_argument("--file", required=True, type=Path)
    draft.add_argument("--summary", required=True)
    confirm = profile_commands.add_parser("confirm")
    confirm.add_argument("--db", required=True, type=Path)
    confirm.add_argument("--id", required=True, type=int)
    confirm.add_argument("--profile-file", required=True, type=Path)
    review = profile_commands.add_parser("review", help="show feedback changes awaiting profile review")
    review.add_argument("--db", required=True, type=Path)
    suggest = profile_commands.add_parser("suggest", help="save an AI-generated profile recommendation")
    suggest.add_argument("--db", required=True, type=Path)
    suggest.add_argument("--file", required=True, type=Path,
                         help="JSON with fingerprint, content, and change_summary")
    no_change = profile_commands.add_parser("no-change", help="record that new feedback needs no profile edit")
    no_change.add_argument("--db", required=True, type=Path)
    no_change.add_argument("--fingerprint", required=True)
    no_change.add_argument("--reason", required=True)
    dismiss = profile_commands.add_parser("dismiss", help="dismiss a pending feedback profile recommendation")
    dismiss.add_argument("--db", required=True, type=Path)
    dismiss.add_argument("--id", required=True, type=int)

    web = groups.add_parser("web", help="run the local web application")
    web.add_argument("--config", required=True, type=Path)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", default=8765, type=int)
    web.add_argument("--allow-remote", action="store_true")

    verify = groups.add_parser("verify", help="verify a complete daily-use installation")
    verify.add_argument("--config", required=True, type=Path)

    service = groups.add_parser("service", help="manage the local web background service")
    service_commands = service.add_subparsers(dest="command", required=True)
    install_service = service_commands.add_parser("install-web")
    install_service.add_argument("--config", required=True, type=Path)
    install_service.add_argument("--port", default=8765, type=int)
    status_service = service_commands.add_parser("status")
    status_service.add_argument("--config", type=Path)
    status_service.add_argument("--port", default=8765, type=int)
    restart_service = service_commands.add_parser("restart-web")
    restart_service.add_argument("--config", type=Path)
    restart_service.add_argument("--port", default=8765, type=int)
    logs_service = service_commands.add_parser("logs")
    logs_service.add_argument("--config", required=True, type=Path)
    sync_service = service_commands.add_parser("install-sync")
    sync_service.add_argument("--config", required=True, type=Path)
    sync_service.add_argument("--interval", type=int, default=300)
    service_commands.add_parser("uninstall-web")

    abstracts = groups.add_parser("abstracts", help="enrich, export, and import traceable abstracts")
    abstract_commands = abstracts.add_subparsers(dest="command", required=True)
    enrich = abstract_commands.add_parser("enrich")
    enrich.add_argument("--config", required=True, type=Path)
    enrich.add_argument("--limit", type=int, default=500)
    enrich.add_argument("--retry", action="store_true")
    provenance = abstract_commands.add_parser("verify-provenance")
    provenance.add_argument("--config", required=True, type=Path)
    provenance.add_argument("--limit", type=int, default=100)
    provenance.add_argument("--budget", type=int, default=180)
    export_missing = abstract_commands.add_parser("export-missing")
    export_missing.add_argument("--config", required=True, type=Path)
    export_missing.add_argument("--output", required=True, type=Path)
    import_abstracts = abstract_commands.add_parser("import")
    import_abstracts.add_argument("--config", required=True, type=Path)
    import_abstracts.add_argument("--file", required=True, type=Path)
    import_abstracts.add_argument("--apply", action="store_true")

    official = groups.add_parser("official", help="plan and import verified publisher issue checks")
    official_commands = official.add_subparsers(dest="command", required=True)
    official_plan = official_commands.add_parser("plan")
    official_plan.add_argument("--config", required=True, type=Path)
    official_plan.add_argument("--output", required=True, type=Path)
    official_status_command = official_commands.add_parser("status")
    official_status_command.add_argument("--config", required=True, type=Path)
    official_collect = official_commands.add_parser("collect-supported")
    official_collect.add_argument("--config", required=True, type=Path)
    official_collect.add_argument("--output", required=True, type=Path)
    official_collect.add_argument("--source", default="")
    official_import = official_commands.add_parser("import")
    official_import.add_argument("--config", required=True, type=Path)
    official_import.add_argument("--file", required=True, type=Path)
    official_import.add_argument("--apply", action="store_true")
    official_fail = official_commands.add_parser("fail")
    official_fail.add_argument("--config", required=True, type=Path)
    official_fail.add_argument("--source", required=True)
    official_fail.add_argument("--issue-key", required=True)
    official_fail.add_argument("--issue-url", required=True)
    official_fail.add_argument("--detail", required=True)

    cleanup = groups.add_parser("cleanup", help="preview or apply recoverable library governance")
    cleanup_commands = cleanup.add_subparsers(dest="command", required=True)
    cleanup_preview = cleanup_commands.add_parser("preview")
    cleanup_preview.add_argument("--config", required=True, type=Path)
    cleanup_apply = cleanup_commands.add_parser("apply")
    cleanup_apply.add_argument("--config", required=True, type=Path)
    cleanup_apply.add_argument("--report", required=True, type=Path)
    sync = groups.add_parser("sync", help="synchronize the opted-in Sites database")
    sync.add_argument("--config", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.group == "sync":
            from .cloud_sync import sync_configured
            result = sync_configured(args.config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("status") in {"succeeded", "unchanged", "disabled", "busy"} else 1
        if args.group == "init":
            result=init_installation(args.state,args.config)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result.get("ok") else 1
        if args.group == "setup":
            result=setup_installation(args.state,args.config,source_state=args.from_state,
                                      install_service=not args.no_service,port=args.port)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result.get("ok") else 1
        if args.group == "service":
            if args.command == "install-sync":
                from .operations import install_sync_service
                result = install_sync_service(args.config, args.interval)
            elif args.command == "install-web":
                result = install_web_service(args.config, port=args.port)
            elif args.command == "restart-web":
                result = restart_web_service(args.config, args.port)
            elif args.command == "uninstall-web":
                result = uninstall_web_service()
            else:
                result = web_service_status(args.config, getattr(args, "port", 8765))
                if args.command == "logs":
                    result = {
                        "stdout_log": result["stdout_log"],
                        "stderr_log": result["stderr_log"],
                        "tip": "先查看 stderr 日志；日志只保存在本机私有 state 目录。",
                    }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.group == "verify":
            result=verify_installation(args.config)
            from .storage import connect, utc_now
            cfg=load_config(args.config)
            db_path=resolve_state(args.config,cfg)/"papers.sqlite3"
            if db_path.exists():
                checked_at=utc_now()
                with connect(db_path) as db:
                    db.execute("INSERT INTO meta(key,value) VALUES('last_local_check',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(checked_at,))
                result["checked_at"]=checked_at
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result["ok"] else 1
        if args.group in {"abstracts","cleanup","official"}:
            cfg=load_config(args.config); state_dir=resolve_state(args.config,cfg); db_path=state_dir/"papers.sqlite3"
            if args.group=="abstracts":
                if args.command=="enrich": result=enrich_abstracts(db_path,cfg,limit=args.limit,retry=args.retry)
                elif args.command=="verify-provenance":
                    from .provenance import verify_provenance
                    result=verify_provenance(db_path,cfg,args.limit,args.budget)
                elif args.command=="export-missing": result=export_missing_task_package(db_path,args.output)
                else:
                    preview=preview_manual_import(db_path,args.file)
                    result=apply_manual_import(db_path,preview) if args.apply else preview
            elif args.group=="official":
                if args.command=="plan": result=write_official_plan(db_path,cfg,args.output)
                elif args.command=="status": result=official_status(db_path,cfg)
                elif args.command=="collect-supported":
                    result=collect_supported_official(db_path,cfg,args.output,args.source)
                elif args.command=="fail":
                    result=record_official_failure(
                        db_path,cfg,args.source,args.issue_key,args.issue_url,args.detail
                    )
                else:
                    preview=preview_official_import(db_path,cfg,args.file)
                    result=apply_official_import(db_path,preview) if args.apply else preview
            elif args.command=="preview":
                result=preview_cleanup(db_path,state_dir,float(cfg.get("relevance_threshold",0.70)))
            else:
                result=apply_cleanup_preview(db_path,state_dir,args.report)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        if args.group == "web":
            if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
                raise ValueError("Remote binding requires --allow-remote and an access-control review")
            import uvicorn
            from .web import create_app
            uvicorn.run(create_app(args.config), host=args.host, port=args.port)
            return 0
        if args.group == "state":
            result = migrate_state(args.source, args.destination, args.merge)
        elif args.group == "feedback":
            if args.command == "set":
                result = set_feedback(args.db,args.identity,None if args.interest=="none" else args.interest,
                                      args.reason,args.favorite,args.reading_status)
            elif args.command == "examples":
                result = feedback_examples(args.db)
            else:
                result = list_feedback(args.db)
        elif args.group == "profile":
            if args.command == "draft":
                result = create_profile_draft(args.db,args.file.read_text(encoding="utf-8"),args.summary)
            elif args.command == "confirm":
                result = confirm_profile(args.db,args.id,args.profile_file)
            elif args.command == "review":
                result = pending_profile_review(args.db)
            elif args.command == "suggest":
                payload = json.loads(args.file.read_text(encoding="utf-8"))
                result = create_feedback_profile_suggestion(
                    args.db, str(payload.get("fingerprint", "")), str(payload.get("content", "")),
                    str(payload.get("change_summary", "")),
                )
            elif args.command == "no-change":
                result = record_profile_review_no_change(args.db,args.fingerprint,args.reason)
            elif args.command == "dismiss":
                result = dismiss_profile_suggestion(args.db,args.id)
            else:
                result = list_profiles(args.db)
        elif args.command == "status":
            result = database_status(args.db)
        elif args.command == "upgrade":
            before = database_status(args.db)
            backup = None
            if before.get("exists") and not before.get("schema_current", False):
                backup = _default_backup(args.db)
                backup_database(args.db, backup)
            result = upgrade_database(args.db)
            result["pre_upgrade_backup"] = str(backup) if backup else None
        elif args.command == "backup":
            result = backup_database(args.db, args.output or _default_backup(args.db))
        else:
            result = restore_database(args.backup, args.db, args.replace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        if isinstance(exc, sqlite3.DatabaseError):
            message = "本地数据库无法读取；数据文件未被删除。请先做文件备份，再运行 db status 或从已验证备份恢复。"
        elif isinstance(exc, FileNotFoundError):
            message = f"找不到所需文件：{exc}。请检查 config/state 路径或重新运行 init。"
        elif isinstance(exc, RuntimeError) and "migration" in str(exc).lower():
            message = f"数据库结构版本不兼容：{exc}。请保留数据库并使用匹配版本升级，不要直接删除。"
        else:
            message = str(exc)
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

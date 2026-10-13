import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from academic_radar.engagement import seed_active_profile
from academic_radar.operations import install_sync_service, install_web_service, setup_installation, verify_installation, web_service_status, recommendation_freshness
from academic_radar.storage import connect, upgrade_database


class OperationsTests(unittest.TestCase):
    def test_sync_service_uses_private_config_and_periodic_background_process(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); config = root / "config.toml"
            config.write_text('state_dir = "."\n[cloud_sync]\nenabled = true\n')
            completed = __import__("subprocess").CompletedProcess([], 0, stdout="")
            with patch("academic_radar.operations.Path.home", return_value=root), \
                 patch("academic_radar.operations.sys.platform", "darwin"), \
                 patch("academic_radar.operations.subprocess.run", return_value=completed) as run:
                result = install_sync_service(config, interval=300)
                with self.assertRaises(ValueError):install_sync_service(config, interval=59)
            path = Path(result["plist"]); payload = plistlib.loads(path.read_bytes())
            self.assertEqual(payload["StartInterval"], 300)
            self.assertTrue(payload["RunAtLoad"])
            self.assertEqual(payload["ProgramArguments"], [sys.executable, "-m", "academic_radar.cli", "sync", "--config", str(config.resolve())])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(run.call_count, 2)

    def test_old_import_is_overdue_even_when_collection_succeeded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/"papers.sqlite3"
            upgrade_database(path)
            active = seed_active_profile(path, "profile")
            with connect(path) as db:
                db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at) VALUES('old','agent-export','succeeded','2020-01-01')")
                db.execute("INSERT INTO agent_jobs(run_id,profile_hash,status,created_at,imported_at) VALUES('old',?,'imported','2020-01-01','2020-01-01T00:00:00+00:00')", (active['profile_hash'],))
                db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at) VALUES('new','collection','succeeded','2026-09-14T00:00:00+00:00')")
                result = recommendation_freshness(db)
            self.assertFalse(result['is_today'])
            self.assertTrue(result['overdue'])
            self.assertTrue(result['pending_collection'])
            self.assertEqual(result['last_import'], '2020-01-01 08:00')

    @staticmethod
    def stopped_service(config, port=8765):
        state=Path(config).parent
        return {"service":"com.personal-academic-radar.web","platform":"darwin","mode":"stopped",
                "loaded":False,"healthy":False,"matches_config":True,"message":"未运行","state":"stopped",
                "stdout_log":str(state/"web.stdout.log"),"stderr_log":str(state/"web.stderr.log")}

    def test_setup_initializes_and_verifies_fresh_private_state(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("academic_radar.operations.web_service_status", side_effect=self.stopped_service):
            state=Path(td)/"private-state"
            result=setup_installation(state,install_service=False)
            self.assertTrue(result["ok"])
            self.assertEqual(result["state_dir"],str(state.resolve()))
            self.assertTrue((state/"papers.sqlite3").exists())
            self.assertTrue(result["verification"]["database"]["schema_current"])
            expected = "跳过后台服务" if sys.platform == "darwin" else "Linux 与 Windows"
            self.assertIn(expected,result["service"]["manual_boundary"])

    def test_setup_migrates_old_state_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("academic_radar.operations.web_service_status", side_effect=self.stopped_service):
            root=Path(td); source=root/"old-state"; destination=root/"new-state"; source.mkdir()
            (source/"research-profile.md").write_text("profile",encoding="utf-8")
            (source/"config.toml").write_text(
                'state_dir = "."\nprofile_file = "research-profile.md"\n'
                '[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234-5678"\n',encoding="utf-8")
            upgrade_database(source/"papers.sqlite3"); seed_active_profile(source/"papers.sqlite3","profile")
            before=(source/"papers.sqlite3").stat().st_size
            result=setup_installation(destination,source_state=source,install_service=False)
            self.assertTrue(result["ok"])
            self.assertIsNotNone(result["migration"])
            self.assertTrue((destination/"papers.sqlite3").exists())
            self.assertEqual((source/"papers.sqlite3").stat().st_size,before)

    def test_setup_never_silently_imports_repository_state(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("academic_radar.operations.web_service_status", side_effect=self.stopped_service):
            root=Path(td); repository_state=root/"state"; destination=root/"private-state"
            repository_state.mkdir()
            upgrade_database(repository_state/"papers.sqlite3")
            with patch("academic_radar.operations.Path.cwd", return_value=root):
                result=setup_installation(destination,install_service=False)
            self.assertTrue(result["ok"])
            self.assertIsNone(result["migration"])
            db=connect(destination/"papers.sqlite3")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0],0)
            db.close()

    def test_web_service_plist_uses_private_config_and_selected_python(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); config=root/"config.toml"; launch_agents=root/"LaunchAgents"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            result=install_web_service(config,launch_agents_dir=launch_agents,python_executable=Path(sys.executable),activate=False)
            self.assertFalse(result["loaded"])
            text=Path(result["plist"]).read_text()
            self.assertIn(str(config.resolve()),text)
            self.assertIn("academic_radar.cli",text)

    def test_service_status_does_not_claim_another_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            launch_agents = root / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            service_config = root / "service" / "config.toml"
            requested_config = root / "requested" / "config.toml"
            for config in (service_config, requested_config):
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(
                    'state_dir = "."\nprofile_file = "research-profile.md"\n'
                    '[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',
                    encoding="utf-8",
                )
            plist_path = launch_agents / "com.personal-academic-radar.web.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {"ProgramArguments": ["python", "-m", "academic_radar.cli", "web", "--config",
                                           str(service_config), "--port", "8765"],
                     "StandardOutPath": str(root / "actual.stdout.log"),
                     "StandardErrorPath": str(root / "actual.stderr.log")},
                    handle,
                )
            completed = __import__("subprocess").CompletedProcess([], 0, stdout="state = running\npid = 10\n")
            with patch("academic_radar.operations.Path.home", return_value=root), \
                 patch("academic_radar.operations.subprocess.run", return_value=completed), \
                 patch("academic_radar.operations._web_healthy", return_value=True), \
                 patch("academic_radar.operations.sys.platform", "darwin"):
                status = web_service_status(requested_config)
            self.assertEqual(status["mode"], "other-config")
            self.assertFalse(status["healthy"])
            self.assertFalse(status["matches_config"])
            self.assertEqual(status["service_config"], str(service_config.resolve()))

    def test_verify_reports_actionable_incomplete_state(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); config=root/"config.toml"; profile=root/"research-profile.md"
            profile.write_text("profile",encoding="utf-8")
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            upgrade_database(root/"papers.sqlite3"); seed_active_profile(root/"papers.sqlite3","profile")
            result=verify_installation(config)
            self.assertTrue(result["ok"])
            checks={item["name"]:item for item in result["checks"]}
            self.assertTrue(checks["database_integrity"]["ok"])
            self.assertTrue(checks["confirmed_profile"]["ok"])
            self.assertFalse(checks["latest_semantic_job"]["ok"])
            self.assertEqual(checks["latest_semantic_job"]["level"], "warning")
            self.assertTrue(result["recommendations"])

    def test_verify_counts_prior_decisions_and_only_flags_first_judgments(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("academic_radar.operations.web_service_status", side_effect=self.stopped_service):
            root = Path(td)
            result = setup_installation(root, install_service=False)
            path = root / "papers.sqlite3"
            with connect(path) as db:
                for identity in ("old", "new"):
                    db.execute("""INSERT INTO papers(identity,title,abstract,venue,published,url,authors_json,
                        first_seen,updated_at,eligibility_status,needs_rescreen)
                        VALUES(?,?,'An abstract','Journal','','','[]','now','now','eligible',1)""", (identity, identity))
                db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,
                    screened_at,rubric_version) VALUES('old','former-profile','codex-agent','host',1,.8,'Saved decision','2020-01-01','old-rubric')""")
                db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,
                    screened_at) VALUES('new','former-profile','external','external',1,.9,'External decision','2026-01-01')""")
                db.execute("""INSERT INTO pipeline_runs(run_id,kind,status,started_at)
                    VALUES('old-run','agent-export','succeeded','2020-01-01')""")
                db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,created_at,imported_at)
                    VALUES('old-run','former-profile','imported','2020-01-01','2020-01-01T00:00:00Z')""")
                self.assertEqual(recommendation_freshness(db)["last_import"], "2020-01-01 08:00")
            check = next(item for item in verify_installation(Path(result["config"]))["checks"]
                         if item["name"] == "semantic_coverage")
            self.assertFalse(check["ok"])
            self.assertIn("已判断=1；待首次判断=1", check["detail"])
            with connect(path) as db:
                db.execute("DELETE FROM screenings WHERE identity='new'")
                db.execute("DELETE FROM papers WHERE identity='new'")
            check = next(item for item in verify_installation(Path(result["config"]))["checks"]
                         if item["name"] == "semantic_coverage")
            self.assertTrue(check["ok"])
            self.assertIn("已有判断保留", check["detail"])

    def test_verify_surfaces_official_issue_coverage_and_latest_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); config=root/"config.toml"; profile=root/"research-profile.md"
            profile.write_text("profile",encoding="utf-8")
            config.write_text(
                'state_dir = "."\nprofile_file = "research-profile.md"\n'
                '[[sources]]\nname = "International Journal of Human-Computer Studies"\n'
                'type = "crossref"\nissn = "1071-5819"\n', encoding="utf-8"
            )
            db_path=root/"papers.sqlite3"; upgrade_database(db_path); seed_active_profile(db_path,"profile")
            db=connect(db_path)
            with db:
                db.execute(
                    """INSERT INTO official_issue_checks(
                    source_name,issue_key,issue_url,status,article_count,imported_count,detail,checked_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    ("International Journal of Human-Computer Studies","volume-212",
                     "https://www.sciencedirect.com/issue","failed",0,0,"blocked","now"),
                )
            db.close()

            result=verify_installation(config)
            checks={item["name"]:item for item in result["checks"]}

            self.assertFalse(checks["official_issue_coverage"]["ok"])
            self.assertIn("0/2",checks["official_issue_coverage"]["detail"])
            self.assertFalse(checks["official_issue_failures"]["ok"])
            self.assertIn("volume-212",checks["official_issue_failures"]["detail"])

    def test_verify_blocks_when_legacy_state_has_no_confirmed_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.toml"
            (root / "research-profile.md").write_text("profile", encoding="utf-8")
            config.write_text(
                'state_dir = "."\nprofile_file = "research-profile.md"\n'
                '[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',
                encoding="utf-8",
            )
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            db = connect(db_path)
            with db:
                db.execute(
                    """INSERT INTO papers(
                    identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                    ) VALUES('doi:10.1/x','10.1/x','Paper','','V','','','[]','now','now')"""
                )
            db.close()

            result = verify_installation(config)

            self.assertFalse(result["ok"])
            confirmed = next(item for item in result["checks"] if item["name"] == "confirmed_profile")
            self.assertEqual(confirmed["level"], "error")
            self.assertIn("academic-radar init", confirmed["action"])


if __name__ == "__main__": unittest.main()

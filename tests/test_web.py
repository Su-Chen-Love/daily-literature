import datetime as dt
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from fastapi.testclient import TestClient
    from academic_radar.web import create_app
except ModuleNotFoundError:  # Allows core tests without optional web dependencies.
    TestClient = None

from academic_radar.engagement import create_profile_draft, seed_active_profile
from academic_radar.storage import connect, upgrade_database
from academic_radar.recommendations import snapshot_run


@unittest.skipIf(TestClient is None, "web test dependencies are not installed")
class WebTests(unittest.TestCase):
    def test_navigation_errors_render_page_but_api_errors_stay_json(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.get('/?history_date=invalid')
                self.assertEqual(response.status_code, 400)
                self.assertIn('每日文献', response.text)
                self.assertIn('请选择有效的历史日期', response.text)
                self.assertIn('text/html', response.headers['content-type'])
                response = client.post('/api/favorite', json={'identity': 'doi:10.1/test', 'favorite': True})
                self.assertEqual(response.status_code, 403)
                self.assertIn('error', response.json())

    def make_app(self, root: Path):
        config = root / "config.toml"
        config.write_text(
            'state_dir = "."\nprofile_file = "research-profile.md"\n'
            '[[sources]]\nname = "Test Venue"\ntype = "crossref"\nissn = "1234-5678"\n',
            encoding="utf-8",
        )
        profile = root / "research-profile.md"
        profile.write_text("confirmed profile", encoding="utf-8")
        db_path = root / "papers.sqlite3"
        upgrade_database(db_path)
        active = seed_active_profile(db_path, "confirmed profile")
        db = connect(db_path)
        with db:
            db.execute(
                """INSERT INTO papers(
                identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("doi:10.1/test", "10.1/test", "A useful paper", "Abstract", "Test Venue",
                 "2026-07-13", "https://doi.org/10.1/test", "[]", "now", "now"),
            )
            db.execute(
                """INSERT INTO screenings(
                identity,profile_hash,provider,model,relevant,score,reasons,themes_json,confidence,
                screened_at,profile_version_id,feedback_snapshot_json,run_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("doi:10.1/test", active["profile_hash"], "codex-agent", "test", 1, .9,
                 "Direct match", "[]", .9, "2026-07-13T08:00:00+00:00", active["id"], "[]", "run"),
            )
            db.execute("""UPDATE papers SET publication_type='Journal Article',publication_type_raw='journal-article',
              publication_type_source='crossref',eligibility_status='eligible' WHERE identity='doi:10.1/test'""")
        db.close()
        return create_app(config), db_path, profile

    def test_all_six_pages_and_health_render(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                for path in ("/", "/library", "/sources", "/profile", "/feedback", "/status"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, path)
                    self.assertIn("每日文献", response.text)
                    self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
                    self.assertIn('rel="icon" type="image/png"', response.text)
                    if path == "/":
                        self.assertIn("app.js?v=daily-20261018", response.text)
                        self.assertEqual(response.text.count('class="nav-icon"'), 6)
                favicon = client.get("/static/images/academic-radar-logo.png")
                self.assertEqual(favicon.status_code, 200)
                self.assertEqual(favicon.headers["content-type"], "image/png")
                self.assertTrue(client.get("/healthz").json()["ok"])

    def test_sync_status_has_polling_message_and_pending_flag(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                status = client.get("/api/sync/status").json()
                self.assertIsInstance(status["message"], str)
                self.assertTrue(status["message"])
                self.assertIs(status["pending"], False)

    def test_old_recommendations_are_history_and_not_today(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            with connect(db_path) as db:
                active = db.execute("SELECT profile_hash FROM profile_versions WHERE status='active'").fetchone()[0]
                db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at) VALUES('run','agent-export','succeeded','2020-01-01')")
                db.execute("INSERT INTO agent_jobs(run_id,profile_hash,status,created_at,imported_at) VALUES('run',?,'imported','2020-01-01','2020-01-01T00:00:00+00:00')", (active,))
                db.execute("INSERT INTO run_papers VALUES('run','doi:10.1/test','selected')")
                snapshot_run(db, 'run')
            with TestClient(app) as client:
                page = client.get('/?history_date=2020-01-01').text
            self.assertIn('今天尚未完成更新', page)
            self.assertIn('2020-01-01 08:00', page)
            self.assertIn('data-history-list', page)
            self.assertNotIn('A useful paper', page.split('data-today-list')[1].split('historical-recommendations')[0])

    def test_today_shows_recommendations_selected_after_rescreen(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            db = connect(db_path)
            with db:
                db.execute(
                    "INSERT INTO pipeline_runs(run_id,kind,status,started_at,finished_at,collected_count,candidate_count,relevant_count) "
                    "VALUES('agent-run','agent-export','succeeded','now','now',0,1,1)"
                )
                db.execute(
                    "INSERT INTO agent_jobs(run_id,profile_hash,status,queue_path,exported_count,imported_count,created_at,imported_at,profile_version_id,feedback_snapshot_json) "
                    "VALUES('agent-run',?,'imported','queue.json',1,1,'now','now',?,'[]')",
                    (db.execute("SELECT profile_hash FROM profile_versions WHERE status='active'").fetchone()[0],
                     db.execute("SELECT id FROM profile_versions WHERE status='active'").fetchone()[0]),
                )
                db.execute("INSERT INTO run_papers(run_id,identity,role) VALUES('agent-run','doi:10.1/test','selected')")
                db.execute("UPDATE screenings SET run_id='agent-run' WHERE identity='doi:10.1/test'")
            with db:
                db.execute("UPDATE agent_jobs SET imported_at=?", (dt.datetime.now(dt.timezone.utc).isoformat(),))
            db.close()
            with TestClient(app) as client:
                response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("A useful paper", response.text)

    def test_profile_change_keeps_saved_today_and_library_decisions(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            stamp = dt.datetime.now(dt.timezone.utc).isoformat()
            with connect(db_path) as db:
                old_profile = db.execute("SELECT profile_hash FROM profile_versions WHERE status='active'").fetchone()[0]
                db.execute("""INSERT INTO pipeline_runs(run_id,kind,status,started_at)
                    VALUES('run','agent-export','succeeded',?)""", (stamp,))
                db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,created_at,imported_at)
                    VALUES('run',?,'imported',?,?)""", (old_profile, stamp, stamp))
                db.execute("INSERT INTO run_papers VALUES('run','doi:10.1/test','selected')")
                db.execute("UPDATE papers SET needs_rescreen=1")
            draft = create_profile_draft(db_path, "A different confirmed profile", "New interests")
            with TestClient(app) as client:
                response = client.post("/profile/confirm", data={
                    "csrf_token": app.state.csrf_token, "version_id": str(draft["id"]),
                }, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                for route in ("/", "/library"):
                    page = client.get(route)
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("A useful paper", page.text)
                    self.assertIn("Direct match", page.text)
                status = client.get("/status").text
            self.assertNotIn("待按新量表重新判断", status)
            self.assertIn("仅导出从未由 Codex 判断过的新文献", status)

    def test_sources_show_completed_official_issue_count(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            db = connect(db_path)
            with db:
                db.execute(
                    """INSERT INTO official_issue_checks(
                    source_name,issue_key,issue_url,status,article_count,imported_count,detail,checked_at
                    ) VALUES('Test Venue','volume-12','https://example.test/12','succeeded',5,5,'ok','now')"""
                )
            db.close()
            with TestClient(app) as client:
                page = client.get("/sources")
            self.assertIn("官网已核验", page.text)
            self.assertIn("1 期 · 5 篇", page.text)

    def test_empty_legacy_state_is_repaired_and_all_pages_render(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.toml").write_text(
                'state_dir = "."\nprofile_file = "research-profile.md"\n'
                '[[sources]]\nname = "Test Venue"\ntype = "crossref"\nissn = "1234-5678"\n',
                encoding="utf-8",
            )
            (root / "research-profile.md").write_text("legacy profile", encoding="utf-8")
            app = create_app(root / "config.toml")
            with TestClient(app) as client:
                for path in ("/", "/library", "/sources", "/profile", "/feedback", "/status"):
                    self.assertEqual(client.get(path).status_code, 200, path)
            db = connect(root / "papers.sqlite3")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile_versions WHERE status='active'").fetchone()[0], 1)
            db.close()

    def test_feedback_form_persists_and_is_csrf_protected(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                denied = client.post("/feedback", data={"identity": "doi:10.1/test"})
                self.assertEqual(denied.status_code, 403)
                response = client.post(
                    "/feedback",
                    data={"csrf_token": app.state.csrf_token, "identity": "doi:10.1/test",
                          "interest": "interested", "reason": "Direct transfer", "favorite": "on",
                          "reading_status": "read_later", "return_to": "/library"},
                    follow_redirects=False,
                )
                self.assertEqual((response.status_code, response.headers["location"]), (303, "/library"))
            db = connect(db_path)
            saved = db.execute("SELECT interest,reason,favorite,reading_status FROM paper_feedback").fetchone()
            self.assertEqual(tuple(saved), ("interested", "Direct transfer", 1, "read_later"))
            db.close()

    def test_validation_error_is_a_friendly_html_page(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post(
                    "/feedback",
                    data={"csrf_token": app.state.csrf_token, "identity": "doi:10.1/test",
                          "interest": "interested", "reason": "", "reading_status": "unread"},
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn("这次操作没有保存", response.text)
            self.assertIn("数据已保留", response.text)

    def test_profile_web_flow_keeps_draft_inactive_until_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, profile_file = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post(
                    "/profile/draft",
                    data={"csrf_token": app.state.csrf_token, "content": "revised profile",
                          "summary": "Tighten boundaries"}, follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(profile_file.read_text(), "confirmed profile")
                db = connect(db_path)
                draft_id = db.execute("SELECT id FROM profile_versions WHERE status='draft'").fetchone()[0]
                db.close()
                response = client.post(
                    "/profile/confirm",
                    data={"csrf_token": app.state.csrf_token, "version_id": str(draft_id)},
                    follow_redirects=False,
                )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(profile_file.read_text(), "revised profile")

    def test_profile_version_labels_stay_contiguous_when_a_record_was_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            create_profile_draft(db_path, "temporary profile", "Temporary change", "feedback-ai")
            db = connect(db_path)
            with db:
                db.execute("DELETE FROM profile_versions WHERE profile_hash=?", (hashlib.sha256(b"temporary profile").hexdigest(),))
            db.close()
            second = create_profile_draft(db_path, "second profile", "Second change")
            self.assertEqual(second["id"], 3)

            with TestClient(app) as client:
                page = client.get("/profile")

            self.assertIn("当前 v1", page.text)
            self.assertIn("<strong>v2</strong>", page.text)
            self.assertNotIn("<strong>v3</strong>", page.text)

    def test_direct_profile_assistant_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post(
                    "/profile/assist",
                    data={"csrf_token": app.state.csrf_token, "materials": "paper notes",
                          "summary": "Generate from notes"},
                    follow_redirects=False,
                )
                page=client.get("/profile")
            self.assertEqual(response.status_code,404)
            self.assertNotIn("备用 API",page.text)
            self.assertNotIn("本地论文解析 Prompt",page.text)

    def test_profile_page_can_switch_back_to_a_historical_version(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, profile_file = self.make_app(Path(td))
            with TestClient(app) as client:
                client.post(
                    "/profile/draft",
                    data={"csrf_token": app.state.csrf_token, "content": "second profile",
                          "summary": "Second version"},
                )
                db = connect(db_path)
                original_id = db.execute("SELECT id FROM profile_versions WHERE status='active'").fetchone()[0]
                second_id = db.execute("SELECT id FROM profile_versions WHERE status='draft'").fetchone()[0]
                db.close()
                client.post("/profile/confirm", data={"csrf_token": app.state.csrf_token,
                                                       "version_id": str(second_id)})
                page = client.get("/profile")
                self.assertIn("切换回 v1", page.text)
                response = client.post(
                    "/profile/confirm",
                    data={"csrf_token": app.state.csrf_token, "version_id": str(original_id)},
                    follow_redirects=False,
                )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(profile_file.read_text(), "confirmed profile")

    def test_source_addition_requires_preview_and_preserves_config_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, _, _ = self.make_app(root)
            config_path = root / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8") + '\n[custom]\nkeep_after_sources = "yes"\n',
                encoding="utf-8",
            )
            # Reload after adding a section that must survive source replacement.
            app = create_app(config_path)
            fake_preview = {"source": {}, "total_results": 1,
                            "samples": [{"title": "Preview paper", "venue": "New Venue", "doi": "10.1/new"}],
                            "since": "2026-07-01"}
            candidate={"candidate_id":"c1","name":"New Venue","config_type":"crossref","issn":"9999-9999","openalex_id":"S9"}
            with TestClient(app) as client, patch("academic_radar.web.source_candidates",return_value=[candidate]), \
                 patch("academic_radar.web.preview_source", return_value=fake_preview):
                found=client.get("/api/sources/search?q=New")
                self.assertEqual(found.status_code,200)
                response = client.post("/api/sources/preview",json={"candidate_id":"c1"},
                    headers={"X-CSRF-Token":app.state.csrf_token})
                self.assertEqual(response.status_code, 200)
                token = next(iter(app.state.pending_sources))
                response = client.post("/api/sources/confirm",json={"token":token},
                    headers={"X-CSRF-Token":app.state.csrf_token})
                self.assertEqual(response.status_code, 200)
            config_text = (root / "config.toml").read_text()
            self.assertIn('name = "New Venue"', config_text)
            self.assertIn('issn = "9999-9999"', config_text)
            self.assertIn('[custom]\nkeep_after_sources = "yes"', config_text)
            self.assertTrue(list(root.glob("config.toml.backup-*")))

    def test_source_name_matching_renders_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, _, _ = self.make_app(root)
            candidate = {"candidate_id":"c1","name": "Candidate Journal", "issn": "1111-2222",
                         "publisher": "Publisher", "openalex_id": "S1","config_type":"crossref"}
            with TestClient(app) as client, patch("academic_radar.web.source_candidates", return_value=[candidate]):
                response = client.get("/api/sources/search?q=Candidate")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"][0]["name"],"Candidate Journal")
                self.assertEqual(client.post("/sources/match").status_code,404)

    def test_fulltext_upload_copies_pdf_to_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, db_path, _ = self.make_app(root)
            boundary = "----radar"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n{app.state.csrf_token}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"identity\"\r\n\r\ndoi:10.1/test\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"return_to\"\r\n\r\n/library\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"paper.pdf\"\r\n"
                "Content-Type: application/pdf\r\n\r\n%PDF-1.4 test\r\n"
                f"--{boundary}--\r\n"
            ).encode("utf-8")
            with TestClient(app) as client:
                response = client.post(
                    "/fulltext",
                    content=body,
                    headers={"content-type": f"multipart/form-data; boundary={boundary}"},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
            db = connect(db_path)
            saved = db.execute("SELECT stored_path,original_name FROM fulltext_files").fetchone()
            self.assertEqual(saved["original_name"], "paper.pdf")
            self.assertTrue(Path(saved["stored_path"]).exists())
            self.assertTrue(Path(saved["stored_path"]).name.startswith("作者未知_2026_A useful paper"))
            db.close()

    def test_library_sort_filters_pagination_and_pdf_entry_render_together(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                client.post("/api/favorite",json={"identity":"doi:10.1/test","favorite":True},
                            headers={"X-CSRF-Token":app.state.csrf_token})
                response = client.get("/library?sort=score_asc&favorite=yes&q=useful&interest=&page_no=1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("匹配度：低到高", response.text)
            self.assertIn("本地对应论文卡片中导入 PDF", response.text)
            self.assertEqual(response.text.count('action="/fulltext"'), 1)
            self.assertNotIn('id="pdf-dialog"', response.text)
            self.assertIn("data-inline-pdf-form", response.text)
            self.assertIn('data-has-fulltext="false"', response.text)
            self.assertIn('data-inline-pdf-submit hidden', response.text)
            self.assertNotIn("查看 PDF", response.text)
            self.assertNotIn("data-abstract-toggle", response.text)

    def test_library_defaults_to_all_interests_and_reading_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                before = client.get("/library")
                client.post("/feedback", data={"csrf_token": app.state.csrf_token, "identity": "doi:10.1/test",
                                                 "reading_status": "read_later"})
                after = client.get("/library")
                all_reading = client.get("/library?reading=")
            self.assertIn('value="" selected>全部兴趣', before.text)
            self.assertIn('value="" selected>全部阅读状态', before.text)
            self.assertIn("A useful paper", before.text)
            self.assertIn("A useful paper", after.text)
            self.assertIn("A useful paper", all_reading.text)

    def test_library_uses_latest_host_judgment_with_a_stable_timestamp_tie(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            with connect(db_path) as db:
                db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,
                  themes_json,confidence,screened_at,profile_version_id,feedback_snapshot_json,run_id)
                  SELECT identity,profile_hash,'external','foreign',1,.99,'Foreign result',themes_json,confidence,
                  '2026-07-14T08:00:00+00:00',profile_version_id,feedback_snapshot_json,'foreign-run'
                  FROM screenings LIMIT 1""")
            with TestClient(app) as client:
                page = client.get("/library")
                self.assertEqual(page.context['papers'][0]['reasons'], 'Direct match')
                with connect(db_path) as db:
                    db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,
                      themes_json,confidence,screened_at,profile_version_id,feedback_snapshot_json,run_id)
                      SELECT identity,profile_hash,'codex-agent','tie',1,.745,'Latest tied host result',themes_json,confidence,
                      screened_at,profile_version_id,feedback_snapshot_json,'tied-run'
                      FROM screenings WHERE provider='codex-agent' LIMIT 1""")
                page = client.get("/library")
            self.assertEqual(page.context['papers'][0]['reasons'], 'Latest tied host result')
            self.assertEqual(page.context['papers'][0]['score'], .745)
            self.assertIn('<strong>75</strong>', page.text)

    def test_manual_paper_addition_is_simple_deduplicated_and_queued(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            citation = (
                "Smith, J. A., & Doe, B. (2026). Human-guided optimization for vehicle routing. "
                "Transportation Science, 60(3), 1–15. https://doi.org/10.1287/trsc.2026.1234"
            )
            abstract = (
                "We study an interactive vehicle-routing system in which decision makers express "
                "contextual preferences and revise candidate routes while an optimization algorithm searches."
            )
            with TestClient(app) as client:
                library = client.get("/library")
                self.assertIn("添加文献", library.text)
                self.assertIn('id="manual-paper-dialog"', library.text)
                denied = client.post("/api/papers/manual", json={"apa_citation": citation, "abstract": abstract})
                self.assertEqual(denied.status_code, 403)
                headers = {"X-CSRF-Token": app.state.csrf_token}
                created = client.post(
                    "/api/papers/manual", json={"apa_citation": citation, "abstract": abstract}, headers=headers,
                )
                repeated = client.post(
                    "/api/papers/manual", json={"apa_citation": citation, "abstract": abstract}, headers=headers,
                )
            self.assertEqual(created.status_code, 201)
            self.assertTrue(created.json()["created"])
            self.assertEqual(repeated.status_code, 200)
            self.assertFalse(repeated.json()["created"])
            db = connect(db_path)
            paper = db.execute("""SELECT doi,title,venue,abstract_source,eligibility_status,
              needs_rescreen,manual_citation FROM papers WHERE doi='10.1287/trsc.2026.1234'""").fetchone()
            count = db.execute("SELECT COUNT(*) FROM papers WHERE doi='10.1287/trsc.2026.1234'").fetchone()[0]
            observation = db.execute(
                "SELECT COUNT(*) FROM observations WHERE identity='doi:10.1287/trsc.2026.1234' AND source='manual-apa'"
            ).fetchone()[0]
            db.close()
            self.assertEqual(count, 1)
            self.assertEqual(observation, 1)
            self.assertEqual(paper["title"], "Human-guided optimization for vehicle routing")
            self.assertEqual(paper["venue"], "Transportation Science")
            self.assertEqual(paper["abstract_source"], "user-provided")
            self.assertEqual((paper["eligibility_status"], paper["needs_rescreen"]), ("eligible", 1))
            self.assertEqual(paper["manual_citation"], citation)

    def test_manual_paper_validation_returns_a_plain_json_error(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post(
                    "/api/papers/manual", json={"apa_citation": "too short", "abstract": "also short"},
                    headers={"X-CSRF-Token": app.state.csrf_token},
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn("APA 引用过短", response.json()["error"])

    def test_manual_abstract_preserves_prior_judgment_without_automatic_rescreen(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            db = connect(db_path)
            with db:
                db.execute("UPDATE papers SET abstract='',abstract_source='missing',needs_rescreen=0")
            db.close()
            abstract = (
                "This study examines how decision makers express preferences, revise candidate routes, "
                "and collaborate with an optimization system across repeated planning rounds."
            )
            with TestClient(app) as client:
                page = client.get("/library")
                self.assertIn("保存摘要", page.text)
                response = client.post("/api/papers/abstract", json={
                    "identity":"doi:10.1/test", "abstract":abstract,
                    "source_url":"https://doi.org/10.1/test",
                }, headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["queued_for_screening"])
            self.assertIn("已有评分保留", response.json()["message"])
            db = connect(db_path)
            paper = db.execute("SELECT abstract_source,abstract_failure_reason,needs_rescreen FROM papers").fetchone()
            attempt = db.execute("SELECT provider,status FROM abstract_attempts ORDER BY id DESC LIMIT 1").fetchone()
            db.close()
            self.assertEqual(tuple(paper), ("user-provided", None, 1))
            self.assertEqual(tuple(attempt), ("manual", "found"))

    def test_manual_paper_existing_judgment_is_not_queued_again(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            with connect(db_path) as db:
                db.execute("UPDATE papers SET abstract='' WHERE identity='doi:10.1/test'")
            with TestClient(app) as client:
                response = client.post("/api/papers/manual", json={
                    "apa_citation": "Smith, J. (2026). A useful paper. Test Venue, 1(1), 1–12.",
                    "abstract": "A complete user-provided abstract describes a decision experiment, its research design, observed results and limitations.",
                }, headers={"X-CSRF-Token": app.state.csrf_token})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["created"])
            self.assertTrue(response.json()["abstract_updated"])
            self.assertFalse(response.json()["queued_for_screening"])
            self.assertIn("已有评分保留", response.json()["message"])

    def test_title_only_manual_paper_absorbs_a_later_doi_without_duplication(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            without_doi = "Smith, J. A. (2026). Preference-aware route planning. Transportation Science, 60(4), 1–12."
            with_doi = without_doi + " https://doi.org/10.1287/trsc.2026.9999"
            abstract = (
                "This study lets dispatchers express contextual route preferences and revise candidate "
                "solutions while an optimization system searches for feasible vehicle routes."
            )
            headers = {"X-CSRF-Token": app.state.csrf_token}
            with TestClient(app) as client:
                first = client.post("/api/papers/manual", json={"apa_citation": without_doi, "abstract": abstract}, headers=headers)
                second = client.post("/api/papers/manual", json={"apa_citation": with_doi, "abstract": abstract}, headers=headers)
            self.assertEqual((first.status_code, second.status_code), (201, 200))
            db = connect(db_path)
            rows = db.execute("SELECT identity,doi FROM papers WHERE title='Preference-aware route planning'").fetchall()
            db.close()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["identity"].startswith("title:"))
            self.assertEqual(rows[0]["doi"], "10.1287/trsc.2026.9999")

    def test_fulltext_opens_directly_in_the_browser(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, _, _ = self.make_app(root)
            boundary = "----radar"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n{app.state.csrf_token}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"identity\"\r\n\r\ndoi:10.1/test\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"paper.pdf\"\r\n"
                "Content-Type: application/pdf\r\n\r\n%PDF-1.4 test\r\n"
                f"--{boundary}--\r\n"
            ).encode("utf-8")
            with TestClient(app) as client:
                client.post("/fulltext", content=body, headers={"content-type": f"multipart/form-data; boundary={boundary}"})
                page = client.get("/library?reading=&interest=")
                file_response = client.get("/fulltext/file?identity=doi:10.1/test")
            self.assertIn("查看 PDF", page.text)
            self.assertIn("/fulltext/file?identity=doi%3A10.1/test", page.text)
            self.assertIn('data-has-fulltext="true"', page.text)
            self.assertEqual(file_response.headers["content-type"], "application/pdf")
            self.assertIn("inline", file_response.headers["content-disposition"])

    def test_favorite_api_is_independent_and_immediate(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post("/api/favorite", json={"identity":"doi:10.1/test","favorite":True},
                                       headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertEqual(response.status_code, 200)
            db=connect(db_path); saved=db.execute("SELECT favorite,interest FROM paper_feedback").fetchone(); db.close()
            self.assertEqual(tuple(saved),(1,None))

    def test_today_uses_selected_new_from_latest_import_and_compact_details(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            db=connect(db_path)
            with db:
                db.execute("""INSERT INTO pipeline_runs(run_id,kind,status,started_at,finished_at)
                  VALUES('run','agent-export','succeeded','now','now')""")
                active=db.execute("SELECT profile_hash,id FROM profile_versions WHERE status='active'").fetchone()
                db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,exported_count,imported_count,created_at,imported_at,profile_version_id)
                  VALUES('run',?,'imported',1,1,'now','now',?)""",(active[0],active[1]))
                db.execute("INSERT INTO run_papers VALUES('run','doi:10.1/test','selected_new')")
            with db:
                db.execute("UPDATE agent_jobs SET imported_at=?", (dt.datetime.now(dt.timezone.utc).isoformat(),))
            db.close()
            with TestClient(app) as client: response=client.get("/")
            self.assertIn("A useful paper",response.text)
            self.assertIn("详情",response.text)
            self.assertNotIn('action="/fulltext"',response.text)
            self.assertNotIn("打开原文", response.text)
            self.assertNotIn("展开摘要", response.text)
            self.assertIn('class="paper-title-link"', response.text)
            self.assertIn("标记与反馈", response.text)
            self.assertNotIn("feedback-drawer", response.text)
            self.assertIn("AI",response.text)

    def test_today_places_completed_feedback_after_unfinished_papers(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            db = connect(db_path)
            with db:
                active = db.execute("SELECT profile_hash,id FROM profile_versions WHERE status='active'").fetchone()
                for identity, title in (("doi:10.1/second", "Second paper"), ("doi:10.1/third", "Third paper")):
                    db.execute("""INSERT INTO papers(identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at,
                      publication_type,eligibility_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (identity, identity[4:], title, "Abstract", "Test Venue", "2026-07-13", "", "[]", "now", "now",
                       "Journal Article", "eligible"))
                    db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,themes_json,confidence,
                      screened_at,profile_version_id,feedback_snapshot_json,run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (identity, active[0], "codex-agent", "test", 1, .8, "Direct match", "[]", .9,
                       "2026-07-13T08:00:00+00:00", active[1], "[]", "run"))
                db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at,finished_at) VALUES('run','agent-export','succeeded','now','now')")
                db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,exported_count,imported_count,created_at,imported_at,profile_version_id)
                  VALUES('run',?,'imported',3,3,'now','now',?)""", (active[0], active[1]))
                db.executemany("INSERT INTO run_papers VALUES('run',?,'selected_new')",
                               [("doi:10.1/test",), ("doi:10.1/second",), ("doi:10.1/third",)])
            with db:
                db.execute("UPDATE agent_jobs SET imported_at=?", (dt.datetime.now(dt.timezone.utc).isoformat(),))
            db.close()
            with TestClient(app) as client:
                for identity, interest, reason in (("doi:10.1/second", "interested", "Useful"),
                                                   ("doi:10.1/third", "not_interested", "Out of scope")):
                    client.post("/feedback", data={"csrf_token": app.state.csrf_token, "identity": identity,
                                                     "interest": interest, "reason": reason, "reading_status": "unread"})
                page = client.get("/").text
            unfinished = page.index('data-interest=""')
            interested = page.index('data-interest="interested"')
            not_interested = page.index('data-interest="not_interested"')
            self.assertLess(unfinished, interested)
            self.assertLess(interested, not_interested)

    def test_feedback_api_returns_completion_status(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post(
                    "/api/feedback",
                    json={"identity": "doi:10.1/test", "interest": "not_interested",
                          "reason": "Outside the current scope", "favorite": False, "reading_status": "unread"},
                    headers={"X-CSRF-Token": app.state.csrf_token},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status_label"], "已完成 · 不感兴趣")
            db = connect(db_path)
            self.assertEqual(db.execute("SELECT interest FROM paper_feedback").fetchone()[0], "not_interested")
            db.close()

    def test_dismissed_system_suggestion_is_not_a_profile_version(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            from academic_radar.engagement import create_feedback_profile_suggestion, pending_profile_review

            with TestClient(app) as client:
                client.post("/feedback", data={"csrf_token": app.state.csrf_token, "identity": "doi:10.1/test",
                                                 "interest": "interested", "reason": "Useful", "reading_status": "unread"})
                review = pending_profile_review(db_path)
                suggestion = create_feedback_profile_suggestion(
                    db_path, review["fingerprint"], "suggested profile", "System suggestion"
                )
                response = client.post("/profile/dismiss", data={"csrf_token": app.state.csrf_token,
                                                                     "version_id": suggestion["version"]["id"]})
                page = client.get("/profile")
            self.assertEqual(response.status_code, 200)
            self.assertIn("1 个已采用版本", page.text)
            self.assertNotIn("System suggestion", page.text)

    def test_feedback_page_is_interactive_without_change_history(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                client.post("/feedback",data={"csrf_token":app.state.csrf_token,"identity":"doi:10.1/test",
                    "interest":"interested","reason":"匹配研究问题","reading_status":"read_later"})
                response=client.get("/feedback?interest=interested")
            self.assertIn("查看与编辑",response.text)
            self.assertIn("清除当前反馈",response.text)
            self.assertNotIn("打开原文",response.text)
            self.assertNotIn("变更历史",response.text)

    def test_source_search_assets_support_debounce_keyboard_and_no_page_post(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                page=client.get("/sources")
                script=client.get("/static/app.js")
            self.assertNotIn("/sources/match",page.text)
            self.assertIn("320",script.text)
            self.assertIn("ArrowDown",script.text)
            self.assertIn("preventDefault",script.text)
            self.assertIn("aria-activedescendant",script.text)
            self.assertIn("aria-selected",script.text)
            self.assertIn("比如：Management Science 或 International Journal of Human-Computer Interaction", page.text)

    def test_duplicate_source_is_marked_and_cannot_preview(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            candidate={"candidate_id":"same","name":"Test Venue","issn":"1234-5678","config_type":"crossref"}
            with TestClient(app) as client, patch("academic_radar.web.source_candidates",return_value=[candidate]):
                found=client.get("/api/sources/search?q=Test").json()["items"][0]
                blocked=client.post("/api/sources/preview",json={"candidate_id":"same"},
                    headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertTrue(found["added"])
            self.assertEqual(blocked.status_code,409)

    def test_source_removal_backs_up_config_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); app, db_path, _=self.make_app(root); config=root/"config.toml"
            config.write_text(config.read_text()+"\n[[sources]]\nname = \"Second Venue\"\ntype = \"crossref\"\nissn = \"9999-9999\"\n",encoding="utf-8")
            app=create_app(config)
            db=connect(db_path)
            with db: db.execute("INSERT INTO observations VALUES('doi:10.1/test','Second Venue','now')")
            db.close()
            with TestClient(app) as client:
                response=client.post("/api/sources/remove",json={"name":"Second Venue"},
                    headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertEqual(response.status_code,200)
            self.assertNotIn('name = "Second Venue"',config.read_text())
            self.assertTrue(list(root.glob("config.toml.backup-*")))
            db=connect(db_path); count=db.execute("SELECT COUNT(*) FROM observations WHERE source='Second Venue'").fetchone()[0]; db.close()
            self.assertEqual(count,1)

    def test_source_removal_failure_keeps_original_config(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); app, _, _=self.make_app(root); config=root/"config.toml"
            config.write_text(config.read_text()+"\n[[sources]]\nname = \"Second Venue\"\ntype = \"crossref\"\nissn = \"9999-9999\"\n",encoding="utf-8")
            original=config.read_text(); app=create_app(config)
            with TestClient(app) as client, patch("academic_radar.web.os.replace",side_effect=OSError("disk")):
                response=client.post("/api/sources/remove",json={"name":"Second Venue"},
                    headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertEqual(response.status_code,500)
            self.assertEqual(config.read_text(),original)

    def test_status_page_has_actionable_buttons_and_no_provider_api(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _=self.make_app(Path(td))
            with TestClient(app) as client: response=client.get("/status")
            for label in ("更新数据库", "当前：", "由 Codex 完整执行", "需要处理", "推荐精确率"):
                self.assertIn(label,response.text)
            for label in ("agent-export", "agent-import"):
                self.assertIn(label,response.text)
            self.assertNotIn("recent-tasks-panel", response.text)
            self.assertNotIn("网页运行方式", response.text)
            self.assertNotIn("交给 Codex 判断相关性", response.text)
            self.assertNotIn("数据库结构版本", response.text)
            self.assertNotIn("API Key",response.text)
            self.assertNotIn("DeepSeek",response.text)

    def test_local_feedback_edits_queue_sync_after_saving(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, db_path, _ = self.make_app(root)
            app.state.config["cloud_sync"] = {"enabled": True}
            token = app.state.csrf_token
            payload = {"identity": "doi:10.1/test", "interest": "interested", "reason": "Useful mechanism",
                       "favorite": True, "reading_status": "read_later"}
            with TestClient(app) as client, patch("academic_radar.web.background_sync", return_value={"status": "queued"}) as queued:
                self.assertEqual(client.post("/api/feedback", json=payload, headers={"X-CSRF-Token": token}).status_code, 200)
                self.assertEqual(client.post("/api/favorite", json={"identity": "doi:10.1/test", "favorite": False},
                                             headers={"X-CSRF-Token": token}).status_code, 200)
                self.assertEqual(client.post("/feedback", data={"csrf_token": token, "identity": "doi:10.1/test",
                    "interest": "not_interested", "reason": "Mechanism outside scope", "reading_status": "read"},
                    follow_redirects=False).status_code, 303)
                self.assertEqual(client.post("/feedback/clear", data={"csrf_token": token, "identity": "doi:10.1/test"},
                                             follow_redirects=False).status_code, 303)
                self.assertEqual(queued.call_count, 4)
                for call in queued.call_args_list:
                    self.assertEqual(call.args, ((root / "config.toml").resolve(),))
                page = client.get("/feedback")
                self.assertNotIn("A useful paper", page.text)
                self.assertIn("没有符合筛选条件的当前反馈", page.text)
            db = connect(db_path)
            try:
                self.assertEqual(tuple(db.execute("SELECT interest,reason,favorite,reading_status FROM paper_feedback").fetchone()),
                                 (None, None, 0, "unread"))
            finally:
                db.close()

    def test_manual_sync_requires_csrf_and_status_never_exposes_private_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, _, _ = self.make_app(root)
            app.state.config["cloud_sync"] = {"enabled": True}
            (root / "cloud-sync-status.json").write_text(json.dumps({
                "status": "failed", "checked_at": "2026-10-09T01:00:00Z", "generation": "a" * 64,
                "records": 20, "error": "TOKEN-SECRET /private/installation https://user:password@example",
                "credentials_file": "/private/installation/credentials", "sync_token": "SECRET",
            }))
            with TestClient(app) as client, patch("academic_radar.web.background_sync", return_value={"status": "queued"}) as queued, \
                 patch("academic_radar.web.background_sync_state", return_value={"running": False}):
                self.assertEqual(client.post("/api/sync", json={}).status_code, 403)
                queued.assert_not_called()
                response = client.post("/api/sync", json={}, headers={"X-CSRF-Token": app.state.csrf_token})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "queued")
                queued.assert_called_once_with((root / "config.toml").resolve())
                status = client.get("/api/sync/status")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()["status"], "failed")
                self.assertEqual(status.json()["records"], 20)
                self.assertEqual(status.json()["generation"], "a" * 64)
                for private in ("SECRET", "/private/installation", "password", "credentials_file", "sync_token"):
                    self.assertNotIn(private, status.text)
            with TestClient(app) as client, patch("academic_radar.web.background_sync_state", return_value={"running": True}):
                self.assertEqual(client.get("/api/sync/status").json()["status"], "running")

    def test_feedback_remains_available_when_cloud_sync_is_not_configured(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client, patch("academic_radar.web.background_sync") as queued:
                response = client.post("/api/favorite", json={"identity": "doi:10.1/test", "favorite": True},
                                       headers={"X-CSRF-Token": app.state.csrf_token})
                self.assertEqual(response.status_code, 200)
                queued.assert_not_called()
                self.assertEqual(client.get("/api/sync/status").json()["status"], "disabled")


if __name__ == "__main__":
    unittest.main()

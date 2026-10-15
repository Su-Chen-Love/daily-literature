import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from academic_radar.cloud_views import export_view_contexts, history_artifact_availability
from academic_radar.recommendations import SCREENING_RUBRIC_VERSION
from academic_radar.storage import connect, upgrade_database


class SharedCloudViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.path = self.state / "papers.sqlite3"
        upgrade_database(self.path)
        self.db = connect(self.path)
        content = "Study human control and collaborative decisions."
        digest = hashlib.sha256(content.encode()).hexdigest()
        (self.state / "research-profile.md").write_text(content)
        (self.state / "cloud-sync-status.json").write_text(json.dumps({
            "status": "succeeded", "synced_at": "2026-10-09T01:00:00Z",
            "token": "SYNC-SECRET", "error": "/private/installation/api-token",
        }))
        self.config = {
            "profile_file": "research-profile.md", "timezone": "Asia/Shanghai", "relevance_threshold": .7,
            "cloud_sync": {"enabled": True, "credentials_file": "/private/installation/CREDENTIAL-SECRET"},
            "user_agent": "CONTACT-SECRET", "sources": [{
                "name": "Journal %_", "type": "crossref", "issn": "1234-567X",
                "api_key": "SOURCE-SECRET", "official_status": "verified",
                "official_issues_url": "https://publisher.example/issues?api_key=URL-SECRET#private",
            }],
        }
        with self.db:
            self.db.execute("""INSERT INTO profile_versions(profile_hash,content,status,source,change_summary,
                created_at,confirmed_at) VALUES('old-profile','Former full profile','superseded','manual',
                'Old confirmed version','2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')""")
            self.db.execute("""INSERT INTO profile_versions(profile_hash,content,status,source,change_summary,
                created_at,confirmed_at) VALUES(?,?,'active','manual','Confirmed interests',
                '2026-10-02T00:00:00Z','2026-10-02T00:00:00Z')""", (digest, content))
            self.db.execute("""INSERT INTO papers(identity,doi,title,abstract,venue,published,published_precision,
                url,authors_json,first_seen,updated_at,publication_type,eligibility_status)
                VALUES('doi:10.1/research','10.1/research','Collaboration study','An original abstract.',
                'Journal %_','2026-10-01','month','https://doi.org/10.1/research','[]',
                '2026-10-03T00:00:00Z','2026-10-03T00:00:00Z','Journal Article','eligible')""")
            self.db.execute("""INSERT INTO observations VALUES('doi:10.1/research','Journal %_ / OpenAlex','2026-10-03T00:00:00Z')""")
            self.db.execute("""INSERT INTO observations VALUES('doi:10.1/research','Journal %_','2026-10-03T00:00:00Z')""")
            self.db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,
                screened_at,rubric_version) VALUES('doi:10.1/research',?,'codex-agent','host',1,.9,
                'Problem, method and evidenced contribution.','2026-10-03T00:00:00Z',?)""", (digest, SCREENING_RUBRIC_VERSION))
            self.db.execute("""INSERT INTO paper_feedback VALUES('doi:10.1/research','interested',
                'A useful control measure',1,'read_later','2026-10-03T00:00:00Z','2026-10-03T00:00:00Z')""")
            self.db.execute("""INSERT INTO feedback_events(identity,interest,reason,favorite,reading_status,created_at)
                VALUES('doi:10.1/research','interested','A useful control measure',1,'read_later','2026-10-03T00:00:00Z')""")
            self.db.execute("""INSERT INTO source_runs(run_id,source,status,count,error,finished_at) VALUES('api-run','Journal %_','failed',3,
                'HTTP 503 https://api.example?token=ERROR-SECRET /private/installation/database','2026-10-03T00:00:00Z')""")
            self.db.execute("""INSERT INTO source_health(source,status,last_error,updated_at)
                VALUES('Journal %_','failed','RAW-ERROR-SECRET','2026-10-03T00:00:00Z')""")
            self.db.execute("""INSERT INTO official_issue_checks(source_name,issue_key,issue_url,status,article_count,detail,checked_at)
                VALUES('Journal %_','metadata-latest-two-as-of-2026-10-09','https://publisher.example/issues','succeeded',0,?,'2026-10-09T00:00:00Z')""",
                (json.dumps({"phase": "latest_two_metadata_refresh", "as_of_date": "2026-10-09",
                    "evidence_type": "publisher_metadata", "issue_keys": ["v10/n2", "v10/n1"],
                    "token": "EVIDENCE-SECRET", "issues": [{"issue_key": "v10/n2", "published_precision": "month",
                    "uncertain_date_count": 3, "article_count": 20, "raw_url": "INTERNAL-SECRET"}]}),))
            self.db.execute("""INSERT INTO profile_review_runs(fingerprint,status,feedback_count,details_json,
                created_at,updated_at) VALUES('review','no_change',1,?,
                '2026-10-04T00:00:00Z','2026-10-04T00:00:00Z')""",
                (json.dumps({"reason": "Existing interests already cover this signal.", "events": "REVIEW-INTERNAL"}),))
            self.db.execute("""INSERT INTO pipeline_runs(run_id,kind,status,started_at,error_summary,details_json)
                VALUES('api-run','collection','failed','2026-10-03T00:00:00Z','RUN-ERROR-SECRET','{"path":"/private/installation"}')""")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_shared_views_include_profile_sources_feedback_and_checks(self):
        self.db.execute("BEGIN")
        self.db.execute("PRAGMA query_only=ON")
        total_changes = self.db.total_changes
        contexts = export_view_contexts(self.db, self.config, self.state)
        self.assertEqual(set(contexts), {"today", "library", "sources", "profile", "feedback", "status"})
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.total_changes, total_changes)
        self.assertEqual(contexts["today"]["totals"]["papers"], 1)
        self.assertEqual(contexts["today"]["totals"]["favorites"], 1)
        self.assertEqual(contexts["library"]["total"], 1)
        profile = contexts["profile"]
        self.assertEqual(len(profile["versions"]), 2)
        self.assertEqual(profile["active"]["version_number"], 2)
        self.assertIn("human control", profile["active"]["content"])
        self.assertEqual(profile["profile_review"]["history_count"], 1)
        self.assertFalse(profile["profile_review"]["needed"])
        self.assertIn("already cover", profile["profile_review"]["latest_review"]["reason"])
        source = contexts["sources"]["sources"][0]
        self.assertEqual(source["coverage"]["paper_count"], 1)
        self.assertEqual(source["coverage"]["abstract_percent"], 100)
        self.assertEqual(source["latest"]["count"], 3)
        self.assertIn("HTTP 503", source["latest"]["error"])
        self.assertIsNone(source["official_check"])
        self.assertEqual(source["official_latest"]["evidence"]["issues"][0]["published_precision"], "month")
        self.assertEqual(contexts["feedback"]["stats"]["total"], 1)
        self.assertEqual(contexts["feedback"]["stats"]["event_count"], 1)
        self.assertEqual(contexts["status"]["recommendation_quality"]["precision_percent"], 100)
        self.assertEqual(contexts["status"]["pipeline_runs"][0]["status"], "failed")
        self.assertIn("source_runs", {item["name"] for item in contexts["status"]["checks"]})

    def test_view_export_never_publishes_internal_errors_paths_or_credentials(self):
        self.db.execute("BEGIN")
        exported = json.dumps(export_view_contexts(self.db, self.config, self.state))
        for secret in ("SYNC-SECRET", "CONTACT-SECRET", "SOURCE-SECRET", "URL-SECRET", "ERROR-SECRET",
                       "RAW-ERROR-SECRET", "EVIDENCE-SECRET", "INTERNAL-SECRET", "REVIEW-INTERNAL",
                       "RUN-ERROR-SECRET", "CREDENTIAL-SECRET", "/private/installation", str(self.state)):
            self.assertNotIn(secret, exported)
        self.assertNotIn("csrf_token", exported)
        self.assertNotIn("synced_at", exported)

    def test_export_uses_the_existing_consistent_snapshot(self):
        self.db.execute("BEGIN")
        self.db.execute("SELECT COUNT(*) FROM paper_feedback").fetchone()
        writer = connect(self.path)
        try:
            with writer:
                writer.execute("UPDATE paper_feedback SET favorite=0,reading_status='read'")
            contexts = export_view_contexts(self.db, self.config, self.state)
            self.assertEqual(contexts["today"]["totals"]["favorites"], 1)
            self.assertEqual(contexts["feedback"]["stats"]["favorites"], 1)
            self.assertEqual(contexts["feedback"]["items"][0]["reading_status"], "read_later")
        finally:
            writer.close()

    def test_export_requires_a_caller_owned_transaction(self):
        with self.assertRaisesRegex(ValueError, "snapshot transaction"):
            export_view_contexts(self.db, self.config, self.state)

    def test_archived_history_summary_is_readonly_and_hides_provenance(self):
        queue = "archive://" + "a" * 64
        backup = "archive://" + "b" * 64
        with self.db:
            self.db.execute("UPDATE pipeline_runs SET details_json=?",
                            (json.dumps({"queue_path": queue, "nested": {"backup_path": backup},
                                         "legacy_path": "/private/mac/archive.sqlite3"}),))
            self.db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,queue_path,created_at)
                VALUES('api-run','old-profile','abandoned',?,'2026-10-01T00:00:00Z')""", (queue,))
        self.db.execute("BEGIN")
        self.db.execute("PRAGMA query_only=ON")
        before = self.db.total_changes
        summary = history_artifact_availability(self.db)
        self.assertEqual(summary, {"unavailable_files": 2, "local_restore_backups": 1})
        exported = export_view_contexts(self.db, self.config, self.state)
        self.assertEqual(exported["status"]["history_artifacts"], summary)
        self.assertEqual(self.db.total_changes, before)
        for private in (queue, backup, "/private/mac/archive.sqlite3"):
            self.assertNotIn(private, json.dumps(exported))

    def test_prior_profile_and_rubric_decisions_are_retained_without_daily_rework(self):
        with self.db:
            self.db.execute("UPDATE screenings SET profile_hash='old-profile',rubric_version='legacy'")
            self.db.execute("UPDATE papers SET needs_rescreen=1")
            self.db.execute("""INSERT INTO pipeline_runs(run_id,kind,status,started_at)
                VALUES('saved-run','agent-export','succeeded','2026-10-03T00:00:00Z')""")
            self.db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,created_at,imported_at)
                VALUES('saved-run','old-profile','imported','2026-10-03T00:00:00Z','2026-10-03T00:00:00Z')""")
        self.db.execute("BEGIN")
        contexts = export_view_contexts(self.db, self.config, self.state)
        self.assertEqual(contexts["library"]["total"], 1)
        self.assertEqual(contexts["today"]["latest_job"]["run_id"], "saved-run")
        self.assertEqual(contexts["today"]["freshness"]["last_import"], "2026-10-03 08:00")
        status = contexts["status"]
        coverage = next(item for item in status["all_checks"] if item["name"] == "semantic_coverage")
        self.assertTrue(coverage["ok"])
        self.assertEqual(status["pending_screening"], 0)
        self.assertNotIn("待按新量表重新判断", status["update_summary"])
        self.assertIn("仅导出从未由 Codex 判断过的新文献", status["update_prompt"])


if __name__ == "__main__":
    unittest.main()

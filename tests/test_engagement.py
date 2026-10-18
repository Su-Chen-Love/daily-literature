import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from academic_radar.engagement import (
    active_profile,
    confirm_profile,
    create_feedback_profile_suggestion,
    create_profile_draft,
    dismiss_profile_suggestion,
    feedback_examples,
    pending_profile_review,
    record_profile_review_no_change,
    seed_active_profile,
    set_feedback,
)
from academic_radar.storage import connect, upgrade_database


class EngagementTests(unittest.TestCase):
    def add_paper(self, db_path: Path, identity: str = "doi:10.1/example") -> None:
        upgrade_database(db_path)
        db = connect(db_path)
        with db:
            db.execute(
                """INSERT INTO papers(
                identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identity, "10.1/example", "Example paper", "Abstract", "Venue", "2026-01-01",
                 "https://doi.org/10.1/example", "[]", "now", "now"),
            )
        db.close()

    def test_feedback_optional_reason_and_keeps_audit_history(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            self.add_paper(db_path)
            empty = set_feedback(db_path, "doi:10.1/example", "interested", "", False, "unread")
            self.assertIsNone(empty["reason"])
            set_feedback(db_path, "doi:10.1/example", "interested", "Transfers directly", True, "read_later")
            set_feedback(db_path, "doi:10.1/example", "not_interested", "Wrong empirical setting", False, "read")
            examples = feedback_examples(db_path)
            self.assertEqual(examples[0]["interest"], "not_interested")
            db = connect(db_path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT needs_rescreen FROM papers").fetchone()[0], 1)
            db.execute("UPDATE papers SET needs_rescreen=0")
            db.commit()
            db.close()
            set_feedback(db_path, "doi:10.1/example", "not_interested", "Wrong empirical setting", True, "read")
            db = connect(db_path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT needs_rescreen FROM papers").fetchone()[0], 0)
            db.close()

    def test_profile_draft_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            profile_file = root / "research-profile.md"
            profile_file.write_text("original", encoding="utf-8")
            original = seed_active_profile(db_path, "original")
            draft = create_profile_draft(db_path, "revised", "Add a boundary")
            self.assertEqual(active_profile(db_path)["id"], original["id"])
            self.assertEqual(profile_file.read_text(), "original")
            confirmed = confirm_profile(db_path, draft["id"], profile_file)
            self.assertEqual(confirmed["status"], "active")
            self.assertEqual(active_profile(db_path)["id"], draft["id"])
            self.assertEqual(profile_file.read_text(), "revised")

            rolled_back = confirm_profile(db_path, original["id"], profile_file)
            self.assertEqual(rolled_back["status"], "active")
            self.assertEqual(active_profile(db_path)["id"], original["id"])
            self.assertEqual(profile_file.read_text(), "original")

    def test_feedback_review_can_suggest_accept_or_dismiss_profile_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            profile_file = root / "research-profile.md"
            profile_file.write_text("original", encoding="utf-8")
            seed_active_profile(db_path, "original")
            self.add_paper(db_path)

            set_feedback(db_path, "doi:10.1/example", "interested", "Directly useful", False, "read")
            review = pending_profile_review(db_path)
            self.assertTrue(review["needed"])
            self.assertEqual(review["feedback_count"], 1)
            suggestion = create_feedback_profile_suggestion(
                db_path, review["fingerprint"], "revised", "Add this method family"
            )
            self.assertEqual(suggestion["status"], "suggested")
            pending = pending_profile_review(db_path)
            self.assertFalse(pending["needed"])
            self.assertEqual(pending["pending_suggestion"]["version_id"], suggestion["version"]["id"])

            dismiss_profile_suggestion(db_path, suggestion["version"]["id"])
            self.assertIsNone(pending_profile_review(db_path)["pending_suggestion"])
            db = connect(db_path)
            self.assertIsNone(db.execute("SELECT id FROM profile_versions WHERE id=?", (suggestion["version"]["id"],)).fetchone())
            self.assertIsNone(db.execute("SELECT profile_version_id FROM profile_review_runs").fetchone()[0])
            db.close()

    def test_feedback_review_can_record_no_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            seed_active_profile(db_path, "original")
            self.add_paper(db_path)
            set_feedback(db_path, "doi:10.1/example", "not_interested", "Out of scope", False, "unread")
            review = pending_profile_review(db_path)
            result = record_profile_review_no_change(db_path, review["fingerprint"], "Already excluded")
            self.assertEqual(result["status"], "no_change")
            reviewed = pending_profile_review(db_path)
            self.assertFalse(reviewed["needed"])
            self.assertEqual(reviewed["latest_review"]["reason"], "Already excluded")
            self.assertEqual(reviewed["history_count"], 1)

    def test_profile_review_uses_only_latest_feedback_per_paper(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            seed_active_profile(db_path, "original")
            self.add_paper(db_path)
            set_feedback(db_path, "doi:10.1/example", "interested", "First reason", False, "unread")
            set_feedback(db_path, "doi:10.1/example", "interested", "Latest reason", False, "read")
            review = pending_profile_review(db_path)
            self.assertEqual(review["feedback_count"], 1)
            self.assertEqual(review["events"][0]["reason"], "Latest reason")
            self.assertEqual(review["feedback_history"][0]["reason"], "Latest reason")


if __name__ == "__main__":
    unittest.main()

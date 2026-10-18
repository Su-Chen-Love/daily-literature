import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
import test_web
from academic_radar.storage import connect
from academic_radar.cloud_sync import apply_remote_feedback
from academic_radar.profile_sync import unified_profile_text


class PersonalCenterTests(unittest.TestCase):
    def make_app(self, directory):
        return test_web.WebTests(methodName='runTest').make_app(directory)

    def test_saved_low_score_and_excluded_papers_remain_accessible(self):
        with tempfile.TemporaryDirectory() as td:
            app, path, _ = self.make_app(Path(td))
            with connect(path) as db:
                db.execute('UPDATE screenings SET score=.55')
                db.execute("UPDATE papers SET eligibility_status='excluded'")
                db.execute("INSERT INTO paper_feedback(identity,interest,reason,favorite,reading_status,created_at,updated_at) VALUES('doi:10.1/test',NULL,NULL,1,'read_later','2026-01-01','2026-01-01')")
            with TestClient(app) as client:
                self.assertNotIn('A useful paper', client.get('/library').text)
                for query in ['favorite=yes', 'reading=read_later']:
                    self.assertIn('A useful paper', client.get('/library?' + query).text)
            with connect(path) as db:
                self.assertEqual(db.execute('SELECT score FROM screenings').fetchone()[0], .55)
                self.assertEqual(db.execute('SELECT favorite,reading_status FROM paper_feedback').fetchone()[:], (1,'read_later'))

    def test_empty_feedback_reason_saves_and_remote_roundtrip_accepts_null(self):
        with tempfile.TemporaryDirectory() as td:
            app, path, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response=client.post('/api/feedback',headers={'X-CSRF-Token':app.state.csrf_token},json={
                    'identity':'doi:10.1/test','interest':'interested','reason':'','favorite':True,'reading_status':'read_later'})
                self.assertEqual(response.status_code,200)
            event={'seq':1,'identity':'doi:10.1/test','interest':'not_interested','reason':None,'favorite':1,'reading_status':'read_later','updated_at':'2099-01-01T00:00:00Z'}
            with connect(path) as db:
                self.assertEqual(apply_remote_feedback(db,[event],1),1)
                item=db.execute('SELECT interest,reason,favorite,reading_status FROM paper_feedback').fetchone()
                self.assertEqual(tuple(item),('not_interested',None,1,'read_later'))

    def test_legacy_checks_redirect_and_center_has_true_timestamps(self):
        with tempfile.TemporaryDirectory() as td:
            app,path,_=self.make_app(Path(td))
            with TestClient(app) as client:
                old=client.get('/status',follow_redirects=False)
                self.assertEqual(old.status_code,303)
                self.assertEqual(old.headers['location'],'/?center=checks')
                result=client.get('/api/center/status').json()
                self.assertIn('更新数据库',result['html'])
                self.assertTrue(result['local_checked_at'])
                page=client.get('/').text
                self.assertNotIn('href="/status"',page)
                self.assertEqual(page.count('name="appearance-font"'),4)
                self.assertEqual(page.count('name="appearance-theme"'),4)
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT value FROM meta WHERE key='last_local_check'").fetchone()[0],result['local_checked_at'])

    def test_legacy_preferences_are_visible_in_single_editor_and_clear_only_on_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            app,path,profile_file=self.make_app(Path(td))
            with connect(path) as db:
                db.execute("INSERT INTO meta(key,value) VALUES('cloud_screening_preferences',?)",(json.dumps({'relevance_threshold':.85,'instructions':'Keep these specific methodological boundaries.'}),))
            with TestClient(app) as client:
                html=client.get('/profile').text
                self.assertIn('Keep these specific methodological boundaries.',html)
                self.assertNotIn('name="instructions"',html)
                content=unified_profile_text(profile_file.read_text(),'Keep these specific methodological boundaries.')
                response=client.post('/profile/draft',data={'csrf_token':app.state.csrf_token,'content':content,'summary':'Consolidate existing content'},follow_redirects=False)
                self.assertEqual(response.status_code,303)
                with connect(path) as db:
                    draft=db.execute("SELECT id FROM profile_versions WHERE status='draft'").fetchone()[0]
                    self.assertEqual(json.loads(db.execute("SELECT value FROM meta WHERE key='cloud_screening_preferences'").fetchone()[0])['relevance_threshold'],.85)
                client.post('/profile/confirm',data={'csrf_token':app.state.csrf_token,'version_id':draft})
            self.assertIn('Keep these specific methodological boundaries.',profile_file.read_text())
            with connect(path) as db:
                self.assertEqual(json.loads(db.execute("SELECT value FROM meta WHERE key='cloud_screening_preferences'").fetchone()[0]),{'relevance_threshold':.7,'instructions':''})

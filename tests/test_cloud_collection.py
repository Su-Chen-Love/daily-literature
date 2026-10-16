"""Entire daily pipeline uses synthetic APIs/evidence; never public collection."""
import contextlib
import copy
import datetime as dt
import io
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from academic_radar import cloud_collection, cloud_runtime as runtime, official
from academic_radar.checkpoints import create_checkpoint, restore_checkpoint
from academic_radar.cloud_views import export_view_contexts
from academic_radar.product import load_config
from academic_radar.storage import connect
import test_official
import test_paper_monitor


class CloudCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.state = self.root / 'state'; self.state.mkdir()
        self.config = self.state / 'config.toml'; self.db_path = self.state / 'papers.sqlite3'
        self.write_config()
        (self.state / 'profile.md').write_text('Human-in-the-loop interactive optimization and routing decisions')
        self.pm = runtime._monitor()
        with contextlib.closing(self.pm.db_open(self.db_path)) as db, db:
            self.pm.upsert(db, self.paper('old'), '2020-01-01T00:00:00+00:00')
            self.pm.confirmed_profile(db, self.state / 'profile.md')
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(runtime, '_monitor', return_value=self.pm))
        self.api = self.stack.enter_context(patch.object(self.pm, 'crossref_collect', side_effect=lambda *a: [self.paper('api')] ))
        self.stack.enter_context(patch.object(self.pm, 'enrich_missing_abstracts'))
        self.enrich = self.stack.enter_context(patch.object(self.pm, 'run_enrichment', return_value={
            'status':'succeeded','checked':0,'deferred':0,'budget_exhausted':False}))
        self.adapter = self.stack.enter_context(patch.object(official, '_collect_print_metadata_source', side_effect=self.package_source))
        self.network = self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected public network')))

    def write_config(self, *, extra='', budget=''):
        self.config.write_text(f'state_dir = "{self.state}"\nprofile_file = "profile.md"\nrelevance_threshold = 0.7\n'
            '[cloud_runtime]\nrole="staging"\n' + budget +
            '[[sources]]\nname="Management Science"\ntype="crossref"\nissn="0025-1909"\n' + extra)

    def paper(self, name):
        return self.pm.Paper('doi:10.1287/' + name, '10.1287/' + name, 'Interactive optimization ' + name,
            'Original complete abstract about interactive optimization and human control. ' * 3,
            'Management Science', '2026-10-01', '', [], 'Management Science',
            publication_type_raw='journal-article', publication_type_source='crossref')

    def package_source(self, source, spec, as_of):
        result = test_official.OfficialIssueTests.metadata_package()['sources'][0]
        result['source_name'] = source['name']
        for issue in result['issues']:
            for paper in issue['papers']:
                paper.update(abstract='Original complete publisher abstract about interactive optimization and human control. ' * 3,
                             abstract_evidence_type='publisher_deposited_metadata',
                             abstract_evidence_url='https://api.crossref.org/works/' + paper['doi'])
                paper.pop('abstract_unavailable_traceable');paper.pop('abstract_failure_reason')
        return result

    def prepare(self, operation='daily-fixture'):
        return runtime.prepare(self.config, operation, no_collect=False)

    def journal(self, operation='daily-fixture'):
        return runtime._read(self.db_path, operation)

    def counts(self):
        with contextlib.closing(connect(self.db_path)) as db:
            return {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                    for name in ('papers','observations','abstract_attempts','official_issue_checks','agent_jobs')}

    def import_result(self, prepared):
        queue = json.loads(Path(prepared['queue_path']).read_text())
        results=[]
        for paper in queue['papers']:
            result=test_paper_monitor.MonitorTests.structured_result(paper['identity'])
            result['evidence_anchors'][0]['quote']=paper['abstract'][:80]
            results.append(result)
        path = self.state / ('results-' + prepared['operation_id'] + '.json')
        path.write_text(json.dumps({'run_id':queue['run_id'], 'profile_hash':queue['profile_hash'],
            'model':'synthetic-fixture' if queue['papers'] else 'no-candidates',
            'source_failures':queue['source_failures'],
            'results':results}))
        return runtime.import_results(self.config, prepared['operation_id'], path)

    def assert_no_queue(self):
        self.assertEqual(self.counts()['agent_jobs'], 0)
        self.assertIsNone(runtime._meta(self.db_path, 'cloud_runtime_export:daily-fixture'))

    def test_order_same_batch_new_identities_and_immutable_report(self):
        order=[]
        def wrap(obj, name, label):
            original=getattr(obj,name)
            def call(*args,**kwargs):
                order.append(label); return original(*args,**kwargs)
            self.stack.enter_context(patch.object(obj,name,side_effect=call))
        for obj,name,label in [(self.pm,'collect_only','api'),(official,'build_official_plan','plan'),
                              (official,'collect_supported_official','official'),(official,'preview_official_import','preview'),
                              (official,'apply_official_import','apply'),(self.pm,'run_enrichment','enrich'),
                              (self.pm,'agent_export','freeze')]: wrap(obj,name,label)
        prepared=self.prepare(); queue=json.loads(Path(prepared['queue_path']).read_text())
        self.assertEqual(order,['api','plan','official','preview','apply','enrich','freeze'])
        self.assertEqual(queue['collection_run_id'],prepared['collection_progress']['api']['run_id'])
        with contextlib.closing(connect(self.db_path)) as db:
            new={r[0] for r in db.execute("SELECT identity FROM run_papers WHERE run_id=? AND role='new'",(prepared['run_id'],))}
        self.assertEqual(new,{'doi:10.1287/api','doi:10.1287/mnsc.test10','doi:10.1287/mnsc.test9'})
        self.assertEqual(queue['collection_report']['status'],'succeeded')
        compact=cloud_collection.compact_report(queue['collection_report'])
        self.assertEqual(compact['official']['publisher_deposited_metadata_sources'],1)
        self.assertEqual(compact['official']['official_page_sources'],0)
        original=Path(prepared['queue_path']).read_bytes()
        again=self.prepare(); self.assertTrue(again['resumed'])
        self.assertEqual(Path(again['queue_path']).read_bytes(),original)
        self.assertEqual(order.count('api'),1)
        queue['collection_report']['official']['status']='failed'
        Path(prepared['queue_path']).write_text(json.dumps(queue))
        with self.assertRaisesRegex(ValueError,'changed'): self.prepare()

    def test_crash_after_api_rows_preserves_batch_and_new_identity_ledger(self):
        original=self.pm.collect_into_db
        def crash(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('fixture crash after API rows')
        with patch.object(self.pm,'collect_into_db',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'API rows'): self.prepare()
        self.assert_no_queue(); self.adapter.assert_not_called()
        with contextlib.closing(connect(self.db_path)) as db:
            row=db.execute("SELECT run_id,status,started_at FROM pipeline_runs WHERE kind='collection'").fetchone()
            self.assertEqual(row['status'],'running')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM run_papers WHERE run_id=? AND role='new'",(row['run_id'],)).fetchone()[0],1)
        prepared=self.prepare()
        self.assertEqual(prepared['collection_report']['api']['run_id'],row['run_id'])
        self.assertEqual(prepared['collection_report']['api']['new'],1)
        with contextlib.closing(connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT started_at FROM pipeline_runs WHERE run_id=?',(row['run_id'],)).fetchone()[0],row['started_at'])
        self.assertEqual(self.counts()['papers'],4)

    def test_crash_after_api_terminal_commit_does_not_recollect(self):
        original=self.pm.collect_only
        def crash(*args,**kwargs):
            original(*args,**kwargs); raise RuntimeError('fixture post-commit crash')
        with patch.object(self.pm,'collect_only',side_effect=crash):
            with self.assertRaises(RuntimeError): self.prepare()
        self.assert_no_queue(); self.api.assert_called_once()
        prepared=self.prepare(); self.api.assert_called_once()
        self.assertEqual(prepared['collection_report']['api']['new'],1)

    def test_crash_after_official_commit_receipt_prevents_duplicate_import(self):
        original=official.apply_official_import
        def crash(*args,**kwargs):
            result=original(*args,**kwargs); raise RuntimeError('fixture after official commit')
        with patch.object(official,'apply_official_import',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'official commit'): self.prepare()
        self.assert_no_queue(); before=self.counts()
        self.assertEqual(self.journal()['collection_progress']['official']['sources'][0]['phase'],'validated')
        prepared=self.prepare()
        self.assertEqual({k:v for k,v in self.counts().items() if k!='agent_jobs'}, {k:v for k,v in before.items() if k!='agent_jobs'})
        self.api.assert_called_once(); self.adapter.assert_called_once()
        self.assertEqual(prepared['collection_report']['official']['sources'][0]['imported']['inserted'],2)

    def test_official_apply_failure_rolls_back_and_resumes_saved_evidence(self):
        with contextlib.closing(connect(self.db_path)) as db, db:
            db.execute("CREATE TRIGGER fixture_abort BEFORE INSERT ON papers WHEN NEW.doi='10.1287/mnsc.test9' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'fixture rollback'): self.prepare()
        self.assert_no_queue(); self.assertEqual(self.counts()['papers'],2)
        self.assertEqual(self.counts()['official_issue_checks'],0)
        with contextlib.closing(connect(self.db_path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'cloud_official_import:%'").fetchone()[0],0)
            db.execute('DROP TRIGGER fixture_abort')
        self.prepare(); self.adapter.assert_called_once(); self.api.assert_called_once()
        self.assertEqual(self.counts()['papers'],4)

    def test_invalid_preview_is_failed_without_partial_apply_and_survives_snapshot(self):
        def corrupt(*args):
            result=self.package_source(*args)
            result['issues'][1]['papers'][0]['title']=''
            return result
        self.adapter.side_effect=corrupt
        with patch.object(official,'apply_official_import',wraps=official.apply_official_import) as apply:
            prepared=self.prepare(); apply.assert_not_called()
        self.assertEqual(self.counts()['papers'],2)
        self.assertEqual(prepared['collection_report']['official']['status'],'failed')
        self.assertEqual(prepared['collection_report']['api']['status'],'succeeded')
        self.import_result(prepared)
        with contextlib.closing(connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT status FROM pipeline_runs WHERE run_id=?',(prepared['run_id'],)).fetchone()[0],'partial')
            db.execute('BEGIN'); status=export_view_contexts(db,load_config(self.config),self.state)['status']
        self.assertEqual(status['collection_report']['official']['failed'],1)
        self.assertIn('daily_collection_coverage',{c['name'] for c in status['checks']})
        self.assertNotIn(str(self.state),json.dumps(status['collection_report']))

    def test_failed_required_apis_cannot_become_unqualified_success(self):
        self.api.side_effect=RuntimeError('synthetic API unavailable')
        prepared=self.prepare()
        self.assertEqual(prepared['collection_report']['api']['status'],'failed')
        self.assertEqual(prepared['collection_exit_code'],1)
        self.assertEqual(prepared['collection_report']['official']['status'],'succeeded')
        self.assertEqual(prepared['collection_report']['status'],'partial')
        self.import_result(prepared)
        with contextlib.closing(connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT status FROM pipeline_runs WHERE run_id=?',(prepared['run_id'],)).fetchone()[0],'partial')

    def test_failure_and_unmapped_sources_are_honest_partial_coverage(self):
        self.write_config(extra='[[sources]]\nname="Unknown journal"\ntype="crossref"\nissn="9999-9999"\n')
        self.adapter.side_effect=urllib.error.HTTPError('https://publisher.example',403,'forbidden',{},None)
        prepared=self.prepare(); report=prepared['collection_report']
        self.assertEqual([s['status'] for s in report['official']['sources']],['failed','unsupported'])
        self.assertEqual(report['official']['status'],'partial')
        self.assertEqual(cloud_collection.compact_report(report)['official']['official_page_sources'],0)
        self.assertEqual(self.counts()['papers'],2)

    def test_total_failure_is_not_hidden_by_mixed_official_outcomes(self):
        self.write_config(extra='[[sources]]\nname="Unknown journal"\ntype="crossref"\nissn="9999-9999"\n')
        self.api.side_effect=RuntimeError('synthetic API unavailable')
        self.adapter.side_effect=RuntimeError('synthetic official unavailable')
        prepared=self.prepare()
        self.assertEqual(prepared['collection_report']['official']['status'],'partial')
        self.assertEqual(prepared['collection_report']['status'],'failed')
        self.import_result(prepared)
        with contextlib.closing(connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT status FROM pipeline_runs WHERE run_id=?',(prepared['run_id'],)).fetchone()[0],'failed')

    def test_expired_stage_on_resume_defers_without_new_requests(self):
        with patch.object(official,'collect_supported_official',side_effect=RuntimeError('fixture interrupted')):
            with self.assertRaises(RuntimeError):self.prepare()
        saved=self.journal();saved['collection_progress']['official']['deadline_at']='2000-01-01T00:00:00+00:00'
        runtime._save(self.db_path,'daily-fixture',saved)
        prepared=self.prepare();self.adapter.assert_not_called()
        self.assertEqual(prepared['collection_report']['official']['status'],'deferred')

    def test_crash_after_freeze_does_not_repeat_collection_or_rewrite_queue(self):
        original=self.pm.agent_export
        def crash(*args,**kwargs):
            original(*args,**kwargs);raise RuntimeError('fixture freeze committed')
        with patch.object(self.pm,'agent_export',side_effect=crash):
            with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(self.counts()['agent_jobs'],1)
        queue_path=next((self.state/'agent_queue').glob('*.json'));before=queue_path.read_bytes()
        prepared=self.prepare();self.assertTrue(prepared['resumed'])
        self.assertEqual(queue_path.read_bytes(),before)
        self.assertEqual(prepared['collection_report']['status'],'succeeded')
        self.assertFalse(prepared['collection_performed'])
        self.api.assert_called_once();self.adapter.assert_called_once();self.enrich.assert_called_once()

    def test_retry_after_stages_before_freeze_reports_no_repeat_collection(self):
        with patch.object(self.pm,'agent_export',side_effect=RuntimeError('fixture before freeze')):
            with self.assertRaises(RuntimeError):self.prepare()
        prepared=self.prepare()
        self.assertFalse(prepared['collection_performed'])
        self.api.assert_called_once();self.adapter.assert_called_once();self.enrich.assert_called_once()

    def test_incomplete_issue_package_is_partial_not_latest_two(self):
        def partial(*args):
            result=self.package_source(*args);result['issues'].pop();return result
        self.adapter.side_effect=partial
        report=self.prepare()['collection_report']
        self.assertEqual(report['official']['status'],'partial')
        self.assertEqual(report['official']['sources'][0]['imported']['metadata_refreshed'],0)

    def test_budget_deferred_sources_are_first_on_next_run(self):
        self.write_config(extra='[[sources]]\nname="Ergonomics"\ntype="crossref"\nissn="0014-0139"\n',
                          budget='[official_collection]\nmax_sources=1\n')
        self.adapter.side_effect=RuntimeError('synthetic publisher failure')
        first=self.prepare('first-day');self.import_result(first)
        second=self.prepare('second-day')
        first_sources=first['collection_report']['official']['sources']
        second_sources=second['collection_report']['official']['sources']
        self.assertEqual([(x['source_name'],x['status']) for x in first_sources],
                         [('Management Science','failed'),('Ergonomics','deferred')])
        self.assertEqual([(x['source_name'],x['status']) for x in second_sources],
                         [('Ergonomics','failed'),('Management Science','deferred')])
        self.assertEqual([c.args[0]['name'] for c in self.adapter.call_args_list],['Management Science','Ergonomics'])
        queue=json.loads(Path(second['queue_path']).read_text());self.assertEqual(queue['papers'],[])

    def test_no_collect_is_explicitly_not_exercised(self):
        prepared=runtime.prepare(self.config,'offline',no_collect=True)
        self.api.assert_not_called();self.adapter.assert_not_called();self.enrich.assert_not_called()
        self.assertEqual(prepared['collection_report'],cloud_collection.offline_report())
        self.import_result(prepared)
        with contextlib.closing(connect(self.db_path)) as db:
            self.assertEqual(cloud_collection.latest_report(db)['official']['status'],'not_exercised')

    def test_config_or_mode_changes_and_parallel_operation_fail_closed(self):
        with patch.object(official,'build_official_plan',side_effect=RuntimeError('fixture interrupt')):
            with self.assertRaises(RuntimeError):self.prepare()
        with self.assertRaises(runtime.Busy):self.prepare('different-operation')
        with self.assertRaisesRegex(ValueError,'mode'):runtime.prepare(self.config,'daily-fixture',no_collect=True)
        self.config.write_text(self.config.read_text().replace('0.7','0.8'))
        with self.assertRaisesRegex(ValueError,'configuration changed'):self.prepare()
        self.assert_no_queue()

    def test_checkpoint_restore_collected_before_apply_and_completed_frozen_queue(self):
        with patch.object(official,'preview_official_import',side_effect=RuntimeError('fixture before preview')):
            with self.assertRaises(RuntimeError):self.prepare()
        progress=self.journal()['collection_progress']; self.assertNotIn(str(self.state),json.dumps(progress))
        checkpoint=self.root/'checkpoint'; restored=self.root/'restored'
        create_checkpoint(self.state,checkpoint,config_path=self.config);restore_checkpoint(checkpoint,restored)
        self.state=restored;self.config=restored/'config.toml';self.db_path=restored/'papers.sqlite3'
        prepared=self.prepare();self.api.assert_called_once();self.adapter.assert_called_once()
        self.assertEqual(prepared['collection_report']['status'],'succeeded')
        self.import_result(prepared)
        second=self.root/'checkpoint2';dest=self.root/'restored2'
        create_checkpoint(self.state,second,config_path=self.config);restore_checkpoint(second,dest)
        self.state=dest;self.config=dest/'config.toml';self.db_path=dest/'papers.sqlite3'
        resumed=self.prepare();self.assertTrue(resumed['resumed'])
        self.assertEqual(prepared['queue_sha256'],resumed['queue_sha256'])
        self.api.assert_called_once();self.adapter.assert_called_once()


class OfficialBudgetTests(unittest.TestCase):
    def test_expired_budget_prevents_network_and_worker_fallback(self):
        token=official._FETCH_DEADLINE.set(time.monotonic()-1)
        try:
            with patch('urllib.request.urlopen') as request:
                with self.assertRaises(official.OfficialBudgetExceeded):official._fetch_text('https://example.org')
                request.assert_not_called()
            with self.assertRaises(official.OfficialBudgetExceeded):official._bounded_map(lambda value:value,[1,2],2)
        finally:official._FETCH_DEADLINE.reset(token)

    def test_response_reads_are_bounded_and_deadline_checked_between_chunks(self):
        clock=[0.0]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read1(self,size):
                self.assert_size=size;clock[0]+=2;return b'hello'
        response=Response();token=official._FETCH_DEADLINE.set(3.0)
        try:
            with patch.object(official.time,'monotonic',side_effect=lambda:clock[0]), patch('urllib.request.urlopen',return_value=response) as request:
                with self.assertRaises(official.OfficialBudgetExceeded):official._fetch_text('https://example.org')
                self.assertEqual(request.call_args.kwargs['timeout'],3.0)
                self.assertLessEqual(response.assert_size,65536)
        finally:official._FETCH_DEADLINE.reset(token)

    def test_budget_validation_rejects_unbounded_values(self):
        for value in (0,901,float('inf'),float('nan'),True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                official.official_collection_budget({'official_collection':{'time_budget_seconds':value}})

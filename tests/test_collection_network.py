"""Synthetic collection failures only. An unplanned network call fails the test."""
import contextlib
import copy
import datetime as dt
import email.utils
import io
import json
import socket
import tempfile
import time
import traceback
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from academic_radar import collection_network as policy, enrichment, official
from academic_radar.storage import connect
from test_paper_monitor import pm


class Response:
    def __init__(self, data=b'{"ok": true}', url='https://api.openalex.org/works/test'):
        self.body = io.BytesIO(data)
        self.headers = {'Content-Type': 'application/json'}
        self.url = url
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, *args): return self.body.read(*args)
    def read1(self, size): return self.body.read(size)
    def geturl(self): return self.url


def http_error(status=429, header=None):
    return urllib.error.HTTPError('https://user:secret@api.openalex.org/private?token=secret', status,
                                  'server response secret', {'Retry-After': header} if header is not None else {}, None)


class CollectionNetworkTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network')).start()
        self.addCleanup(patch.stopall)
        self.sleep = patch('time.sleep', side_effect=AssertionError('Unexpected sleep')).start()

    def calls(self, url='https://api.openalex.org/works/test'):
        return [lambda: pm.request_json(url, {}, 10, 2, 0.1),
                lambda: enrichment.MetadataClient({}).request('openalex', url),
                lambda: official._fetch_text(url)]

    def paper(self, name):
        return pm.Paper('doi:10.1/' + name, '10.1/' + name, 'Original research ' + name,
                        '', 'Journal', '2026-10-01', '', [], 'Journal',
                        publication_type_raw='journal-article', publication_type_source='crossref')

    def add_papers(self, root, count=2):
        path = root / 'papers.sqlite3'
        with contextlib.closing(pm.db_open(path)) as db, db:
            for i in range(count): pm.upsert(db, self.paper(str(i)), '2026-10-01')
        return path

    def test_denials_are_terminal_and_sanitized_in_every_client(self):
        errors = [http_error(status, '999') for status in (401, 403, 407)] + [
            urllib.error.URLError(http_error(403)),
            urllib.error.URLError(urllib.error.URLError(http_error(401))),
            urllib.error.URLError('Tunnel connection failed:403 Forbidden private-secret'),
            urllib.error.URLError(OSError('HTTP Error 407: Proxy Authentication Required private-secret')),
            urllib.error.URLError('Network access is denied private-secret'),
            urllib.error.URLError('Blocked by network policy private-secret'),
            urllib.error.URLError(PermissionError('Permission denied private-secret')),
        ]
        for error in errors:
            for call in self.calls():
                with self.subTest(error=type(error).__name__, call=call):
                    self.network.reset_mock(); self.network.side_effect = error
                    with self.assertRaises(policy.NetworkAccessDenied) as raised: call()
                    self.assertEqual(str(raised.exception), str(policy.NetworkAccessDenied()))
                    self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    def test_shared_denial_gate_blocks_later_clients_without_network(self):
        with policy.collection_network_scope():
            self.network.side_effect = urllib.error.URLError('Tunnel connection failed: 403 Forbidden')
            for call in self.calls():
                with self.assertRaises(policy.NetworkAccessDenied): call()
            self.assertEqual(self.network.call_count, 1)
        self.network.side_effect = None; self.network.return_value = Response()
        self.assertTrue(pm.request_json('https://api.openalex.org/new-run', {}, 1, 0, 1)['ok'])
        self.assertEqual(self.network.call_count, 2)

    def test_dns_timeout_and_server_outage_remain_retryable(self):
        errors = [urllib.error.URLError(socket.gaierror('Temporary name resolution failure')),
                  urllib.error.URLError(TimeoutError('timed out')), TimeoutError('timed out'),
                  urllib.error.URLError(http_error(503))]
        self.sleep.side_effect = None
        for error in errors:
            for call in self.calls():
                self.network.reset_mock(); self.sleep.reset_mock()
                self.network.side_effect = [error, Response()]
                call()
                self.assertEqual(self.network.call_count, 2)
                self.assertEqual(self.sleep.call_count, 1)

    def test_long_retry_after_defers_without_sleep_even_with_no_retries(self):
        future = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2), usegmt=True)
        for header in ('7200', future):
            for call in self.calls():
                self.network.reset_mock(); self.network.side_effect = urllib.error.URLError(http_error(429, header))
                with self.assertRaises(policy.ProviderDeferred) as raised: call()
                self.assertGreater(raised.exception.delay, 7100)
                self.assertEqual(self.network.call_count, 1)
            self.network.side_effect = http_error(503, header)
            with self.assertRaises(policy.ProviderDeferred): pm.request_json('https://example.test', {}, 1, 0, 1)
        self.sleep.assert_not_called()

    def test_retry_after_parser_rejects_malformed_and_negative_values(self):
        now = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)
        for value in ('-1', 'NaN', 'inf', '-inf', '1e999', 'nonsense-secret', '1' * 129, ''):
            self.assertIsNone(policy.retry_after_seconds(http_error(429, value), now=now))
        self.assertEqual(policy.retry_after_seconds(http_error(429, '12.5'), now=now), 12.5)
        self.assertEqual(policy.retry_after_seconds(http_error(429, 'Fri, 16 Oct 2026 00:02:00 GMT'), now=now), 120)
        self.assertEqual(policy.retry_after_seconds(http_error(429, 'Thu, 15 Oct 2026 00:00:00 GMT'), now=now), 0)
        self.assertEqual(policy.retry_after_seconds(http_error(429, 'Fri, 16 Oct 2026 00:02:00 -0000'), now=now), 120)

    def test_malformed_headers_use_bounded_backoff_in_all_clients(self):
        self.sleep.side_effect = None
        for value in ('-1', 'NaN', 'inf', 'nonsense-secret'):
            for call in self.calls():
                self.network.reset_mock(); self.sleep.reset_mock()
                self.network.side_effect = [http_error(429, value), Response()]
                call()
                self.assertEqual(self.network.call_count, 2)
                self.assertEqual(self.sleep.call_count, 1)
                self.assertGreater(self.sleep.call_args.args[0], 0)
                self.assertLess(self.sleep.call_args.args[0], 3)

    def test_short_cooldown_is_never_truncated(self):
        self.sleep.side_effect = None
        for call in self.calls():
            self.sleep.reset_mock(); self.network.side_effect = [http_error(429, '7'), Response()]
            call()
            self.sleep.assert_called_once_with(7.0)

    def test_cooldown_exceeding_remaining_budget_defers_without_sleep(self):
        client = enrichment.MetadataClient({}); client.deadline = time.monotonic() + 5
        self.network.side_effect = http_error(429, '8')
        with self.assertRaises(policy.ProviderDeferred): client.request('openalex', 'https://api.openalex.org')
        token = official._FETCH_DEADLINE.set(time.monotonic() + 5)
        try:
            with self.assertRaises(policy.ProviderDeferred): official._fetch_text('https://api.openalex.org')
        finally: official._FETCH_DEADLINE.reset(token)
        self.sleep.assert_not_called()

    def test_official_cooldown_survives_deadline_expiring_during_response(self):
        clock = [100.0]
        def response(*args, **kwargs):
            clock[0] = 106.0
            raise http_error(429, '7')
        with policy.collection_network_scope(), patch('time.monotonic', side_effect=lambda: clock[0]):
            token = official._FETCH_DEADLINE.set(105.0)
            try:
                self.network.side_effect = response
                with self.assertRaises(policy.ProviderDeferred): official._fetch_text('https://api.openalex.org/one')
            finally: official._FETCH_DEADLINE.reset(token)
            token = official._FETCH_DEADLINE.set(200.0)
            try:
                with self.assertRaises(policy.ProviderDeferred): official._fetch_text('https://api.openalex.org/next-source')
            finally: official._FETCH_DEADLINE.reset(token)
            self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    def test_parallel_denial_wins_over_an_earlier_future_cooldown(self):
        def work(value):
            state = policy.current_network_state()
            if value == 0: raise state.defer('https://api.openalex.org', 7200)
            state.reject_denial(http_error(403))
        with self.assertRaises(policy.NetworkAccessDenied): official._bounded_map(work, [0, 1], 1)
        self.network.assert_not_called()

    def test_terminal_denial_traceback_never_echoes_a_prior_worker_error(self):
        def work(value):
            if value == 0: raise urllib.error.HTTPError('https://secret.test', 500, 'SECRET_BODY', {}, None)
            policy.current_network_state().reject_denial(http_error(403))
        with self.assertRaises(policy.NetworkAccessDenied) as raised: official._bounded_map(work, [0, 1], 1)
        formatted = ''.join(traceback.format_exception(type(raised.exception), raised.exception, raised.exception.__traceback__))
        self.assertNotIn('SECRET_BODY', formatted)
        self.assertNotIn('secret.test', formatted)

    def test_existing_payload_error_retry_behavior_is_preserved(self):
        self.sleep.side_effect = None
        self.network.side_effect = [Response(b'bad json'), Response()]
        self.assertTrue(pm.request_json('https://api.openalex.org', {}, 10, 1, 1)['ok'])
        self.assertEqual(self.network.call_count, 2)
        self.network.reset_mock(); self.sleep.reset_mock()
        self.network.side_effect = [Response(b'oversized'), Response(b'ok')]
        client = enrichment.MetadataClient({})
        self.assertEqual(client.request('openalex', 'https://api.openalex.org', max_bytes=2)[0], b'ok')
        self.assertEqual(self.network.call_count, 2)
        self.sleep.assert_called_once()

    def test_first_exhausted_inline_429_cannot_claim_healthy_coverage(self):
        for header in (None, '0'):
            with tempfile.TemporaryDirectory() as td, contextlib.closing(pm.db_open(Path(td)/'db')) as db:
                cfg = {'sources':[{'name':'Journal','type':'crossref','issn':'1234-5678'}]}
                self.network.reset_mock(); self.network.side_effect = http_error(429, header)
                self.sleep.side_effect = None
                with patch.object(pm, 'crossref_collect', return_value=[self.paper('single')]):
                    papers, _, failures = pm.collect_into_db(cfg, db, 'now', 'run')
                self.assertEqual(len(papers), 1)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]['status'], 'degraded')
                self.assertIn('deferred', failures[0]['error'])
                self.assertEqual(self.network.call_count, 2)

    def test_first_exhausted_official_429_defers_the_source(self):
        cfg, plan = self.official_plan()
        items = [{'DOI':'10.1/' + str(i), 'title':['Original paper'], 'volume':'2', 'issue':str(i),
                  'abstract':'Original abstract' if i == 9 else '',
                  'published-print':{'date-parts':[[2026, i, 1]]}} for i in (9, 10)]
        def respond(request, **_):
            if request.full_url.startswith('https://api.crossref.org/'):
                return Response(json.dumps({'message':{'items':items}}).encode())
            raise http_error(429, '0')
        self.network.side_effect = respond; self.sleep.side_effect = None
        with self.assertRaises(policy.ProviderDeferred):
            official._collect_print_metadata_source(cfg['sources'][0], plan['sources'][0], plan['as_of_date'])
        self.assertEqual(self.network.call_count, 4)

    def test_deferral_shared_across_inline_official_workers_and_enrichment_then_reset(self):
        with policy.collection_network_scope():
            self.network.side_effect = http_error(429, '7200')
            with self.assertRaises(policy.ProviderDeferred): pm.openalex_abstract('10.1/one', {})
            with self.assertRaises(policy.ProviderDeferred):
                official._bounded_map(lambda n: official._fetch_json('https://api.openalex.org/' + str(n)), range(20), 6)
            with self.assertRaises(policy.ProviderDeferred):
                enrichment.MetadataClient({}).request('openalex', 'https://api.openalex.org/another')
            self.assertEqual(self.network.call_count, 1)
        self.network.side_effect = None; self.network.return_value = Response()
        pm.request_json('https://api.openalex.org/later-run', {}, 1, 0, 1)
        self.assertEqual(self.network.call_count, 2)

    def test_origin_cooldown_does_not_disable_unrelated_publishers(self):
        client = enrichment.MetadataClient({})
        self.network.side_effect = http_error(429, '7200')
        with self.assertRaises(policy.ProviderDeferred): client.request('publisher', 'https://publisher-one.test/article')
        self.network.side_effect = None; self.network.return_value = Response()
        client.minimum_interval['publisher'] = 0
        client.request('publisher', 'https://publisher-two.test/article')
        with self.assertRaises(policy.ProviderDeferred): client.request('publisher', 'https://publisher-one.test/other')
        self.assertEqual(self.network.call_count, 2)

    def test_redirect_cooldown_gates_final_origin_and_conservatively_the_resolver(self):
        client = enrichment.MetadataClient({})
        self.network.side_effect = urllib.error.HTTPError(
            'https://publisher-one.test/article', 429, 'limited', {'Retry-After': '7200'}, None)
        with self.assertRaises(policy.ProviderDeferred): client.request('publisher', 'https://doi.org/10.1/one')
        for url in ('https://publisher-one.test/second', 'https://doi.org/10.2/two', 'https://dx.doi.org/10.3/three'):
            with self.assertRaises(policy.ProviderDeferred): client.request('publisher', url)
        self.assertEqual(self.network.call_count, 1)
        client.minimum_interval['publisher'] = 0
        self.network.side_effect = None; self.network.return_value = Response()
        client.request('publisher', 'https://publisher-two.test/direct')
        self.assertEqual(self.network.call_count, 2)

    def test_direct_publisher_cooldown_defers_unknown_resolver_targets_only(self):
        client = enrichment.MetadataClient({})
        self.network.side_effect = urllib.error.HTTPError('https://publisher-one.test/one', 429, 'limited', {'Retry-After':'7200'}, None)
        with self.assertRaises(policy.ProviderDeferred): client.request('publisher', 'https://publisher-one.test/one')
        for scheme in ('http', 'https'):
            for host in ('doi.org', 'dx.doi.org', 'www.doi.org'):
                with self.assertRaises(policy.ProviderDeferred) as raised: client.request('publisher', scheme + '://' + host + '/10.2/unknown')
                self.assertTrue(raised.exception.conservative_resolver)
                self.assertIn('unresolved DOI resolver', str(raised.exception))
        self.assertEqual(self.network.call_count, 1)
        self.network.side_effect = None; self.network.return_value = Response()
        client.minimum_interval['publisher'] = 0
        client.request('publisher', 'https://publisher-two.test/one')
        self.assertEqual(self.network.call_count, 2)
        # An unrelated later client has no retained origin or resolver gate.
        enrichment.MetadataClient({}).request('publisher', 'https://doi.org/10.2/new-run')
        self.assertEqual(self.network.call_count, 3)

    def test_api_cooldown_does_not_set_a_publisher_resolver_gate(self):
        with policy.collection_network_scope() as state:
            self.network.side_effect = http_error(429, '7200')
            with self.assertRaises(policy.ProviderDeferred): pm.request_json('https://api.openalex.org/works', {}, 1, 1, 1)
            self.assertFalse(state.resolver_origins)
            self.network.side_effect = None; self.network.return_value = Response()
            enrichment.MetadataClient({}).request('publisher', 'https://doi.org/10.2/available')
            self.assertEqual(self.network.call_count, 2)

    @staticmethod
    def legacy_http_error(url):
        # Reproduce Python 3.9 HTTPError(fp=None): addinfourl/tempfile fields
        # were never initialized, but filename/code/headers are available.
        error = urllib.error.HTTPError.__new__(urllib.error.HTTPError)
        error.code = 429
        error.msg = "limited"
        error.hdrs = {"Retry-After": "7200"}
        error.fp = None
        error.filename = url
        return error

    def test_missing_response_file_uses_constructor_url_without_geturl(self):
        error = self.legacy_http_error('https://publisher-one.test/final')
        with self.assertRaises(KeyError): error.geturl()
        client = enrichment.MetadataClient({})
        self.network.side_effect = urllib.error.URLError(error)
        with self.assertRaises(policy.ProviderDeferred): client.request('publisher', 'https://doi.org/10.1/one')
        for url in ('https://publisher-one.test/another', 'https://doi.org/10.2/two'):
            with self.assertRaises(policy.ProviderDeferred): client.request('publisher', url)
        self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    def test_missing_final_url_metadata_still_gates_requested_origin(self):
        error = self.legacy_http_error(None)
        with self.assertRaises(KeyError): error.geturl()
        with policy.collection_network_scope():
            self.network.side_effect = error
            with self.assertRaises(policy.ProviderDeferred): pm.request_json('https://api.openalex.org/one', {}, 10, 1, 1)
            with self.assertRaises(policy.ProviderDeferred): official._fetch_text('https://api.openalex.org/two')
        self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    def test_stored_final_url_metadata_does_not_call_broken_geturl(self):
        error = http_error(429, '7200')
        error.url = 'https://publisher-final.test/article'
        client = enrichment.MetadataClient({})
        self.network.side_effect = error
        with patch.object(error, 'geturl', side_effect=KeyError('file')) as geturl:
            with self.assertRaises(policy.ProviderDeferred): client.request('publisher', 'https://doi.org/10.1/one')
            with self.assertRaises(policy.ProviderDeferred): client.request('publisher', 'https://publisher-final.test/another')
            geturl.assert_not_called()
        self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    def test_api_denial_does_not_fall_back_or_continue_sources(self):
        for first_type in ('crossref', 'openalex'):
            with tempfile.TemporaryDirectory() as td, contextlib.closing(pm.db_open(Path(td) / 'db')) as db:
                cfg = {'sources': [{'name': 'First', 'type': first_type, 'issn': '1234-5678', 'openalex_id': 'S1'},
                                   {'name': 'Later', 'type': 'openalex', 'openalex_id': 'S2'}]}
                before = copy.deepcopy(cfg)
                self.network.reset_mock(); self.network.side_effect = urllib.error.URLError('Tunnel connection failed:403 Forbidden')
                with self.assertRaises(policy.NetworkAccessDenied): pm.collect_into_db(cfg, db, 'now', 'run')
                self.assertEqual(self.network.call_count, 1)
                self.assertEqual(cfg, before)

    def test_inline_abstract_denial_is_not_swallowed(self):
        self.network.side_effect = http_error(403)
        with self.assertRaises(policy.NetworkAccessDenied): pm.enrich_missing_abstracts([self.paper('one'), self.paper('two')], {})
        self.assertEqual(self.network.call_count, 1)

    def test_multiple_source_429_records_partial_coverage_without_repeat(self):
        with tempfile.TemporaryDirectory() as td, contextlib.closing(pm.db_open(Path(td) / 'db')) as db:
            cfg = {'sources': [{'name': name, 'type': 'openalex', 'openalex_id': name} for name in ('S1', 'S2')]}
            before = copy.deepcopy(cfg); self.network.side_effect = http_error(429, '7200')
            papers, new, failures = pm.collect_into_db(cfg, db, 'now', 'run')
            self.assertFalse(papers); self.assertFalse(new); self.assertEqual(len(failures), 2)
            self.assertEqual(self.network.call_count, 1); self.assertEqual(cfg, before)
            self.assertTrue(all(item['status'] == 'failed' and 'deferred' in item['error'] for item in failures))
            self.assertTrue(all(row[0] is None for row in db.execute('SELECT last_success_at FROM source_health')))

    def test_enrichment_denial_stops_batch_priming_and_per_paper_fallback(self):
        for providers in (None, [('openalex', enrichment.lookup_openalex), ('crossref', enrichment.lookup_crossref)]):
            with tempfile.TemporaryDirectory() as td:
                path = self.add_papers(Path(td)); self.network.reset_mock(); self.network.side_effect = http_error(403)
                context = patch.object(enrichment, 'PROVIDERS', providers) if providers else contextlib.nullcontext()
                with context, self.assertRaises(policy.NetworkAccessDenied): enrichment.enrich_abstracts(path, {})
                self.assertEqual(self.network.call_count, 1)
                with contextlib.closing(connect(path)) as db:
                    self.assertEqual(db.execute('SELECT status FROM task_runs').fetchone()[0], 'failed')

    def test_enrichment_deferred_requests_report_partial_not_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.add_papers(Path(td)); self.network.side_effect = http_error(429, '7200')
            with patch.object(enrichment, 'PROVIDERS', [('openalex', enrichment.lookup_openalex)]):
                result = enrichment.enrich_abstracts(path, {})
            self.assertEqual(self.network.call_count, 1)
            self.assertEqual(result['status'], 'partial'); self.assertEqual(result['deferred_providers'], 1)
            with contextlib.closing(connect(path)) as db:
                self.assertEqual([row[0] for row in db.execute('SELECT status FROM abstract_attempts')], ['skipped', 'skipped'])

    def official_plan(self):
        cfg = {'sources': [{'name': name, 'type': 'crossref', 'issn': '0025-1909'} for name in ('First', 'Second')]}
        plan = {'as_of_date': '2026-10-16', 'sources': [
            {'source_name': name, 'provider': 'INFORMS PubsOnline', 'issues_url': 'https://pubsonline.informs.org/loi/mnsc'}
            for name in ('First', 'Second')]}
        return cfg, plan

    def test_official_sources_propagate_denial_and_share_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, plan = self.official_plan(); root = Path(td)
            self.network.side_effect = http_error(403)
            with self.assertRaises(policy.NetworkAccessDenied):
                official.collect_supported_official(root/'unused-db', cfg, root/'denied.json', plan=plan)
            self.assertEqual(self.network.call_count, 1); self.assertFalse((root/'denied.json').exists())
            self.network.reset_mock(); self.network.side_effect = http_error(429, '7200')
            result = official.collect_supported_official(root/'unused-db', cfg, root/'cooldown.json', plan=plan)
            self.assertEqual(self.network.call_count, 1)
            self.assertEqual(result['sources'], 0); self.assertEqual(len(result['deferred']), 2)

    def test_official_abstract_fallback_propagates_denial_and_deferral(self):
        cfg, plan = self.official_plan()
        items = [{'DOI': '10.1/' + str(i), 'title': ['Original paper'], 'volume': '2', 'issue': str(i),
                  'published-print': {'date-parts': [[2026, i, 1]]}} for i in (9, 10)]
        for status in (403, 429):
            self.network.reset_mock()
            def respond(request, **_):
                if request.full_url.startswith('https://api.crossref.org/'):
                    return Response(json.dumps({'message': {'items': items}}).encode())
                if request.full_url.startswith('https://api.openalex.org/'):
                    raise http_error(status, '7200')
                self.fail('Unexpected provider')
            self.network.side_effect = respond
            expected = policy.NetworkAccessDenied if status == 403 else policy.ProviderDeferred
            with self.assertRaises(expected):
                official._collect_print_metadata_source(cfg['sources'][0], plan['sources'][0], plan['as_of_date'])
            self.assertEqual(self.network.call_count, 2)


if __name__ == '__main__': unittest.main()

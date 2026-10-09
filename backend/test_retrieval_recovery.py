"""Bounded recovery from provider outages, including concurrent cooldowns."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.services.search import SearchService
from src.config import Settings
from src.services.source_details import source_details
from src.tools.search.academic import ArxivSearch, SemanticScholarSearch
from src.tools.search.http import ProviderHTTP, SearchError


def row(url, title='robot planning paper'):
    return {'url': url, 'title': title, 'snippet': 'robot planning experiments'}


class ProviderRecoveryTests(unittest.TestCase):
    def test_default_search_never_initializes_semantic_scholar_or_expands_citations(self):
        config = Settings(_env_file=None, search_engines='duckduckgo')
        with patch('src.services.search.ENGINES', {
            'duckduckgo': Mock(return_value=SimpleNamespace(name='duckduckgo')),
            'arxiv': Mock(return_value=SimpleNamespace(name='arxiv')),
            'openalex': Mock(return_value=SimpleNamespace(name='openalex')),
            'semantic_scholar': Mock(side_effect=AssertionError('disabled provider')),
        }):
            service = SearchService(config)
        self.assertEqual([e.name for e in service.academic_engines], ['arxiv', 'openalex'])
        self.assertFalse(config.search_citation_expansion)
        rows = [{'semantic_scholar_id': 'legacy-id'}]
        self.assertEqual(service._expand(rows, 'robot paper'), rows)

    def test_arxiv_has_separate_connection_and_read_timeouts(self):
        response = Mock(status_code=200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
        with patch('src.tools.search.http.requests.request', return_value=response) as request:
            self.assertEqual(ArxivSearch(read_timeout=35).search('robot planning'), [])
        self.assertEqual(request.call_args.kwargs['timeout'], (8, 35))

    def test_unauthenticated_semantic_scholar_uses_slower_pacing(self):
        self.assertGreater(SemanticScholarSearch().http.interval, SemanticScholarSearch('test-key').http.interval)

    def test_http_date_retry_after_enters_cooldown_without_early_retry(self):
        response = Mock(status_code=429, headers={'Retry-After': format_datetime(datetime.now(timezone.utc) + timedelta(minutes=2))})
        client = ProviderHTTP('test', interval=0)
        with patch('src.tools.search.http.requests.request', return_value=response) as request:
            for _ in range(2):
                with self.assertRaisesRegex(SearchError, 'rate_limited'):
                    client.request('GET', 'https://example.org')
        self.assertEqual(request.call_count, 1)
        response.close.assert_called_once()

    def test_missing_retry_after_uses_backoff_before_successful_retry(self):
        busy = Mock(status_code=429, headers={})
        ok = Mock(status_code=200)
        with patch('src.tools.search.http.requests.request', side_effect=[busy, ok]) as request, \
                patch('src.tools.search.http.time.sleep') as sleep, \
                patch('src.tools.search.http.time.monotonic', return_value=100):
            result = ProviderHTTP('test', interval=0).request('GET', 'https://example.org')
        self.assertIs(result, ok)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(sleep.call_args.args[0], 5)

    def test_concurrent_query_cannot_slip_between_429_and_cooldown(self):
        closing, release, second_started = threading.Event(), threading.Event(), threading.Event()
        response = Mock(status_code=429, headers={'Retry-After': '120'})
        def close():
            closing.set()
            release.wait(3)
        response.close.side_effect = close
        client = ProviderHTTP('test', interval=0)
        def query(second=False):
            if second:
                second_started.set()
            try:
                client.request('GET', 'https://example.org')
            except SearchError as error:
                return error.category
        with patch('src.tools.search.http.requests.request', return_value=response) as request:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(query)
                try:
                    self.assertTrue(closing.wait(2))
                    second = pool.submit(query, True)
                    self.assertTrue(second_started.wait(2))
                finally:
                    release.set()
                self.assertEqual(first.result(2), 'rate_limited')
                self.assertEqual(second.result(2), 'rate_limited')
            self.assertEqual(request.call_count, 1)


class DiscoveryFallbackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.service = SearchService.__new__(SearchService)
        self.service.settings = SimpleNamespace(search_profile='academic')
        self.service.cache_dir = Path(directory.name)
        self.service.engines = [SimpleNamespace(name='web1', search=Mock()), SimpleNamespace(name='web2', search=Mock())]
        self.service.academic_engines = [
            SimpleNamespace(name=name, search=Mock(side_effect=SearchError(name, category)))
            for name, category in [('arxiv', 'timeout'), ('semantic_scholar', 'rate_limited')]]

    def test_site_fallback_recovers_both_channels_without_masking_original_failure(self):
        paper = row('https://arxiv.org/abs/2401.00001')
        index = row('https://www.semanticscholar.org/paper/robot/abc')
        self.service.engines[0].search.side_effect = lambda q, n: [paper, index] if 'site:' in q else []
        self.service.engines[1].search.return_value = []
        results = self.service.search('robot planning')
        warnings = results[0]['retrieval_warnings']
        failed = [w for w in warnings if w['provider'] in {'arxiv', 'semantic_scholar'}]
        self.assertEqual(len(failed), 2)
        self.assertTrue(all(w['fallback_provider'] == 'web1' for w in failed))
        self.assertEqual({w['category'] for w in failed}, {'timeout', 'rate_limited'})
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r['source_engines'] == ['web1'] for r in results))
        self.assertEqual(self.service.engines[0].search.call_count, 2)
        self.assertEqual(self.service.engines[1].search.call_count, 1)

    def test_missing_site_stays_unresolved_and_second_web_provider_is_tried(self):
        self.service.engines[0].search.return_value = [row('https://arxiv.org/abs/2401.00001')]
        self.service.engines[1].search.return_value = []
        results = self.service.search('robot planning')
        warnings = {w['provider']: w for w in results[0]['retrieval_warnings']}
        self.assertIn('fallback_provider', warnings['arxiv'])
        self.assertNotIn('fallback_provider', warnings['semantic_scholar'])
        self.assertEqual(self.service.engines[1].search.call_count, 2)
        fallback_query = self.service.engines[1].search.call_args.args[0]
        self.assertIn('site:semanticscholar.org', fallback_query)
        self.assertNotIn('site:arxiv.org', fallback_query)

    def test_mirrors_host_spoofs_and_search_pages_cannot_claim_recovery(self):
        self.service.engines[0].search.return_value = [
            row('https://blog.example.org/paper/robot'), row('https://arxiv.org.evil.example/abs/2401.00001'),
            row('https://arxiv.org/search/?query=robot')]
        self.service.engines[1].search.return_value = []
        results = self.service.search('robot planning')
        self.assertTrue(all('fallback_provider' not in w for w in results[0]['retrieval_warnings']))

    def test_fallback_has_two_request_ceiling_and_can_be_disabled(self):
        self.service.engines[0].search.side_effect = RuntimeError('private failure')
        self.service.engines[1].search.side_effect = RuntimeError('private failure')
        warnings = [{'provider': 'arxiv', 'category': 'timeout'}]
        self.assertEqual(self.service._academic_fallback('robot', 5, warnings), [])
        self.assertTrue(all(e.search.call_count == 1 for e in self.service.engines))
        self.service.settings.academic_web_fallback = False
        self.assertEqual(self.service._academic_fallback('robot', 5, warnings), [])
        self.assertTrue(all(e.search.call_count == 1 for e in self.service.engines))

    def test_diagnostics_distinguish_real_fallback_and_unresolved_error(self):
        item = row('https://arxiv.org/abs/2401.00001')
        item['retrieval_warnings'] = [
            {'provider': 'arxiv', 'category': 'timeout', 'fallback_provider': 'tavily', 'fallback_results': 2},
            {'provider': 'semantic_scholar', 'category': 'rate_limited'},
        ]
        source = source_details({1: {'results': [item]}})['sources'][0]
        self.assertEqual(source['warnings'], ['semantic_scholar: rate_limited'])
        self.assertEqual(source['fallbacks'], [{'provider': 'arxiv', 'category': 'timeout', 'via': 'tavily', 'count': 2}])


if __name__ == '__main__':
    unittest.main()

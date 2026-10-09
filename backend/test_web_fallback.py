"""Offline public-web failover tests: no external searches or model calls."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from ddgs.exceptions import DDGSException, TimeoutException, RatelimitException

from src.services.search import SearchService
from src.tools.search.duckduckgo import DuckDuckGoSearch, YahooSearch
from src.tools.search.http import SearchError, SearchUnavailable


class WebBackendTests(unittest.TestCase):
    def test_explicit_backend_and_chinese_region(self):
        with patch('src.tools.search.duckduckgo.DDGS') as factory:
            client = factory.return_value.__enter__.return_value
            client.text.return_value = [{'title': 'Fact', 'href': 'https://example.org', 'body': 'Evidence'}]
            result = YahooSearch(timeout=7).search('产业竞争', 3)
        factory.assert_called_once_with(timeout=7)
        client.text.assert_called_once_with('产业竞争', backend='yahoo', max_results=3, region='cn-zh')
        self.assertEqual(result[0]['url'], 'https://example.org')

    def test_missing_backend_never_silently_uses_auto(self):
        with patch('src.tools.search.duckduckgo.DDGS_ENGINES', {'text': {}}), \
             patch('src.tools.search.duckduckgo.DDGS') as factory:
            with self.assertRaisesRegex(SearchError, 'backend_unavailable'):
                YahooSearch().search('query')
            factory.assert_not_called()

    def test_empty_result_does_not_retry(self):
        with patch('src.tools.search.duckduckgo.DDGS') as factory:
            client = factory.return_value.__enter__.return_value
            client.text.side_effect = DDGSException('No results found.')
            self.assertEqual(DuckDuckGoSearch().search('query'), [])
            client.text.assert_called_once()

    def test_timeout_retries_once_then_cools_down(self):
        with patch('src.tools.search.duckduckgo.DDGS') as factory, patch('src.tools.search.duckduckgo.time.sleep'):
            client = factory.return_value.__enter__.return_value
            client.text.side_effect = TimeoutException('private URL with credentials')
            engine = DuckDuckGoSearch()
            for _ in range(2):
                with self.assertRaisesRegex(SearchError, '^duckduckgo: timeout$'):
                    engine.search('query')
            self.assertEqual(client.text.call_count, 2)

    def test_rate_limit_does_not_retry_and_error_is_sanitized(self):
        with patch('src.tools.search.duckduckgo.DDGS') as factory:
            client = factory.return_value.__enter__.return_value
            client.text.side_effect = RatelimitException('SECRET https://private')
            engine = YahooSearch()
            for _ in range(2):
                with self.assertRaisesRegex(SearchError, '^yahoo: rate_limited$'):
                    engine.search('query')
            client.text.assert_called_once()


class WebFailoverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.s = SearchService.__new__(SearchService)
        self.s.settings = SimpleNamespace(search_profile='web', search_fallback_max_providers=3)
        self.s.cache_dir = Path(temp.name)
        self.primary = SimpleNamespace(name='duckduckgo', search=Mock(side_effect=SearchError('duckduckgo', 'timeout')))
        self.yahoo = SimpleNamespace(name='yahoo', search=Mock(return_value=[{
            'title': 'Fact', 'url': 'https://example.org', 'snippet': 'Evidence'}]))
        self.last = SimpleNamespace(name='google', search=Mock())
        self.s.engines = [self.primary]
        self.s.fallback_engines = [self.yahoo, self.last]

    def test_switches_then_stops_and_preserves_provenance(self):
        for _ in range(2):
            row = self.s.search('query')[0]
            self.assertEqual(row['source_engine'], 'yahoo')
            self.assertEqual(row['retrieval_warnings'][0]['category'], 'timeout')
            self.assertEqual(row['retrieval_warnings'][0]['fallback_provider'], 'yahoo')
        self.yahoo.search.assert_called_once()
        self.last.search.assert_not_called()

    def test_primary_success_never_contacts_backups(self):
        self.primary.search.side_effect = None
        self.primary.search.return_value = self.yahoo.search.return_value
        self.s.search('query')
        self.yahoo.search.assert_not_called()
        self.last.search.assert_not_called()

    def test_empty_primary_also_uses_backup(self):
        self.primary.search.side_effect = None
        self.primary.search.return_value = []
        row = self.s.search('query')[0]
        self.assertEqual(row['retrieval_warnings'][0]['category'], 'no_results')
        self.yahoo.search.assert_called_once()

    def test_all_query_failures_keep_provider_categories_and_no_secret(self):
        self.yahoo.search.return_value = []
        self.last.search.side_effect = RuntimeError('SECRET key and private URL')
        with self.assertRaises(SearchUnavailable) as caught:
            self.s.search_many(['one', 'two'])
        message = str(caught.exception)
        for text in ('duckduckgo=timeout', 'yahoo=no_results', 'google=provider_error'):
            self.assertIn(text, message)
        self.assertNotIn('SECRET', message)
        self.assertEqual(len(caught.exception.warnings), 6)

    def test_provider_limit_and_duplicate_primary_are_respected(self):
        self.s.settings.search_fallback_max_providers = 1
        self.s.fallback_engines = [self.primary, self.yahoo, self.last]
        self.yahoo.search.return_value = []
        with self.assertRaises(SearchUnavailable):
            self.s.search('query')
        self.primary.search.assert_called_once()
        self.yahoo.search.assert_called_once()
        self.last.search.assert_not_called()

    def test_discovery_variant_retains_scope_dates_and_exclusions(self):
        query = 'electric vehicle battery industry comparison costs 2026 site:example.org -advertising "solid state"'
        variant = self.s.discovery_query(query)
        for protected in ('2026', 'site:example.org', '-advertising', '"solid state"'):
            self.assertIn(protected, variant)
        self.assertLess(len(variant), len(query))
        self.assertIsNone(self.s.discovery_query('short query'))
        self.assertIsNone(self.s.discovery_query('one two three OR four five six'))

    def test_one_variant_recovers_empty_queries_without_model(self):
        original = 'one two three four five six seven'
        self.primary.search.side_effect = lambda q, n: [] if q == original else self.yahoo.search.return_value
        self.yahoo.search.side_effect = lambda q, n: []
        self.last.search.return_value = []
        rows = self.s.search(original)
        self.assertEqual(rows[0]['matched_queries'], ['one two three four'])
        self.assertTrue(rows[0]['query_relaxed'])
        self.assertEqual(self.primary.search.call_count, 2)
        self.assertEqual(self.yahoo.search.call_count, 1)

    def test_variant_never_retries_rate_limited_channel(self):
        self.primary.search.side_effect = SearchError('duckduckgo', 'rate_limited')
        self.yahoo.search.return_value = []
        self.last.search.return_value = []
        with self.assertRaises(SearchUnavailable):
            self.s.search('one two three four five six seven')
        self.primary.search.assert_called_once()
        self.assertEqual(self.yahoo.search.call_count, 2)
        self.assertEqual(self.last.search.call_count, 2)

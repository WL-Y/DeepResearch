"""Offline regression suite: no API keys, network traffic or model charges."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import requests

from src.config import Settings
from src.services.retrieval_cache import RetrievalCache
from src.services.retrieval_ranking import research_profile, semantic_rerank, lexical_score
from src.services.source_identity import merge_records, normalize_doi, normalize_url
from src.services.source_reader import SourceReader
from src.services.search import SearchService
from src.tools.search.academic import ArxivSearch, SemanticScholarSearch, OpenAlexSearch, CrossrefMetadata
from src.tools.search.http import ProviderHTTP, SearchError
from src.tools.search.tavily import TavilySearch
from src.services.source_details import source_details
from src.services.reporting import ReportingService
from src.models import TaskSummary, TodoItem


def record(n, **kwargs):
    return {'url': f'https://example.org/{n}', 'title': f'robot paper {n}', 'snippet': 'robot experiment', **kwargs}


def service():
    s = SearchService.__new__(SearchService)
    s.settings = SimpleNamespace(search_read_budget=2, search_read_attempts=6)
    s.source_reader = Mock()
    s.source_reader.read_document.side_effect = lambda url, q: {'content': 'Read ' + url, 'content_status': '正文节选'}
    return s


class RetrievalTests(unittest.TestCase):
    def test_previously_read_sources_do_not_consume_new_read_budget(self):
        s = service()
        old = [record(i, content='old', title='robot experiment robot paper') for i in range(5)]
        s.search = Mock(return_value=[record(6), record(7)])
        result = s.search_many(['robot'], existing_results=old)
        read = {c.args[0] for c in s.source_reader.read_document.call_args_list}
        self.assertEqual(read, {'https://example.org/6', 'https://example.org/7'})
        self.assertEqual(sum(bool(r.get('content')) for r in result), 7)

    def test_failed_reads_are_replaced_with_later_candidates(self):
        s = service()
        s.search = Mock(return_value=[record(i) for i in range(6)])
        s.source_reader.read_document.side_effect = lambda u, q: {'content': 'text' if int(u.rsplit('/', 1)[-1]) >= 3 else ''}
        result = s.search_many(['robot'], max_total=2)
        self.assertEqual([r['url'] for r in result], ['https://example.org/3', 'https://example.org/4'])
        self.assertEqual(s.source_reader.read_document.call_count, 5)

    def test_read_failures_have_hard_attempt_budget(self):
        s = service()
        s.search = Mock(return_value=[record(i) for i in range(20)])
        s.source_reader.read_document.side_effect = RuntimeError('unreachable')
        s.search_many(['robot'])
        self.assertEqual(s.source_reader.read_document.call_count, 6)

    def test_new_candidates_precede_retrying_old_failed_reads(self):
        s = service()
        s.search = Mock(return_value=[record(8)])
        old = [record(i, read_attempted=True, title='robot robot experiment') for i in range(5)]
        s.search_many(['robot'], existing_results=old)
        self.assertIn('https://example.org/8', [c.args[0] for c in s.source_reader.read_document.call_args_list])

    def test_failed_direct_read_uses_bounded_extraction_fallback(self):
        s = service()
        extractor = SimpleNamespace(name='tavily', extract=Mock(return_value=[
            {'url': 'https://example.org/1', 'raw_content': 'complete cleaned body ' * 30}
        ]))
        s.engines = [extractor]
        s.source_reader.read_cached_extraction.return_value = None
        s.source_reader.read_extracted_text.return_value = {
            'content': 'traceable chunks', 'acquisition_status': 'service_extracted_text'}
        item = record(1, read_attempted=True)
        s._extract_fallback([item], 'robot')
        extractor.extract.assert_called_once_with(['https://example.org/1'])
        self.assertEqual(item['content'], 'traceable chunks')
        self.assertEqual(item['read_attempts'][-1]['via'], 'tavily_extract')

    def test_fulltext_url_precedes_metadata_landing_page(self):
        s = service()
        doc = s._read_candidate(record(1, fulltext_urls=['https://public.org/paper.pdf']), 'robot')
        self.assertEqual(doc['fetched_url'], 'https://public.org/paper.pdf')
        s.source_reader.read_document.assert_called_once()

    def test_scholarly_index_page_not_claimed_as_body(self):
        s = service()
        doc = s._read_candidate(record(1, url='https://openalex.org/W123'), 'robot')
        self.assertFalse(doc['content'])
        s.source_reader.read_document.assert_not_called()

    def test_provider_failure_is_not_hidden_by_success_or_cached(self):
        s = service()
        bad = SimpleNamespace(name='broken', search=Mock(side_effect=SearchError('broken', 'rate_limited')))
        good = SimpleNamespace(name='working', search=Mock(return_value=[record(1)]))
        with tempfile.TemporaryDirectory() as tmp:
            s.cache_dir = Path(tmp)
            s.engines = [bad, good]
            for _ in range(2):
                result = s.search('robot')
                self.assertEqual(result[0]['retrieval_warnings'][0]['category'], 'rate_limited')
            self.assertEqual(good.search.call_count, 1)
            self.assertEqual(bad.search.call_count, 2)

    def test_routing_preserves_web_and_only_adds_academic_when_relevant(self):
        s = service()
        s.engines = [SimpleNamespace(name='web')]
        s.academic_engines = [SimpleNamespace(name='arxiv')]
        self.assertEqual(len(s._engines_for('世界模型论文')), 2)
        self.assertEqual(len(s._engines_for('新能源汽车市场营收')), 1)
        s.settings.search_profile = 'web'
        self.assertEqual(len(s._engines_for('世界模型论文')), 1)
        self.assertEqual(research_profile('Kubernetes deployment documentation'), 'technical')

    def test_missing_tavily_key_does_not_break_duckduckgo(self):
        settings = Settings(_env_file=None, search_engines='duckduckgo,tavily', tavily_api_key='', academic_search_engines='')
        s = SearchService(settings)
        self.assertEqual([e.name for e in s.engines], ['duckduckgo'])

    def test_semantic_rerank_cannot_invent_or_drop_candidates(self):
        items = [record(1), record(2)]
        self.assertEqual(semantic_rerank(Mock(run=Mock(return_value='{"ranking":[1,0]}')), 'robot', items), list(reversed(items)))
        for ranking in [[0, 0], [0, 4], [False, 1], [1]]:
            with self.assertRaises(ValueError):
                semantic_rerank(Mock(run=Mock(return_value=json.dumps({'ranking': ranking}))), 'robot', items)

    def test_semantic_failure_falls_back_and_retains_warning(self):
        s = service()
        s.search = Mock(return_value=[record(1), record(2)])
        s.reranker = Mock(run=Mock(side_effect=RuntimeError('model offline')))
        result = s.search_many(['robot'])
        self.assertEqual(len(result), 2)
        self.assertIn({'provider': 'reranker', 'category': 'fallback_to_lexical'}, result[0]['retrieval_warnings'])

    def test_chinese_overlap_uses_word_fragments(self):
        self.assertGreater(lexical_score(record(1, title='世界模型在机器人决策中的应用'), '机器人世界模型路线'), 0)

    def test_expansion_is_bounded_cached_and_not_recursive(self):
        s = service()
        seed = record(1, semantic_scholar_id='p1')
        s.academic_engines = [SimpleNamespace(name='semantic_scholar', expand=Mock(return_value=[record(2, discovery_relation=[{'seed_url': seed['url']}])]))]
        with tempfile.TemporaryDirectory() as tmp:
            s.cache_dir = Path(tmp)
            expanded = s._expand([seed], 'robot paper')
            self.assertEqual(len(expanded), 2)
            s._expand(expanded, 'robot paper')
            s.academic_engines[0].expand.assert_called_once()


class IdentityTests(unittest.TestCase):
    def test_report_deduplicates_doi_across_tasks(self):
        summaries = [TaskSummary(task=TodoItem(id=i, title='robot', intent='', query='robot'),
                                summary='事实[1]。', sources=[url], source_titles=['Robot'],
                                source_details=[{'doi': '10.1234/r'}])
                     for i, url in enumerate(['https://doi.org/10.1234/r', 'https://publisher.org/paper'], 1)]
        reporter = ReportingService.__new__(ReportingService)
        output = reporter._renumber_citations(summaries)
        self.assertEqual(output[0][1], output[1][1])

    def test_merging_does_not_mutate_checkpoint_input(self):
        item = record(1, source_engines=['tavily'], versions=[{'url': 'https://example.org/1'}])
        before = json.dumps(item)
        merge_records([item, record(1, source_engines=['arxiv'])])
        self.assertEqual(json.dumps(item), before)

    def test_source_details_excludes_raw_text_and_handles_legacy(self):
        self.assertEqual(source_details({})['total'], 0)
        result = source_details({1: {'results': [record(1, content='private passage', snippet='internal snippet')]}})
        self.assertEqual(result['read_count'], 1)
        self.assertNotIn('content', result['sources'][0])
        self.assertNotIn('snippet', result['sources'][0])

    def test_source_details_separates_acquisition_from_prompt_selection(self):
        result = source_details({1: {'results': [record(1, content='selected',
            acquisition_status='full_text_extracted', context_status='selected_passages_used',
            extracted_chars=90000, context_chars=6000,
            evidence_chunks=[{'chunk_id': 'c0001', 'text': 'selected'}])]}})
        self.assertEqual(result['full_text_count'], 1)
        self.assertEqual(result['partial_text_count'], 0)
        self.assertEqual(result['sources'][0]['evidence_chunk_count'], 1)
        self.assertNotIn('evidence_chunks', result['sources'][0])

    def test_bridge_doi_arxiv_merge_preserves_provenance_and_content(self):
        records = [record(1, url='https://doi.org/10.1234/ABC', source_engine='tavily'),
                   record(2, url='https://arxiv.org/abs/2301.12345v2', content='version 2', fetched_url='https://arxiv.org/html/2301.12345v2', source_engine='arxiv'),
                   record(3, doi='10.1234/abc', arxiv_id='2301.12345', source_engine='semantic_scholar')]
        merged = merge_records(records)
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0]['source_engines']), {'tavily', 'arxiv', 'semantic_scholar'})
        self.assertEqual(len(merged[0]['versions']), 3)
        self.assertEqual(merged[0]['content'], 'version 2')
        self.assertIn('v2', merged[0]['fetched_url'])

    def test_same_title_without_authors_does_not_merge(self):
        self.assertEqual(len(merge_records([record(1, title='A shared long paper title about robots'), record(2, title='A shared long paper title about robots')])), 2)

    def test_exact_title_and_shared_authors_merge_but_conflicting_dois_do_not(self):
        items = [record(i, title='A shared long paper title about robots', authors=['Alice Smith', 'Bob Jones']) for i in (1, 2)]
        self.assertEqual(len(merge_records(items)), 1)
        items[0]['doi'], items[1]['doi'] = '10.1234/a', '10.1234/b'
        self.assertEqual(len(merge_records(items)), 2)

    def test_tracking_removed_but_meaningful_queries_retained(self):
        self.assertEqual(normalize_url('https://x.org/a?utm_source=a&id=2#foo'), 'https://x.org/a?id=2')
        self.assertNotEqual(normalize_url('https://x.org/a?id=1'), normalize_url('https://x.org/a?id=2'))
        self.assertEqual(normalize_doi('https://doi.org/10.1234/ABC'), '10.1234/abc')


class CacheTests(unittest.TestCase):
    def test_ttl_expiry_and_corruption_are_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = RetrievalCache(tmp)
            with patch('src.services.retrieval_cache.time.time', return_value=100):
                cache.put('key', ['value'])
                self.assertEqual(cache.get('key', 10), ['value'])
            with patch('src.services.retrieval_cache.time.time', return_value=110):
                self.assertIsNone(cache.get('key', 10))
            with patch.object(Path, 'read_text', return_value='broken'):
                self.assertIsNone(cache.get('key', 10))

    def test_cached_body_reuses_download_across_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = SourceReader(cache_dir=tmp)
            with patch.object(reader, '_fetch', return_value=('Experiment details ' * 100, 'PDF', 'https://x.org/p.pdf', False)) as fetch:
                reader.read_document('https://x.org/p.pdf', 'robot')
                reader.read_document('https://x.org/p.pdf', 'robot')
                reader.read_document('https://x.org/p.pdf', 'latency')
                self.assertEqual(fetch.call_count, 1)


class AdapterTests(unittest.TestCase):
    def test_arxiv_atom_preserves_metadata_not_fulltext(self):
        text = '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:x="http://arxiv.org/schemas/atom"><entry><id>http://arxiv.org/abs/2301.12345v2</id><title>Robot paper</title><summary>Abstract</summary><author><name>Alice</name></author><published>2023-01-01</published><x:doi>10.1234/robot</x:doi><link title="pdf" href="http://arxiv.org/pdf/2301.12345v2"/></entry></feed>'
        item = ArxivSearch.parse(text)[0]
        self.assertEqual(item['authors'], ['Alice'])
        self.assertEqual(item['publication_type'], 'preprint')
        self.assertNotIn('content', item)
        self.assertEqual(item['doi'], '10.1234/robot')

    def test_semantic_scholar_external_ids_and_open_pdf(self):
        item = SemanticScholarSearch.parse({'paperId': 'p1', 'title': 'Robot', 'externalIds': {'ArXiv': '2301.12345', 'DOI': '10.1234/robot'}, 'openAccessPdf': {'url': 'https://x.org/p.pdf'}})
        self.assertEqual(item['url'], 'https://arxiv.org/abs/2301.12345')
        self.assertEqual(item['fulltext_urls'], ['https://x.org/p.pdf'])
        self.assertNotIn('content', item)

    def test_openalex_reconstructs_abstract_and_oa_link(self):
        item = OpenAlexSearch.parse({'id': 'https://openalex.org/W1', 'title': 'Robot', 'abstract_inverted_index': {'world': [1], 'Hello': [0]}, 'best_oa_location': {'pdf_url': 'https://x.org/p.pdf'}})
        self.assertEqual(item['snippet'], 'Hello world')
        self.assertEqual(item['fulltext_urls'], ['https://x.org/p.pdf'])

    def test_crossref_requires_matching_doi(self):
        engine = CrossrefMetadata()
        engine.http = Mock(json=Mock(return_value={'message': {'DOI': '10.1234/other'}}))
        with self.assertRaises(SearchError):
            engine.lookup('10.1234/robot')

    def test_crossref_does_not_relabel_read_preprint(self):
        s = service()
        s.crossref = Mock(lookup=Mock(return_value={'doi': '10.1234/r', 'publication_type': 'journal-article'}))
        item = record(1, doi='10.1234/r', publication_type='preprint', content='preprint excerpt')
        with tempfile.TemporaryDirectory() as tmp:
            s.cache_dir = Path(tmp)
            s._verify_metadata([item])
        self.assertEqual(item['publication_type'], 'preprint')
        self.assertEqual(item['verified_metadata']['publication_type'], 'journal-article')


class HTTPTests(unittest.TestCase):
    def response(self, status, payload=None, headers=None):
        return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=payload or {'results': []}))

    def test_unauthorized_is_classified_not_reported_as_empty(self):
        client = ProviderHTTP('test', interval=0)
        with patch('src.tools.search.http.requests.request', return_value=self.response(401)) as request:
            with self.assertRaisesRegex(SearchError, 'authentication_error'):
                client.json('GET', 'https://x.org/?api_key=secret')
            with self.assertRaises(SearchError):
                client.json('GET', 'https://x.org/')
            self.assertEqual(request.call_count, 1)

    def test_429_retry_is_bounded_and_respects_long_retry_after(self):
        client = ProviderHTTP('test', interval=0)
        with patch('src.tools.search.http.requests.request', return_value=self.response(429, headers={'Retry-After': '120'})) as request:
            with self.assertRaisesRegex(SearchError, 'rate_limited'):
                client.json('GET', 'https://x.org/')
            self.assertEqual(request.call_count, 1)

    def test_transient_503_retries_once(self):
        with patch('src.tools.search.http.requests.request', side_effect=[self.response(503), self.response(200)]) as request, patch('src.tools.search.http.time.sleep'):
            result = ProviderHTTP('test', interval=0).json('GET', 'https://x.org/')
            self.assertEqual(result, {'results': []})
            self.assertEqual(request.call_count, 2)

    def test_timeouts_have_safe_classification(self):
        with patch('src.tools.search.http.requests.request', side_effect=requests.Timeout('secret')), patch('src.tools.search.http.time.sleep'):
            with self.assertRaisesRegex(SearchError, 'test: timeout'):
                ProviderHTTP('test').json('GET', 'https://x.org/')

    def test_tavily_rejects_malformed_envelope(self):
        engine = TavilySearch('fixture-key')
        engine.http = Mock(json=Mock(return_value={'detail': 'failure'}))
        with self.assertRaisesRegex(SearchError, 'invalid_response'):
            engine.search('robot')

    def test_tavily_extract_returns_raw_content_and_uses_bearer_auth(self):
        engine = TavilySearch('fixture-key')
        engine.http = Mock(json=Mock(return_value={'results': [
            {'url': 'https://x.org', 'raw_content': 'body'}]}))
        self.assertEqual(engine.extract(['https://x.org'])[0]['raw_content'], 'body')
        call = engine.http.json.call_args
        self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer fixture-key')
        self.assertEqual(call.kwargs['json']['extract_depth'], 'basic')


if __name__ == '__main__':
    unittest.main()

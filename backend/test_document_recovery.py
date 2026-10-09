"""Offline full-text recognition, bounded retrieval and checkpoint recovery."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from src.models import TaskSummary, TodoItem
from src.repository import ResearchRepository
from src.services.document_scope import classify_document
from src.services.research import ResearchManager
from src.services.reporting import ReportingService
from src.services.retrieval_ranking import clearly_off_topic, lexical_score
from src.services.search import SearchService
from src.services.source_reader import SourceReader
from src.tools.note import NoteTool
from src.tools.search.http import ProviderHTTP, SearchError


URL = 'https://www.nature.com/articles/test-paper'
QUOTE = 'Our agent learns a world model and plans actions.'


def raw_document(**changes):
    return {'kind': '网页', 'partial': False, 'fetched_at': 'now', 'fetched_url': URL,
        'text': 'Abstract\n' + QUOTE + '\nIntroduction\n' + ('Scientific background. ' * 100)
                + '\nMethods\n' + (QUOTE + ' ') * 100 + '\nReferences\nPublication details.', **changes}


def summary_for(task):
    return TaskSummary(task=task, summary='Saved evidence[1].', sources=[URL], source_titles=['Paper'],
        source_details=[{'kind': 'primary', 'relevance': 'direct', 'document_scope': 'webpage_body',
            'acquisition_status': 'full_text_extracted', 'claims': [{
                'relation': 'direct_report', 'quote_match': 'body_excerpt', 'context_located': True,
                'quote': QUOTE, 'source_context': QUOTE}]}])


class DocumentTests(unittest.TestCase):
    def test_publisher_body_recognized_despite_cookie_query_parameter(self):
        raw = raw_document(fetched_url=URL + '?error=cookies_not_supported')
        result = SourceReader()._excerpt(raw, 'world model')
        self.assertEqual(result['document_scope'], 'paper_fulltext')
        self.assertTrue(result['full_text_available'])

    def test_abstract_wall_and_table_of_contents_are_not_full_text(self):
        for text in ('Abstract\n' + 'Summary. ' * 500 + '\nSubscribe to read this article.',
                     'Introduction\nMethods\nResults\nReferences\n' + 'Bibliography. ' * 400):
            result = SourceReader()._excerpt(raw_document(text=text), '')
            self.assertEqual(result['acquisition_status'], 'metadata_only')
            self.assertFalse(result['full_text_available'])

    def test_partial_download_and_service_extraction_stay_unverified(self):
        for changes, status in (({'partial': True}, 'partial_text_extracted'),
                                ({'acquisition_status': 'service_extracted_text'}, 'service_extracted_text')):
            result = SourceReader()._excerpt(raw_document(**changes), '')
            self.assertEqual(result['acquisition_status'], status)
            self.assertFalse(result['full_text_available'])

    def test_captcha_cannot_be_used_as_evidence(self):
        for url in (URL, 'https://arxiv.org/html/2301.00001'):
            result = SourceReader()._excerpt(raw_document(fetched_url=url,
                text='Radware Bot Manager Captcha\nVerify you are human. ' * 20), '')
            self.assertEqual(result['acquisition_status'], 'read_failed')
            self.assertEqual(result['content'], '')

    def test_publisher_lookalike_and_blog_not_promoted(self):
        for url in ('https://nature.com.evil.test/articles/fake', 'https://nature.com/blog/something'):
            self.assertEqual(classify_document(raw_document(fetched_url=url))[0], 'webpage_body')

    def test_cache_refresh_never_downloads_or_changes_claims(self):
        reader = SourceReader()
        reader._cached_raw = Mock(return_value=raw_document())
        reader._raw_document = Mock(side_effect=AssertionError('No network'))
        record = {'url': URL, 'claims': [{'quote': QUOTE}], 'content': QUOTE}
        original = copy.deepcopy(record)
        metadata = reader.refresh_metadata(record)
        self.assertEqual(metadata['document_scope'], 'paper_fulltext')
        self.assertEqual(record, original)
        reader._raw_document.assert_not_called()

    def test_missing_or_different_cache_and_proxy_are_not_upgraded(self):
        reader = SourceReader()
        reader._cached_raw = Mock(return_value=None)
        self.assertEqual(reader.refresh_metadata({'url': URL}), {})
        reader._cached_raw.return_value = raw_document()
        self.assertEqual(reader.refresh_metadata({'url': URL, 'document_id': 'different-version'}), {})
        self.assertEqual(reader.refresh_metadata({'url': URL, 'acquisition_status': 'service_extracted_text'}), {})

    def test_cached_incomplete_arxiv_html_falls_back_to_pdf(self):
        reader = SourceReader()
        reader._cached_raw = Mock(side_effect=lambda u: raw_document(partial=True,
            fetched_url=u) if '/html/' in u else None)
        reader._raw_document = Mock(side_effect=lambda u: raw_document(kind='PDF', fetched_url=u))
        result = reader.read_document('https://arxiv.org/abs/2301.00001')
        self.assertEqual(result['acquisition_status'], 'full_text_extracted')
        self.assertIn('/pdf/', result['fetched_url'])
        reader._raw_document.assert_called_once()

    def test_read_candidate_continues_past_metadata_and_preserves_fallback(self):
        search = SearchService.__new__(SearchService)
        search.source_reader = SimpleNamespace(read_document=Mock(side_effect=[
            {'content': 'Abstract', 'document_scope': 'metadata_page', 'acquisition_status': 'metadata_only'},
            {'content': QUOTE, 'document_scope': 'paper_fulltext', 'acquisition_status': 'full_text_extracted'}]))
        item = {'url': URL, 'versions': [{'url': URL + '.pdf'}]}
        result = search._read_candidate(item, 'world models')
        self.assertEqual(result['content'], QUOTE)
        self.assertEqual(len(result['read_attempts']), 2)
        search.source_reader.read_document.side_effect = [
            {'content': 'Abstract', 'document_scope': 'metadata_page', 'acquisition_status': 'metadata_only'},
            RuntimeError('No body')]
        result = search._read_candidate(item, 'world models')
        self.assertEqual(result['acquisition_status'], 'metadata_only')
        self.assertEqual(len(result['read_attempts']), 2)

    def test_metadata_does_not_consume_successful_read_budget(self):
        search = SearchService.__new__(SearchService)
        search.settings = SimpleNamespace(search_read_budget=1, search_read_attempts=3)
        search._read_candidate = Mock(side_effect=[
            {'content': 'Abstract', 'acquisition_status': 'metadata_only'},
            {'content': QUOTE, 'acquisition_status': 'full_text_extracted'}])
        pool = [{'url': URL}, {'url': URL + '2'}]
        search._read_sources(pool, 'world models')
        self.assertEqual(search._read_candidate.call_count, 2)

    def test_homonyms_filtered_only_for_relevant_task(self):
        item = {'title': 'Planet browser game', 'snippet': 'Space exploration'}
        self.assertTrue(clearly_off_topic(item, 'world models PlaNet'))
        self.assertFalse(clearly_off_topic(item, 'browser games'))
        self.assertFalse(clearly_off_topic({'title': 'World models for planetary robots'}, 'world models'))
        self.assertFalse(clearly_off_topic({'title': 'Learning latent dynamics for planning'}, 'world models'))
        self.assertEqual(lexical_score({'title': 'paper experiments full text'}, 'world models paper experiments full text'), 0)

    def test_exhausted_provider_has_cooldown_and_recovers(self):
        client = ProviderHTTP('fixture', interval=0, retries=1)
        response = Mock(status_code=200)
        with patch('src.tools.search.http.time.monotonic', return_value=100) as clock, \
             patch('src.tools.search.http.time.sleep'), \
             patch('src.tools.search.http.requests.request', side_effect=requests.Timeout('secret')) as request:
            for _ in range(2):
                with self.assertRaisesRegex(SearchError, 'fixture: timeout'):
                    client.request('GET', URL)
            self.assertEqual(request.call_count, 2)
            clock.return_value = 131
            request.side_effect = None
            request.return_value = response
            self.assertIs(client.request('GET', URL), response)

    def test_short_429_also_cools_down(self):
        client = ProviderHTTP('fixture', interval=0, retries=1)
        with patch('src.tools.search.http.time.sleep'), \
             patch('src.tools.search.http.requests.request', return_value=Mock(status_code=429, headers={})) as request:
            for _ in range(2):
                with self.assertRaisesRegex(SearchError, 'rate_limited'):
                    client.request('GET', URL)
            self.assertEqual(request.call_count, 2)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = ResearchRepository(str(Path(temp.name, 'research.db')))
        self.notes = NoteTool(temp.name)
        self.task = TodoItem(id=1, title='世界模型论文', query='world models', intent='研究')
        self.reader = SourceReader()
        self.reader._cached_raw = Mock(return_value=raw_document())
        self.search = SimpleNamespace(source_reader=self.reader, search_many=Mock(return_value=[{'url': URL}]))
        self.planning = SimpleNamespace(plan=Mock(return_value=[self.task]))
        self.summarization = SimpleNamespace(summarize=Mock(side_effect=lambda t, _: summary_for(t)))
        self.reporting = ReportingService.__new__(ReportingService)
        self.reporting.generate = Mock(return_value='# Report')
        self.manager = ResearchManager(self.repo, self.planning, self.search, self.summarization,
            self.reporting, self.notes)

    async def finish(self, run):
        await asyncio.wait_for(asyncio.shield(self.manager._jobs[run]), 5)
        return self.repo.get(run)

    async def test_resume_reclassifies_saved_body_without_model_or_search(self):
        run = 'saved-publisher'
        self.repo.create(run, 'world models')
        self.task.status = 'completed'
        self.repo.record_event(run, 'error', {'message': 'old classification'},
            {'status': 'error', 'tasks_json': [self.task.model_dump()]})
        old = summary_for(self.task)
        self.repo.save_checkpoint(run, 1, summary=old.model_dump(),
            results=[{'url': URL, 'document_scope': 'webpage_body', 'content': QUOTE}])
        await self.manager.resume(run)
        self.assertEqual((await self.finish(run))['status'], 'completed')
        self.search.search_many.assert_not_called()
        self.summarization.summarize.assert_not_called()
        checkpoint = self.repo.checkpoints(run)[1]
        self.assertEqual(checkpoint['results'][0]['document_scope'], 'paper_fulltext')
        saved = TaskSummary.model_validate(checkpoint['summary'])
        self.assertEqual(saved.source_details[0]['claims'], old.source_details[0]['claims'])




if __name__ == '__main__':
    unittest.main()

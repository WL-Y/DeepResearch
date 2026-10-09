"""Offline full-document coverage, truthful accounting and restart regressions."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Settings
from src.models import TodoItem, TaskSummary
from src.repository import ResearchRepository
from src.services.fulltext_analysis import analyze_fulltext, split_text
from src.services.source_reader import SourceReader
from src.services.source_details import source_details
from src.services.summarization import SummarizationService
from src.services.research import ResearchManager
from src.services.reporting import ReportingService
from src.services.cost_control import ResearchLimit
from src.tools.note import NoteTool


class FulltextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.notes = NoteTool(directory.name)
        self.reader = SourceReader(cache_dir=Path(directory.name) / 'cache', max_chars=1000)
        self.task = TodoItem(id=1, title='研究问题', query='experiments', intent='分析条件与结果')
        self.agent = Mock()
        self.agent.max_input_chars = 70000
        self.agent.run.side_effect = lambda prompt: json.dumps({'summary':
            '结论仅适用于 CONDITION_AT_END[1]。' if 'CONDITION_AT_END' in prompt else '方法的结果[1]。'})
        self.service = SummarizationService(self.agent, fulltext_chunk_chars=20000)

    def document(self, text=None, **changes):
        raw = {'text': text or ('Introduction\n' + 'Data and experimental results. ' * 1800 + '\nCONDITION_AT_END'),
               'fetched_url': 'https://arxiv.org/html/2506.04280', 'fetched_at': '2026-09-13',
               'kind': '网页', 'partial': False, **changes}
        record = {**self.reader._excerpt(raw, 'experiments'), 'url': 'https://arxiv.org/abs/2506.04280',
                  'title': 'An experimental paper', 'snippet': 'Search overview'}
        return raw, record

    def test_slices_reconstruct_all_characters_including_last_appendix(self):
        text = 'Opening\n' + '\\"\x00中' * 14000 + '\nAPPENDIX END'
        parts = list(split_text(text))
        self.assertEqual(''.join(p[2] for p in parts), text)
        self.assertEqual(parts[0][0], 0)
        self.assertEqual(parts[-1][1], len(text))
        self.assertTrue(all(left[1] == right[0] for left, right in zip(parts, parts[1:])))
        self.assertTrue(all(len(json.dumps(p[2], ensure_ascii=False)) <= 24000 for p in parts))

    def test_cached_full_document_reaches_model_and_tail_survives_into_report(self):
        raw, record = self.document()
        self.assertLess(len(record['content']), len(raw['text']))
        self.reader._raw_document = Mock(side_effect=AssertionError('no download'))
        captured = []
        original = self.service._format_results
        def capture(rows):
            if rows[0].get('analysis_phase') == 'reading':
                captured.extend(r['content'] for r in rows)
            return original(rows)
        self.service._format_results = capture
        result = analyze_fulltext(self.service, self.notes, 'run', self.task, [record], self.reader)
        self.assertEqual(''.join(captured), raw['text'])
        self.assertIn('CONDITION_AT_END', result.summary)
        self.assertEqual(result.sources, [record['url']])
        self.assertEqual(record['analyzed_chars'], len(raw['text']))
        self.assertEqual(record['analysis_status'], 'completed')
        self.assertEqual(record['analysis_completed_parts'], record['analysis_total_parts'])
        writer = Mock(run=Mock(return_value='# 报告\n\n结论仅适用于 CONDITION_AT_END[1]。'))
        ReportingService(writer, self.notes).generate('问题', [result], 'run')
        self.assertIn('CONDITION_AT_END', writer.run.call_args.args[0])
        self.reader._raw_document.assert_not_called()

    def test_source_formatter_does_not_apply_second_six_thousand_character_cut(self):
        text = 'Beginning ' + 'a' * 13000 + ' FINAL_CONDITION'
        formatted = self.service._format_results([{'url': 'https://example.org', 'title': 'Paper', 'content': text}])
        self.assertIn(text, formatted)

    def test_partial_failure_resume_reuses_paid_parts_and_finishes_remaining_text(self):
        raw, record = self.document()
        self.agent.run.side_effect = [json.dumps({'summary': 'first[1].'}), RuntimeError('offline')]
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            analyze_fulltext(self.service, self.notes, 'run', self.task, [record], self.reader)
        completed = record['analyzed_chars']
        self.assertGreater(completed, 0)
        self.assertLess(completed, len(raw['text']))
        self.assertEqual(record['analysis_completed_parts'], 1)
        self.agent.run.reset_mock()
        self.agent.run.side_effect = lambda prompt: json.dumps({'summary': 'remaining CONDITION_AT_END[1].'})
        result = analyze_fulltext(self.service, self.notes, 'run', self.task, [record], self.reader)
        self.assertEqual(record['analyzed_chars'], len(raw['text']))
        self.assertEqual(self.agent.run.call_count, record['analysis_total_parts'])  # remaining parts + synthesis
        self.assertIn('CONDITION_AT_END', result.summary)
        self.agent.run.reset_mock()
        analyze_fulltext(self.service, self.notes, 'run', self.task, [record], self.reader)
        self.agent.run.assert_not_called()

    def test_budget_stop_keeps_exact_completed_amount_and_does_not_claim_full_read(self):
        raw, record = self.document()
        self.agent.run.side_effect = [json.dumps({'summary': 'first[1].'}), ResearchLimit('budget')]
        with self.assertRaises(ResearchLimit):
            analyze_fulltext(self.service, self.notes, 'run', self.task, [record], self.reader)
        public = source_details({1: {'results': [record]}})['sources'][0]
        self.assertEqual(public['analysis_completed_parts'], 1)
        self.assertLess(public['analyzed_chars'], len(raw['text']))
        self.assertNotEqual(public['analysis_status'], 'completed')
        self.assertNotIn('analysis_text', public)
        self.assertNotIn('analysis_text_hash', public)

    def test_extraction_service_text_is_all_analyzed_without_claiming_original_complete(self):
        raw, record = self.document(acquisition_status='service_extracted_text')
        analyze_fulltext(self.service, self.notes, 'run', self.task, [record], self.reader)
        self.assertEqual(record['analyzed_chars'], len(raw['text']))
        self.assertEqual(record['analysis_scope'], 'available_text')
        self.assertFalse(record['full_text_available'])

    def test_missing_snapshot_does_not_fall_back_to_small_preview(self):
        _, record = self.document()
        record['analysis_text_hash'] = '0' * 64
        with self.assertRaisesRegex(ValueError, '全文缓存'):
            analyze_fulltext(self.service, self.notes, 'run', self.task, [record], self.reader)
        self.agent.run.assert_not_called()

    def test_snapshot_survives_url_cache_update_and_ttl_expiry(self):
        raw, record = self.document()
        self.reader.cache_ttl = 0
        self.reader.cache.put(self.reader._body_key(raw['fetched_url']), {**raw, 'text': 'different document'})
        self.assertEqual(self.reader.analysis_text(record), raw['text'])

    def test_changed_tail_invalidates_previous_document_analysis(self):
        raw, record = self.document()
        analyze_fulltext(self.service, self.notes, 'run', self.task, [record], self.reader)
        self.agent.run.reset_mock()
        _, changed = self.document(text=raw['text'] + '\nNEW_APPENDIX')
        analyze_fulltext(self.service, self.notes, 'run', self.task, [changed], self.reader)
        self.assertGreater(self.agent.run.call_count, 0)
        self.assertTrue(any('NEW_APPENDIX' in c.args[0] for c in self.agent.run.call_args_list))

    def test_legacy_limit_setting_cannot_restore_excerpt_mode(self):
        config = Settings(_env_file=None, SUMMARY_SOURCE_MAX_CHARS=6000)
        self.assertNotIn('summary_source_max_chars', config.model_dump())
        self.assertEqual(config.fulltext_chunk_chars, 20000)
        self.assertGreaterEqual(config.research_max_model_calls, 200)


class FulltextIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_persists_actual_analysis_progress_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            notes = NoteTool(directory)
            repo = ResearchRepository(str(Path(directory) / 'research.db'))
            reader = SourceReader(cache_dir=Path(directory) / 'cache', max_chars=500)
            text = 'Introduction\n' + 'results ' * 3000 + '\nFINAL_CONDITION'
            record = {**reader._excerpt({'text': text, 'kind': '网页', 'partial': False,
                'fetched_url': 'https://example.org/paper', 'fetched_at': 'now'}, 'results'),
                'url': 'https://example.org/paper', 'title': 'Paper', 'snippet': ''}
            task = TodoItem(id=1, title='Research', intent='', query='paper')
            search = SimpleNamespace(source_reader=reader, search_many=Mock(return_value=[record]))
            agent = Mock(run=Mock(return_value=json.dumps({'summary': 'FINAL_CONDITION[1].'})))
            service = SummarizationService(agent)
            manager = ResearchManager(repo, SimpleNamespace(plan=Mock(return_value=[task])), search, service,
                ReportingService(Mock(run=Mock(return_value='# Report\n\nFINAL_CONDITION[1].')), notes), notes)
            run = await manager.start('Research')
            await asyncio.wait_for(manager._jobs[run], 5)
            self.assertEqual(repo.get(run)['status'], 'completed')
            saved = repo.checkpoints(run)[1]
            self.assertEqual(saved['results'][0]['analyzed_chars'], len(text))
            self.assertEqual(saved['summary']['retrieval']['analysis_version'], 1)
            self.assertEqual(saved['summary']['source_details'][0]['analysis_status'], 'completed')

    async def test_legacy_excerpt_summary_is_not_reused_as_fulltext_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = ResearchRepository(str(Path(directory) / 'research.db'))
            task = TodoItem(id=1, title='Research', intent='', query='paper')
            manager = ResearchManager(repo, SimpleNamespace(), SimpleNamespace(source_reader=SourceReader()),
                SummarizationService(Mock()), SimpleNamespace(), NoteTool(directory))
            old = TaskSummary(task=task, summary='old excerpt', sources=['https://example.org'])
            self.assertIsNone(manager._restore_summary('run', task, {'summary': old.model_dump()}))


if __name__ == '__main__':
    unittest.main()

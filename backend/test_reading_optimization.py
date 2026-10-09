"""Offline budget, selection, resume and cross-task source-note reuse tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.models import TodoItem, TaskSummary
from src.services.cost_control import ResearchLimit
from src.services.reading_policy import select_sources, task_limits
from src.services.source_notes import analyze_selected_sources, cached_note
from src.services.source_reader import SourceReader
from src.services.summarization import SummarizationService
from src.services.source_details import source_details
from src.tools.note import NoteTool


class ReadingOptimizationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.notes = NoteTool(temp.name)
        self.reader = SourceReader(cache_dir=Path(temp.name) / 'cache')
        self.task = TodoItem(id=1, title='世界模型概念界定', intent='定义范围', query='world model definition')
        self.selector = Mock()
        self.selector.run.return_value = json.dumps({'selected': [{'index': 0, 'reason': '奠基性原文'},
                                                                   {'index': 1, 'reason': '直接定义'}]})
        self.agent = Mock(max_input_chars=70000, model='test', system_prompt='notes', max_output_tokens=5000)
        self.agent.client.base_url = 'https://test.invalid'
        self.agent.run.side_effect = lambda prompt: json.dumps({'summary': 'Facts and limits[1].'})
        self.service = SummarizationService(self.agent, optimize_reading=True, selection_agent=self.selector)

    def record(self, index, chars=2000, title='World model definition', **updates):
        url = f'https://example.org/{index}.pdf'
        text = ('Findings for source ' + str(index) + '. ') * (chars // 23 + 1)
        text = text[:chars]
        raw = {'text': text, 'kind': 'PDF', 'partial': False, 'fetched_at': 'now', 'fetched_url': url}
        return {'url': url, 'title': title, 'snippet': 'World model definition and conditions',
                **self.reader._excerpt(raw, 'world model'), **updates}

    def select(self, rows, task=None, reusable=None):
        return select_sources(self.service, self.notes, 'run', task or self.task, rows, self.reader, reusable)

    def test_concept_and_comparison_have_different_budgets(self):
        self.assertEqual(task_limits(self.task), (2, 8))
        self.assertEqual(task_limits(self.task.model_copy(update={'title': '世界模型路线比较'})), (4, 16))

    def test_irrelevant_long_survey_rejected_by_one_batch_call(self):
        rows = [self.record(0), self.record(1), self.record(2, 130000, 'Emotion in reinforcement learning')]
        selected = self.select(rows)
        self.assertEqual(selected, rows[:2])
        self.assertFalse(rows[2]['reading_selected'])
        self.assertEqual(rows[2]['analysis_status'], 'not_selected')
        self.selector.run.assert_called_once()
        self.agent.run.assert_not_called()
        self.select(rows)
        self.selector.run.assert_called_once()

    def test_exact_new_part_budget_does_not_truncate_selected_documents(self):
        rows = [self.record(0, 100000), self.record(1, 100000)]
        selected = self.select(rows)
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(self.reader.analysis_text(selected[0])), 100000)
        self.assertIn('预算', rows[1]['reading_selection_reason'])
        self.assertTrue(rows[1]['full_text_available'])

    def test_completed_cached_note_has_zero_new_reading_cost(self):
        rows = [self.record(0, 100000), self.record(1, 100000)]
        selected = self.select(rows, reusable=lambda r: r is rows[0])
        self.assertEqual(len(selected), 2)

    def test_missing_body_is_not_sent_for_expensive_analysis(self):
        rows = [self.record(0, acquisition_status='metadata_only', document_scope='metadata_page')]
        with self.assertRaisesRegex(ResearchLimit, '没有符合'):
            self.select(rows)
        self.selector.run.assert_not_called()
        self.assertIn('仅摘要', rows[0]['reading_selection_reason'])

    def test_selector_cannot_invent_indexes_and_failure_is_not_retried(self):
        rows = [self.record(i) for i in range(3)]
        self.selector.run.return_value = '{"selected":[{"index":99,"reason":"fake"}]}'
        self.assertEqual(len(self.select(rows)), 2)
        self.select(rows)
        self.selector.run.assert_called_once()
        self.assertTrue(any('不可用' in r['reading_selection_reason'] for r in rows))

    def test_budget_failure_does_not_launch_fallback_fulltext_calls(self):
        self.selector.run.side_effect = ResearchLimit('budget exhausted')
        with self.assertRaisesRegex(ResearchLimit, 'budget exhausted'):
            self.select([self.record(i) for i in range(3)])
        self.agent.run.assert_not_called()

    def test_paid_legacy_sources_survive_reduced_source_limit(self):
        rows = [self.record(i) for i in range(4)]
        self.notes.summary_batches('run', 1, {'version': 1, 'batches': {
            str(i): {'source_urls': [rows[i]['url']], 'status': 'completed'} for i in range(3)}})
        selected = self.select(rows)
        self.assertEqual(selected, rows[:3])
        self.assertFalse(rows[3]['reading_selected'])

    def test_cross_task_notes_reused_without_model_and_changed_text_invalidates(self):
        row = self.record(0, 30000)
        first = analyze_selected_sources(self.service, self.notes, 'run', self.task, [row], self.reader)
        self.assertEqual(self.agent.run.call_count, 2)  # two reading blocks, no paid join
        self.assertTrue(row['analysis_coverage_verified'])
        self.assertTrue(cached_note(self.service, self.notes, 'run', row))
        second_task = self.task.model_copy(update={'id': 2, 'title': '方法局限与评价条件', 'query': 'limitations'})
        self.agent.run.reset_mock()
        row2 = self.record(0, 30000)
        second = analyze_selected_sources(self.service, self.notes, 'run', second_task, [row2], self.reader)
        self.agent.run.assert_not_called()
        self.assertTrue(row2['analysis_reused'])
        self.assertEqual(second.summary, first.summary)
        self.assertEqual(second.task.id, 2)
        self.assertEqual(second.retrieval['reused_sources'], 1)
        changed = self.record(0, 30001)
        self.assertIsNone(cached_note(self.service, self.notes, 'run', changed))
        self.assertIsNone(cached_note(self.service, self.notes, 'another-run', row))

    def test_cross_task_notes_invalidate_on_model_change(self):
        row = self.record(0)
        analyze_selected_sources(self.service, self.notes, 'run', self.task, [row], self.reader)
        self.agent.model = 'different'
        self.assertIsNone(cached_note(self.service, self.notes, 'run', row))

    def test_shared_note_never_reuses_task_specific_legacy_batch(self):
        from src.services.fulltext_analysis import analyze_fulltext
        row = self.record(0)
        analyze_fulltext(self.service, self.notes, 'run', self.task, [row], self.reader)
        self.agent.run.reset_mock()
        analyze_selected_sources(self.service, self.notes, 'run', self.task, [row], self.reader)
        self.agent.run.assert_not_called()  # exact same-task legacy work reused
        self.assertIsNone(cached_note(self.service, self.notes, 'run', row))

    def test_no_model_selection_does_not_claim_rejected_sources_were_read(self):
        self.service.selection_agent = None
        rows = [self.record(i) for i in range(5)]
        result = analyze_selected_sources(self.service, self.notes, 'run', self.task, rows, self.reader)
        self.assertEqual(len(result.sources), 2)
        self.assertEqual(self.agent.run.call_count, 2)
        public = source_details({1: {'results': rows}})
        self.assertEqual(len(public['sources']), 5)
        self.assertEqual(sum(r['reading_decisions'][0]['selected'] for r in public['sources']), 2)

    def test_source_selection_decisions_are_kept_per_task(self):
        row = self.record(0, reading_selected=False, reading_selection_reason='task one skip')
        other = {**row, 'reading_selected': True, 'reading_selection_reason': 'task two select'}
        public = source_details({1: {'results': [row]}, 2: {'results': [other]}})
        decisions = public['sources'][0]['reading_decisions']
        self.assertEqual([d['selected'] for d in decisions], [False, True])

    def test_known_neurips_abstract_is_not_full_text(self):
        row = self.reader._excerpt({'text': 'Abstract. ' * 150, 'kind': '网页', 'partial': False,
            'fetched_at': 'now', 'fetched_url': 'https://proceedings.neurips.cc/paper/2018/hash/id-Abstract.html'}, '')
        self.assertEqual(row['acquisition_status'], 'metadata_only')

"""Regression cases for uncertain acquisition and lossless analysis transport."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.models import TodoItem, TaskSummary
from src.services.source_reader import SourceReader
from src.services.source_details import source_details
from src.services.summarization import SummarizationService
from src.services.fulltext_analysis import analyze_fulltext
from src.services.cost_control import OutputTruncated, ResearchLimit
from src.services.reporting import ReportingService
from src.services.report_compression import prepare_materials
from src.tools.note import NoteTool


class AcquisitionTests(unittest.TestCase):
    def raw(self, **changes):
        return {'text': 'Introduction\n' + 'Background facts. ' * 180 + '\nMethods\n' + 'Method details. ' * 180,
                'kind': '网页', 'partial': False, 'fetched_url': 'https://nature.com/articles/example',
                'fetched_at': 'now', **changes}

    def test_substantive_body_without_references_is_not_only_abstract(self):
        document = SourceReader()._excerpt(self.raw(), '')
        self.assertEqual(document['acquisition_status'], 'body_text_unconfirmed')
        self.assertTrue(document['content'])
        self.assertFalse(document['full_text_available'])
        self.assertEqual(document['completeness_status'], 'unconfirmed')
        public = source_details({1: {'results': [{'url': 'https://nature.com/articles/example', **document}]}})
        self.assertEqual(public['unconfirmed_text_count'], 1)
        self.assertIn('completeness_reason', public['sources'][0])

    def test_explicit_teaser_is_partial_even_on_general_webpage(self):
        document = SourceReader()._excerpt(self.raw(fetched_url='https://example.org/blog',
            text='Some introductory body. ' * 300 + '\nSubscribe to read the full article.'), '')
        self.assertEqual(document['acquisition_status'], 'partial_text_extracted')
        self.assertFalse(document['full_text_available'])
        self.assertIn('订阅', document['completeness_reason'])

    def test_pdf_empty_page_is_uncertain_not_a_known_truncation(self):
        response = Mock(headers={'content-type': 'application/pdf'}, is_redirect=False,
                        is_permanent_redirect=False, encoding=None)
        response.iter_content.return_value = [b'%PDF-test']
        pages = [Mock(extract_text=Mock(return_value='Actual paper text. ' * 100)),
                 Mock(extract_text=Mock(return_value=''))]
        with patch.object(SourceReader, '_validate_public_url'), \
             patch('src.services.source_reader.requests.get', return_value=response), \
             patch('src.services.source_reader.PdfReader', return_value=Mock(pages=pages)):
            document = SourceReader().read_document('https://example.org/paper.pdf')
        self.assertEqual(document['pdf_empty_pages'], [2])
        self.assertEqual(document['acquisition_status'], 'body_text_unconfirmed')
        self.assertFalse(document['acquisition_partial'])
        self.assertFalse(document['full_text_available'])

    def test_scanned_pdf_page_markers_do_not_count_as_body(self):
        response = Mock(headers={'content-type': 'application/pdf'}, is_redirect=False,
                        is_permanent_redirect=False, encoding=None)
        response.iter_content.return_value = [b'%PDF-test']
        pages = [Mock(extract_text=Mock(return_value='')) for _ in range(30)]
        with patch.object(SourceReader, '_validate_public_url'), \
             patch('src.services.source_reader.requests.get', return_value=response), \
             patch('src.services.source_reader.PdfReader', return_value=Mock(pages=pages)):
            document = SourceReader().read_document('https://example.org/scanned.pdf')
        self.assertEqual(document['acquisition_status'], 'read_failed')
        self.assertEqual(document['failure_category'], 'ocr_required')

    def test_pdf_page_limit_and_character_limit_are_explicit_partial(self):
        with patch.object(SourceReader, '_fetch', return_value=(
                'content ' * 100, 'PDF', 'https://example.org/p.pdf', True,
                {'partial_reason': 'PDF 达到页数或处理时间上限'})):
            document = SourceReader().read_document('https://example.org/p.pdf')
        self.assertEqual(document['completeness_status'], 'partial')
        self.assertIn('页数', document['completeness_reason'])
        with patch.object(SourceReader, '_fetch', return_value=(
                'content ' * 100, 'PDF', 'https://example.org/p.pdf', False)):
            document = SourceReader(max_extracted_chars=250).read_document('https://example.org/p.pdf')
        self.assertIn('字符上限', document['completeness_reason'])
        self.assertEqual(document['extracted_chars'], 250)


class AnalysisRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.notes = NoteTool(temp.name)
        self.reader = SourceReader(cache_dir=Path(temp.name) / 'cache')
        self.task = TodoItem(id=1, title='Question', query='q', intent='research')
        self.agent = Mock(max_input_chars=70000)
        self.service = SummarizationService(self.agent)
        self.text = 'A' * 4000 + 'B' * 3990 + 'TAIL_FACT!'
        self.record = {'url': 'https://example.org/paper', 'title': 'Paper',
            **self.reader._excerpt({'text': self.text, 'kind': 'PDF', 'partial': False,
                'fetched_url': 'https://example.org/paper', 'fetched_at': 'now'}, '')}

    def analyze(self):
        return analyze_fulltext(self.service, self.notes, 'run', self.task, [self.record], self.reader)

    def test_truncated_single_block_splits_and_preserves_tail_and_citations(self):
        calls = []
        def run(prompt):
            calls.append(prompt)
            if self.text in prompt:
                raise OutputTruncated('partial JSON')
            return json.dumps({'summary': 'TAIL_FACT![1]' if 'TAIL_FACT!' in prompt else 'Opening[1]'})
        self.agent.run.side_effect = run
        result = self.analyze()
        self.assertIn('TAIL_FACT!', result.summary)
        self.assertNotIn('[2]', result.summary)
        self.assertEqual(result.sources, [self.record['url']])
        self.assertTrue(self.record['analysis_coverage_verified'])
        self.assertEqual(self.record['analyzed_chars'], len(self.text))
        self.assertEqual(self.record['analysis_missing_ranges'], [])
        self.agent.run.reset_mock()
        self.analyze()
        self.agent.run.assert_not_called()

    def test_failure_in_second_child_preserves_first_child_and_exact_gap(self):
        self.agent.run.side_effect = [OutputTruncated('partial'), json.dumps({'summary': 'Opening[1]'}),
                                      RuntimeError('offline')]
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            self.analyze()
        self.assertEqual(self.record['analyzed_chars'], 4000)
        self.assertEqual(self.record['analysis_missing_ranges'], [[4000, 8000]])
        self.assertFalse(self.record['analysis_coverage_verified'])
        self.agent.run.reset_mock()
        self.agent.run.side_effect = lambda prompt: json.dumps({'summary': 'TAIL_FACT![1]'})
        result = self.analyze()
        self.assertEqual(self.agent.run.call_count, 1)
        self.assertIn('TAIL_FACT!', result.summary)
        self.assertTrue(self.record['analysis_coverage_verified'])

    def test_repeated_truncation_is_bounded_and_does_not_retry_on_resume(self):
        from src.services.summary_batches import SummaryBatchIncomplete
        self.agent.run.side_effect = OutputTruncated('partial')
        for _ in range(2):
            with self.assertRaises(SummaryBatchIncomplete):
                self.analyze()
        self.assertEqual(self.agent.run.call_count, 4)
        self.assertEqual(self.record['analyzed_chars'], 0)
        self.assertEqual(self.record['analysis_missing_ranges'], [[0, 8000]])

    def test_corrupt_inline_snapshot_is_rejected_before_model(self):
        self.record['analysis_text'] = 'changed contents'
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.analyze()
        self.agent.run.assert_not_called()

    def test_invalid_chunk_coverage_is_rejected_before_model(self):
        with patch('src.services.fulltext_analysis.split_text', return_value=iter([(0, 2, 'AA')])):
            with self.assertRaisesRegex(ValueError, '覆盖'):
                self.analyze()
        self.agent.run.assert_not_called()


class ReportCoverageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.notes = NoteTool(temp.name)
        self.writer = Mock(model='test')
        self.service = ReportingService(self.writer, self.notes, report_materials_max_chars=5000)
        self.task = TodoItem(id=1, title='Research', query='q', intent='research')

    def summary(self, text):
        return TaskSummary(task=self.task, summary=text, sources=['https://example.org/paper'], source_titles=['Paper'])

    def test_complete_notes_reach_single_writer_without_reduction(self):
        self.service.report_materials_max_chars = 160000
        item = self.summary(('\n\n'.join(f'Finding number {i} under condition {i}[1].' for i in range(1000))) + '\n\nTAIL_CONDITION[1].')
        self.writer.run.return_value = '# Report\n\nFindings and conditions [1].'
        result = self.service.generate('Topic', [item], 'run')
        self.assertIn('Report', result)
        self.writer.run.assert_called_once()
        self.assertIn('TAIL_CONDITION', self.writer.run.call_args.args[0])
        state = self.notes.load_json('run', 'report-input.json')
        self.assertEqual(state['omitted_passages'], 0)
        self.assertEqual(state['model_calls'], 0)
        self.writer.run.reset_mock()
        self.assertEqual(self.service.generate('Topic', [item], 'run'), result)
        self.writer.run.assert_not_called()

    def test_overflow_selection_is_local_and_explicit(self):
        item = self.summary('\n\n'.join(f'Long finding {i} under condition {i}[1].' for i in range(1200)))
        normalized = self.service._renumber_citations([item])
        packed = prepare_materials(self.service, normalized, 'run')
        state = self.notes.load_json('run', 'report-input.json')
        self.assertEqual(state['mode'], 'selected_passages')
        self.assertGreater(state['omitted_passages'], 0)
        self.assertLessEqual(len(packed), 5000)
        self.assertIn('[1]', json.loads(packed)['sections'][0]['summary'])
        self.assertEqual(prepare_materials(self.service, normalized, 'run'), packed)
        self.writer.run.assert_not_called()

    def test_budget_stop_keeps_materials_and_does_not_write_draft(self):
        self.writer.run.side_effect = ResearchLimit('budget')
        with self.assertRaisesRegex(ResearchLimit, 'budget'):
            self.service.generate('Topic', [self.summary('Finding [1].')], 'run')
        self.assertEqual(self.notes.load_report('run'), '')
        state = self.notes.load_json('run', 'report-input.json')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['model_calls'], 0)
        self.writer.run.assert_called_once()

    def test_legacy_compression_checkpoints_do_not_block_or_replay(self):
        saved = {'status': 'incomplete', 'attempts': {'old': 2}, 'completed': {'old': 'Paid [1].'}}
        self.notes.save_report_materials('run', saved)
        item = self.summary('\n\n'.join(f'Finding {i} under condition {i}[1].' for i in range(180)))
        packed = prepare_materials(self.service, self.service._renumber_citations([item]), 'run')
        self.assertTrue(packed)
        self.writer.run.assert_not_called()
        self.assertEqual(self.notes.load_json('run', 'report-materials.json'), saved)

    def test_overflow_retains_each_task_source_and_limitation(self):
        items = []
        for i in range(3):
            items.append(TaskSummary(task=TodoItem(id=i+1, title=f'Topic {i}', query='q', intent='research'),
                summary='\n\n'.join(f'Result {n} under condition {n}[1].' for n in range(300))
                    + '\n\nFailure limitation: only evaluated in one setting [1].',
                sources=[f'https://example.org/{i}']))
        packed = json.loads(prepare_materials(self.service, self.service._renumber_citations(items), 'run'))
        self.assertEqual(len(packed['sources']), 3)
        for i, section in enumerate(packed['sections']):
            self.assertIn(f'[{i+1}]', section['summary'])
            self.assertIn('Failure limitation', section['summary'])
        self.writer.run.assert_not_called()

    def test_indivisible_oversize_evidence_is_not_truncated(self):
        with self.assertRaisesRegex(ResearchLimit, '覆盖每个研究任务'):
            prepare_materials(self.service, self.service._renumber_citations([
                self.summary('|table cell ' + 'x' * 8000 + '| [1]')]), 'run')
        self.writer.run.assert_not_called()

    def test_more_than_fifteen_sources_reach_writer_materials(self):
        item = TaskSummary(task=self.task, summary='\n\n'.join(f'Finding[{i}].' for i in range(1, 18)),
            sources=[f'https://example.org/{i}' for i in range(1, 18)])
        packed = json.loads(prepare_materials(self.service, self.service._renumber_citations([item]), 'run'))
        self.assertEqual(len(packed['sources']), 17)
        self.assertIn('[17]', packed['sections'][0]['summary'])
        self.writer.run.assert_not_called()

"""Offline regressions for research without evidence gates or report reviewers."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Settings
from src.models import TaskSummary, TodoItem
from src.repository import ResearchRepository
from src.services.report_materials import compact_materials
from src.services.reporting import ReportingService
from src.services.research import ResearchManager
from src.services.summarization import SummarizationService
from src.tools.note import NoteTool
from test_support import GOOD, summary


class DirectReportingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.notes = NoteTool(directory.name)

    def test_snippet_only_summary_needs_no_assessments_or_claims(self):
        agent = Mock(run=Mock(return_value=json.dumps({'summary': 'Reported result[1].'})))
        item = SummarizationService(agent).summarize(summary().task, [
            {'title': 'Search result', 'url': 'https://example.org', 'snippet': 'Reported result'}])
        self.assertEqual(item.summary, 'Reported result[1].')
        self.assertEqual(item.source_details[0]['read_depth'], 'search_snippet')
        self.assertNotIn('claims', item.source_details[0])
        self.assertEqual(agent.run.call_count, 1)

    def test_invalid_citation_does_not_trigger_a_second_summary_call(self):
        agent = Mock(run=Mock(return_value=json.dumps({'summary': 'Finding[99].'})))
        item = SummarizationService(agent).summarize(summary().task, [
            {'title': 'Source', 'url': 'https://example.org', 'snippet': 'Finding'}])
        self.assertEqual(item.summary, 'Finding[99].')
        self.assertEqual(agent.run.call_count, 1)

    def test_report_is_written_once_and_saved_without_a_status_banner(self):
        writer = Mock(run=Mock(return_value=GOOD))
        service = ReportingService(writer, self.notes)
        report = service.generate('topic', [summary()], 'run')
        self.assertEqual(writer.run.call_count, 1)
        self.assertTrue(report.startswith('# Report'))
        self.assertIn('42%', report)
        self.assertIn('"citation"', report)
        self.assertEqual(self.notes.load_report('run'), report)
        self.assertFalse((self.notes.runs_dir / 'run' / 'audit.json').exists())
        self.assertNotIn('audit', self.notes.load_json('run', 'report-state.json'))
        self.assertEqual(service.generate('topic', [summary()], 'run'), report)
        self.assertEqual(writer.run.call_count, 1)

    def test_previous_review_failure_reuses_paid_draft(self):
        self.notes.save_report_state('run', {
            'version': 1, 'signature': 'legacy', 'draft': GOOD,
            'audit': {'status': 'audit_unavailable', 'rounds': []},
        })
        writer = Mock(run=Mock(side_effect=AssertionError('must reuse draft')))
        report = ReportingService(writer, self.notes).generate('topic', [summary()], 'run')
        self.assertTrue(report.startswith('# Report'))
        writer.run.assert_not_called()
        self.assertNotIn('audit', self.notes.load_json('run', 'report-state.json'))

    def test_input_capacity_keeps_every_task_and_does_not_require_original_claims(self):
        items = []
        for task_id in range(1, 6):
            items.append(TaskSummary(task=TodoItem(id=task_id, title=f'Task {task_id}', query='q', intent=''),
                summary=('Long finding[1]. ' * 2000),
                sources=[f'https://example.org/{task_id}/{i}' for i in range(8)],
                source_titles=[f'Source {i}' for i in range(8)]))
        normalized = ReportingService.__new__(ReportingService)._renumber_citations(items)
        packed = compact_materials(normalized, max_chars=5000)
        payload = json.loads(packed)
        self.assertLessEqual(len(packed), 5000)
        self.assertEqual(len(payload['sections']), 5)
        self.assertTrue(all(section['summary'] for section in payload['sections']))
        self.assertLessEqual(len(payload['sources']), 15)
        self.assertNotIn('claims', packed)

    def test_empty_source_catalog_can_still_supply_a_summary(self):
        item = TaskSummary(task=summary().task, summary='A useful synthesis without source metadata.')
        payload = json.loads(compact_materials([(item, {})]))
        self.assertEqual(payload['sections'][0]['summary'], item.summary)
        self.assertEqual(payload['sources'], [])

    def test_old_env_switches_cannot_reenable_removed_checks(self):
        path = self.notes.workspace / 'legacy.env'
        path.write_text('REPORT_AUDIT_ENABLED=true\nREPORT_REQUIRE_PRIMARY_BODY=true\n'
                        'REPORT_QUALITY_MODE=full\nRESEARCH_GAP_ROUNDS=2\n', encoding='utf-8')
        config = Settings(_env_file=path)
        self.assertNotIn('report_audit_enabled', config.model_dump())
        self.assertNotIn('report_require_primary_body', config.model_dump())
        self.assertEqual(config.report_materials_max_chars, 160000)


class DirectResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.notes = NoteTool(directory.name)
        self.repo = ResearchRepository(str(Path(directory.name) / 'research.db'))
        self.rows = [{'title': 'Project page', 'url': 'https://example.org/project',
                      'snippet': 'The project describes an approach.', 'document_scope': 'webpage_body'}]
        self.search = SimpleNamespace(search_many=Mock(return_value=self.rows))
        self.summarizer_agent = Mock(run=Mock(return_value=json.dumps({'summary': 'An approach[1].'})))
        self.summarizer = SummarizationService(self.summarizer_agent)
        self.writer = Mock(run=Mock(return_value='# Report\n\nAn approach[1].'))
        self.reporter = ReportingService(self.writer, self.notes)

    async def finish(self, manager, run):
        job = manager._jobs.get(run)
        if job:
            await asyncio.wait_for(asyncio.shield(job), 5)
        return self.repo.get(run)

    async def test_academic_task_completes_with_webpage_or_search_snippet(self):
        task = TodoItem(id=1, title='Visual reasoning benchmark paper', query='visual reasoning benchmark',
                        verification_query='benchmark original paper', intent='research', required_methods=['MMMU'])
        manager = ResearchManager(self.repo, SimpleNamespace(plan=Mock(return_value=[task])),
            self.search, self.summarizer, self.reporter, self.notes)
        run = await manager.start('Visual reasoning')
        record = await self.finish(manager, run)
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['tasks'][0]['status'], 'completed')
        self.assertEqual(self.search.search_many.call_count, 1)
        self.assertEqual(self.summarizer_agent.run.call_count, 1)
        self.assertEqual(self.writer.run.call_count, 1)
        self.assertEqual(record['progress_text'], '研究完成！')

    async def test_exhausted_legacy_gap_state_can_resume_and_unlock_dependents(self):
        first = TodoItem(id=1, title='Benchmark', query='q1', intent='', status='failed')
        second = TodoItem(id=2, title='Comparison', query='q2', intent='', depends_on=[1], status='failed')
        old = TaskSummary(task=first, summary='Saved finding[1].', sources=[self.rows[0]['url']],
                          source_titles=['Project page'], source_details=[{'document_scope': 'webpage_body'}])
        run = 'legacy'
        self.repo.create(run, 'Visual reasoning')
        self.repo.record_event(run, 'error', {'message': 'old evidence gate'},
            {'status': 'error', 'tasks_json': [first.model_dump(), second.model_dump()]})
        self.repo.save_checkpoint(run, 1, results=self.rows)
        self.notes.task_progress(run, 1, {'query': first.query, 'results': self.rows,
            'summary': {**old.model_dump(), 'gaps': [{'category': 'primary_body_missing'}]},
            'gap_rounds': 2, 'gap_stopped': True})
        planning = SimpleNamespace(plan=Mock(), refine_dependent=Mock(side_effect=lambda task, _: task))
        manager = ResearchManager(self.repo, planning, self.search, self.summarizer, self.reporter, self.notes)
        await manager.resume(run)
        record = await self.finish(manager, run)
        self.assertEqual(record['status'], 'completed')
        self.assertTrue(all(task['status'] == 'completed' for task in record['tasks']))
        self.assertEqual(self.search.search_many.call_count, 1)  # only the dependent task
        self.assertEqual(self.summarizer_agent.run.call_count, 1)
        planning.plan.assert_not_called()
        self.assertEqual(planning.refine_dependent.call_count, 1)
        self.assertNotIn('gap_rounds', self.notes.task_progress(run, 1))
        self.assertEqual(self.repo.checkpoints(run)[1]['summary']['summary'], old.summary)

    async def test_failed_independent_task_no_longer_triggers_missing_evidence_gate(self):
        tasks = [TodoItem(id=i, title=f'Task {i}', query=f'q{i}', intent='') for i in (1, 2)]
        self.search.search_many.side_effect = lambda queries, **kw: self.rows if queries[0] == 'q1' else []
        manager = ResearchManager(self.repo, SimpleNamespace(plan=Mock(return_value=tasks)),
            self.search, self.summarizer, self.reporter, self.notes)
        run = await manager.start('Topic')
        record = await self.finish(manager, run)
        self.assertEqual(record['status'], 'completed')
        self.assertEqual([task['status'] for task in record['tasks']], ['completed', 'failed'])
        self.assertEqual(self.writer.run.call_count, 1)

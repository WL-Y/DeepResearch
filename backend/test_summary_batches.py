"""Offline tests for durable batching, provenance and sibling-task isolation."""
import asyncio
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.models import TaskSummary, TodoItem
from src.repository import ResearchRepository
from src.services.cost_control import OutputTruncated, ResearchLimit
from src.services.research import ResearchManager
from src.services.summarization import SummarizationService
from src.services.summary_batches import summarize_batches, SummaryBatchIncomplete
from src.tools.note import NoteTool


def records(n):
    return [{'url': f'https://example.org/{i}', 'title': f'Paper {i}', 'content': f'Evidence {i}'} for i in range(n)]


def summarized(task, rows, **kwargs):
    return TaskSummary(task=task, summary='\n'.join(f'Finding {r["title"]}[{i}].' for i, r in enumerate(rows, 1)),
        sources=[r['url'] for r in rows], source_titles=[r['title'] for r in rows],
        source_details=[{'excerpt': r['content']} for r in rows])


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.notes = NoteTool(self.temp.name)
        self.service = SummarizationService(Mock())
        self.service.summarize = Mock(side_effect=summarized)
        self.task = TodoItem(id=1, title='Task', query='papers', intent='research')

    def run_batches(self, rows, **kwargs):
        return summarize_batches(self.service, self.notes, 'run', self.task, rows, **kwargs)

    def test_four_source_batches_merge_citations_and_provenance(self):
        rows = records(9)
        result = self.run_batches(rows)
        self.assertEqual([len(c.args[1]) for c in self.service.summarize.call_args_list], [4, 4, 1])
        self.assertEqual(result.sources, [r['url'] for r in rows])
        self.assertIn('Paper 8[9]', result.summary)
        self.assertEqual(result.source_details[8]['excerpt'], 'Evidence 8')

    def test_successful_batches_reused_after_restart(self):
        rows = records(8)
        first = self.run_batches(rows)
        self.service.summarize.reset_mock()
        self.notes = NoteTool(self.temp.name)
        self.task.status = 'running'
        second = self.run_batches(rows)
        self.assertEqual(first.summary, second.summary)
        self.service.summarize.assert_not_called()

    def test_only_changed_batch_is_reprocessed(self):
        rows = records(8)
        self.run_batches(rows)
        self.service.summarize.reset_mock()
        rows[6]['content'] = 'Corrected evidence'
        result = self.run_batches(rows)
        self.assertEqual(self.service.summarize.call_count, 1)
        self.assertEqual(self.service.summarize.call_args.args[1], rows[4:])
        self.assertEqual(result.source_details[6]['excerpt'], 'Corrected evidence')

    def test_truncation_splits_only_affected_batch(self):
        rows = records(8)
        def analyze(task, batch, **kwargs):
            if batch[0]['url'].endswith('/4') and len(batch) == 4:
                raise OutputTruncated('partial JSON')
            return summarized(task, batch)
        self.service.summarize.side_effect = analyze
        result = self.run_batches(rows)
        self.assertEqual([len(c.args[1]) for c in self.service.summarize.call_args_list], [4, 4, 2, 2])
        self.assertEqual(result.sources, [r['url'] for r in rows])
        nodes = self.notes.summary_batches('run', 1)['batches'].values()
        self.assertTrue(any(n.get('partial_output') == 'partial JSON' for n in nodes))
        self.service.summarize.reset_mock()
        self.run_batches(rows)
        self.service.summarize.assert_not_called()

    def test_unsplittable_truncation_not_retried_on_resume(self):
        self.service.summarize.side_effect = OutputTruncated('half a source')
        for _ in range(2):
            with self.assertRaises(SummaryBatchIncomplete):
                self.run_batches(records(1))
        self.assertEqual(self.service.summarize.call_count, 1)
        entry = next(iter(self.notes.summary_batches('run', 1)['batches'].values()))
        self.assertEqual(entry['partial_output'], 'half a source')
        self.assertNotIn('summary', entry)

    def test_network_failure_resume_preserves_completed_batches(self):
        self.service.summarize.side_effect = [summarized(self.task, records(8)[:4]), RuntimeError('offline')]
        with self.assertRaises(RuntimeError):
            self.run_batches(records(8))
        self.service.summarize.reset_mock()
        self.service.summarize.side_effect = summarized
        result = self.run_batches(records(8))
        self.assertEqual(self.service.summarize.call_count, 1)
        self.assertEqual(len(result.sources), 8)

    def test_failed_batch_retry_has_persistent_bound(self):
        self.service.summarize.side_effect = RuntimeError('offline')
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.run_batches(records(1))
        with self.assertRaises(SummaryBatchIncomplete):
            self.run_batches(records(1))
        self.assertEqual(self.service.summarize.call_count, 2)

    def test_budget_exhaustion_does_not_split_or_call_again(self):
        self.service.summarize.side_effect = ResearchLimit('total budget')
        with self.assertRaisesRegex(ResearchLimit, 'total budget'):
            self.run_batches(records(8))
        self.assertEqual(self.service.summarize.call_count, 1)
        self.assertEqual(len(self.notes.summary_batches('run', 1)['batches']), 1)

    def test_changed_task_or_inventory_is_not_silently_reused(self):
        self.run_batches(records(1))
        self.task.intent = 'different research question'
        self.run_batches(records(1))
        self.run_batches(records(1), prior_inventory=[{'title': 'Earlier source', 'url': 'https://earlier.test'}])
        self.assertEqual(self.service.summarize.call_count, 3)

    def test_incremental_merge_batches_only_new_evidence(self):
        rows = records(9)
        first = self.run_batches(rows[:4])
        self.service.summarize.reset_mock()
        merged = self.service.update_summary(self.task, rows[:4], first, rows,
            summarize=lambda task, batch, **kwargs: self.run_batches(batch, **kwargs))
        self.assertEqual([len(c.args[1]) for c in self.service.summarize.call_args_list], [4, 1])
        self.assertEqual(merged.sources, [r['url'] for r in rows])
        self.assertIn('Paper 8[9]', merged.summary)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_task_does_not_cancel_successful_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = ResearchRepository(str(Path(directory, 'research.db')))
            notes = NoteTool(directory)
            tasks = [TodoItem(id=i, title=f'Task {i}', query=f'query {i}', intent='research',
                depends_on=[1, 2] if i == 3 else []) for i in (1, 2, 3)]
            failed = threading.Event()
            service = SummarizationService(Mock())
            def analyze(task, rows, **kwargs):
                if task.id == 2:
                    failed.set()
                    raise OutputTruncated('partial JSON')
                self.assertTrue(failed.wait(3))
                return summarized(task, rows)
            service.summarize = Mock(side_effect=analyze)
            search = SimpleNamespace(search=Mock(return_value=records(4)))
            manager = ResearchManager(repo, SimpleNamespace(plan=Mock(return_value=tasks)), search,
                service, SimpleNamespace(generate=Mock()), notes)
            run = await manager.start('topic')
            await asyncio.wait_for(manager._jobs[run], 5)
            rec = repo.get(run)
            self.assertEqual(rec['status'], 'error')
            self.assertEqual([t['status'] for t in rec['tasks']], ['completed', 'failed', 'failed'])
            self.assertTrue(repo.checkpoints(run)[1]['summary'])
            self.assertTrue(notes.summary_batches(run, 1))
            successful = copy.deepcopy(repo.checkpoints(run)[1]['summary'])
            calls = service.summarize.call_count
            await manager.resume(run)
            await asyncio.wait_for(manager._jobs[run], 5)
            self.assertEqual(service.summarize.call_count, calls)
            self.assertEqual(repo.checkpoints(run)[1]['summary'], successful)
            self.assertEqual(search.search.call_count, 2)

    async def test_worker_success_checkpoint_survives_user_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = ResearchRepository(str(Path(directory, 'research.db')))
            notes = NoteTool(directory)
            started, release = threading.Event(), threading.Event()
            task = TodoItem(id=1, title='Task', query='query', intent='research')
            service = SummarizationService(Mock())
            def analyze(t, rows, **kwargs):
                started.set()
                release.wait(3)
                return summarized(t, rows)
            service.summarize = Mock(side_effect=analyze)
            manager = ResearchManager(repo, SimpleNamespace(plan=Mock(return_value=[task])),
                SimpleNamespace(search=Mock(return_value=records(2))), service,
                SimpleNamespace(generate=Mock(return_value='# Report')), notes)
            run = await manager.start('topic')
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                await manager.cancel(run)
            finally:
                release.set()
            workers = list(manager._inflight.get(run, set()))
            await asyncio.gather(*workers)
            self.assertTrue(notes.summary_batches(run, 1))
            await manager.resume(run)
            await asyncio.wait_for(manager._jobs[run], 5)
            self.assertEqual(repo.get(run)['status'], 'completed')
            self.assertEqual(service.summarize.call_count, 1)


if __name__ == '__main__':
    unittest.main()

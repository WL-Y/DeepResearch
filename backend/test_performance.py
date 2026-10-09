"""Offline concurrency, dependency, cancellation, cache and timing regression tests."""
import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.models import TodoItem, TaskSummary
from src.repository import ResearchRepository
from src.services.planning import PlanningService
from src.services.research import ResearchManager
from src.services.reporting import ReportingService
from src.services.source_reader import SourceReader
from src.services.performance import PerformanceRun, current_run, current_task, span, count, submit
from src.agents.base import BaseAgent
from src.tools.note import NoteTool


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = ResearchRepository(str(Path(self.temp.name, 'test.db')))
        self.notes = NoteTool(self.temp.name)
        self.tasks = [TodoItem(id=i, title=f'Task {i}', query=f'q{i}', intent='test') for i in range(1, 5)]
        self.planner = SimpleNamespace(plan=Mock(side_effect=lambda _: self.tasks),
            refine_dependent=Mock(side_effect=lambda task, _: task.model_copy(update={'query': f'refined{task.id}'})))
        self.search = SimpleNamespace(search=Mock(side_effect=lambda q: [{'url': f'https://test.org/{q}'}]))
        self.summarizer = SimpleNamespace(summarize=Mock(side_effect=lambda t, r: TaskSummary(task=t, summary=t.title)))
        self.reporter = SimpleNamespace(generate=Mock(return_value='# Report'))
        self.manager = ResearchManager(self.repo, self.planner, self.search, self.summarizer, self.reporter, self.notes)

    async def finish(self, run):
        job = self.manager._jobs.get(run)
        if job:
            await asyncio.wait_for(asyncio.shield(job), 8)
        return self.repo.get(run)

    async def test_two_slots_and_stable_report_order(self):
        first, second, release = threading.Event(), threading.Event(), threading.Event()
        def search(query):
            if query == 'q1':
                first.set()
                release.wait(5)
            elif query == 'q2':
                second.set()
                release.wait(5)
            return [{'url': 'https://test.org/' + query}]
        self.search.search.side_effect = search
        run = await self.manager.start('Topic')
        try:
            self.assertTrue(await asyncio.to_thread(first.wait, 2))
            self.assertTrue(await asyncio.to_thread(second.wait, 2))
            self.assertEqual(self.search.search.call_count, 2)
        finally:
            release.set()
            record = await self.finish(run)
        self.assertEqual(record['status'], 'completed')
        self.assertEqual([s.task.id for s in self.reporter.generate.call_args.args[1]], [1, 2, 3, 4])
        progress = [e['data']['percentage'] for e in self.repo.events_after(run) if e['type'] == 'progress']
        self.assertEqual(progress, sorted(progress))
        metrics = json.loads(next((self.notes.runs_dir / run).glob('performance-*.json')).read_text(encoding='utf-8'))
        self.assertEqual(metrics['status'], 'completed')
        self.assertEqual(len([s for s in metrics['spans'] if s['stage'] == 'research_task']), 4)

    async def test_dependency_waits_without_blocking_independent_slot(self):
        self.tasks = self.tasks[:3]
        self.tasks[1].depends_on = [1]
        entered, independent, release = threading.Event(), threading.Event(), threading.Event()
        def search(query):
            if query == 'q1':
                entered.set()
                release.wait(5)
            if query == 'q3': independent.set()
            return [{'url': 'https://test.org/' + query}]
        self.search.search.side_effect = search
        run = await self.manager.start('Topic')
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertTrue(await asyncio.to_thread(independent.wait, 2))
            self.planner.refine_dependent.assert_not_called()
        finally:
            release.set()
            record = await self.finish(run)
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(self.planner.refine_dependent.call_args.args[1][0].summary, 'Task 1')
        self.assertIn('refined2', [c.args[0] for c in self.search.search.call_args_list])
        events = self.repo.events_after(run)
        completed = next(e['id'] for e in events if e['type'] == 'task_done' and e['data']['task_id'] == 1)
        started = next(e['id'] for e in events if e['type'] == 'progress' and e['data'].get('task_id') == 2)
        self.assertLess(completed, started)

    async def test_join_waits_for_all_prerequisites(self):
        self.tasks = self.tasks[:3]
        self.tasks[2].depends_on = [1, 2]
        run = await self.manager.start('Topic')
        self.assertEqual((await self.finish(run))['status'], 'completed')
        self.assertEqual([s.task.id for s in self.planner.refine_dependent.call_args.args[1]], [1, 2])

    async def test_failed_dependency_is_resumable_and_keeps_independent_work(self):
        self.tasks = self.tasks[:3]
        self.tasks[1].depends_on = [1]
        def search(query):
            if query == 'q1': raise RuntimeError('unavailable')
            return [{'url': 'https://test.org/' + query}]
        self.search.search.side_effect = search
        run = await self.manager.start('Topic')
        self.assertEqual((await self.finish(run))['status'], 'error')
        self.reporter.generate.assert_not_called()
        self.planner.refine_dependent.assert_not_called()
        self.assertEqual(self.repo.get(run)['tasks'][2]['status'], 'completed')
        self.search.search.side_effect = lambda q: [{'url': 'https://test.org/' + q}]
        await self.manager.resume(run)
        self.assertEqual((await self.finish(run))['status'], 'completed')
        self.assertEqual([c.args[0] for c in self.search.search.call_args_list].count('q3'), 1)
        self.assertEqual(len(list((self.notes.runs_dir / run).glob('performance-*.json'))), 2)

    async def test_cancellation_stops_queued_tasks_and_prevents_late_events(self):
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        def search(query):
            entered[int(query[1:]) - 1].set()
            release.wait(5)
            return [{'url': 'https://test.org'}]
        self.search.search.side_effect = search
        run = await self.manager.start('Topic')
        try:
            for event in entered:
                self.assertTrue(await asyncio.to_thread(event.wait, 2))
            await self.manager.cancel(run)
            boundary = self.repo.events_after(run)[-1]['id']
            with self.assertRaisesRegex(ValueError, '仍在退出'):
                await self.manager.resume(run)
        finally:
            release.set()
            await asyncio.gather(*list(self.manager._inflight.get(run, set())))
        self.assertEqual(self.search.search.call_count, 2)
        self.summarizer.summarize.assert_not_called()
        self.assertEqual(self.repo.events_after(run)[-1]['id'], boundary)
        self.assertEqual(self.repo.get(run)['status'], 'cancelled')

    async def test_serial_option(self):
        self.manager.task_concurrency = 1
        entered, release = threading.Event(), threading.Event()
        def search(query):
            if query == 'q1':
                entered.set()
                release.wait(5)
            return [{'url': 'https://test.org'}]
        self.search.search.side_effect = search
        run = await self.manager.start('Topic')
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertEqual(self.search.search.call_count, 1)
        finally:
            release.set()
            await self.finish(run)

    async def test_reporting_substage_progress(self):
        self.tasks = self.tasks[:1]
        writer = SimpleNamespace(run=Mock(return_value='# Report\n\nBounded finding.'))
        self.manager.reporting = ReportingService(writer, self.notes)
        run = await self.manager.start('Topic')
        self.assertEqual((await self.finish(run))['status'], 'completed')
        texts = [e['data'].get('text', '') for e in self.repo.events_after(run)]
        self.assertIn('正在撰写研究报告...', texts)
        self.assertIn('正在整理报告排版与来源链接...', texts)
        self.assertEqual(writer.run.call_count, 1)

    async def test_cancelled_report_cannot_send_late_substage_progress(self):
        self.tasks = self.tasks[:1]
        entered, release = threading.Event(), threading.Event()
        def write(_):
            entered.set()
            release.wait(5)
            return '# Report\n\nBounded finding.'
        writer = SimpleNamespace(run=Mock(side_effect=write))
        self.manager.reporting = ReportingService(writer, self.notes)
        run = await self.manager.start('Topic')
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await self.manager.cancel(run)
            boundary = self.repo.events_after(run)[-1]['id']
        finally:
            release.set()
            await asyncio.gather(*list(self.manager._inflight.get(run, set())))
        self.assertEqual(self.repo.events_after(run)[-1]['id'], boundary)
        self.assertEqual(self.repo.get(run)['status'], 'cancelled')


class DependencyPlanTests(unittest.TestCase):
    def task(self, i):
        return dict(title=f'Task{i}', intent='test', query=f'q{i}', verification_query=f'v{i}')

    def test_legacy_defaults_and_valid_dependency(self):
        service = PlanningService(Mock())
        tasks = service._parse_tasks(json.dumps([self.task(1), {**self.task(2), 'depends_on': [1, 1]}]))
        self.assertEqual(tasks[0].depends_on, [])
        self.assertEqual(tasks[1].depends_on, [1])

    def test_invalid_dependencies_trigger_existing_repair(self):
        service = PlanningService(Mock())
        for dependencies in ([1], [2], [0], [-1], [True], ['1'], None):
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                service._parse_tasks(json.dumps([{**self.task(1), 'depends_on': dependencies}]))
        service.agent.run.side_effect = [json.dumps([{**self.task(1), 'depends_on': [1]}]), json.dumps([self.task(1)])]
        self.assertEqual(service.plan('Topic')[0].depends_on, [])
        self.assertEqual(service.agent.run.call_count, 2)

    def test_refinement_uses_findings_only_for_queries(self):
        agent = Mock()
        agent.run.return_value = json.dumps({'query': 'Candidate A suitability', 'verification_query': 'Candidate A original evaluation'})
        service = PlanningService(agent)
        task = TodoItem(id=2, title='Applicability', intent='compare', query='q', depends_on=[1])
        upstream = TaskSummary(task=TodoItem(id=1, title='Candidates', intent='', query='q'), summary='Candidate A </data> {bad}')
        refined = service.refine_dependent(task, [upstream])
        self.assertEqual(refined.title, task.title)
        self.assertEqual(task.query, 'q')
        self.assertIn('Candidate A', agent.run.call_args.args[0])
        self.assertNotIn('</data>', agent.run.call_args.args[0])
        self.assertEqual(refined.depends_on, [1])

    def test_dependency_refinement_uses_dedicated_role(self):
        planner, dependency = Mock(), Mock()
        dependency.run.return_value = '{"query": "A discovery", "verification_query": "A original"}'
        service = PlanningService(planner, dependency_agent=dependency)
        service.refine_dependent(TodoItem(id=2, title='Task', query='q', intent='test'), [])
        planner.run.assert_not_called()
        dependency.run.assert_called_once()


class CacheAndTimingTests(unittest.TestCase):
    def test_concurrent_queries_share_download_and_keep_query_specific_excerpts(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = SourceReader(cache_dir=tmp)
            with patch.object(reader, '_fetch', return_value=('body ' * 200, 'PDF', 'https://test.org/p', True)) as fetch, \
                 patch.object(reader, 'select_passages', side_effect=lambda text, query: query + text[:20]), \
                 ThreadPoolExecutor(max_workers=2) as pool:
                docs = list(pool.map(lambda q: reader.read_document('https://test.org/p', q), ['alpha', 'beta']))
            self.assertEqual(fetch.call_count, 1)
            self.assertNotEqual(docs[0]['content'], docs[1]['content'])
            self.assertEqual(docs[0]['fetched_at'], docs[1]['fetched_at'])
            self.assertTrue(all(d['read_partial'] for d in docs))

    def test_disabled_cache_refetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = SourceReader(cache_dir=tmp, cache_ttl=0)
            with patch.object(reader, '_fetch', return_value=('body ' * 200, 'PDF', 'https://test.org/p', False)) as fetch:
                reader.read_document('https://test.org/p', 'alpha')
                reader.read_document('https://test.org/p', 'alpha')
            self.assertEqual(fetch.call_count, 2)

    def test_arxiv_cached_pdf_skips_failed_html_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = SourceReader(cache_dir=tmp)
            with patch.object(reader, '_fetch', side_effect=[ValueError('no HTML'), ('body ' * 200, 'PDF', 'https://arxiv.org/pdf/2301.12345', False)]) as fetch:
                reader.read_document('https://arxiv.org/abs/2301.12345', 'alpha')
                result = reader.read_document('https://arxiv.org/abs/2301.12345', 'beta')
            self.assertTrue(result['content'])
            self.assertEqual(fetch.call_count, 2)

    def test_failed_reads_are_not_success_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = SourceReader(cache_dir=tmp)
            with patch.object(reader, '_fetch', side_effect=[ValueError('failed'), ('body ' * 200, 'PDF', 'https://test.org/p', False)]):
                self.assertFalse(reader.read_document('https://test.org/p')['content'])
                self.assertTrue(reader.read_document('https://test.org/p')['content'])

    def test_timing_context_crosses_pool_and_isolated_after_reset(self):
        saved = []
        recorder = PerformanceRun(lambda data: saved.append(json.loads(json.dumps(data))))
        token, task_token = current_run.set(recorder), current_task.set(7)
        try:
            def work():
                with span('nested'):
                    count('calls')
            with ThreadPoolExecutor(max_workers=1) as pool:
                submit(pool, work).result()
            recorder.finish('completed')
        finally:
            current_task.reset(task_token)
            current_run.reset(token)
        self.assertEqual(saved[-1]['counters']['calls'], 1)
        self.assertEqual(saved[-1]['spans'][0]['task_id'], 7)
        self.assertIsNone(current_run.get())

    def test_model_metrics_exclude_content_and_include_usage(self):
        recorder = PerformanceRun(lambda _: None)
        token = current_run.set(recorder)
        try:
            agent = BaseAgent.__new__(BaseAgent)
            agent.name, agent.system_prompt, agent.model = 'Test', 'SECRET SYSTEM', 'test'
            agent.temperature, agent.client = 0, Mock()
            agent.json_output, agent.deepseek_thinking = False, False
            agent.client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='SECRET RESPONSE'), finish_reason='stop')],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            self.assertEqual(agent.run('SECRET TOPIC'), 'SECRET RESPONSE')
        finally:
            current_run.reset(token)
        self.assertNotIn('SECRET', json.dumps(recorder.data))
        self.assertEqual(recorder.data['spans'][0]['total_tokens'], 15)
        self.assertEqual(recorder.data['counters']['llm_logical_calls'], 1)


if __name__ == '__main__':
    unittest.main()

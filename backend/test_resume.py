"""Offline resume tests: no model calls or real research records."""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.models import TaskSummary, TodoItem
from src.repository import ResearchRepository
from src.services.research import ResearchManager
from src.tools.note import NoteTool


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = ResearchRepository(str(Path(self.temp.name, 'research.db')))
        self.notes = NoteTool(self.temp.name)
        self.planning = SimpleNamespace(plan=Mock(side_effect=lambda _: [
            TodoItem(id=i, title=f'Task {i}', intent='', query=f'query{i}') for i in (1, 2)]))
        self.search = SimpleNamespace(search=Mock(return_value=[{'title': 'Source', 'url': 'https://example.org', 'snippet': 'Evidence'}]))
        self.summary = SimpleNamespace(summarize=Mock(side_effect=lambda task, results: TaskSummary(
            task=task, summary=f'Evidence for {task.title}[1].', sources=['https://example.org'],
            source_titles=['Source'], source_details=[{'kind': 'primary'}])))
        self.report = SimpleNamespace(generate=Mock(return_value='# Finished'))
        self.manager = self.make_manager()

    def make_manager(self):
        return ResearchManager(self.repo, self.planning, self.search, self.summary, self.report, self.notes)

    async def finish(self, run):
        job = self.manager._jobs.get(run)
        if job:
            await asyncio.wait_for(asyncio.shield(job), 5)
        return self.repo.get(run)

    async def test_reporting_failure_reuses_complete_summaries_after_restart(self):
        self.report.generate.side_effect = [RuntimeError('model unavailable'), '# Finished']
        run = await self.manager.start('Topic')
        self.assertEqual((await self.finish(run))['status'], 'error')
        self.repo = ResearchRepository(str(self.repo.database_path))
        self.manager = self.make_manager()
        response = await self.manager.resume(run)
        self.assertEqual(response['research_id'], run)
        record = await self.finish(run)
        self.assertEqual(record['status'], 'completed')
        self.assertIsNone(record['error'])
        self.assertEqual(len(self.repo.list_recent()), 1)
        self.assertEqual(self.planning.plan.call_count, 1)
        self.assertEqual(self.search.search.call_count, 2)
        self.assertEqual(self.summary.summarize.call_count, 2)
        restored = self.report.generate.call_args.args[1]
        self.assertEqual(restored[0].source_details, [{'kind': 'primary'}])
        events = [json.loads(message.decode().split('data: ')[1]) async for message in self.manager.stream(run)]
        self.assertEqual(events[0]['type'], 'resumed')
        self.assertNotIn('error', [e['type'] for e in events])
        self.assertEqual(events[-1]['stage'], 'completed')

    async def test_summary_failure_reuses_retrieval(self):
        self.summary.summarize.side_effect = RuntimeError('summary unavailable')
        run = await self.manager.start('Topic')
        self.assertEqual((await self.finish(run))['status'], 'error')
        self.summary.summarize.side_effect = lambda task, _: TaskSummary(task=task, summary='Recovered')
        await self.manager.resume(run)
        self.assertEqual((await self.finish(run))['status'], 'completed')
        self.assertEqual(self.search.search.call_count, 2)
        self.assertEqual(self.planning.plan.call_count, 1)

    async def test_failed_task_retries_but_completed_task_is_reused(self):
        self.search.search.side_effect = [[{'title': 'A', 'url': 'https://a.test'}], RuntimeError('search failed')]
        self.report.generate.side_effect = [RuntimeError('report failed'), '# Finished']
        run = await self.manager.start('Topic')
        await self.finish(run)
        self.search.search.side_effect = None
        await self.manager.resume(run)
        self.assertEqual((await self.finish(run))['status'], 'completed')
        self.assertEqual(self.search.search.call_count, 3)
        self.assertEqual(self.search.search.call_args.args[0], 'query2')
        self.assertEqual(self.summary.summarize.call_count, 2)

    async def test_planning_failure_can_retry_without_creating_new_run(self):
        self.planning.plan.side_effect = [RuntimeError('planning failed'), [TodoItem(id=1, title='A', query='q', intent='')]]
        run = await self.manager.start('Topic')
        await self.finish(run)
        await self.manager.resume(run)
        self.assertEqual((await self.finish(run))['status'], 'completed')
        self.assertEqual(len(self.repo.list_recent()), 1)

    async def test_old_completed_notes_are_reused_but_missing_notes_are_rebuilt(self):
        run = 'legacy'
        self.repo.create(run, 'Topic')
        tasks = self.planning.plan('Topic')
        for task in tasks: task.status = 'completed'
        self.repo.record_event(run, 'error', {'message': 'old error'},
                               {'status': 'error', 'tasks_json': [t.model_dump() for t in tasks]})
        self.notes.save_note(run, 1, 'Task 1', 'Original evidence[1].\n\n## 来源记录与读取状态\n\n'
            + json.dumps({'sources': [{'url': 'https://original.test', 'title': 'Original', 'kind': 'primary'}]}))
        await self.manager.resume(run)
        self.assertEqual((await self.finish(run))['status'], 'completed')
        self.assertEqual(self.search.search.call_count, 1)
        self.assertEqual(self.search.search.call_args.args, ('query2',))
        self.assertEqual(self.report.generate.call_args.args[1][0].sources, ['https://original.test'])

    async def test_duplicate_resume_does_not_launch_two_jobs(self):
        self.repo.create('run', 'Topic')
        self.repo.mark_interrupted_runs()
        responses = await asyncio.gather(self.manager.resume('run'), self.manager.resume('run'))
        self.assertEqual([r['research_id'] for r in responses], ['run', 'run'])
        await self.finish('run')
        self.assertEqual(self.planning.plan.call_count, 1)
        self.assertEqual(len([e for e in self.repo.events_after('run') if e['type'] == 'resumed']), 1)

    async def test_cancel_waits_for_old_worker_before_resume(self):
        entered, release = threading.Event(), threading.Event()
        def blocking_search(_):
            entered.set()
            release.wait(5)
            return [{'url': 'https://example.org'}]
        self.search.search.side_effect = blocking_search
        run = await self.manager.start('Topic')
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await self.manager.cancel(run)
            self.assertEqual(self.repo.get(run)['status'], 'cancelled')
            with self.assertRaisesRegex(ValueError, '仍在退出'):
                await self.manager.resume(run)
        finally:
            release.set()
            await asyncio.gather(*list(self.manager._inflight.get(run, set())))
        self.search.search.side_effect = None
        await self.manager.resume(run)
        self.assertEqual((await self.finish(run))['status'], 'completed')

    async def test_completed_and_missing_runs_cannot_resume(self):
        with self.assertRaises(KeyError):
            await self.manager.resume('missing')
        run = await self.manager.start('Topic')
        await self.finish(run)
        with self.assertRaises(ValueError):
            await self.manager.resume(run)

    async def test_checkpoint_survives_restart_and_is_removed_with_its_run(self):
        self.repo.create('run', 'Topic')
        self.repo.save_checkpoint('run', 1, results=[{'url': 'https://example.org'}])
        self.repo.mark_interrupted_runs()
        self.assertIn('继续研究', self.repo.get('run')['error'])
        self.assertEqual(self.repo.checkpoints('run')[1]['results'][0]['url'], 'https://example.org')
        self.repo.delete('run')
        self.assertEqual(self.repo.checkpoints('run'), {})


class ResumeApiTests(unittest.TestCase):
    def setUp(self):
        # Do not enter the real application's lifespan or run its actual jobs.
        from src import main
        from fastapi.testclient import TestClient
        self.main = main
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_removed_followup_endpoint_cannot_start_search_or_model_calls(self):
        with patch.object(self.main, 'planning_svc') as planning, \
             patch.object(self.main, 'search_svc') as search, \
             patch.object(self.main, 'summarization_svc') as summarization, \
             patch.object(self.main, 'repository') as repository:
            response = self.client.post('/api/research/followup', json={
                'research_id': 'run', 'question': 'More details?',
            })
            self.assertIn(response.status_code, (404, 405))
            self.assertNotIn('/api/research/followup', self.main.app.openapi()['paths'])
            for service in (planning, search, summarization, repository):
                self.assertEqual(service.mock_calls, [])

    def test_resume_endpoint_maps_success_missing_and_invalid_state(self):
        manager = SimpleNamespace(resume=AsyncMock(return_value={'research_id': 'run', 'status': 'resuming'}))
        with patch.object(self.main, 'research_manager', manager):
            response = self.client.post('/api/research/run/resume')
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()['research_id'], 'run')
            manager.resume.side_effect = KeyError('missing')
            self.assertEqual(self.client.post('/api/research/missing/resume').status_code, 404)
            manager.resume.side_effect = ValueError('只有中断的任务可以继续')
            response = self.client.post('/api/research/run/resume')
            self.assertEqual(response.status_code, 409)
            self.assertIn('只有中断', response.json()['detail'])

    def test_event_endpoint_uses_snapshot_cursor_and_latest_reconnect_header(self):
        cursors = []
        async def stream(run, after):
            cursors.append(after)
            yield b'data: {"type":"progress","stage":"completed"}\n\n'
        with patch.object(self.main, 'repository', SimpleNamespace(get=Mock(return_value={'id': 'run'}))), \
             patch.object(self.main, 'research_manager', SimpleNamespace(stream=stream)):
            self.assertEqual(self.client.get('/api/research/run/events?after=20', headers={'Last-Event-ID': '25'}).status_code, 200)
            self.client.get('/api/research/run/events?after=20', headers={'Last-Event-ID': 'bad'})
            self.assertEqual(cursors, [25, 20])
            self.assertEqual(self.client.get('/api/research/run/events?after=-1').status_code, 422)


if __name__ == '__main__': unittest.main()

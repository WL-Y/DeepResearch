"""Optional search-query refinement must not break the dependency chain."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.models import TodoItem, TaskSummary
from src.repository import ResearchRepository
from src.services.planning import PlanningService, DependencyQueryError
from src.services.research import ResearchManager
from src.services.cost_control import ResearchLimit
from src.tools.note import NoteTool
from test_core import SequenceAgent


def task(number=2):
    return TodoItem(id=number, title=f'Task {number}', intent='research',
        query=f'original query {number}', verification_query=f'original verification {number}',
        depends_on=[number - 1] if number > 1 else [])


class QueryValidationTests(unittest.TestCase):
    def test_whitespace_normalizes_without_extra_model_call(self):
        agent = SequenceAgent([json.dumps({'query': '  Dreamer\nrobot\tcontrol  ',
                                          'verification_query': ' Dreamer\r\noriginal paper '})])
        result = PlanningService(agent).refine_dependent(task(), [])
        self.assertEqual(result.query, 'Dreamer robot control')
        self.assertEqual(result.verification_query, 'Dreamer original paper')
        self.assertEqual(len(agent.prompts), 1)

    def test_validation_reports_field_and_actual_length(self):
        with self.assertRaisesRegex(DependencyQueryError, 'query 长度为 301'):
            PlanningService.normalize_queries({'query': 'x' * 301, 'verification_query': 'valid'})

    def test_rejects_invalid_empty_and_duplicate_pairs(self):
        for payload in [[], {'query': 'a'}, {'query': 2, 'verification_query': 'b'},
                        {'query': '  ', 'verification_query': 'b'},
                        {'query': 'A\nB', 'verification_query': 'a b'}]:
            with self.subTest(payload=payload), self.assertRaises(DependencyQueryError):
                PlanningService.normalize_queries(payload)

    def test_invalid_json_has_recoverable_error_type(self):
        agent = SequenceAgent(['not json'])
        with self.assertRaises(DependencyQueryError):
            PlanningService(agent).refine_dependent(task(), [])
        self.assertEqual(len(agent.prompts), 1)

    def test_limit_checked_after_normalizing_whitespace(self):
        result = PlanningService.normalize_queries({'query': 'a' + ' ' * 400 + 'b',
                                                     'verification_query': 'original'})
        self.assertEqual(result['query'], 'a b')


class DependencyFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ResearchRepository(str(Path(self.tmp.name, 'research.db')))
        self.agent = SequenceAgent([])
        self.planning = PlanningService(self.agent)
        self.planning.plan = Mock(return_value=[task(i) for i in (1, 2, 3)])
        self.search = SimpleNamespace(search=Mock(return_value=[{'url': 'https://example.org', 'snippet': 'evidence'}]))
        self.summary = SimpleNamespace(summarize=Mock(side_effect=lambda task, _: TaskSummary(task=task, summary='evidence')))
        self.report = SimpleNamespace(generate=Mock(return_value='# Finished'))
        self.manager = ResearchManager(self.repo, self.planning, self.search, self.summary,
                                       self.report, NoteTool(self.tmp.name))

    async def run_research(self):
        run = await self.manager.start('Topic')
        await asyncio.wait_for(asyncio.shield(self.manager._jobs[run]), 5)
        return run, self.repo.get(run)

    async def test_invalid_optimization_falls_back_and_dependents_finish(self):
        self.planning.dependency_agent = SequenceAgent([
            json.dumps({'query': 'x' * 301, 'verification_query': 'valid'}), 'not JSON'])
        run, record = await self.run_research()
        self.assertEqual(record['status'], 'completed')
        self.assertTrue(all(t['status'] == 'completed' for t in record['tasks']))
        self.assertEqual([c.args[0] for c in self.search.search.call_args_list],
                         ['original query 1', 'original query 2', 'original query 3'])
        events = [e for e in self.repo.events_after(run) if e['type'] == 'query_refinement_fallback']
        self.assertEqual(len(events), 2)
        self.assertIn('301', events[0]['data']['reason'])
        self.assertEqual(len(self.planning.dependency_agent.prompts), 2)
        self.report.generate.assert_called_once()

    async def test_budget_errors_do_not_fall_back_and_bypass_limits(self):
        self.planning.refine_dependent = Mock(side_effect=ResearchLimit('test quota reached'))
        run, record = await self.run_research()
        self.assertEqual(record['status'], 'error')
        self.assertIn('test quota reached', record['error'])
        self.assertEqual(self.search.search.call_count, 1)
        self.assertFalse(any(e['type'] == 'query_refinement_fallback' for e in self.repo.events_after(run)))
        self.report.generate.assert_not_called()

    async def test_invalid_original_query_is_not_silently_used(self):
        self.planning.plan.return_value[1].query = ' '
        self.planning.refine_dependent = Mock(side_effect=DependencyQueryError('bad optimization'))
        run, record = await self.run_research()
        self.assertEqual(record['status'], 'error')
        self.assertEqual(self.search.search.call_count, 1)
        self.assertFalse(any(e['type'] == 'query_refinement_fallback' for e in self.repo.events_after(run)))


if __name__ == '__main__':
    unittest.main()

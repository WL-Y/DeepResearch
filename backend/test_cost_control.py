"""Cost regressions: offline mocks only, never contact a model provider."""
import json
import re
import asyncio
import tempfile
import unittest
import openai
import httpx
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.agents.base import BaseAgent
from src.models import TodoItem
from src.services.cost_control import ResearchBudget, ResearchLimit, current_budget
from src.services.reporting import ReportingService
from src.services.summarization import SummarizationService
from src.tools.note import NoteTool
from src.repository import ResearchRepository
from src.services.research import ResearchManager
from test_core import SequenceAgent
from test_support import summary, GOOD


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.notes = NoteTool(self.temp.name)

    def test_parallel_reservations_cannot_overspend(self):
        budget = ResearchBudget(self.notes, 'run', token_limit=1800)
        def reserve(_):
            try: return budget.reserve('中文' * 30, 300)
            except ResearchLimit: return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            reservations = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(bool(r) for r in reservations), 1)
        self.assertLessEqual(budget.data['used'], 1800)

    def test_report_stage_has_independent_token_and_call_limits(self):
        budget = ResearchBudget(
            self.notes, 'run', token_limit=10000, call_limit=10,
            stage_token_limits={'report': 3000},
            stage_call_limits={'report': 1},
        )
        key = budget.reserve('报告材料' * 10, 300, stage='report')
        budget.settle(key, 200)
        with self.assertRaises(ResearchLimit):
            budget.reserve('再次写作', 300, stage='report')
        # A non-report call still has access to the remaining global budget.
        key = budget.reserve('规划', 300, stage='planner')
        budget.settle(key, 100)
        self.assertEqual(budget.data['stage_calls']['report'], 1)

    def test_resume_preserves_usage_and_call_limit(self):
        first = ResearchBudget(self.notes, 'run', call_limit=1)
        key = first.reserve('test', 300)
        first.settle(key, 100)
        second = ResearchBudget(self.notes, 'run', call_limit=1)
        self.assertEqual(second.data['used'], 100)
        with self.assertRaises(ResearchLimit): second.reserve('again', 300)

    def test_unknown_usage_and_crash_keep_reservations(self):
        first = ResearchBudget(self.notes, 'run')
        key = first.reserve('test', 300)
        reserved = first.data['used']
        first.settle(key, None)
        self.assertEqual(first.data['used'], reserved)
        key = first.reserve('crash', 300)
        restarted = ResearchBudget(self.notes, 'run')
        self.assertIn(key, restarted.data['reservations'])
        self.assertEqual(restarted.data['used'], first.data['used'])

    def test_release_removes_token_and_call_reservation(self):
        budget = ResearchBudget(self.notes, 'run')
        key = budget.reserve('test', 300, stage='planner')
        budget.release(key)
        self.assertEqual(budget.data['used'], 0)
        self.assertEqual(budget.data['calls'], 0)
        self.assertEqual(budget.data['stage_used']['planner'], 0)
        self.assertEqual(budget.data['stage_calls']['planner'], 0)
        self.assertNotIn(key, budget.data['reservations'])

    def test_cancel_blocks_subsequent_calls_but_allows_settlement(self):
        budget = ResearchBudget(self.notes, 'run')
        key = budget.reserve('first', 300)
        budget.stop()
        with self.assertRaises(ResearchLimit): budget.reserve('second', 300)
        budget.settle(key, 50)
        self.assertEqual(budget.data['used'], 50)

    def test_legacy_usage_is_not_reset(self):
        self.notes.save_performance('run', 'old', {'counters': {'llm_logical_calls': 2},
            'spans': [{'stage': 'llm.Reporter', 'total_tokens': 1700000}]})
        budget = ResearchBudget(self.notes, 'run')
        self.assertGreaterEqual(budget.data['used'], 1700000)
        with self.assertRaises(ResearchLimit): budget.reserve('continue', 300)

    def agent(self, response=None):
        with patch('src.agents.base.openai.OpenAI') as factory:
            agent = BaseAgent('Test', 'system', base_url='https://api.deepseek.com', max_output_tokens=300)
        client = factory.return_value
        client.with_options.return_value = client
        client.chat.completions.create.return_value = response or SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='answer'), finish_reason='stop')],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        return agent, client

    def test_official_thinking_disabled_output_cap_and_no_sdk_retry(self):
        agent, client = self.agent()
        token = current_budget.set(ResearchBudget(self.notes, 'run'))
        try: self.assertEqual(agent.run('topic'), 'answer')
        finally: current_budget.reset(token)
        args = client.chat.completions.create.call_args.kwargs
        self.assertEqual(args['max_tokens'], 300)
        self.assertEqual(args['extra_body'], {'thinking': {'type': 'disabled'}})
        client.with_options.assert_called_once_with(max_retries=0)
        self.assertEqual(self.notes.load_cost_state('run')['used'], 15)

    def test_json_agents_use_provider_json_mode(self):
        agent, client = self.agent()
        agent.json_output = True
        self.assertEqual(agent.run('audit'), 'answer')
        self.assertEqual(
            client.chat.completions.create.call_args.kwargs['response_format'],
            {'type': 'json_object'},
        )

    def test_other_hosts_do_not_receive_deepseek_parameters(self):
        with patch('src.agents.base.openai.OpenAI'):
            agent = BaseAgent('Test', 'system', base_url='https://other.example/v1')
        self.assertEqual(agent.extra_body, {})

    def test_large_input_rejected_before_network(self):
        agent, client = self.agent()
        agent.max_input_chars = 10
        with self.assertRaises(ResearchLimit): agent.run('x' * 20)
        client.chat.completions.create.assert_not_called()

    def test_truncated_output_is_charged_and_not_returned(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='partial'), finish_reason='length')],
                                   usage=SimpleNamespace(total_tokens=200))
        agent, client = self.agent(response)
        budget = ResearchBudget(self.notes, 'run')
        token = current_budget.set(budget)
        try:
            with self.assertRaises(ResearchLimit): agent.run('topic')
        finally: current_budget.reset(token)
        self.assertEqual(budget.data['used'], 200)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_connection_failure_releases_reservation(self):
        agent, client = self.agent()
        client.chat.completions.create.side_effect = openai.APIConnectionError(
            request=httpx.Request('POST', 'https://api.deepseek.com/chat/completions')
        )
        budget = ResearchBudget(self.notes, 'run')
        token = current_budget.set(budget)
        try:
            with self.assertRaisesRegex(RuntimeError, '无法连接模型服务'):
                agent.run('topic')
        finally:
            current_budget.reset(token)
        self.assertEqual(budget.data['used'], 0)
        self.assertEqual(budget.data['calls'], 0)
        self.assertEqual(budget.data['reservations'], {})

    def test_timeout_keeps_reservation_when_provider_usage_is_unknown(self):
        agent, client = self.agent()
        client.chat.completions.create.side_effect = openai.APITimeoutError(
            request=httpx.Request('POST', 'https://api.deepseek.com/chat/completions')
        )
        budget = ResearchBudget(self.notes, 'run')
        token = current_budget.set(budget)
        try:
            with self.assertRaisesRegex(RuntimeError, '模型服务请求超时'):
                agent.run('topic')
        finally:
            current_budget.reset(token)
        self.assertGreater(budget.data['used'], 0)
        self.assertEqual(budget.data['calls'], 1)

    def test_persistence_failure_prevents_paid_request(self):
        agent, client = self.agent()
        budget = ResearchBudget(self.notes, 'run')
        token = current_budget.set(budget)
        try:
            with patch.object(self.notes, 'save_cost_state', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError): agent.run('topic')
        finally: current_budget.reset(token)
        client.chat.completions.create.assert_not_called()


class ReportCheckpointTests(unittest.TestCase):



    def test_changed_evidence_invalidates_old_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = SequenceAgent([GOOD, GOOD])
            service = ReportingService(writer, NoteTool(tmp))
            service.generate('topic', [summary()], 'run')
            updated = summary()
            updated.summary += '新增限定。'
            service.generate('topic', [updated], 'run')
            self.assertEqual(len(writer.prompts), 2)



class IncrementalEvidenceTests(unittest.TestCase):
    def result(self, url, body):
        return {'url': url, 'title': 'Source', 'snippet': body, 'content': body}

    def test_no_new_evidence_skips_model_even_when_order_changes(self):
        agent = SequenceAgent([])
        service = SummarizationService(agent)
        results = [self.result('https://a.test', 'old'), self.result('https://b.test', 'second')]
        prior = summary()
        self.assertIs(service.update_summary(prior.task, results, prior, list(reversed(results))), prior)
        self.assertEqual(agent.prompts, [])

    def test_new_evidence_only_and_citation_offsets(self):
        agent = SequenceAgent([json.dumps({'summary': 'New finding[1].', 'source_assessments': [], 'comparisons': [], 'gaps': []})])
        service = SummarizationService(agent)
        previous = summary()
        results = [self.result(previous.sources[0], 'UNIQUE_OLD_BODY')]
        new = self.result('https://new.test', 'NEW_BODY')
        merged = service.update_summary(previous.task, results, previous, results + [new])
        self.assertNotIn('UNIQUE_OLD_BODY', agent.prompts[0])
        self.assertIn('NEW_BODY', agent.prompts[0])
        self.assertIn('New finding[2]', merged.summary)
        self.assertEqual(merged.sources, previous.sources + ['https://new.test'])







class PartialResumeTests(unittest.IsolatedAsyncioTestCase):

    async def test_old_over_budget_research_does_not_send_any_model_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = NoteTool(tmp)
            notes.save_cost_state('run', {'version': 1, 'used': 1700000, 'calls': 30, 'reservations': {}})
            repo = ResearchRepository(str(Path(tmp, 'test.db')))
            repo.create('run', 'Topic')
            repo.mark_interrupted_runs()
            with patch('src.agents.base.openai.OpenAI') as factory:
                agent = BaseAgent('Planner', 'system', api_key='fake', base_url='https://api.deepseek.com')
            planning = SimpleNamespace(plan=lambda topic: agent.run(topic))
            manager = ResearchManager(repo, planning, Mock(), Mock(), Mock(), notes)
            await manager.resume('run')
            await asyncio.wait_for(asyncio.shield(manager._jobs['run']), 10)
            self.assertEqual(repo.get('run')['status'], 'error')
            self.assertIn('预算', repo.get('run')['error'])
            factory.return_value.chat.completions.create.assert_not_called()


if __name__ == '__main__': unittest.main()

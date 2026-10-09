"""General report admission and recovery: independent of any live research run."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.agents.base import BaseAgent
from src.services.cost_control import ResearchBudget, ResearchLimit, current_budget
from src.services.reporting import ReportingService
from src.tools.note import NoteTool
from test_support import summary, GOOD


class ReportAdmissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.notes = NoteTool(directory.name)
        with patch('src.agents.base.openai.OpenAI') as factory:
            self.agent = BaseAgent('Reporter', 'Write a sourced report.',
                base_url='https://api.deepseek.com', budget_stage='report_write',
                max_input_chars=14000, max_output_tokens=1000)
        self.client = factory.return_value
        self.client.with_options.return_value = self.client
        self.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=GOOD), finish_reason='stop')],
            usage=SimpleNamespace(total_tokens=2000))
        self.service = ReportingService(self.agent, self.notes)

    def test_mixed_languages_and_lengths_fit_actual_request_with_one_call(self):
        for index, phrase in enumerate(('An evaluated finding 42% [1].', '结果为42%，仅适用于给定条件[1]。',
                                        'A result "quoted" with a backslash \\ [1].')):
            with self.subTest(phrase=phrase):
                run = f'run{index}'
                item = summary()
                item.summary = '\n\n'.join(f'{n}: {phrase}' for n in range(800))
                budget = ResearchBudget(self.notes, run, token_limit=15000,
                    stage_token_limits={'report_write': 10000}, stage_call_limits={'report_write': 2})
                token = current_budget.set(budget)
                self.client.chat.completions.create.reset_mock()
                try:
                    self.service.generate('Compare results and conditions', [item], run)
                finally:
                    current_budget.reset(token)
                self.client.chat.completions.create.assert_called_once()
                request = self.client.chat.completions.create.call_args.kwargs
                prompt = ''.join(m['content'] for m in request['messages'])
                self.assertLessEqual(len(prompt), self.agent.max_input_chars)
                self.assertLessEqual(ResearchBudget.estimate_tokens(prompt) + 1000 + 1024, 10000)
                self.assertEqual(budget.data['stage_calls']['report_write'], 1)
                manifest = self.notes.load_json(run, 'report-input.json')
                self.assertGreater(manifest['omitted_passages'], 0)

    def test_legacy_compression_is_charged_but_does_not_consume_writing_slot(self):
        budget = ResearchBudget(self.notes, 'run', token_limit=100000,
            stage_call_limits={'report': 1, 'report_write': 2})
        old = budget.reserve('old compression', 500, stage='report')
        budget.settle(old, 700)
        token = current_budget.set(budget)
        try:
            self.service.generate('Topic', [summary()], 'run')
        finally:
            current_budget.reset(token)
        self.assertEqual(budget.data['stage_calls'], {'report': 1, 'report_write': 1})
        self.assertEqual(budget.data['calls'], 2)
        self.assertEqual(budget.data['used'], 2700)

    def test_insufficient_remaining_budget_does_not_start_paid_request(self):
        budget = ResearchBudget(self.notes, 'run', token_limit=1000)
        token = current_budget.set(budget)
        try:
            with self.assertRaisesRegex(ResearchLimit, '剩余预算不足'):
                self.service.generate('Topic', [summary()], 'run')
        finally:
            current_budget.reset(token)
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(budget.data['calls'], 0)

    def test_large_topic_counts_toward_input_limit(self):
        with self.assertRaisesRegex(ResearchLimit, '剩余预算不足'):
            self.service.generate('x' * 15000, [summary()], 'run')
        self.client.chat.completions.create.assert_not_called()

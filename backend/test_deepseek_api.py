"""Offline regression coverage for the official DeepSeek request contract."""
import tempfile
import unittest
import json
import httpx
import openai
from types import SimpleNamespace as NS
from unittest.mock import patch

from src.agents.base import BaseAgent
from src.services.cost_control import ResearchBudget, current_budget
from src.tools.note import NoteTool


class DeepSeekAPITests(unittest.TestCase):
    def test_installed_sdk_serializes_official_request(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={
                'id': 'offline', 'object': 'chat.completion', 'created': 0,
                'model': 'deepseek-flash',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{}'},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 1, 'total_tokens': 6}})
        agent, _ = self.agent(deepseek_thinking=True, deepseek_reasoning_effort='low', json_output=True)
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            agent.client = openai.OpenAI(api_key='offline-test', base_url='https://api.deepseek.com',
                                        http_client=transport, max_retries=0)
            self.assertEqual(agent.run('Return JSON {}'), '{}')
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), 'https://api.deepseek.com/chat/completions')
        payload = json.loads(requests[0].content)
        self.assertEqual(payload['thinking'], {'type': 'enabled'})
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertNotIn('extra_body', payload)

    def agent(self, **kwargs):
        kwargs.setdefault('base_url', 'https://api.deepseek.com')
        with patch('src.agents.base.openai.OpenAI') as factory:
            agent = BaseAgent('Test', 'Extract facts.', **kwargs)
        client = factory.return_value
        client.with_options.return_value = client
        client.chat.completions.create.return_value = NS(
            choices=[NS(message=NS(content='{"facts": []}'), finish_reason='stop')],
            usage=NS(total_tokens=30, prompt_tokens=20, completion_tokens=10,
                     prompt_cache_hit_tokens=15, prompt_cache_miss_tokens=5,
                     prompt_tokens_details=NS(cached_tokens=15),
                     completion_tokens_details=NS(reasoning_tokens=4)))
        self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
        return agent, client

    def test_disabled_thinking_does_not_send_effort(self):
        agent, client = self.agent(max_output_tokens=1234)
        agent.run('Return facts')
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request['model'], 'deepseek-flash')
        self.assertEqual(request['extra_body'], {'thinking': {'type': 'disabled'}})
        self.assertNotIn('reasoning_effort', request)
        self.assertEqual(request['temperature'], 0.3)
        self.assertEqual(request['max_tokens'], 1234)
        self.assertIs(request['stream'], False)

    def test_thinking_effort_and_temperature_are_exclusive(self):
        for effort in ('low', 'high', 'max'):
            with self.subTest(effort=effort):
                agent, client = self.agent(deepseek_thinking=True, deepseek_reasoning_effort=effort)
                agent.run('Return facts')
                request = client.chat.completions.create.call_args.kwargs
                self.assertEqual(request['extra_body'], {'thinking': {'type': 'enabled'}})
                self.assertEqual(request['reasoning_effort'], effort)
                self.assertNotIn('temperature', request)

    def test_json_mode_adds_required_prompt_instruction(self):
        agent, client = self.agent(json_output=True)
        agent.run('Return {"facts": []}')
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request['response_format'], {'type': 'json_object'})
        self.assertIn('JSON', request['messages'][0]['content'])

    def test_other_provider_does_not_receive_deepseek_fields(self):
        agent, client = self.agent(base_url='https://other.example/v1', deepseek_thinking=True,
                                   json_output=True, model='deepseek-v4-flash')
        agent.run('Return facts')
        request = client.chat.completions.create.call_args.kwargs
        for key in ('extra_body', 'reasoning_effort', 'response_format'):
            self.assertNotIn(key, request)
        self.assertEqual(request['model'], 'deepseek-v4-flash')

    def test_legacy_flash_aliases_use_canonical_model(self):
        for model in ('deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'):
            agent, _ = self.agent(model=model)
            self.assertEqual(agent.model, 'deepseek-flash')

    def test_invalid_effort_rejected_before_client_creation(self):
        with patch('src.agents.base.openai.OpenAI') as factory:
            with self.assertRaises(ValueError):
                BaseAgent('Test', 'system', base_url='https://api.deepseek.com',
                          deepseek_reasoning_effort='invalid')
            factory.assert_not_called()

    def test_usage_includes_cache_and_reasoning_details(self):
        agent, _ = self.agent()
        metrics = {}
        agent._run('facts', metrics)
        self.assertEqual(metrics['cached_tokens'], 15)
        self.assertEqual(metrics['prompt_cache_hit_tokens'], 15)
        self.assertEqual(metrics['reasoning_tokens'], 4)
        self.assertEqual(metrics['total_tokens'], 30)

    def test_incomplete_responses_are_charged_without_retry(self):
        for reason in ('aborted', 'insufficient_system_resource', 'content_filter', 'tool_calls', None):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                agent, client = self.agent()
                client.chat.completions.create.return_value.choices[0].finish_reason = reason
                budget = ResearchBudget(NoteTool(directory), 'run')
                token = current_budget.set(budget)
                try:
                    with self.assertRaisesRegex(RuntimeError, '未正常完成'):
                        agent.run('facts')
                finally:
                    current_budget.reset(token)
                self.assertEqual(budget.data['used'], 30)
                self.assertEqual(budget.data['calls'], 1)
                client.chat.completions.create.assert_called_once()

    def test_missing_choices_preserves_usage(self):
        agent, client = self.agent()
        client.chat.completions.create.return_value.choices = []
        metrics = {}
        with self.assertRaisesRegex(RuntimeError, 'choices'):
            agent._run('facts', metrics)
        self.assertEqual(metrics['total_tokens'], 30)

import openai
from src.services.performance import span, count
from src.services.cost_control import current_budget, ResearchLimit, OutputTruncated
from urllib.parse import urlparse


class BaseAgent:
    """基础 Agent 类，封装 LLM 调用"""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model: str = "deepseek-flash",
        api_key: str = "",
        base_url: str = "",
        timeout: float = 90.0,
        max_retries: int = 0,
        temperature: float = 0.3,
        max_output_tokens: int = 5000,
        max_input_chars: int = 70000,
        deepseek_thinking: bool = False,
        json_output: bool = False,
        deepseek_reasoning_effort: str = "high",
        budget_stage: str = "",
    ):
        self.name = name
        self.budget_stage = budget_stage
        self.system_prompt = system_prompt
        self._is_deepseek = urlparse(base_url).hostname == 'api.deepseek.com'
        self.model = ('deepseek-flash' if self._is_deepseek and model in {
            'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'} else model)
        if self._is_deepseek and deepseek_reasoning_effort not in {'low', 'high', 'max'}:
            raise ValueError('DeepSeek 思考强度须为 low、high 或 max')
        self.deepseek_thinking = bool(deepseek_thinking and self._is_deepseek)
        self.deepseek_reasoning_effort = deepseek_reasoning_effort
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.max_input_chars = max_input_chars
        self.json_output = json_output
        self._supports_json_output = urlparse(base_url).hostname in {
            'api.deepseek.com', 'api.openai.com'
        }
        self.extra_body = ({'thinking': {'type': 'enabled' if deepseek_thinking else 'disabled'}}
                           if urlparse(base_url).hostname == 'api.deepseek.com' else {})
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    def _system_message(self):
        message = self.system_prompt
        if self.json_output and self._supports_json_output and 'json' not in message.lower():
            message += '\nReturn a JSON object matching the user-provided schema.'
        return message

    def run(self, user_prompt: str) -> str:
        # One logical SDK call; its duration includes SDK retries/backoff.
        # Internal HTTP retries are intentionally not counted as separate calls.
        with span('llm.' + self.name) as metrics:
            metrics['input_chars'] = len(self._system_message()) + len(user_prompt)
            if metrics['input_chars'] > getattr(self, 'max_input_chars', 70000):
                raise ResearchLimit('单次模型输入超出材料预算，已停止调用；请先精简证据包。')
            budget = current_budget.get()
            budget_stage = getattr(self, 'budget_stage', '') or ('report' if self.name == 'Reporter' else self.name.casefold())
            metrics['budget_stage'] = budget_stage
            reservation = budget.reserve(
                self._system_message() + user_prompt,
                getattr(self, 'max_output_tokens', 5000),
                stage=budget_stage,
            ) if budget else None
            try:
                count('llm_logical_calls')
                return self._run(user_prompt, metrics)
            except openai.APITimeoutError as error:
                # The provider may have received the request before the client
                # timed out, so retain the conservative reservation.
                raise RuntimeError(
                    '模型服务请求超时；服务端是否已处理未知，因此暂时保留本次预算预留。'
                ) from error
            except openai.APIConnectionError as error:
                # A connection error has no provider response/usage to settle.
                if reservation:
                    budget.release(reservation)
                    reservation = None
                raise RuntimeError(
                    '无法连接模型服务，请检查后端进程的外网权限、代理或防火墙后继续研究。'
                ) from error
            finally:
                if reservation:
                    budget.settle(reservation, metrics.get('total_tokens'))

    def _run(self, user_prompt: str, metrics: dict) -> str:
        """执行一次 LLM 调用"""
        client = self.client.with_options(max_retries=0) if current_budget.get() else self.client
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self._system_message()},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            **({'reasoning_effort': self.deepseek_reasoning_effort} if self.deepseek_thinking
               else {'temperature': self.temperature}),
            max_tokens=getattr(self, 'max_output_tokens', 5000),
            **({'extra_body': self.extra_body} if getattr(self, 'extra_body', None) else {}),
            **({'response_format': {'type': 'json_object'}}
               if getattr(self, 'json_output', False)
               and getattr(self, '_supports_json_output', False) else {}),
        )

        usage = getattr(response, 'usage', None)
        for field in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                      'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
            value = getattr(usage, field, None)
            if isinstance(value, int):
                metrics[field] = value
        details = getattr(usage, 'completion_tokens_details', None)
        reasoning_tokens = getattr(details, 'reasoning_tokens', None)
        if isinstance(reasoning_tokens, int):
            metrics['reasoning_tokens'] = reasoning_tokens
        prompt_details = getattr(usage, 'prompt_tokens_details', None)
        cached = getattr(prompt_details, 'cached_tokens', None)
        if type(cached) is int:
            metrics['cached_tokens'] = cached
        if not response.choices:
            raise RuntimeError(f'{self.name} 返回的响应缺少 choices')
        choice = response.choices[0]
        content = choice.message.content or ""
        metrics['output_chars'] = len(content)
        finish_reason = getattr(choice, 'finish_reason', None)
        if isinstance(finish_reason, str):
            metrics['finish_reason'] = finish_reason
        if finish_reason == 'length':
            raise OutputTruncated(content)
        if finish_reason != 'stop':
            reasons = {'aborted': '生成中断', 'insufficient_system_resource': '模型服务资源不足',
                       'content_filter': '内容被服务过滤', 'tool_calls': '返回了当前任务不支持的工具调用'}
            raise RuntimeError(f"{self.name} 未正常完成：{reasons.get(finish_reason, '未知结束状态')}；未采用不完整内容。")
        if not content.strip():
            raise RuntimeError(f"{self.name} 返回了空响应")
        return content

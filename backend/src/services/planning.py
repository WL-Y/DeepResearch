import re
import json
from datetime import datetime
from typing import List
from src.agents.base import BaseAgent
from src.models import TodoItem, TaskSummary
from src.prompts import (
    PLANNER_PROMPT,
    PLANNER_REPAIR_PROMPT,
    render_prompt,
    DEPENDENT_QUERY_PROMPT,
)


class DependencyQueryError(ValueError):
    """An optional query refinement was malformed, not a failed research task."""


class PlanningService:
    """规划服务：按复杂度生成 1–5 个必要任务。"""

    def __init__(self, agent: BaseAgent, dependency_agent: BaseAgent | None = None):
        self.agent = agent
        self.dependency_agent = dependency_agent or agent

    def plan(self, topic: str) -> List[TodoItem]:
        """分解研究主题为子任务列表"""
        now = datetime.now()
        prompt = render_prompt(PLANNER_PROMPT,
            current_date=now.strftime("%Y年%m月%d日"),
            current_year=now.year,
            topic=topic,
        )
        response = self.agent.run(prompt)
        try:
            return self._parse_tasks(response)
        except (TypeError, ValueError, json.JSONDecodeError) as first_error:
            repair_prompt = render_prompt(PLANNER_REPAIR_PROMPT,
                topic=topic,
                validation_error=str(first_error)[:300],
                invalid_output=response[:2000],
            )
            repaired_response = self.agent.run(repair_prompt)
            try:
                return self._parse_tasks(repaired_response)
            except (TypeError, ValueError, json.JSONDecodeError) as second_error:
                raise ValueError(
                    f"规划结果连续两次未通过校验: {str(second_error)[:300]}"
                ) from second_error

    def _parse_tasks(self, text: str) -> List[TodoItem]:
        tasks = self._extract_json(text)
        if not 1 <= len(tasks) <= 5:
            raise ValueError("子任务数量必须在 1–5 个之间")

        parsed = []
        seen_titles = set()
        seen_queries = set()
        required_fields = {"title", "intent", "query", "verification_query"}

        for index, item in enumerate(tasks, 1):
            if not isinstance(item, dict):
                raise TypeError(f"第 {index} 个子任务必须是 JSON 对象")
            if not required_fields <= set(item) or set(item) - required_fields - {"required_methods", "depends_on"}:
                raise ValueError(
                    f"第 {index} 个子任务须包含 title、intent、query、verification_query，可附 required_methods、depends_on"
                )

            normalized = {}
            limits = {
                "title": 100,
                "intent": 500,
                "query": 300,
                "verification_query": 300,
            }
            for field, limit in limits.items():
                value = item[field]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"第 {index} 个子任务的 {field} 必须是非空字符串")
                value = value.strip()
                if len(value) > limit:
                    raise ValueError(f"第 {index} 个子任务的 {field} 过长")
                normalized[field] = value

            title_key = self._dedupe_key(normalized["title"])
            query_key = self._dedupe_key(normalized["query"])
            verification_key = self._dedupe_key(normalized["verification_query"])
            if (
                title_key in seen_titles
                or query_key in seen_queries
                or verification_key in seen_queries
            ):
                raise ValueError("子任务标题或搜索查询存在重复")
            if verification_key == query_key:
                raise ValueError("发现查询与核验查询不能相同")
            seen_titles.add(title_key)
            seen_queries.add(query_key)
            seen_queries.add(verification_key)
            methods = item.get("required_methods", [])
            if not isinstance(methods, list) or len(methods) > 3 or any(
                not isinstance(value, str) or not value.strip() or len(value) > 100 for value in methods
            ):
                raise ValueError("required_methods 必须是最多三个非空方法名称")
            dependencies = item.get('depends_on', [])
            if not isinstance(dependencies, list) or any(type(d) is not int or not 1 <= d < index for d in dependencies):
                raise ValueError('depends_on 只能引用当前任务之前的数组序号（从 1 开始），不能自依赖或形成环')
            parsed.append(TodoItem(id=index, **normalized, required_methods=list(dict.fromkeys(m.strip() for m in methods)),
                                   depends_on=list(dict.fromkeys(dependencies))))

        return parsed

    def refine_dependent(self, task: TodoItem, upstream: list[TaskSummary]) -> TodoItem:
        """Use predecessor findings as search guidance, never as new primary evidence."""
        context = [{'task_id': s.task.id, 'summary': s.summary[:4000]}
                   for s in upstream]
        response = self.dependency_agent.run(render_prompt(DEPENDENT_QUERY_PROMPT,
            task=task.model_dump(), upstream=context))
        try:
            payload = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', response.strip()))
        except json.JSONDecodeError as error:
            raise DependencyQueryError('搜索词优化返回无效 JSON') from error
        return task.model_copy(update=self.normalize_queries(payload))

    @classmethod
    def normalize_queries(cls, payload):
        """Normalize whitespace without truncating names or changing scope."""
        if not isinstance(payload, dict) or set(payload) != {'query', 'verification_query'}:
            raise DependencyQueryError('搜索词优化结果字段无效，应为 query 和 verification_query')
        normalized = {}
        for field, value in payload.items():
            if not isinstance(value, str):
                raise DependencyQueryError(f'{field} 应为字符串，实际为 {type(value).__name__}')
            value = re.sub(r'\s+', ' ', value).strip()
            if not value:
                raise DependencyQueryError(f'{field} 为空')
            if len(value) > 300:
                raise DependencyQueryError(f'{field} 长度为 {len(value)} 字符，超过 300 字符上限')
            normalized[field] = value
        if cls._dedupe_key(normalized['query']) == cls._dedupe_key(normalized['verification_query']):
            raise DependencyQueryError('发现查询与核验查询规范化后重复')
        return normalized

    def _extract_json(self, text: str) -> list:
        """从 Agent 响应中提取 JSON 数组"""
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\[", text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, list):
                return value
        raise ValueError(f"无法从响应中提取 JSON: {text[:200]}")

    @staticmethod
    def _dedupe_key(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

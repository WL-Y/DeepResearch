import re
import json
from datetime import datetime
from typing import List, Tuple
from src.models import TaskSummary
from src.agents.base import BaseAgent
from src.prompts import REPORTER_PROMPT, render_prompt
from src.services.evidence import (SOURCES_SECTION_PATTERN, hide_citations,
                                   limit_citation_groups, source_key, source_link)
from src.services.references import citation_catalog
from src.tools.note import NoteTool
from src.services.performance import measured
from src.services.report_compression import prepare_materials
from src.services.cost_control import OutputTruncated, ResearchLimit, ResearchBudget, current_budget
import hashlib

class ReportingService:
    """报告服务：整合所有子任务总结，生成最终报告"""

    _CITATION_PATTERN = re.compile(r"\[(\d+)\]")
    _MAX_REPORT_SOURCES = 15

    def __init__(self, agent: BaseAgent, note_tool: NoteTool, *, report_materials_max_chars: int = 160000):
        self.agent = agent
        self.note_tool = note_tool
        self.report_materials_max_chars = max(5000, min(480000, report_materials_max_chars))

    @measured('reporting')
    def generate(self, topic: str, summaries: List[TaskSummary], research_id: str, *, on_progress=None) -> str:
        renumbered_summaries = self._renumber_citations(summaries)

        signature = hashlib.sha256(json.dumps({'version': 4, 'topic': topic,
            'summaries': [s.model_dump() for s in summaries]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        saved = self.note_tool.load_json(research_id, 'report-state.json')
        state = saved if isinstance(saved, dict) and saved.get('signature') == signature else {
            'version': 2, 'signature': signature, 'draft': ''}
        # Pre-removal states are tied to this research ID. Reuse the paid draft
        # or continuation checkpoint, but never replay the former review stage.
        if isinstance(saved, dict) and saved.get('version') == 1:
            for key in ('draft', 'partial_draft', 'output_truncated', 'continuation_attempts'):
                if key in saved:
                    state[key] = saved[key]
        def persist_state():
            self.note_tool.save_report_state(research_id, state)
        def build_prompt(extra=''):
            formatted = self._prepare_input(topic, renumbered_summaries, research_id, on_progress, extra=extra)
            return render_prompt(REPORTER_PROMPT,
                current_date=datetime.now().strftime('%Y年%m月%d日'), topic=topic, summaries=formatted) + extra
        draft = state.get('draft') or ''
        if not draft:
            prompt = '' if state.get('partial_draft') else build_prompt()
            if on_progress:
                on_progress('正在撰写研究报告...')
            draft = self._write_draft(prompt, state, persist_state, on_progress, build_prompt=build_prompt)
            state['draft'] = draft
        if not draft.strip():
            raise ValueError('报告返回了空内容')
        persist_state()
        if on_progress:
            on_progress('正在整理报告排版与来源链接...')
        report = self._finalize_report(draft, renumbered_summaries)
        self.note_tool.save_report(research_id, report)
        return report

    def _prepare_input(self, topic, normalized, research_id, on_progress, *, extra=''):
        """Fit complete request overhead and remaining token budget, not raw notes alone."""
        overhead = render_prompt(REPORTER_PROMPT,
            current_date=datetime.now().strftime('%Y年%m月%d日'), topic=topic, summaries='')
        system = getattr(self.agent, 'system_prompt', '')
        overhead += (system if isinstance(system, str) else '') + extra
        input_limit = getattr(self.agent, 'max_input_chars', 180000)
        input_limit = input_limit if type(input_limit) is int else 180000
        # render_prompt JSON-encodes strings. Account for escaping in the actual
        # prompt below, tightening the local packet without any model calls.
        chars = min(self.report_materials_max_chars, input_limit - len(overhead) - 1024)
        tokens = None
        budget = current_budget.get()
        if budget:
            stage = getattr(self.agent, 'budget_stage', '') or 'report'
            output = getattr(self.agent, 'max_output_tokens', 12000)
            output = output if type(output) is int else 12000
            available = min(budget.token_limit - budget.data['used'],
                budget.stage_token_limits.get(stage, budget.token_limit) - budget.data['stage_used'].get(stage, 0))
            tokens = available - output - 1024 - ResearchBudget.estimate_tokens(overhead) - 1024
        if chars <= 0 or (tokens is not None and tokens <= 0):
            raise ResearchLimit('剩余预算不足以容纳报告提示和输出；已保留研究笔记。')
        for _ in range(8):
            packed = prepare_materials(self, normalized, research_id, None, max_chars=chars, max_tokens=tokens)
            prompt = render_prompt(REPORTER_PROMPT,
                current_date=datetime.now().strftime('%Y年%m月%d日'), topic=topic, summaries=packed)
            actual = prompt + (system if isinstance(system, str) else '') + extra
            char_over = len(actual) - input_limit
            token_over = (ResearchBudget.estimate_tokens(actual) + output + 1024 - available) if budget else 0
            if char_over <= 0 and token_over <= 0:
                if on_progress:
                    state = self.note_tool.load_json(research_id, 'report-input.json')
                    on_progress(f'本地整理报告材料完成：{state["included_passages"]}/{state["total_passages"]} 个完整段落，准备一次写作')
                return packed
            chars -= max(1024, char_over + 256) if char_over > 0 else 0
            if tokens is not None and token_over > 0:
                tokens -= token_over + 256
            if chars <= 0 or (tokens is not None and tokens <= 0):
                break
        raise ResearchLimit('报告材料无法在当前输入与剩余预算内完整组织；已保留原始笔记。')

    def _write_draft(self, prompt, state, persist, on_progress, *, build_prompt=None):
        """Checkpoint incomplete output and allow one bounded continuation.

        A partial draft is saved before continuation. Admission is
        persisted before continuation so repeated resumes cannot add attempts.
        """
        partial = state.get('partial_draft', '')
        if state.get('output_truncated') and not partial:
            raise ResearchLimit('模型输出被截断且没有可续写正文；请检查输出或思考配置后恢复。')
        if not partial:
            state['next_action'] = 'write_report'
            persist()
            try:
                return self.agent.run(prompt)
            except OutputTruncated as error:
                partial = error.content
                state.update(partial_draft=partial, output_truncated=True)
                persist()
                if not partial.strip():
                    raise
        if state.get('continuation_attempts', 0) >= 1:
            raise ResearchLimit('报告续写额度已用完；已保存未完成草稿和研究材料，请调整输出方案后恢复。')
        if on_progress:
            on_progress('报告已保存，正在续写剩余内容（最多一次）...')
        extra = render_prompt(
            '\n已生成但未完成的草稿（JSON 数据）：{partial}\n', partial=partial)
        extra += ('接着草稿末尾继续，仅输出剩余正文；若末句中断则先补完该句。'
                         '无需重复标题或已有段落。沿用证据编号，完成尚未覆盖的核心分析和结论后结束。')
        continuation = build_prompt(extra) if build_prompt else prompt + extra
        state['next_action'] = 'continue_truncated_report'
        state['continuation_attempts'] = state.get('continuation_attempts', 0) + 1
        persist()
        try:
            tail = self.agent.run(continuation)
        except OutputTruncated as error:
            state['partial_draft'] = partial + error.content
            persist()
            raise ResearchLimit('报告续写仍达到输出上限；已保存全部未完成草稿，未重复重写或发布残稿。') from error
        draft = partial + tail
        state.update(draft=draft, output_truncated=False)
        state.pop('partial_draft', None)
        persist()
        return draft

    def _finalize_report(
        self,
        draft: str,
        summaries_with_maps: List[Tuple[TaskSummary, dict]],
    ) -> str:
        """将核心来源引用转为可显示为上标的链接，附相同编号的来源列表。"""
        source_by_number = {}
        source_tasks = {}
        source_details = {}
        source_contexts = {}
        for summary, _ in summaries_with_maps:
            for index, source in enumerate(summary.sources):
                match = re.match(r"\[(\d+)\]\s*(https?://\S+)", source)
                if not match:
                    continue
                number = int(match.group(1))
                url = match.group(2)
                title = (
                    summary.source_titles[index]
                    if index < len(summary.source_titles)
                    else url
                )
                source_by_number[number] = (title, url)
                source_tasks.setdefault(number, set()).add(summary.task.title)
                detail = summary.source_details[index] if index < len(summary.source_details) else {}
                source_details.setdefault(number, []).append(detail)
                source_contexts.setdefault(number, []).extend(
                    line for line in summary.summary.splitlines() if f'[{number}]' in line
                )

        body = SOURCES_SECTION_PATTERN.sub("", draft).strip()
        body = limit_citation_groups(body, {
            number: {'title': title, 'url': url, 'details': source_details[number],
                     'text': '\n'.join(source_contexts[number])}
            for number, (title, url) in source_by_number.items()
        })
        candidates = {int(n) for n in self._CITATION_PATTERN.findall(body) if int(n) in source_by_number}
        selected_numbers = []
        covered_tasks = set()
        covered_claims = set()
        paragraphs = body.splitlines()
        numeric_claims = {
            number: {i for i, line in enumerate(paragraphs)
                     if f"[{number}]" in line and re.search(r"\d", self._CITATION_PATTERN.sub("", line))}
            for number in candidates
        }
        # Greedy coverage: preserve sources supporting quantitative claims and
        # different research questions, with a preference for direct original work.
        while candidates and len(selected_numbers) < self._MAX_REPORT_SOURCES:
            def score(number):
                details = source_details[number]
                primary = any(d.get('reference_type') in {'一手论文', '官方资料'} or
                              (d.get("kind") == "primary" and d.get("relevance") == "direct") for d in details)
                related = any(d.get("relevance") == "direct" for d in details)
                read = any(d.get("has_content") for d in details)
                frequency = len(re.findall(rf"\[{number}\]", body))
                tier = 2 if primary else 1 if related else 0
                if any(d.get("kind") == "commentary" for d in details) and not primary:
                    tier = 0
                return (tier, 6 * len(numeric_claims[number] - covered_claims)
                        + 4 * len(source_tasks[number] - covered_tasks)
                        + 5 * primary + 2 * related + read + min(frequency, 3), -number)
            chosen = max(candidates, key=score)
            candidates.remove(chosen)
            selected_numbers.append(chosen)
            covered_tasks.update(source_tasks[chosen])
            covered_claims.update(numeric_claims[chosen])

        # Keep the coverage-based selection, but number the visible sources in
        # order of first appearance, rather than in ranking order.
        first_seen = list(dict.fromkeys(int(n) for n in self._CITATION_PATTERN.findall(body)))
        selected_numbers.sort(key=first_seen.index)
        selected_sources = [source_by_number[number] for number in selected_numbers]
        body = hide_citations(
            body, {number: value[0] for number, value in source_by_number.items()},
            citation_links={number: (index, source_by_number[number][1])
                            for index, number in enumerate(selected_numbers, 1)},
        )
        if not selected_sources:
            return body

        links = "\n".join(
            f"{index}. {source_link(title, url)}"
            for index, (title, url) in enumerate(selected_sources, 1)
        )
        report = f"{body}\n\n## 核心来源\n\n{links}"
        return report

    def _renumber_citations(self, summaries: List[TaskSummary]) -> List[Tuple[TaskSummary, dict]]:
        """重新编号所有引用，使其全局唯一"""
        result = []
        url_to_global_number = {}
        # Build identity aliases across tasks, including DOI/arXiv bridge records.
        records = [{**(s.source_details[i] if i < len(s.source_details) else {}),
                    'url': url, 'title': s.source_titles[i] if i < len(s.source_titles) else url}
                   for s in summaries for i, url in enumerate(s.sources)]
        catalog = citation_catalog(records)

        for s in summaries:
            # 提取当前总结中的所有引用编号
            citations = {
                int(number)
                for number in self._CITATION_PATTERN.findall(s.summary)
                if 1 <= int(number) <= len(s.sources)
            }

            # 只为正文实际使用的来源编号，并在所有子任务间按 URL 去重。
            citation_map = {}
            updated_sources = []
            updated_source_titles = []
            updated_details = []
            for old_num in sorted(citations):
                source = s.sources[old_num - 1]
                source_title = (
                    s.source_titles[old_num - 1]
                    if old_num - 1 < len(s.source_titles)
                    else source
                )
                original_detail = s.source_details[old_num - 1] if old_num <= len(s.source_details) else {}
                reference = catalog.get(source_key(source), {**original_detail, 'url': source, 'title': source_title})
                identity = source_key(reference['url'])
                if identity not in url_to_global_number:
                    url_to_global_number[identity] = len(url_to_global_number) + 1
                new_num = url_to_global_number[identity]
                citation_map[old_num] = new_num
                updated_sources.append(f"[{new_num}] {reference['url']}")
                updated_source_titles.append(reference['title'])
                updated_details.append({**original_detail, 'reference_type': reference.get('reference_type'),
                                        'consulted_urls': reference.get('consulted_urls', [source])})

            # 更新总结中的引用编号
            def replace_citation(match):
                old_num = int(match.group(1))
                new_num = citation_map.get(old_num)
                return f'[{new_num}]' if new_num is not None else "[引用不可用]"

            updated_summary = self._CITATION_PATTERN.sub(replace_citation, s.summary)

            result.append((TaskSummary(
                task=s.task,
                summary=updated_summary,
                sources=updated_sources,
                source_titles=updated_source_titles,
                source_details=updated_details,
                retrieval=s.retrieval,
            ), citation_map))

        return result

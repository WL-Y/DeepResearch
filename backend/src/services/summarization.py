from datetime import datetime
import re
import json

from src.models import TodoItem, TaskSummary
from src.agents.base import BaseAgent
from src.prompts import SUMMARIZER_PROMPT, render_prompt
from src.services.source_details import read_depth, retrieval_record
from src.services.performance import measured
import hashlib


class SummarizationService:
    """总结服务：总结搜索结果"""

    def __init__(self, agent: BaseAgent, fulltext_chunk_chars: int = 20000, *,
                 optimize_reading=False, selection_agent=None, analysis_source_limit=4,
                 analysis_part_limit=16, concept_source_limit=2, concept_part_limit=8):
        self.agent = agent
        self.optimize_reading = optimize_reading
        self.selection_agent = selection_agent
        self.analysis_source_limit = analysis_source_limit
        self.analysis_part_limit = analysis_part_limit
        self.concept_source_limit = concept_source_limit
        self.concept_part_limit = concept_part_limit
        self.fulltext_chunk_chars = max(4000, min(30000, fulltext_chunk_chars))

    @staticmethod
    def evidence_key(result):
        fields = {k: result.get(k) for k in ('url', 'title', 'snippet', 'content', 'fetched_url',
                  'content_status', 'doi', 'verified_metadata', 'analysis_text_hash', 'analysis_part', 'analysis_phase')}
        return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def update_summary(self, task, previous_results, previous, expanded, *, summarize=None):
        known = {self.evidence_key(r) for r in previous_results}
        delta = []
        for result in expanded:
            key = self.evidence_key(result)
            if key not in known:
                delta.append(result)
                known.add(key)
        if not delta:
            return previous
        # Only new/changed evidence is sent. A changed version is appended, not
        # substituted beneath an already-grounded old citation.
        added = (summarize or self.summarize)(task, delta,
                               prior_inventory=[{'title': title, 'url': url,
                                                 'read_status': previous.source_details[i].get('acquisition_status')
                                                 if i < len(previous.source_details) else 'unknown'}
                                                for i, (url, title) in enumerate(zip(previous.sources, previous.source_titles))])
        return self.merge_summaries(task, previous_results, previous, added)

    @staticmethod
    def merge_summaries(task, previous_results, previous, added):
        """Merge completed batches without another model call or lost provenance."""
        offset = len(previous.sources)
        mapped = re.sub(r'\[(\d+)\]', lambda m: f'[{int(m[1]) + offset}]', added.summary)
        return TaskSummary(task=task,
            summary=previous.summary + '\n\n' + mapped,
            sources=previous.sources + added.sources, source_titles=previous.source_titles + added.source_titles,
            source_details=previous.source_details + added.source_details,
            retrieval={'incremental': True, 'new_candidates': len(added.sources)})

    @measured('summarization')
    def summarize(self, task: TodoItem, search_results: list, *, prior_inventory=None) -> TaskSummary:
        """总结单个任务的搜索结果"""
        formatted = self._format_results(search_results)
        prompt = render_prompt(SUMMARIZER_PROMPT,
            current_date=datetime.now().strftime("%Y年%m月%d日"),
            task_title=task.title,
            task_intent=task.intent,
            task_query=task.query,
            verification_query=task.verification_query or "未单独设置",
            required_methods=task.required_methods,
            search_results=formatted,
        )
        if prior_inventory:
            prompt += render_prompt('\n此前已读资料目录（仅用于判断获取情况，不用于新增事实）：{inventory}\n',
                                    inventory=prior_inventory)
            prompt += '本轮只包含增量材料；未在增量出现不代表此前未取得原文。'
        if any(r.get('analysis_phase') == 'synthesis' for r in search_results):
            prompt += ('\n输入为同一来源已完成的分段分析。合并成该来源的完整研究笔记，保留各段的关键发现、'
                       '数字及条件、反证和限制；去掉重复。所有事实统一引用 [1]，不新增原文未支持的判断。')
        def validate_summary(payload):
            if not isinstance(payload, dict) or not isinstance(payload.get('summary'), str) or not payload['summary'].strip():
                raise ValueError('摘要格式无效')
        response = self.agent.run(prompt)
        try:
            payload = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip()))
            validate_summary(payload)
        except (ValueError, TypeError):
            # Retry the JSON transport contract once, without reviewing evidence.
            response = self.agent.run(prompt + "\n上次格式无效，请返回含非空 summary 字符串的 JSON 对象。")
            payload = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip()))
            validate_summary(payload)
        summary = payload["summary"]
        details = []
        for index, result in enumerate(search_results, 1):
            details.append({
                **{key: result[key] for key in (
                    'doi', 'arxiv_id', 'authors', 'published_at', 'publication_type', 'venue',
                    'source_engines', 'versions', 'fetched_url', 'fetched_at', 'read_partial',
                    'read_attempts', 'discovered_at', 'metadata_status', 'verified_metadata',
                    'discovery_relation', 'retrieval_warnings', 'acquisition_status',
                    'context_status', 'context_chars', 'full_text_available', 'completeness_status', 'completeness_reason', 'pdf_empty_pages',
                    'acquisition_partial', 'document_id', 'failure_category',
                    'document_scope', 'source_type', 'analysis_text_hash',
                ) if key in result},
                "read_status": result.get("content_status", "仅搜索摘要"),
                "has_content": bool(result.get("content")),
                "read_depth": read_depth(result),
            })
        return TaskSummary(task=task, summary=summary,
            sources=[r['url'] for r in search_results],
            source_titles=[r.get('title') or r['url'] for r in search_results],
            source_details=details, retrieval=retrieval_record(task, search_results))

    def _format_results(self, results: list) -> str:
        """格式化搜索结果为提示文本"""
        lines = []
        for i, r in enumerate(results, 1):
            # The caller splits long documents into contiguous parts. Never
            # select or truncate a second time while building the model input.
            content = r.get('content', '')
            part = r.get('analysis_part')
            position = f"（原文字符 {part['start'] + 1}–{part['end']}）" if part else ''
            source_text = f"\n正文{position}:\n{content}" if content else ""
            status = r.get("content_status", "仅搜索摘要")
            lines.append(
                f"[{i}] {r['title']}\nURL: {r['url']}\n"
                f"文献资料（文献日期不等于产品发布日期）: {json.dumps({k: r[k] for k in ('doi', 'published_at', 'publication_type', 'source_type') if k in r}, ensure_ascii=False)}\n"
                f"读取状态: {status}\n"
                f"搜索摘要: {r.get('snippet', '') if not content else ''}{source_text}\n"
            )
        return "\n".join(lines)

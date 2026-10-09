"""Reuse task-independent whole-document notes within one research run."""
import hashlib
import json
import threading

from src.models import TodoItem, TaskSummary
from src.prompts import SUMMARIZER_PROMPT
from src.services.fulltext_analysis import ANALYSIS_FIELDS, analyze_fulltext
from src.services.reading_policy import select_sources

_LOCKS = [threading.Lock() for _ in range(64)]
NOTE_INTENT = ('完整记录这份来源本身的定义、方法、假设、实验条件和数字结果、适用范围、'
               '局限、反证及附录发现，供不同研究问题复用。不要围绕某个下游任务筛除其他方面。'
               '严格区分原文事实与作者推测；只引用该来源 [1]。')


def cache_key(service, record):
    digest = record.get('analysis_text_hash')
    if not digest:
        return None
    contract = {'version': 1, 'text_hash': digest,
                'fetched_url': record.get('fetched_url') or record['url'],
                'acquisition_status': record.get('acquisition_status'),
                'document_scope': record.get('document_scope'), 'prompt': SUMMARIZER_PROMPT,
                'intent': NOTE_INTENT, 'chunk_chars': service.fulltext_chunk_chars,
                'model': str(getattr(service.agent, 'model', '')),
                'endpoint': str(getattr(getattr(service.agent, 'client', None), 'base_url', '')),
                'system': str(getattr(service.agent, 'system_prompt', '')),
                'max_output_tokens': str(getattr(service.agent, 'max_output_tokens', '')),
                'thinking': str(getattr(service.agent, 'deepseek_thinking', False)),
                'reasoning_effort': str(getattr(service.agent, 'deepseek_reasoning_effort', ''))}
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cached_note(service, notes, research_id, record):
    key = cache_key(service, record)
    if key is None:
        return None
    data = notes.source_note(research_id, key)
    try:
        if data.get('key') != key or data['analysis']['analysis_text_hash'] != record['analysis_text_hash']:
            return None
        note = TaskSummary.model_validate(data['summary'])
        analysis = data['analysis']
        if (not note.summary.strip() or len(note.sources) != 1 or not analysis['analysis_coverage_verified']
                or analysis['analysis_status'] != 'completed'
                or analysis['analyzed_chars'] != record.get('extracted_chars')
                or analysis['analysis_missing_ranges']):
            return None
        return data
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def analyze_selected_sources(service, notes, research_id, task, records, reader, on_progress=None):
    selected = select_sources(service, notes, research_id, task, records, reader,
        reusable=lambda r: cached_note(service, notes, research_id, r) is not None, on_progress=on_progress)
    state = notes.summary_batches(research_id, task.id)
    legacy = bool(state and state.get('batches') and state.get('source_note_version') != 1)
    if not state or not state.get('batches'):
        notes.summary_batches(research_id, task.id, {'version': 1, 'batches': {}, 'source_note_version': 1})
    neutral = TodoItem(id=task.id, title='来源全文事实笔记', intent=NOTE_INTENT,
                       query='来源自身的定义、方法、结果与局限', verification_query='原文全部分块、末尾与附录')
    combined, reused = None, 0
    for record in selected:
        # Verify that the referenced snapshot still exists before claiming reuse.
        text = reader.analysis_text(record)
        if record.get('analysis_text_hash') and hashlib.sha256(text.encode()).hexdigest() != record['analysis_text_hash']:
            raise ValueError('全文快照哈希不一致，不能复用笔记')
        key = cache_key(service, record)
        lock = _LOCKS[int(key[:8], 16) % len(_LOCKS)] if key else threading.Lock()
        with lock:
            saved = cached_note(service, notes, research_id, record) if not legacy else None
            if saved:
                note = TaskSummary.model_validate(saved['summary'])
                record.update(saved['analysis'], analysis_reused=True)
                # Rebind source metadata without changing the note's local [1] citations.
                note.sources = [record['url']]
                note.source_titles = [record.get('title') or record['url']]
                note.task = task
                reused += 1
                if on_progress:
                    on_progress('复用已完成的全文笔记：' + record.get('title', record['url']))
            else:
                note = analyze_fulltext(service, notes, research_id, task if legacy else neutral,
                                        [record], reader, on_progress, merge_short_notes=not legacy)
                record['analysis_reused'] = False
                if key and not legacy:
                    notes.source_note(research_id, key, {'key': key, 'summary': note.model_dump(),
                        'analysis': {k: record[k] for k in ANALYSIS_FIELDS if k in record}})
                note.task = task
            combined = note if combined is None else service.merge_summaries(task, [], combined, note)
    combined.retrieval.update(analysis_version=1, reading_optimized=True,
                              selected_sources=len(selected), candidate_sources=len(records), reused_sources=reused)
    return combined

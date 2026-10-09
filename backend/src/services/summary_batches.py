"""Bounded source batches with durable success and truncation checkpoints."""
import hashlib
import json

from src.models import TaskSummary
from src.prompts import SUMMARIZER_PROMPT
from src.services.cost_control import OutputTruncated, ResearchLimit


class SummaryBatchIncomplete(ValueError):
    pass


def summarize_batches(service, notes, research_id, task, results, *, on_text_complete=None, **kwargs):
    state = notes.summary_batches(research_id, task.id) or {'version': 1, 'batches': {}}
    if state.get('version') != 1 or not isinstance(state.get('batches'), dict):
        raise SummaryBatchIncomplete('整理批次存档格式无效；原始检索资料已保留。')
    task_data = task.model_dump(exclude={'status'})
    # A query/source/context change invalidates only the affected cached work.
    contract = {'task': task_data, 'context': kwargs, 'prompt': SUMMARIZER_PROMPT,
                'fulltext_chunk_chars': service.fulltext_chunk_chars}

    def persist():
        notes.summary_batches(research_id, task.id, state)

    def completed(batch):
        if on_text_complete and len(batch) == 1 and batch[0].get('analysis_phase') == 'reading':
            position = batch[0].get('analysis_part')
            if position:
                on_text_complete(position['start'], position['end'])

    def can_split_text(batch, depth):
        return (len(batch) == 1 and batch[0].get('analysis_phase') in {'reading', 'synthesis'}
                and len(batch[0].get('content', '')) >= 2000 and depth < 4)

    def process(batch, depth=0):
        key = hashlib.sha256(json.dumps({**contract, 'sources': [service.evidence_key(r) for r in batch]},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        entry = state['batches'].get(key)
        if entry and entry.get('status') == 'completed':
            saved = TaskSummary.model_validate(entry['summary'])
            if saved.sources != [r['url'] for r in batch]:
                raise SummaryBatchIncomplete('整理批次来源与存档不一致，已停止复用。')
            saved.task = task
            completed(batch)
            return saved
        if entry and entry.get('status') == 'truncated' and can_split_text(batch, depth):
            entry['status'] = 'split_text'
            persist()
        if entry and entry.get('status') == 'truncated':
            raise SummaryBatchIncomplete(f'任务“{task.title}”的单来源整理仍超出输出上限；已保留截断内容和其他完成批次，不自动重试。')
        if not entry or entry.get('status') not in {'split', 'split_text'}:
            attempts = entry.get('attempts', 0) if entry else 0
            if attempts >= 2:
                raise SummaryBatchIncomplete(f'任务“{task.title}”有整理批次两次未完成；已保留其他批次，不重复消耗。')
            entry = {'status': 'running', 'attempts': attempts + 1,
                     'source_urls': [r['url'] for r in batch]}
            state['batches'][key] = entry
            persist()
            try:
                summary = service.summarize(task, batch, **kwargs)
            except OutputTruncated as error:
                entry.update(status='split' if len(batch) > 1 else
                             'split_text' if can_split_text(batch, depth) else 'truncated',
                             partial_output=error.content)
                persist()
                if entry['status'] == 'truncated':
                    raise SummaryBatchIncomplete(f'任务“{task.title}”的单来源整理输出被截断；已保留材料和完成批次，不影响其他独立任务。') from error
            except ResearchLimit:
                # A denied call is not a failed batch attempt. The global cost
                # ledger remains authoritative and is never reset here.
                entry.update(status='budget_stopped', attempts=attempts)
                persist()
                raise
            except Exception:
                entry['status'] = 'failed'
                persist()
                raise
            else:
                entry.update(status='completed', summary=summary.model_dump())
                persist()  # Also survives cancellation of the awaiting coroutine.
                completed(batch)
                return summary
        if entry.get('status') == 'split_text':
            row = batch[0]
            text = row['content']
            middle = len(text) // 2
            position = row.get('analysis_part', {'start': 0, 'end': len(text)})
            base = position['start']
            children = []
            for start, end in ((0, middle), (middle, len(text))):
                child = {**row, 'content': text[start:end],
                         'analysis_part': {**position, 'start': base + start, 'end': base + end}}
                children.append(process([child], depth + 1))
            # Both children describe the SAME source: citations remain [1].
            summary = children[0].model_copy(deep=True)
            summary.summary = '\n\n'.join(child.summary for child in children)
            entry.update(status='completed', summary=summary.model_dump())
            persist()
            return summary
        middle = len(batch) // 2
        first = process(batch[:middle])
        second = process(batch[middle:])
        summary = service.merge_summaries(task, batch[:middle], first, second)
        entry.update(status='completed', summary=summary.model_dump())
        persist()
        return summary

    combined, previous = None, []
    batches, pending, size = [], [], 0
    for result in results:
        encoded_size = len(json.dumps(result.get('content') or result.get('snippet') or '', ensure_ascii=False))
        if pending and (len(pending) == 4 or size + encoded_size > 30000):
            batches.append(pending)
            pending, size = [], 0
        pending.append(result)
        size += encoded_size
    if pending:
        batches.append(pending)
    for batch in batches:
        added = process(batch)
        combined = added if combined is None else service.merge_summaries(task, previous, combined, added)
        previous += batch
    if combined is None:
        raise SummaryBatchIncomplete('没有可整理的检索材料。')
    return combined

"""Sequential coverage of extracted text, with durable part and synthesis results."""
import hashlib
import json
from src.models import TaskSummary
from src.services.cost_control import ResearchLimit
from src.services.summary_batches import summarize_batches

ANALYSIS_FIELDS = ('analysis_version', 'analysis_status', 'analysis_scope', 'analysis_expected_chars',
                   'analyzed_chars', 'analysis_total_parts', 'analysis_completed_parts', 'analysis_text_hash', 'analysis_coverage_verified',
                   'analysis_completed_ranges', 'analysis_missing_ranges')


def split_text(text, chunk_chars=20000, encoded_budget=24000):
    """Contiguous, non-overlapping slices: joining them reconstructs every character."""
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        while len(json.dumps(text[start:end], ensure_ascii=False)) > encoded_budget and end - start > 1:
            end = start + (end - start) // 2
        if end < len(text):
            boundary = text.rfind('\n', start + (end - start) // 2, end)
            if boundary >= 0:
                end = boundary + 1
        yield start, end, text[start:end]
        start = end


def analyze_fulltext(service, notes, research_id, task, results, reader, on_progress=None, *, merge_short_notes=False):
    input_limit = getattr(service.agent, 'max_input_chars', 70000)
    input_limit = input_limit if isinstance(input_limit, int) else 70000
    text_budget = min(24000, max(1000, input_limit // 3))
    combined = None

    def progress(message):
        if on_progress:
            on_progress(message)

    for source_index, record in enumerate(results, 1):
        text = reader.analysis_text(record)
        digest = hashlib.sha256(text.encode()).hexdigest()
        parts = list(split_text(text, service.fulltext_chunk_chars, text_budget)) or [(0, 0, '')]
        if (''.join(p[2] for p in parts) != text or parts[0][0] != 0
                or parts[-1][1] != len(text)
                or any(a[1] != b[0] for a, b in zip(parts, parts[1:]))):
            raise ValueError('全文分块覆盖校验失败，未调用模型')
        if record.get('analysis_text_hash') and record['analysis_text_hash'] != digest:
            raise ValueError('全文快照哈希不一致，未调用模型')
        scope = ('extracted_full_text' if record.get('full_text_available') else
                 'available_text' if record.get('content') else 'search_snippet')
        record.update(analysis_version=1, analysis_status='reading', analysis_scope=scope,
                      analysis_expected_chars=len(text), analyzed_chars=0,
                      analysis_total_parts=len(parts), analysis_completed_parts=0,
                      analysis_coverage_verified=False, analysis_completed_ranges=[],
                      analysis_missing_ranges=[[0, len(text)]] if text else [])
        def mark_completed(start, end):
            ranges = sorted(record['analysis_completed_ranges'] + [[start, end]])
            merged = []
            for lo, hi in ranges:
                if merged and lo <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            missing, cursor = [], 0
            for lo, hi in merged:
                if lo > cursor:
                    missing.append([cursor, lo])
                cursor = hi
            if cursor < len(text):
                missing.append([cursor, len(text)])
            record.update(analysis_completed_ranges=merged, analysis_missing_ranges=missing,
                          analyzed_chars=sum(hi - lo for lo, hi in merged))
            progress(f"已分析 {record['analyzed_chars']:,}/{len(text):,} 字符")

        part_notes = []
        first = None
        for index, (start, end, content) in enumerate(parts, 1):
            progress(f'正在分析来源 {source_index}/{len(results)}：第 {index}/{len(parts)} 块')
            part = {**record, 'content': content, 'snippet': '', 'evidence_chunks': [],
                    'analysis_text_hash': digest, 'analysis_phase': 'reading',
                    'analysis_part': {'start': start, 'end': end},
                    'content_status': '按原文顺序提供的正文片段；其他片段单独分析'}
            # One source-part per call: completion can be attributed exactly.
            try:
                analyzed = summarize_batches(service, notes, research_id, task, [part],
                                              on_text_complete=mark_completed)
            except Exception:
                record['analysis_status'] = 'incomplete'
                progress('正文分析尚未完成，已保留完成分块和剩余字符区间')
                raise
            first = first or analyzed
            part_notes.append(analyzed.summary)
            record.update(analysis_completed_parts=index)
            progress(f'来源 {source_index}/{len(results)} 已分析 {end:,}/{len(text):,} 字符（{index}/{len(parts)} 块）')

        record['analysis_coverage_verified'] = (record['analyzed_chars'] == len(text)
                                                and not record['analysis_missing_ranges'])
        if not record['analysis_coverage_verified']:
            raise ValueError('全文分析区间未覆盖全部提取文本，不能标记为完成')
        record['analysis_status'] = 'summarizing'
        progress(f'正在汇总来源 {source_index}/{len(results)} 的分块分析')
        if merge_short_notes and len('\n\n'.join(part_notes)) <= min(12000, text_budget):
            # Local concatenation preserves EVERY finding and its [1] citation.
            # The final writer handles prose; no paid rewrite just to join notes.
            part_notes = ['\n\n'.join(part_notes)]
        # Read every part before synthesis. Hierarchical reduction handles long
        # books without dropping notes from their middle or end.
        for level in range(12):
            if len(part_notes) == 1:
                break
            joined = '\n\n'.join(part_notes)
            groups = list(split_text(joined, service.fulltext_chunk_chars, text_budget))
            reduced = []
            for start, end, content in groups:
                part = {**record, 'content': content, 'snippet': '', 'evidence_chunks': [],
                        'analysis_text_hash': digest, 'analysis_phase': 'synthesis',
                        'analysis_part': {'start': start, 'end': end, 'level': level},
                        'content_status': '同一来源的分段分析记录，不是新增原文'}
                result = summarize_batches(service, notes, research_id, task, [part])
                reduced.append(result.summary)
            part_notes = reduced
        if len(part_notes) != 1:
            raise ResearchLimit('全文已分块分析，但汇总仍超出容量；已保存所有分析结果。')
        record['analysis_status'] = 'completed'
        detail = {**(first.source_details[0] if first.source_details else {}),
                  **{key: record[key] for key in ANALYSIS_FIELDS if key in record},
                  'has_content': bool(record.get('content')),
                  'read_status': record.get('content_status', ''),
                  'read_depth': 'full_text' if scope == 'extracted_full_text' else 'available_text'}
        source_summary = TaskSummary(task=task, summary=part_notes[0], sources=[record['url']],
            source_titles=[record.get('title') or record['url']], source_details=[detail],
            retrieval={'analysis_version': 1})
        combined = source_summary if combined is None else service.merge_summaries(task, [], combined, source_summary)
        progress(f'来源 {source_index}/{len(results)} 已完成正文分析与汇总')
    if combined is None:
        raise ValueError('没有可分析的来源')
    combined.retrieval['analysis_version'] = 1
    return combined

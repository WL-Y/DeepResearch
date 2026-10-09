"""Public source diagnostics, excluding full text and model/internal prompts."""
from src.services.source_identity import merge_records


def source_details(checkpoints):
    records = []
    for task_id, checkpoint in checkpoints.items():
        results = checkpoint.get('results')
        if isinstance(results, list):
            for r in results:
                if isinstance(r, dict):
                    item = dict(r)
                    if 'reading_selected' in r:
                        item['reading_decisions'] = [{'task_id': task_id, 'selected': r['reading_selected'],
                            'reason': r.get('reading_selection_reason', '')}]
                    records.append(item)
    items = []
    for r in merge_records(records)[:100]:
        item = {k: r[k] for k in ('url', 'title', 'doi', 'authors', 'published_at', 'publication_type',
                                  'source_engines', 'versions', 'fetched_url', 'fetched_at', 'discovered_at',
                                  'content_status', 'read_partial', 'metadata_status', 'acquisition_status',
                                  'context_status', 'extracted_chars', 'context_chars', 'full_text_available', 'completeness_status', 'completeness_reason', 'pdf_empty_pages',
                                  'failure_category', 'analysis_version', 'analysis_status', 'analysis_scope',
                                  'analysis_expected_chars', 'analyzed_chars', 'analysis_total_parts',
                                  'analysis_completed_parts', 'analysis_coverage_verified', 'analysis_missing_ranges',
                                  'reading_decisions', 'estimated_reading_parts', 'analysis_reused') if k in r}
        item['has_content'] = bool(r.get('content'))
        item['evidence_chunk_count'] = len(r.get('evidence_chunks', []))
        warnings = [w for w in r.get('retrieval_warnings', []) if isinstance(w, dict)]
        item['warnings'] = list(dict.fromkeys(f"{w.get('provider', '')}: {w.get('category', '')}"
                                 for w in warnings if not (w.get('fallback_provider') and w.get('fallback_results', 0) > 0)))
        item['fallbacks'] = [{'provider': w.get('provider', ''), 'category': w.get('category', ''),
                             'via': w['fallback_provider'], 'count': w['fallback_results']}
                            for w in warnings if w.get('fallback_provider') and w.get('fallback_results', 0) > 0]
        items.append(item)
    return {
        'sources': items,
        'total': len(items),
        'read_count': sum(i['has_content'] for i in items),
        'full_text_count': sum(i.get('acquisition_status') == 'full_text_extracted' for i in items),
        'unconfirmed_text_count': sum(i.get('acquisition_status') == 'body_text_unconfirmed' for i in items),
        'partial_text_count': sum(i.get('acquisition_status') == 'partial_text_extracted' for i in items),
        'service_text_count': sum(i.get('acquisition_status') == 'service_extracted_text' for i in items),
        'snippet_only_count': sum(i.get('acquisition_status') == 'read_failed' for i in items),
        'metadata_only_count': sum(i.get('acquisition_status') == 'metadata_only' for i in items),
    }


def read_depth(result):
    acquisition = result.get('acquisition_status')
    context = result.get('context_status')
    if acquisition == 'full_text_extracted' and context == 'full_text_used':
        return 'full_text'
    if acquisition == 'full_text_extracted' and result.get('content'):
        return 'indexed_excerpt'
    if acquisition == 'partial_text_extracted' and result.get('content'):
        return 'partial_excerpt'
    if acquisition == 'service_extracted_text' and result.get('content'):
        return 'service_excerpt'
    if result.get('content'):
        return 'excerpt'  # The current reader cannot certify full-document coverage.
    if result.get('snippet'):
        return 'search_snippet'
    return 'title_only' if result.get('title') else 'unknown'


def retrieval_record(task, results):
    queries = list(dict.fromkeys([task.query, task.verification_query] +
                                 [q for r in results for q in r.get('matched_queries', [])]))
    statuses = {}
    for result in results:
        status = result.get('acquisition_status', 'legacy_unknown')
        statuses[status] = statuses.get(status, 0) + 1
    return {'queries': [q for q in queries if q], 'retained_candidates': len(results),
            'body_excerpt_count': sum(bool(r.get('content')) for r in results),
            'acquisition_status_counts': statuses,
            'discovered_at': sorted({r['discovered_at'] for r in results if r.get('discovered_at')}),
            'fetched_at': sorted({r['fetched_at'] for r in results if r.get('fetched_at')}),
            'exclusions_status': 'not_recorded',
            'note': '仅统计实际输入的候选资料，不代表搜索引擎总命中数；旧记录的缺失时间不推测。'}

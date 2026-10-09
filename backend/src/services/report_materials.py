"""Bound report inputs without imposing evidence-verification requirements."""
import json
import re

from src.services.cost_control import ResearchLimit
from src.services.editorial import prose_passages, is_internal_passage, is_material_limit, clean_limit
from src.services.references import display_reference_type


def compact_materials(summaries, max_chars=40000, *, strict=False, whole_paragraphs=False):
    """Select complete, source-linked passages for an article, not task logs."""
    sections, references = [], {}
    for summary, _ in summaries:
        ids = []
        for index, source in enumerate(summary.sources):
            match = re.match(r'\[(\d+)\]\s*(https?://\S+)', source)
            if not match:
                continue
            number = int(match[1])
            ids.append(number)
            detail = summary.source_details[index] if index < len(summary.source_details) else {}
            references.setdefault(number, {
                'id': number, 'url': match[2],
                'title': (summary.source_titles[index] if index < len(summary.source_titles) else match[2])[:160],
                'type': display_reference_type({
                    **detail,
                    'url': match[2],
                    'title': summary.source_titles[index] if index < len(summary.source_titles) else match[2],
                }),
                'basis': '已逐块分析全部提取正文' if detail.get('analysis_status') == 'completed' and detail.get('analysis_scope') == 'extracted_full_text' else
                         ('原文内容' if detail.get('document_scope') == 'paper_fulltext' else '网页正文')
                         if detail.get('has_content') else '搜索摘要或来源摘要',
            })
            versions = detail.get('consulted_urls', [])
            if len(versions) > 1:
                references[number]['related_versions'] = versions[:6]
        heading = re.sub(r'^任务\s*\d+\s*[：:.、-]?\s*', '', summary.task.title)
        heading = heading.strip() if heading.strip() not in {'新增资料', '新增或变化资料'} else ''
        passages = (list(dict.fromkeys(p.strip() for p in re.split(r'\n\s*\n', summary.summary) if p.strip()))
                    if whole_paragraphs else prose_passages(summary.summary))
        sections.append({'heading': heading[:100] or '研究发现', 'source_ids': list(dict.fromkeys(ids)),
                         'passages': [p for p in passages if not is_internal_passage(p)]})

    def priority(number):
        kind = references[number]['type']
        return 3 if kind.startswith(('一手论文', '官方资料')) else 2 if kind.startswith('综述') else 1

    # Reserve one reference per section before filling the catalog.
    selected = []
    for section in sections:
        if section['source_ids']:
            number = max(section['source_ids'], key=priority)
            if number not in selected and len(selected) < 15:
                selected.append(number)
    selected += sorted((n for n in references if n not in selected), key=priority, reverse=True)[:15 - len(selected)]
    if strict:
        selected = list(references)
    payload = {'sections': [{'heading': s['heading'], 'summary': '', 'limitations': []} for s in sections],
               'sources': [references[n] for n in selected]}
    encode = lambda: json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    # Source-directory capacity is an input-size concern, never an evidence gate.
    if strict and len(encode()) > max_chars:
        raise ResearchLimit('报告资料目录超出输入容量，已保留全部来源，未丢弃材料。')
    while not strict and payload['sources'] and len(encode()) > max_chars // 2:
        payload['sources'].pop()
    if len(encode()) > max_chars:
        raise ResearchLimit('报告资料目录超出输入容量，请缩小研究范围。')
    included = {source['id'] for source in payload['sources']}
    # Never erase citations while keeping their assertion. Skip the entire
    # passage when one of its sources cannot fit in the selected catalog.
    if strict and any(not {int(n) for n in re.findall(r'\[(\d+)\]', p)} <= included
                      for s in sections for p in s['passages']):
        raise ResearchLimit('报告材料含无法对应来源的引用，未静默删除相关结论。')
    queues = [[p for p in s['passages'] if {int(n) for n in re.findall(r'\[(\d+)\]', p)} <= included]
              for s in sections]
    for offset in range(max((len(q) for q in queues), default=0)):
        for section, queue in zip(payload['sections'], queues):
            if offset >= len(queue):
                continue
            passage = queue[offset]
            if is_material_limit(passage):
                section['limitations'].append(clean_limit(passage))
                if len(encode()) > max_chars:
                    if strict:
                        raise MaterialsTooLarge('报告材料需要分层汇总')
                    section['limitations'].pop()
            else:
                previous = section['summary']
                section['summary'] = (previous + '\n\n' + passage).strip()
                if len(encode()) > max_chars:
                    if strict:
                        raise MaterialsTooLarge('报告材料需要分层汇总')
                    section['summary'] = previous
    return encode()


class MaterialsTooLarge(ResearchLimit):
    """All sources fit, but findings require explicit compression."""

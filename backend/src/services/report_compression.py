"""Prepare report evidence locally: no model calls before the single writing call."""
import hashlib
import json
import re

from src.services.cost_control import ResearchBudget, ResearchLimit
from src.services.report_materials import compact_materials


def prepare_materials(service, normalized, research_id, on_progress=None, *, max_chars=None, max_tokens=None):
    limit = min(service.report_materials_max_chars, max_chars or service.report_materials_max_chars)
    # This first pass preserves all substantive passages and source references.
    # Capacity selection, when needed, happens explicitly below, never by slicing.
    capacity = 10000 + 6 * sum(len(json.dumps(s.model_dump(), ensure_ascii=False)) for s, _ in normalized)
    full = json.loads(compact_materials(normalized, capacity, strict=True, whole_paragraphs=True))
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    units = []
    for section in full['sections']:
        units.append([('summary', p) for p in section['summary'].split('\n\n') if p.strip()]
                     + [('limitations', p) for p in section['limitations']])
    total = sum(map(len, units))
    selection = {'mode': 'complete_notes', 'total_passages': total, 'included_passages': total,
                 'omitted_passages': 0, 'original_notes_preserved': True}
    full['material_selection'] = selection

    def fits(value):
        text = encode(value)
        return len(text) <= limit and (max_tokens is None or ResearchBudget.estimate_tokens(text) <= max_tokens)

    selected = [set(range(len(group))) for group in units]
    if not fits(full):
        selected = [set() for _ in units]
        selection = {**selection, 'mode': 'selected_passages', 'included_passages': 0,
                     'omitted_passages': total}
        payload = {'sections': [{'heading': s['heading'], 'summary': '', 'limitations': []}
                                for s in full['sections']], 'sources': full['sources'],
                   'material_selection': selection}

        def render():
            for index, section in enumerate(payload['sections']):
                section['summary'] = '\n\n'.join(units[index][i][1] for i in sorted(selected[index])
                                                 if units[index][i][0] == 'summary')
                section['limitations'] = [units[index][i][1] for i in sorted(selected[index])
                                         if units[index][i][0] == 'limitations']
            count = sum(map(len, selected))
            selection.update(included_passages=count, omitted_passages=total-count)
            return payload

        def admit(section, index):
            selected[section].add(index)
            if fits(render()):
                return True
            selected[section].remove(index)
            render()
            return False

        def ids(text):
            return set(re.findall(r'\[(\d+)\]', text))

        def is_limit(unit):
            return unit[0] == 'limitations' or bool(re.search(
                r'局限|限制|失败|反证|不确定|不可比|limitation|failure|uncertain', unit[1], re.I))

        def rank(unit):
            text = unit[1]
            return (bool(ids(text)), is_limit(unit), bool(re.search(r'\d', re.sub(r'\[\d+\]', '', text))), -len(text))

        # Reserve coverage for every task, every cited source and task limitations.
        # If even this cannot fit, stop before paying; do not produce an empty report.
        for section, group in enumerate(units):
            if group and not any(admit(section, i) for i in sorted(range(len(group)), key=lambda i: rank(group[i]), reverse=True)):
                raise ResearchLimit('报告输入预算不足以覆盖每个研究任务；原始笔记已保留。')
        required = {str(source['id']) for source in full['sources']}
        covered = set().union(*(ids(units[s][i][1]) for s, group in enumerate(selected) for i in group))
        while required - covered:
            candidates = [(s, i) for s, group in enumerate(units) for i, unit in enumerate(group)
                          if i not in selected[s] and ids(unit[1]) & (required - covered)]
            candidates.sort(key=lambda si: (len(ids(units[si[0]][si[1]][1]) & (required-covered)),
                                            -len(units[si[0]][si[1]][1])), reverse=True)
            chosen = next(((s, i) for s, i in candidates if admit(s, i)), None)
            if chosen is None:
                raise ResearchLimit('报告输入预算不足以保留各来源的完整证据段落；原始笔记已保留。')
            covered |= ids(units[chosen[0]][chosen[1]][1])
        for s, group in enumerate(units):
            limits = [i for i, unit in enumerate(group) if is_limit(unit)]
            if limits and not any(i in selected[s] for i in limits):
                if not any(admit(s, i) for i in sorted(limits, key=lambda i: len(group[i][1]))):
                    raise ResearchLimit('报告输入预算不足以保留任务的限制条件；原始笔记已保留。')
        queues = [sorted((i for i in range(len(group)) if i not in selected[s]),
                         key=lambda i: rank(group[i]), reverse=True) for s, group in enumerate(units)]
        for offset in range(max(map(len, queues), default=0)):
            for s, queue in enumerate(queues):
                if offset < len(queue):
                    admit(s, queue[offset])
        full = render()

    packed = encode(full)
    manifest = {'version': 2, 'status': 'completed', **full['material_selection'],
                'output_chars': len(packed), 'estimated_input_tokens': ResearchBudget.estimate_tokens(packed),
                'selected_passage_indices': [sorted(group) for group in selected],
                'signature': hashlib.sha256(packed.encode()).hexdigest(), 'materials': full,
                'model_calls': 0}
    # Preserve old paid reduction checkpoints for audit instead of rewriting them.
    service.note_tool.save_report_input(research_id, manifest)
    if on_progress:
        if selection['mode'] == 'complete_notes':
            on_progress(f'已在本地整理全部报告笔记（{len(packed):,} 字符），直接一次写作')
        else:
            on_progress(f'已在本地选取 {selection["included_passages"]}/{total} 个完整证据段落；覆盖全部任务和来源，原始笔记保留')
    return packed

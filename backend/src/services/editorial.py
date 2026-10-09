"""Remove machine-inserted labels from writing inputs, preserving substantive limits."""
import re


def prose_passages(text):
    # These are exact pipeline labels, not a ban on the word "task" in a topic.
    text = re.sub(r'^\s*(?:#{1,6}\s*)?(?:任务\s*\d+\s*)?新增(?:或变化)?资料[：:]?\s*$', '', text, flags=re.M)
    text = re.sub(r'^\s*新增或变化材料（与此前结论对照，冲突不得删除）：\s*$', '', text, flags=re.M)
    text = re.sub(r'【内部限定：[^】]*】', '', text)
    passages = []
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        # Keep tables and code as units; never cut a table in the middle.
        pieces = [paragraph] if '|' in paragraph or '```' in paragraph else re.split(r'(?<=[。！？])(?!\[|[）”’])|(?<=[.!?])\s+(?=[A-Z])', paragraph)
        for piece in pieces:
            piece = piece.strip()
            if piece and piece not in passages:
                passages.append(piece)
    return passages


def is_internal_passage(text):
    return bool(re.search(r'\[(?:任务\s*\d+|\d+\s*所在任务)[^\]]*\]|\[引用不可用\]|\b(?:verification_failed|doi_verified|metadata_status|selected_passages_used|full_text_extracted)\b', text))


def is_material_limit(text):
    return bool(re.search(r'未取得|未提供|未获得|未能获取|本轮(?:资料|材料|选段|证据)|本次(?:检索|摘录)', text))


def clean_limit(text):
    return re.sub(r'本轮(?:资料|材料|选段|证据)|本次(?:检索|摘录)', '现有资料', text)

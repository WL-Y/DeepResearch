"""Bibliographic presentation, without certifying claims or blocking reports."""
import re
from urllib.parse import urlsplit

from src.services.evidence import source_key
from src.services.source_identity import merge_records, normalize_url


def reference_title(title):
    title = re.sub(r'\s+', ' ', title or '').strip()
    title = re.sub(r'^(?:ICLR\s+(?:Poster|Oral|Spotlight)\s+|\[Paper Note\]\s*|\[\d{4}\.\d{4,5}(?:v\d+)?\]\s*)', '', title, flags=re.I)
    return re.sub(r'\s*[|–—-]\s*(?:alphaXiv|arXiv|OpenReview)\s*$', '', title, flags=re.I)


def reference_type(record):
    """Labels describe document type, not an automated evidence verdict."""
    title = reference_title(record.get('title', ''))
    host = (urlsplit(record.get('url', '')).hostname or '').lower()
    publication = str(record.get('publication_type') or '').lower()
    paper = (record.get('document_scope') == 'paper_fulltext'
             or host in {'arxiv.org', 'export.arxiv.org', 'aclanthology.org', 'openaccess.thecvf.com'}
             or (host in {'doi.org', 'dx.doi.org'} and source_key(record.get('url', '')).startswith('arxiv:'))
             or publication in {'journal-article', 'proceedings-article', 'preprint', 'article', 'review'})
    mirror = host in {'alphaxiv.org', 'www.alphaxiv.org', 'en.papernotes.org'}
    if mirror:
        return '第三方整理'
    if paper and re.search(r'\bsurvey\b|\breview\b|综述|系统性回顾', title, re.I):
        return '综述'
    if paper:
        return '一手论文'
    if record.get('source_type') == 'official' or re.search(r'(?:^|\.)gov(?:\.[a-z]{2})?$', host):
        return '官方资料'
    if host in {'openreview.net', 'iclr.cc', 'neurips.cc', 'icml.cc'}:
        return '会议资料'
    if record.get('source_type') == 'third_party' or re.search(r'\b(?:blog|blogs|wiki)\b', record.get('url', ''), re.I):
        return '第三方整理'
    return '网页资料'


def reference_priority(record):
    kind = reference_type(record)
    # Bibliographic records and original documents ahead of discovery wrappers.
    host = (urlsplit(record.get('url', '')).hostname or '').lower()
    doi = record.get('doi') or ''
    published = bool(doi and not doi.startswith('10.48550/') and record.get('metadata_status') == 'doi_verified')
    return (published, kind in {'一手论文', '官方资料'}, kind == '综述',
            host not in {'alphaxiv.org', 'www.alphaxiv.org'},
            record.get('document_scope') == 'paper_fulltext', bool(record.get('has_content') or record.get('content')))


def citation_catalog(records):
    """Choose one stable main reference per known work, retaining read origins.

    Exact title matching is limited to a scholarly record and a conference/mirror
    wrapper. Two independent papers are never joined solely by title.
    """
    records = [dict(r) for r in records]
    for record in list(records):
        actual_url = record.get('fetched_url')
        if actual_url and actual_url != record['url'] and record.get('document_scope') == 'paper_fulltext':
            actual = {**record, 'url': actual_url}
            if reference_type(actual) in {'一手论文', '综述'}:
                records.append(actual)
    groups = merge_records(records)
    title_groups = {}
    for group in groups:
        title = reference_title(group.get('title', ''))
        key = re.sub(r'\W+', '', title.casefold())
        if len(key) >= 35 and '...' not in title and '…' not in title:
            title_groups.setdefault(key, []).append(group)
    for matches in title_groups.values():
        scholarly = [g for g in matches if reference_type(g) in {'一手论文', '综述'}
                     or source_key(g['url']).startswith('arxiv:')]
        wrappers = [g for g in matches if g not in scholarly and reference_type(g) in {'会议资料', '第三方整理'}]
        if len(scholarly) == 1:
            base = scholarly[0]
            for wrapper in wrappers:
                base['versions'].extend(wrapper.get('versions', []))
                groups.remove(wrapper)
    catalog = {}
    for group in groups:
        keys = {source_key(group['url'])}
        keys.update(source_key(v['url']) for v in group.get('versions', []) if v.get('url'))
        candidates = [r for r in records if source_key(r.get('url', '')) in keys]
        best = max(candidates or [group], key=reference_priority)
        chosen = dict(best)
        chosen['url'] = normalize_url(best['url'])
        chosen['title'] = reference_title(best.get('title') or group.get('title') or chosen['url'])
        chosen['reference_type'] = reference_type(best)
        chosen['consulted_urls'] = list(dict.fromkeys(r['url'] for r in candidates))
        # Show one arXiv work link while preserving actual version URLs internally.
        if source_key(chosen['url']).startswith('arxiv:') and reference_type(best) != '第三方整理':
            chosen['url'] = 'https://arxiv.org/abs/' + source_key(chosen['url']).split(':', 1)[1]
        for key in keys:
            catalog[key] = chosen
    return catalog


def display_reference_type(record):
    label = record.get('reference_type') or reference_type(record)
    if source_key(record.get('url', '')).startswith('arxiv:') and label in {'一手论文', '综述'}:
        label += '·预印本'
    return label

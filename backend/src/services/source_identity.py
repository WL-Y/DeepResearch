"""Merge discovery records, retaining aliases and version-specific read provenance."""
import re
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote
from src.services.evidence import source_key


def normalize_url(url):
    try:
        p = urlsplit(url.strip())
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
            return ''
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid', 'msclkid'}]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', urlencode(query), ''))
    except (ValueError, AttributeError):
        return ''


def normalize_doi(value):
    value = unquote(str(value or '')).strip()
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value, flags=re.I)
    return value.casefold() if re.fullmatch(r'10\.\d{4,9}/\S+', value, re.I) else ''


def tokens(text):
    text = text.casefold()
    english = re.findall(r'[a-z0-9][a-z0-9_.-]*', text)
    chinese = [chunk[i:i + 2] for chunk in re.findall(r'[\u3400-\u9fff]+', text) for i in range(len(chunk) - 1)]
    return set(english + chinese) - {'the', 'and', 'for', 'of', 'in', 'original', 'paper', 'official'}


def identity_keys(item):
    keys = {'url:' + source_key(item['url'])}
    doi = normalize_doi(item.get('doi'))
    if not doi and (urlsplit(item['url']).hostname or '') in {'doi.org', 'dx.doi.org'}:
        doi = normalize_doi(item['url'])
    if doi:
        item['doi'] = doi
        keys.add('doi:' + doi)
    arxiv = item.get('arxiv_id') or ''
    if arxiv:
        keys.add('arxiv:' + re.sub(r'v\d+$', '', arxiv))
    if source_key(item['url']).startswith('arxiv:'):
        keys.add(source_key(item['url']))
    for field in ('semantic_scholar_id', 'openalex_id'):
        if item.get(field):
            keys.add(field + ':' + item[field])
    return keys


def same_title_authors(left, right):
    # Only conservative exact title + two shared named authors, never title alone.
    title = lambda x: re.sub(r'\W+', '', x.get('title', '').casefold())
    if len(title(left)) < 24 or title(left) != title(right):
        return False
    # An arXiv DOI identifies a preprint, not a conflicting journal publication.
    publication_doi = lambda item: item.get('doi') if not str(item.get('doi', '')).startswith('10.48550/arxiv.') else None
    if publication_doi(left) and publication_doi(right) and left['doi'] != right['doi']:
        return False
    authors = lambda x: {re.sub(r'\W+', '', a.casefold()) for a in x.get('authors', []) if a}
    return len(authors(left) & authors(right)) >= 2


def merge_records(records):
    groups = []
    for raw in records:
        item = deepcopy(raw)
        item['url'] = normalize_url(item.get('url', ''))
        if not item['url']:
            continue
        item.setdefault('title', item['url'])
        item.setdefault('snippet', '')
        item['source_engines'] = sorted(set(item.get('source_engines', []) + ([item['source_engine']] if item.get('source_engine') else [])))
        keys = identity_keys(item)
        matches = [g for g in groups if keys & g[0] or same_title_authors(g[1], item)]
        if not matches:
            item.setdefault('versions', [{'url': item['url'], 'published_at': item.get('published_at', ''),
                                         'publication_type': item.get('publication_type', 'unknown')}])
            groups.append([keys, item])
            continue
        target = matches[0]
        # A bridge record (DOI + arXiv) can join two earlier result groups.
        for other_keys, other in [*matches[1:], [keys, item]]:
            target[0].update(other_keys)
            dest = target[1]
            for field in ('source_engines', 'matched_queries', 'fulltext_urls', 'discovery_relation', 'retrieval_warnings', 'reading_decisions'):
                for value in other.get(field, []):
                    if value not in dest.setdefault(field, []):
                        dest[field].append(value)
            for version in other.get('versions', [{'url': other['url'], 'published_at': other.get('published_at', ''), 'publication_type': other.get('publication_type', 'unknown')}]):
                if version not in dest['versions']:
                    dest['versions'].append(version)
            if other.get('content') and not dest.get('content'):
                # A passage always travels with its actual URL/status; do not combine texts from versions.
                for field in ('content', 'fetched_url', 'content_status', 'read_partial',
                              'acquisition_partial', 'acquisition_status', 'context_status',
                              'context_chars', 'full_text_available', 'completeness_status', 'completeness_reason', 'pdf_empty_pages', 'document_id',
                              'evidence_chunks', 'extracted_chars', 'fetched_at', 'read_query'):
                    if field in other:
                        dest[field] = other[field]
                for field in ('analysis_text_hash', 'analysis_text'):
                    dest.pop(field, None)
                    if field in other:
                        dest[field] = other[field]
            for field, value in other.items():
                if value and not dest.get(field):
                    dest[field] = value
            if len(other.get('snippet', '')) > len(dest.get('snippet', '')):
                dest['snippet'] = other['snippet']
        for other in matches[1:]:
            groups.remove(other)
    return [g[1] for g in groups]

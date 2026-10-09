"""Official scholarly APIs. Metadata/abstracts are never labelled as full text."""
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote
from .base import SearchEngine
from .http import ProviderHTTP, SearchError


class ArxivSearch(SearchEngine):
    name = 'arxiv'

    def __init__(self, read_timeout=30):
        self.http = ProviderHTTP(self.name, interval=3.1, timeout=(8, read_timeout))

    def search(self, query, max_results=5):
        # Planner supplies natural language, not arbitrary API query operators.
        stopwords = {'the', 'and', 'in', 'of', 'for', 'on', 'a', 'an', 'original', 'paper', 'papers', 'official'}
        terms = [t for t in re.findall(r'[\w-]+', query) if t.casefold() not in stopwords][:8]
        if not terms:
            return []
        response = self.http.request('GET', 'https://export.arxiv.org/api/query', params={
            'search_query': ' AND '.join(f'all:"{term}"' for term in terms),
            'start': 0, 'max_results': min(max_results, 20), 'sortBy': 'relevance',
        })
        try:
            return self.parse(response.text)
        except ET.ParseError:
            raise SearchError(self.name, 'invalid_response') from None
        finally:
            response.close()

    @staticmethod
    def parse(text):
        ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
        root = ET.fromstring(text)
        results = []
        for entry in root.findall('a:entry', ns):
            url = entry.findtext('a:id', '', ns).replace('http://', 'https://')
            if '/abs/' not in url:
                raise SearchError('arxiv', 'invalid_response')
            results.append({
                'title': ' '.join(entry.findtext('a:title', '', ns).split()),
                'url': url, 'snippet': ' '.join(entry.findtext('a:summary', '', ns).split()),
                'arxiv_id': url.split('/abs/')[-1], 'doi': entry.findtext('x:doi', '', ns),
                'authors': [a.findtext('a:name', '', ns) for a in entry.findall('a:author', ns)],
                'published_at': entry.findtext('a:published', '', ns),
                'updated_at': entry.findtext('a:updated', '', ns),
                'publication_type': 'preprint', 'venue': entry.findtext('x:journal_ref', '', ns),
                'fulltext_urls': [link.get('href', '').replace('http://', 'https://')
                                 for link in entry.findall('a:link', ns) if link.get('title') == 'pdf'],
            })
        return results


class SemanticScholarSearch(SearchEngine):
    name = 'semantic_scholar'
    fields = 'title,url,abstract,externalIds,authors,year,openAccessPdf,publicationTypes'

    def __init__(self, api_key=''):
        self.http = ProviderHTTP(self.name, interval=1.1 if api_key else 3.1)
        self.headers = {'x-api-key': api_key} if api_key else {}

    def search(self, query, max_results=5):
        data = self.http.json('GET', 'https://api.semanticscholar.org/graph/v1/paper/search',
                             headers=self.headers, params={'query': query, 'limit': min(max_results, 20), 'fields': self.fields})
        if not isinstance(data.get('data'), list):
            raise SearchError(self.name, 'invalid_response')
        return [self.parse(p) for p in data['data'] if p.get('title') and p.get('paperId')]

    @staticmethod
    def parse(p):
        ids = p.get('externalIds') or {}
        arxiv_id = ids.get('ArXiv', '')
        doi = ids.get('DOI', '')
        pdf = (p.get('openAccessPdf') or {}).get('url', '')
        return {
            'title': p['title'], 'snippet': p.get('abstract') or '',
            'url': f'https://arxiv.org/abs/{arxiv_id}' if arxiv_id else
                   f'https://doi.org/{doi}' if doi else p.get('url') or f"https://www.semanticscholar.org/paper/{p['paperId']}",
            'doi': doi, 'arxiv_id': arxiv_id, 'semantic_scholar_id': p['paperId'],
            'authors': [a['name'] for a in (p.get('authors') or []) if a.get('name')],
            'published_at': str(p.get('year') or ''),
            'publication_type': ','.join(p.get('publicationTypes') or []) or 'unknown',
            'fulltext_urls': [pdf] if pdf else [],
        }

    def expand(self, seed, limit=4):
        paper_id = seed.get('semantic_scholar_id')
        if not paper_id:
            return []
        results = []
        # One hop only, half backward references and half forward citations.
        for edge, field in [('references', 'citedPaper'), ('citations', 'citingPaper')]:
            data = self.http.json('GET', f'https://api.semanticscholar.org/graph/v1/paper/{quote(paper_id, safe="")}/{edge}',
                                 headers=self.headers, params={'fields': self.fields, 'limit': max(1, (limit + 1) // 2)})
            if not isinstance(data.get('data'), list):
                raise SearchError(self.name, 'invalid_response')
            for row in data['data']:
                paper = row.get(field) or {}
                if paper.get('paperId') and paper.get('title'):
                    item = self.parse(paper)
                    item['discovery_relation'] = [{'type': edge, 'seed_url': seed['url']}]
                    results.append(item)
        return results[:limit]


class OpenAlexSearch(SearchEngine):
    name = 'openalex'

    def __init__(self, api_key=''):
        self.http = ProviderHTTP(self.name, interval=.3)
        self.headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}

    def search(self, query, max_results=5):
        data = self.http.json('GET', 'https://api.openalex.org/works', headers=self.headers,
                             params={'search': query, 'per_page': min(max_results, 20)})
        if not isinstance(data.get('results'), list):
            raise SearchError(self.name, 'invalid_response')
        return [self.parse(p) for p in data['results'] if p.get('title')]

    @staticmethod
    def parse(p):
        words = {}
        for word, positions in (p.get('abstract_inverted_index') or {}).items():
            for position in positions:
                words[position] = word
        location = p.get('primary_location') or {}
        open_location = p.get('best_oa_location') or {}
        doi = p.get('doi') or ''
        return {
            'title': p['title'], 'snippet': ' '.join(words[i] for i in sorted(words)),
            'url': doi or location.get('landing_page_url') or p['id'],
            'doi': doi, 'openalex_id': p['id'],
            'authors': [a['author']['display_name'] for a in (p.get('authorships') or []) if a.get('author', {}).get('display_name')],
            'published_at': p.get('publication_date') or '', 'publication_type': p.get('type') or 'unknown',
            'venue': (location.get('source') or {}).get('display_name') or '',
            'fulltext_urls': [open_location['pdf_url']] if open_location.get('pdf_url') else [],
        }


class CrossrefMetadata:
    """Exact DOI lookups only. Never infer a DOI from a loose title match."""
    name = 'crossref'

    def __init__(self, mailto=''):
        self.http = ProviderHTTP(self.name, interval=1.1)
        self.params = {'mailto': mailto} if mailto else {}

    def lookup(self, doi):
        data = self.http.json('GET', 'https://api.crossref.org/works/' + quote(doi, safe=''), params=self.params)
        p = data.get('message')
        if not isinstance(p, dict) or not p.get('DOI') or p['DOI'].casefold() != doi.casefold():
            raise SearchError(self.name, 'metadata_mismatch')
        date = (p.get('published') or p.get('issued') or {}).get('date-parts', [[]])[0]
        return {'doi': p['DOI'], 'title': ' '.join(p.get('title') or []),
                'authors': [' '.join(filter(None, [a.get('given'), a.get('family')])) for a in p.get('author', [])],
                'published_at': '-'.join(str(n).zfill(2) for n in date),
                'publication_type': p.get('type') or 'unknown', 'venue': ' '.join(p.get('container-title') or []),
                'publisher': p.get('publisher') or '', 'updates': p.get('update-to') or [],
                'metadata_status': 'doi_verified'}

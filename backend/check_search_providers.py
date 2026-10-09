"""Opt-in smoke checks against public scholarly APIs; no LLM or paid web searches."""
import argparse
import json
from src.config import settings
from src.tools.search.academic import ArxivSearch, SemanticScholarSearch, OpenAlexSearch, CrossrefMetadata
from src.tools.search.tavily import TavilySearch
from src.tools.search.http import SearchError
from src.services.source_reader import SourceReader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly allow a small number of external API requests')
    parser.add_argument('--read-content', action='store_true', help='Also verify actual document/extraction coverage')
    args = parser.parse_args()
    if not args.live:
        parser.error('Use --live to make external API requests; use unittest test_retrieval for offline tests.')
    checks = [
        ('arxiv', lambda: ArxivSearch().search('world models', 1)),
        ('semantic_scholar', lambda: SemanticScholarSearch(settings.semantic_scholar_api_key).search('world models', 1)),
        ('openalex', lambda: OpenAlexSearch(settings.openalex_api_key).search('world models', 1)),
        ('crossref', lambda: [CrossrefMetadata(settings.crossref_mailto).lookup('10.1038/nphys1170')]),
    ]
    for name, run in checks:
        try:
            results = run()
            print(json.dumps({'provider': name, 'status': 'ok' if results else 'no_results',
                              'count': len(results), 'title': results[0].get('title') if results else None}, ensure_ascii=False), flush=True)
        except Exception as error:
            print(json.dumps({'provider': name, 'status': error.category if isinstance(error, SearchError) else type(error).__name__}), flush=True)
    if args.read_content:
        reader = SourceReader(
            cache_dir='./cache/source', cache_ttl=settings.source_cache_ttl_seconds,
            max_bytes=settings.source_max_bytes, pdf_max_pages=settings.source_pdf_max_pages,
            max_extracted_chars=settings.source_max_extracted_chars,
        )
        document = reader.read_document('https://arxiv.org/abs/1803.10122', 'world models methods experiments')
        print(json.dumps({
            'provider': 'source_reader', 'status': document.get('acquisition_status'),
            'context_status': document.get('context_status'),
            'extracted_chars': document.get('extracted_chars', 0),
            'context_chars': document.get('context_chars', 0),
            'evidence_chunks': len(document.get('evidence_chunks', [])),
            'fetched_url': document.get('fetched_url'),
            'failure_category': document.get('failure_category'),
        }, ensure_ascii=False), flush=True)
        if settings.tavily_api_key and not settings.tavily_api_key.startswith('your_'):
            try:
                extracted = TavilySearch(settings.tavily_api_key).extract([
                    'https://arxiv.org/abs/1803.10122'])
                print(json.dumps({
                    'provider': 'tavily_extract',
                    'status': 'ok' if extracted else 'no_content',
                    'extracted_chars': len(extracted[0].get('raw_content', '')) if extracted else 0,
                }, ensure_ascii=False), flush=True)
            except Exception as error:
                print(json.dumps({'provider': 'tavily_extract',
                    'status': error.category if isinstance(error, SearchError) else type(error).__name__}), flush=True)


if __name__ == '__main__':
    main()

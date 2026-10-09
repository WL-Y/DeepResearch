"""Budgeted discovery, scholarly routing, provenance, ranking and evidence reading."""
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit
from src.tools.search import ENGINES
from src.tools.search.academic import CrossrefMetadata
from src.tools.search.http import SearchError, SearchUnavailable
from src.config import Settings
from src.services.source_reader import SourceReader
from src.services.source_identity import merge_records, normalize_url, normalize_doi
from src.services.retrieval_cache import RetrievalCache
from src.services.retrieval_ranking import research_profile, is_fresh_query, lexical_score, semantic_rerank, clearly_off_topic
from src.services.retrieval_ranking import method_present
from src.services.references import reference_type
from src.services.evidence import source_key
from src.services.performance import measured, submit, count, span
from src.services.cost_control import ResearchLimit

logger = logging.getLogger(__name__)


class SearchService:
    def __init__(self, settings: Settings, reranker=None):
        self.settings = settings
        self.reranker = reranker
        self.cache_dir = Path('./cache/search')
        self.source_reader = SourceReader(
            cache_dir='./cache/source', cache_ttl=settings.source_cache_ttl_seconds,
            max_bytes=settings.source_max_bytes,
            pdf_max_pages=settings.source_pdf_max_pages,
            max_extracted_chars=settings.source_max_extracted_chars,
        )
        self.engines = []
        self.academic_engines = []
        self.fallback_engines = []
        names = settings.search_engines or settings.search_api
        for target, configured in ((self.engines, names), (self.academic_engines, settings.academic_search_engines),
                                   (self.fallback_engines, settings.search_fallback_engines)):
            for name in dict.fromkeys(n.strip() for n in configured.split(',') if n.strip()):
                if name not in ENGINES:
                    logger.warning('Unknown search provider: %s', name)
                    continue
                if name in {'tavily', 'semantic_scholar', 'openalex'}:
                    key = getattr(settings, name + '_api_key')
                    if name == 'tavily' and (not key or key.startswith('your_')):
                        logger.warning('tavily: missing_api_key; provider disabled')
                        continue
                    engine = ENGINES[name](api_key=key)
                elif name == 'arxiv':
                    engine = ENGINES[name](read_timeout=settings.arxiv_read_timeout_seconds)
                elif name in {'duckduckgo', 'yahoo', 'brave_web', 'google'}:
                    engine = ENGINES[name](timeout=settings.search_web_timeout_seconds, retries=settings.search_web_retries)
                else:
                    engine = ENGINES[name]()
                target.append(engine)
        self.crossref = CrossrefMetadata(settings.crossref_mailto) if settings.crossref_enabled else None
        if not self.engines:
            raise ValueError('没有可用的通用搜索引擎，请检查配置')

    def _option(self, name, default):
        return getattr(getattr(self, 'settings', None), name, default)

    def _engines_for(self, query):
        profile = self._option('search_profile', 'auto')
        scholarly = profile == 'academic' or (profile == 'auto' and research_profile(query) == 'academic')
        engines = self.engines + (getattr(self, 'academic_engines', []) if scholarly else [])
        return list({e.name: e for e in engines}.values())

    @measured('provider_search')
    def _provider_search(self, engine, query, max_results):
        cache = RetrievalCache(self.cache_dir)
        ttl = self._option('search_fresh_cache_ttl_seconds', 3600) if is_fresh_query(query) else self._option('search_cache_ttl_seconds', 86400)
        key = f'search-v4|{engine.name}|{query}|{max_results}'
        cached = cache.get(key, ttl)
        if isinstance(cached, list) and cached and all(isinstance(r, dict) and r.get('url') for r in cached):
            count('search_cache_hits')
            return cached
        count('search_cache_misses')
        with span('provider.' + engine.name):
            results = engine.search(query, max_results)
        if not isinstance(results, list):
            raise SearchError(engine.name, 'invalid_response')
        normalized = []
        for r in results[:max_results]:
            if not isinstance(r, dict) or not normalize_url(r.get('url', '')):
                continue
            normalized.append({**r, 'source_engine': engine.name, 'source_engines': [engine.name],
                               'discovered_at': datetime.now(timezone.utc).isoformat()})
        if normalized:
            cache.put(key, normalized)
        return normalized

    @measured('search_query')
    def search(self, query: str, max_results: int = 5, *, fallback_context: str = '') -> list[dict]:
        if not query.strip() or max_results < 1:
            raise ValueError('搜索查询和结果预算不能为空')
        engines = self._engines_for(query)
        results, warnings = [], []
        with ThreadPoolExecutor(max_workers=min(5, len(engines))) as executor:
            futures = {submit(executor, self._provider_search, e, query, max_results): e.name for e in engines}
            # Consume in provider order, independent of network completion order.
            for future, name in futures.items():
                try:
                    found = future.result()
                    results.extend(found)
                    if not found:
                        warnings.append({'provider': name, 'category': 'no_results'})
                except Exception as error:
                    category = error.category if isinstance(error, SearchError) else 'provider_error'
                    warnings.append({'provider': name, 'category': category,
                                     'error_type': getattr(error, 'error_type', '') or type(error).__name__,
                                     'detail': getattr(error, 'detail', '')})
                    logger.warning('Search provider %s: %s (%s) %s', name, category,
                                   getattr(error, 'error_type', '') or type(error).__name__,
                                   getattr(error, 'detail', ''))
        if not results:
            tried = {engine.name for engine in engines}
            backups = [engine for engine in getattr(self, 'fallback_engines', []) if engine.name not in tried]
            for engine in backups[:self._option('search_fallback_max_providers', 5)]:
                try:
                    found = self._provider_search(engine, query, max_results)
                    if found:
                        results.extend(found)
                        for warning in warnings:
                            if warning['provider'] not in {'arxiv', 'semantic_scholar', 'openalex'}:
                                warning.update(fallback_provider=engine.name, fallback_results=len(found))
                        break
                    warnings.append({'provider': engine.name, 'category': 'no_results'})
                except Exception as error:
                    category = error.category if isinstance(error, SearchError) else 'provider_error'
                    warnings.append({'provider': engine.name, 'category': category,
                                     'error_type': getattr(error, 'error_type', '') or type(error).__name__,
                                     'detail': getattr(error, 'detail', '')})
                    logger.warning('Search fallback %s: %s (%s) %s', engine.name, category,
                                   getattr(error, 'error_type', '') or type(error).__name__,
                                   getattr(error, 'detail', ''))
        # At most two web requests for all failed academic channels together.
        # This finds paper entrances; it does not claim the failed API recovered.
        fallback = self._academic_fallback(query, max_results, warnings)
        results.extend(fallback)
        if not results:
            results.extend(self._simplified_fallback(query, max_results, warnings, fallback_context))
        unique = merge_records(results)
        if not unique:
            raise SearchUnavailable(warnings)
        for item in unique:
            item['retrieval_warnings'] = warnings
        return unique

    @staticmethod
    def discovery_query(query, fallback_context=''):
        """Broaden an empty query from the research scope, keeping date constraints."""
        if fallback_context and fallback_context.strip():
            context = re.sub(r'\s+', ' ', fallback_context).strip()[:120]
            dates = list(dict.fromkeys(re.findall(r'(?<!\d)(?:19|20)\d{2}(?:[-–至]\d{2,4})?(?!\d)', query)))
            variant = ' '.join(dict.fromkeys([context, *dates]))
            return variant if variant.casefold() != query.strip().casefold() else None
        terms = re.findall(r'"[^\"]*"|\S+', query)
        if len(terms) <= 5 or query.count('"') % 2 or any(t in {'OR', 'AND', 'NOT'} for t in terms):
            return None
        protected = [t for t in terms if t.startswith(('site:', '-', '"')) or re.match(r'^(?:19|20)\d{2}', t)]
        keep = set(terms[:4] + protected)
        result = ' '.join(dict.fromkeys(t for t in terms if t in keep))
        return result if result != query.strip() else None

    def _simplified_fallback(self, query, max_results, warnings, fallback_context=''):
        if not self._option('search_query_relaxation', True):
            return []
        variant = self.discovery_query(query, fallback_context)
        if not variant:
            return []
        # Only retry channels which returned an empty response. Never use a
        # changed query to bypass a network-error, access-denial or rate cooldown.
        empty = {w['provider'] for w in warnings if w['category'] == 'no_results'}
        engines = list({e.name: e for e in self.engines + getattr(self, 'fallback_engines', [])}.values())
        max_fallbacks = self._option('search_fallback_max_providers', 5)
        for engine in [e for e in engines if e.name in empty][:max_fallbacks]:
            try:
                found = self._provider_search(engine, variant, max_results)
            except Exception as error:
                warnings.append({'provider': engine.name,
                    'category': error.category if isinstance(error, SearchError) else 'provider_error',
                    'error_type': getattr(error, 'error_type', '') or type(error).__name__,
                    'detail': getattr(error, 'detail', ''), 'query_variant': variant})
                continue
            if found:
                for warning in warnings:
                    if warning['provider'] not in {'arxiv', 'semantic_scholar', 'openalex'}:
                        warning.update(fallback_provider=engine.name, fallback_results=len(found), fallback_query=variant)
                return [{**r, 'matched_queries': [variant], 'discovery_query': variant,
                         'query_relaxed': True} for r in found]
        return []

    def _academic_fallback(self, query, max_results, warnings):
        failures = [w for w in warnings if w['provider'] in {'arxiv', 'semantic_scholar', 'openalex'}
                    and w['category'] in {'timeout', 'network_error', 'rate_limited', 'server_error',
                                          'authentication_error', 'provider_error'}]
        if not failures or not self._option('academic_web_fallback', True):
            return []
        # Use an independent search index, never another API hostname or key to
        # bypass a provider's cooldown. Exact host filtering rejects blog mirrors.
        domains = {'arxiv': 'arxiv.org', 'semantic_scholar': 'semanticscholar.org', 'openalex': 'doi.org'}
        collected = []
        web_engines = list({e.name: e for e in self.engines + getattr(self, 'fallback_engines', [])}.values())
        for engine in web_engines[:2]:
            pending_sites = [domains[w['provider']] for w in failures if not w.get('fallback_provider')]
            scoped_query = query + ' ' + ('site:' + pending_sites[0] if len(pending_sites) == 1 else
                                         '(' + ' OR '.join('site:' + host for host in pending_sites) + ')')
            try:
                found = self._provider_search(engine, scoped_query, max_results)
            except Exception:
                continue
            found = [r for r in found if (urlsplit(r['url']).hostname or '').removeprefix('www.') in pending_sites
                     and (source_key(r['url']).startswith('arxiv:') or '/paper/' in r['url']
                          or (urlsplit(r['url']).hostname or '') == 'doi.org')
                     and not clearly_off_topic(r, query) and lexical_score(r, query) > 0]
            if not found:
                continue
            for warning in failures:
                host = domains[warning['provider']]
                replacements = [r for r in found if (urlsplit(r['url']).hostname or '').removeprefix('www.') == host]
                if replacements and not warning.get('fallback_provider'):
                    warning.update(fallback_provider=engine.name, fallback_results=len(replacements))
            collected.extend(found)
            if all(w.get('fallback_provider') for w in failures):
                break
        # If some requested sites were absent, their warnings stay unresolved.
        return merge_records(collected)

    @measured('retrieval')
    def search_many(self, queries: list[str], max_results_per_query: int = 5, max_total: int = 8,
                    existing_results: list[dict] | None = None, priority_terms: list[str] | None = None,
                    fallback_context: str = '') -> list[dict]:
        unique_queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        if not unique_queries or max_total < 1:
            raise ValueError('至少需要一个非空搜索查询和正数结果预算')
        combined = [dict(item) for item in (existing_results or [])]
        errors = []
        with ThreadPoolExecutor(max_workers=min(2, len(unique_queries))) as executor:
            futures = {submit(executor, self.search, q, max_results_per_query,
                              fallback_context=fallback_context): q for q in unique_queries}
            for future, query in futures.items():
                try:
                    for result in future.result():
                        combined.append({**result, 'matched_queries': list(dict.fromkeys(result.get('matched_queries', []) + [query]))})
                except SearchUnavailable as error:
                    errors.extend({**warning, 'query': query} for warning in error.warnings)
                except Exception as error:
                    # Never retain raw HTTP/SDK exceptions containing credentials.
                    errors.append({'provider': 'query', 'category': 'query_failed', 'query': query,
                                   'error_type': type(error).__name__})
        unique = merge_records(combined)
        if not unique:
            raise SearchUnavailable(errors)
        query_text = ' '.join(unique_queries)
        unique = [r for r in unique if not clearly_off_topic(r, query_text)]
        if not unique:
            raise RuntimeError('搜索返回的候选资料均为明显同名异义结果，尚未取得与研究问题相关的材料')
        ranked = sorted(unique, key=lambda r: self._source_score(r, query_text), reverse=True)
        reserved = self._reserve(ranked, priority_terms or [])
        ranked = reserved + [r for r in ranked if r not in reserved]
        ranked = self._expand(ranked, query_text)
        ranked = [r for r in ranked if not clearly_off_topic(r, query_text)]
        ranked = sorted(merge_records(ranked), key=lambda r: self._source_score(r, query_text), reverse=True)
        limit = self._option('search_rerank_candidates', 16)
        # Required methods must enter the semantic shortlist, not just the final read queue.
        reserved = self._reserve(ranked, priority_terms or [])
        ranked = reserved + [r for r in ranked if r not in reserved]
        if getattr(self, 'reranker', None) and self._option('search_rerank_enabled', True):
            try:
                ranked = semantic_rerank(self.reranker, query_text, ranked[:limit]) + ranked[limit:]
            except ResearchLimit:
                raise
            except Exception:
                errors.append({'provider': 'reranker', 'category': 'fallback_to_lexical'})
        reserved = self._reserve(ranked, priority_terms or [])
        ranked = reserved + [r for r in ranked if r not in reserved]
        # Existing readable results must not consume the entire candidate window.
        original_slots = self._original_candidates(ranked, query_text)
        read_order = reserved + [r for r in original_slots if r not in reserved]
        read_order += [r for r in ranked if r not in read_order]
        pool = read_order[:max_total + self._option('search_read_attempts', 10)]
        self._verify_metadata(pool)
        pool = merge_records(pool)
        self._read_sources(pool, query_text)
        # Failed early reads do not crowd successfully read replacements out of the output.
        reserved = self._reserve(pool, priority_terms or [])
        remainder = [r for r in pool if r not in reserved]
        selected = (reserved + [r for r in remainder if self._usable_body(r)]
                    + [r for r in remainder if not self._usable_body(r)])[:max_total]
        for item in selected:
            item.setdefault('content_status', '仅搜索摘要')
            item['retrieval_warnings'] = item.get('retrieval_warnings', []) + errors
        logger.info('Retrieval candidates=%d selected=%d read=%d query_failures=%d', len(unique), len(selected), sum(bool(r.get('content')) for r in selected), len(errors))
        return selected

    @staticmethod
    def _original_candidates(ranked, query, limit=2):
        """Give relevant original documents reading slots, not every survey."""
        highest = max((lexical_score(r, query) for r in ranked), default=0)
        return [r for r in ranked
                if reference_type(r) in {'一手论文', '官方资料'}
                and lexical_score(r, query) >= max(1, highest * 0.4)][:limit]

    @staticmethod
    def _reserve(ranked, methods):
        reserved = []
        for method in methods:
            candidates = [r for r in ranked if method_present(method, r.get('title', ''))
                          and not re.search(r'survey|review|tutorial|awesome|综述|教程', r.get('title', ''), re.I)]
            if candidates:
                selected = next((r for r in candidates if SearchService._usable_body(r)), candidates[0])
                if selected not in reserved:
                    reserved.append(selected)
        return reserved

    @measured('citation_expansion')
    def _expand(self, ranked, query):
        engine = next((e for e in getattr(self, 'academic_engines', []) if e.name == 'semantic_scholar'), None)
        if not engine or not self._option('search_citation_expansion', True) or self._option('search_profile', 'auto') == 'web':
            return ranked
        if research_profile(query) != 'academic' and self._option('search_profile', 'auto') != 'academic':
            return ranked
        seeds = [r for r in ranked if r.get('semantic_scholar_id') and not r.get('citation_expanded') and not r.get('discovery_relation')]
        additions = []
        for seed in seeds[:self._option('search_expansion_seeds', 1)]:
            limit = self._option('search_expansion_results', 4)
            cache = RetrievalCache(self.cache_dir)
            key = f"citations-v1|{seed['semantic_scholar_id']}|{limit}"
            try:
                found = cache.get(key, self._option('search_cache_ttl_seconds', 86400))
                if found is None:
                    found = engine.expand(seed, limit)
                    cache.put(key, found)
                seed['citation_expanded'] = True
                for item in found:
                    # An edge is a discovery lead, not evidence of agreement or relevance.
                    if lexical_score(item, query) > 0:
                        additions.append({**item, 'source_engines': ['semantic_scholar'], 'matched_queries': []})
            except Exception:
                seed.setdefault('retrieval_warnings', []).append({'provider': 'semantic_scholar', 'category': 'expansion_failed'})
        return ranked + additions

    @measured('metadata_verification')
    def _verify_metadata(self, pool):
        if not getattr(self, 'crossref', None):
            return
        checked = 0
        cache = RetrievalCache(self.cache_dir / 'metadata')
        for item in pool:
            doi = normalize_doi(item.get('doi'))
            if not doi or doi.startswith('10.48550/arxiv.') or item.get('metadata_status') == 'doi_verified' or checked >= 3:
                continue
            checked += 1
            try:
                metadata = cache.get(doi, 604800)
                if metadata is None:
                    metadata = self.crossref.lookup(doi)
                    cache.put(doi, metadata)
                # Store formal-version metadata separately; don't relabel a read preprint as peer-reviewed.
                item['verified_metadata'] = metadata
                item['metadata_status'] = 'doi_verified'
                for field in ('authors', 'published_at', 'venue'):
                    if not item.get(field):
                        item[field] = metadata.get(field)
            except Exception:
                item['metadata_status'] = 'verification_failed'
                item.setdefault('retrieval_warnings', []).append({'provider': 'crossref', 'category': 'metadata_failed'})

    @measured('source_read_batch')
    def _read_sources(self, pool, query):
        # Budget counts NEW successful reads, not indices occupied by previously read sources.
        unread = [r for r in pool if not self._usable_body(r)]
        # New candidates before failed old candidates during gap filling.
        unread.sort(key=lambda r: bool(r.get('read_attempted')))
        budget = self._option('search_read_budget', 8)
        attempts = self._option('search_read_attempts', 16)
        successes, tried = 0, 0
        with ThreadPoolExecutor(max_workers=3) as executor:
            while unread and successes < budget and tried < attempts:
                count = min(3, budget - successes, attempts - tried, len(unread))
                batch, unread = unread[:count], unread[count:]
                futures = {submit(executor, self._read_candidate, r, query): r for r in batch}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        document = future.result()
                    except Exception:
                        document = {'content': '', 'content_status': '仅获得搜索摘要；原始正文读取失败',
                                    'acquisition_status': 'read_failed',
                                    'context_status': 'search_snippet_only',
                                    'failure_category': 'reader_error'}
                    item.update(document)
                    item['read_attempted'] = True
                    tried += 1
                    successes += self._usable_body(item)
        if successes < budget:
            failures = [item for item in pool if item.get('read_attempted') and not self._usable_body(item)]
            self._extract_fallback(failures[:budget - successes], query)

    def _extract_fallback(self, items, query):
        """Use Tavily Extract only after direct reading failed; never call an LLM."""
        if not items or not self._option('tavily_extract_fallback', True):
            return
        extractor = next((engine for engine in getattr(self, 'engines', [])
                          if engine.name == 'tavily' and hasattr(engine, 'extract')), None)
        if not extractor:
            return
        pending = []
        for item in items[:self._option('tavily_extract_max_urls', 5)]:
            cached = self.source_reader.read_cached_extraction(item['url'], query)
            if cached:
                item.update(cached)
                item.setdefault('read_attempts', []).append({
                    'url': item['url'], 'success': True, 'via': 'tavily_extract_cache'})
            else:
                pending.append(item)
        if not pending:
            return
        try:
            extracted = extractor.extract([item['url'] for item in pending])
        except Exception as error:
            category = error.category if isinstance(error, SearchError) else 'extract_failed'
            for item in pending:
                item.setdefault('retrieval_warnings', []).append(
                    {'provider': 'tavily_extract', 'category': category})
            return
        by_url = {record.get('url'): record for record in extracted if isinstance(record, dict)}
        for item in pending:
            record = by_url.get(item['url'])
            if not record or not record.get('raw_content'):
                item.setdefault('retrieval_warnings', []).append(
                    {'provider': 'tavily_extract', 'category': 'no_content'})
                continue
            try:
                document = self.source_reader.read_extracted_text(
                    item['url'], record['raw_content'], query, kind='Tavily正文提取')
                item.update(document)
                item.setdefault('read_attempts', []).append({
                    'url': item['url'], 'success': True, 'via': 'tavily_extract'})
            except Exception:
                item.setdefault('retrieval_warnings', []).append(
                    {'provider': 'tavily_extract', 'category': 'invalid_content'})

    @staticmethod
    def _usable_body(document):
        return bool(document.get('content')) and document.get('acquisition_status') not in {
            'metadata_only', 'read_failed', 'partial_text_extracted', 'service_extracted_text', 'body_text_unconfirmed'} and document.get('document_scope') not in {
                'metadata_page', 'access_barrier'}

    def _read_candidate(self, item, query):
        urls = [item['url']]
        if (urlsplit(item['url']).hostname or '') not in {'arxiv.org', 'www.arxiv.org'}:
            urls = item.get('fulltext_urls', []) + urls
        else:
            urls += item.get('fulltext_urls', [])
        # Follow a known paper identifier from a discovery mirror to the original
        # repository within the existing six-address read budget.
        identity = source_key(item['url'])
        if identity.startswith('arxiv:') and (urlsplit(item['url']).hostname or '') not in {'arxiv.org', 'www.arxiv.org'}:
            urls.insert(0, 'https://arxiv.org/abs/' + identity.split(':', 1)[1])
        urls += [v['url'] for v in item.get('versions', [])]
        # Index landing pages are metadata, not paper full text.
        candidates = list(dict.fromkeys(u for u in urls if (urlsplit(u).hostname or '') not in
                                       {'www.semanticscholar.org', 'semanticscholar.org', 'openalex.org', 'api.openalex.org'}))[:6]
        attempts = []
        document = {'content': '', 'content_status': '仅获得搜索摘要；原始正文读取失败',
                    'acquisition_status': 'read_failed', 'context_status': 'search_snippet_only',
                    'failure_category': 'no_readable_url'}
        fallback = None
        for url in candidates:
            try:
                document = self.source_reader.read_document(url, query)
            except Exception:
                document = {'content': '', 'content_status': '仅获得搜索摘要；原始正文读取失败',
                            'acquisition_status': 'read_failed', 'failure_category': 'reader_error'}
            attempts.append({'url': url, 'success': bool(document.get('content')),
                             'failure_category': document.get('failure_category', '')})
            if document.get('content'):
                document.setdefault('fetched_url', url)
                if fallback is None or (fallback.get('document_scope') == 'metadata_page'
                                        and document.get('document_scope') != 'metadata_page'):
                    fallback = document
            if self._usable_body(document):
                break
        else:
            document = fallback or document
        document['read_attempts'] = attempts
        return document

    _normalize_url = staticmethod(normalize_url)

    @staticmethod
    def _source_score(item, query_text):
        host = (urlsplit(item.get('url', '')).hostname or '').lower()
        first_party = int(host.endswith(('.gov', '.gov.cn', '.edu', '.ac.uk'))
                          or host in {'arxiv.org', 'openaccess.thecvf.com', 'doi.org'}
                          or host.startswith(('docs.', 'developer.')))
        editorial = bool(re.search(r'/blog/|awesome[-_]|新闻|产业评论|出货', item.get('url', '') + ' ' + item.get('title', ''), re.I))
        primary_title = int(not re.search(r'survey|review|tutorial|综述|教程|路线图', item.get('title', ''), re.I))
        return (max(0, lexical_score(item, query_text) - 3 * editorial), primary_title,
                len(item.get('matched_queries', [])), first_party)

"""Fetch and snapshot extracted documents; excerpts serve discovery, not analysis coverage."""

import ipaddress
import socket
import re
import time
import threading
from datetime import datetime, timezone
from src.services.retrieval_cache import RetrievalCache
from src.services.document_scope import classify_document, completeness, VERSION as SCOPE_VERSION
from src.services.performance import measured, count
from io import BytesIO
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from pypdf import PdfReader
import hashlib


class _ReadableTextParser(HTMLParser):
    _BLOCKED = {"script", "style", "svg", "noscript", "nav", "footer", "form"}
    _TEXT_TAGS = {"title", "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th",
                  "blockquote", "pre", "code", "dt", "dd", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocked_depth = 0
        self.capture_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr" and not self.blocked_depth:
            self.parts.append("\n| ")
        if tag in self._BLOCKED:
            self.blocked_depth += 1
        elif not self.blocked_depth and tag in self._TEXT_TAGS:
            self.capture_depth += 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._BLOCKED and self.blocked_depth:
            self.blocked_depth -= 1
        elif not self.blocked_depth and tag in self._TEXT_TAGS and self.capture_depth:
            self.capture_depth -= 1
            self.parts.append(" | " if tag in {"td", "th"} else "\n")
        elif tag == "tr" and not self.blocked_depth:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.blocked_depth and self.capture_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text + " ")

    def text(self) -> str:
        return "\n".join(" ".join(line.split()) for line in "".join(self.parts).splitlines() if line.strip())


class SourceReader:
    """Reads public HTTP(S) pages while blocking local/private network targets."""

    def __init__(self, timeout: int = 10, max_bytes: int = 50_000_000, max_chars: int = 14_000,
                 cache_dir=None, cache_ttl: int = 604800, pdf_max_pages: int = 500,
                 max_extracted_chars: int = 5_000_000):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_chars = max_chars
        self.pdf_max_pages = pdf_max_pages
        self.max_extracted_chars = max_extracted_chars
        self.cache = RetrievalCache(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        # Fixed-size striped locks prevent duplicate simultaneous downloads
        # without accumulating a lock for every URL ever encountered.
        self._body_locks = [threading.Lock() for _ in range(64)]

    def read(self, url: str, query: str = "") -> str:
        return self.read_document(url, query)["content"]

    @measured('source_read')
    def read_document(self, url: str, query: str = "") -> dict:
        """Return selected evidence plus independent acquisition/context status."""
        error = "unknown"
        candidates = [url]
        parsed = urlparse(url)
        if parsed.hostname in {"arxiv.org", "www.arxiv.org"}:
            match = re.search(r"/(?:abs|pdf|html)/(\d{4}\.\d{4,5}(?:v\d+)?)", parsed.path)
            if match:
                candidates = [f"https://arxiv.org/html/{match[1]}", f"https://arxiv.org/pdf/{match[1]}"]
        # A valid cached PDF also avoids retrying an unavailable HTML variant.
        fallback, cached_urls = None, set()
        for candidate in candidates:
            raw = self._cached_raw(candidate)
            if raw is not None:
                count('body_cache_hits')
                cached_urls.add(candidate)
                document = self._excerpt(raw, query)
                if document.get('acquisition_status') == 'full_text_extracted':
                    return document
                if document.get('content'):
                    fallback = document
        for candidate in candidates:
            if candidate in cached_urls:
                continue
            try:
                raw = self._raw_document(candidate)
                document = self._excerpt(raw, query)
                if document.get('acquisition_status') == 'full_text_extracted':
                    return document
                if document.get('content'):
                    fallback = document
                error = document.get('failure_category', 'incomplete_document')
            except Exception as exc:
                error = self._failure_category(exc)
        if fallback:
            return fallback
        return {
            "content": "",
            "content_status": "仅获得搜索摘要；原始正文读取失败",
            "acquisition_status": "read_failed",
            "completeness_status": "failed",
            "completeness_reason": "正文读取失败：" + error,
            "context_status": "search_snippet_only",
            "read_error": error,
            "failure_category": error,
            "evidence_chunks": [],
        }

    def _excerpt(self, raw, query):
        scope, scope_reason = classify_document(raw)
        if scope == 'access_barrier':
            return {'content': '', 'document_scope': scope, 'document_scope_version': SCOPE_VERSION,
                    'document_scope_reason': scope_reason, 'acquisition_status': 'read_failed',
                    'content_status': scope_reason, 'failure_category': 'access_restricted',
                    'completeness_status': 'failed', 'completeness_reason': scope_reason,
                    'full_text_available': False, 'evidence_chunks': [], 'fetched_url': raw['fetched_url']}
        metadata_only = scope == 'metadata_page'
        service_extracted = raw.get('acquisition_status') == 'service_extracted_text'
        completeness_status, completeness_reason = completeness(raw, scope)
        complete = completeness_status == 'confirmed'
        uses_full_text = complete and len(raw['text']) <= self.max_chars
        chunks = self.select_passage_records(raw['text'], query)
        content = self.select_passages(raw['text'], query)
        acquisition_status = ('metadata_only' if metadata_only else 'service_extracted_text' if service_extracted else
                              'full_text_extracted' if complete else
                              'body_text_unconfirmed' if completeness_status == 'unconfirmed' else 'partial_text_extracted')
        context_status = 'full_text_used' if uses_full_text else 'selected_passages_used'
        if metadata_only:
            status = '仅取得论文介绍页或摘要页，不等同于论文正文'
        elif complete and uses_full_text:
            status = f"{raw['kind']}正文已获取并缓存"
        elif complete:
            status = f"{raw['kind']}正文已获取并缓存"
        elif service_extracted:
            status = f"{raw['kind']}已返回清洗正文"
        elif completeness_status == 'unconfirmed':
            status = f"{raw['kind']}正文已获取，完整性未确认"
        else:
            status = f"{raw['kind']}仅部分获取"
        # Content-addressed snapshots survive URL-cache refreshes and resume.
        digest = hashlib.sha256(raw['text'].encode()).hexdigest()
        if self.cache and self.cache.get('analysis-v1|' + digest, float('inf')) is None:
            self.cache.put('analysis-v1|' + digest, {'text': raw['text']})
        return {
            'content': content,
            'completeness_status': completeness_status,
            'completeness_reason': completeness_reason,
            'pdf_empty_pages': raw.get('pdf_empty_pages', []),
            'document_scope': scope,
            'document_scope_version': SCOPE_VERSION,
            'document_scope_reason': scope_reason,
            'content_status': status,
            'fetched_url': raw['fetched_url'],
            'extracted_chars': len(raw['text']),
            # Compatibility: historically this meant either an incomplete fetch
            # or a bounded prompt projection.  New fields distinguish the two.
            'read_partial': raw['partial'] or not uses_full_text,
            'acquisition_partial': completeness_status == 'partial',
            'acquisition_status': acquisition_status,
            'context_status': context_status,
            'context_chars': len(content),
            'full_text_available': complete,
            'document_id': hashlib.sha256(raw['fetched_url'].encode()).hexdigest()[:16],
            'evidence_chunks': chunks,
            'fetched_at': raw['fetched_at'],
            'read_query': query,
            'analysis_text_hash': digest,
            **({'analysis_text': raw['text']} if not self.cache else {}),
        }

    def analysis_text(self, record):
        """Return all extracted text from the exact snapshot, never a ranked prefix."""
        if isinstance(record.get('analysis_text'), str):
            return record['analysis_text']
        digest = record.get('analysis_text_hash')
        if digest and self.cache:
            raw = self.cache.get('analysis-v1|' + digest, float('inf'))
            if isinstance(raw, dict) and isinstance(raw.get('text'), str):
                if hashlib.sha256(raw['text'].encode()).hexdigest() == digest:
                    return raw['text']
            raise ValueError('该来源的全文缓存缺失或损坏，无法继续全文分析；已保留完成分块。')
        # Legacy records can use their original cached body without new HTTP.
        if self.cache:
            for url in dict.fromkeys(u for u in (record.get('fetched_url'), record.get('url')) if u):
                key = self._proxy_key(url) if record.get('acquisition_status') == 'service_extracted_text' else self._body_key(url)
                raw = self.cache.get(key, float('inf'))
                if (isinstance(raw, dict) and isinstance(raw.get('text'), str)
                        and (not record.get('extracted_chars') or len(raw['text']) == record['extracted_chars'])
                        and (not record.get('fetched_at') or raw.get('fetched_at') == record['fetched_at'])):
                    text = raw['text']
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    self.cache.put('analysis-v1|' + digest, {'text': text})
                    record['analysis_text_hash'] = digest
                    return text
        if record.get('extracted_chars', 0) > len(record.get('content', '')):
            raise ValueError('历史来源仅保留了选段，全文缓存不可用；不能将选段标记为全文分析。')
        return record.get('content') or record.get('snippet') or record.get('title', '')

    def _body_key(self, url):
        return (f'body-v3|{url}|{self.max_bytes}|pages{self.pdf_max_pages}|'
                f'chars{self.max_extracted_chars}')

    def refresh_metadata(self, record):
        """Reclassify cached raw text without HTTP or changing evidence passages."""
        if record.get('acquisition_status') == 'service_extracted_text':
            return {}  # A separate direct fetch cannot certify proxy evidence.
        urls = [record.get('url'), record.get('fetched_url')]
        for url in dict.fromkeys(u for u in urls if u):
            raw = self._cached_raw(url)
            if raw is None:
                continue
            if (record.get('document_id') and record['document_id'] !=
                    hashlib.sha256(raw['fetched_url'].encode()).hexdigest()[:16]):
                continue
            document = self._excerpt(raw, '')
            return {k: document[k] for k in ('document_scope', 'document_scope_version',
                    'document_scope_reason', 'acquisition_status', 'full_text_available',
                    'acquisition_partial', 'completeness_status', 'completeness_reason', 'pdf_empty_pages') if k in document}
        return {}

    def _proxy_key(self, url):
        return f'proxy-body-v1|{url}|chars{self.max_extracted_chars}'

    def read_cached_extraction(self, url, query=''):
        raw = self.cache.get(self._proxy_key(url), self.cache_ttl) if self.cache else None
        if isinstance(raw, dict) and len(str(raw.get('text', '')).strip()) >= 200:
            count('proxy_body_cache_hits')
            return self._excerpt(raw, query)
        return None

    def read_extracted_text(self, url, text, query='', kind='正文提取服务'):
        """Index cleaned content returned by a secondary extraction service."""
        if len(str(text).strip()) < 200:
            raise ValueError('正文提取服务未返回足够内容')
        clipped = str(text)[:self.max_extracted_chars]
        raw = {
            'text': clipped,
            'kind': kind,
            'fetched_url': url,
            'partial': len(str(text)) > self.max_extracted_chars,
            'acquisition_status': 'service_extracted_text',
            'fetched_at': datetime.now(timezone.utc).isoformat(),
        }
        if self.cache:
            self.cache.put(self._proxy_key(url), raw)
        return self._excerpt(raw, query)

    def _cached_raw(self, url):
        raw = self.cache.get(self._body_key(url), self.cache_ttl) if self.cache else None
        if (isinstance(raw, dict) and isinstance(raw.get('text'), str)
                and len(raw['text'].strip()) >= 200
                and all(isinstance(raw.get(k), str) for k in ('kind', 'fetched_url', 'fetched_at'))
                and type(raw.get('partial')) is bool):
            return raw
        return None

    def _raw_document(self, url):
        key = self._body_key(url)
        with self._body_locks[hash(key) % len(self._body_locks)]:
            raw = self._cached_raw(url)
            if raw is not None:
                count('body_cache_hits')
                return raw
            count('body_cache_misses')
            fetched = self._fetch(url)
            raw_text, kind, fetched_url, partial = fetched[:4]
            diagnostics = fetched[4] if len(fetched) > 4 else {}
            if diagnostics.get('pdf_text_chars', len(raw_text.strip())) < 200:
                raise ValueError('未提取到足够正文，可能需要脚本或 OCR')
            raw = {'text': raw_text[:self.max_extracted_chars], 'kind': kind, 'fetched_url': fetched_url,
                   'partial': partial or len(raw_text) > self.max_extracted_chars,
                   'fetched_at': datetime.now(timezone.utc).isoformat(), **diagnostics}
            if len(raw_text) > self.max_extracted_chars:
                raw['partial_reason'] = '提取文本超过字符上限，缓存仅保留上限以内内容'
            if self.cache:
                self.cache.put(key, raw)
            return raw

    @measured('source_fetch')
    def _fetch(self, url: str) -> tuple:
        current_url = url
        deadline = time.monotonic() + 35
        for _ in range(4):
            self._validate_public_url(current_url)
            count('source_http_requests')
            response = requests.get(
                current_url,
                headers={"User-Agent": "DeepResearch/1.0 (+public-source-reader)"},
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise ValueError("重定向缺少目标")
                current_url = urljoin(current_url, location)
                continue

            try:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not any(kind in content_type for kind in ("text/html", "text/plain", "application/pdf")):
                    raise ValueError("不支持的正文格式")
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65_536):
                    size += len(chunk)
                    if size > self.max_bytes or time.monotonic() > deadline:
                        raise ValueError("来源超过读取预算")
                    chunks.append(chunk)
                encoding = response.encoding
                if not encoding or encoding.lower() == "iso-8859-1":
                    encoding = "utf-8"
            finally:
                response.close()
            payload = b"".join(chunks)
            if "application/pdf" in content_type or payload.startswith(b"%PDF"):
                reader = PdfReader(BytesIO(payload))
                pages = []
                blank_pages = []
                text_chars = 0
                for index, page in enumerate(reader.pages[:self.pdf_max_pages], 1):
                    if time.monotonic() > deadline:
                        break
                    page_text = page.extract_text() or ""
                    text_chars += len(page_text.strip())
                    if not page_text.strip():
                        blank_pages.append(index)
                    pages.append(f"\n[PDF 第 {index} 页]\n" + page_text)
                return ("\n".join(pages), "PDF", current_url,
                        len(pages) < len(reader.pages),
                        {'pdf_empty_pages': blank_pages, 'pdf_text_chars': text_chars,
                         'partial_reason': 'PDF 达到页数或处理时间上限' if len(pages) < len(reader.pages) else ''})
            raw = payload.decode(encoding, errors="replace")
            if content_type.startswith("text/plain"):
                return raw, "文本", current_url, False
            parser = _ReadableTextParser()
            parser.feed(raw)
            return parser.text(), "网页", current_url, False
        raise ValueError("重定向次数超限")

    def select_passages(self, text: str, query: str = "") -> str:
        if len(text) <= self.max_chars:
            return text
        records = self.select_passage_records(text, query)
        return "\n\n".join(
            f"[证据块 {record['chunk_id']}｜{record['locator']}]\n{record['text']}"
            for record in records
        )

    def select_passage_records(self, text: str, query: str = "") -> list[dict]:
        """Select intact, addressable windows from the whole extracted document."""
        if not text:
            return []
        # Overlap keeps claims split at a window edge available as one quote.
        windows = [(start, min(start + 1600, len(text)), text[start:start + 1600])
                   for start in range(0, len(text), 1400)]
        terms = set(re.findall(r"[a-z0-9-]{3,}|[\u4e00-\u9fff]{2,6}", query.lower()))
        def score(index):
            block = windows[index][2].lower()
            relevance = sum(term in block for term in terms)
            evidence_sections = len(re.findall(
                r"abstract|introduction|method|approach|experiment|result|ablation|appendix|"
                r"hardware|latency|protocol|baseline|definition|摘要|引言|定义|方法|实验|结果|附录|硬件|延迟|评测",
                block,
            ))
            numeric = min(len(re.findall(r"\d+(?:\.\d+)?\s*(?:%|hz|ms|gpu|hours)", block)), 6)
            return relevance * 4 + min(evidence_sections, 8) + numeric

        chosen = set(range(len(windows))) if len(text) <= self.max_chars else {0}
        for index in sorted(range(1, len(windows)), key=lambda i: (-score(i), i)):
            if index in chosen:
                continue
            if sum(len(windows[i][2]) + 80 for i in chosen) + len(windows[index][2]) + 80 > self.max_chars:
                continue
            chosen.add(index)
        records = []
        page_markers = [(match.start(), match.group(1))
                        for match in re.finditer(r'\[PDF 第\s*(\d+)\s*页\]', text)]
        for index in sorted(chosen):
            start, end, block = windows[index]
            page = next((number for position, number in reversed(page_markers) if position <= start), None)
            locator = f'PDF 第 {page} 页' if page else f'正文字符 {start + 1}-{end}'
            records.append({
                'chunk_id': f'c{index + 1:04d}',
                'locator': locator,
                'start_char': start,
                'end_char': end,
                'text': block,
                'score': score(index),
            })
        return records

    @staticmethod
    def _failure_category(error: Exception) -> str:
        if isinstance(error, requests.Timeout):
            return 'timeout'
        if isinstance(error, requests.ConnectionError):
            return 'network_error'
        if isinstance(error, requests.HTTPError):
            status = getattr(error.response, 'status_code', None)
            if status in {401, 403}:
                return 'access_restricted'
            if status == 429:
                return 'rate_limited'
            if status == 404:
                return 'not_found'
            return 'http_error'
        message = str(error)
        if 'OCR' in message:
            return 'ocr_required'
        if '格式' in message:
            return 'unsupported_format'
        if '预算' in message:
            return 'read_budget_exceeded'
        if '重定向' in message:
            return 'redirect_limit'
        return type(error).__name__.casefold()

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("仅允许读取公开的 HTTP(S) 来源")
        if parsed.username or parsed.password:
            raise ValueError("来源 URL 不允许包含认证信息")

        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("不允许读取本地或私有网络地址")

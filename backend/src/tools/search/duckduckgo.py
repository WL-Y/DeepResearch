"""Explicit public-web backends, with bounded retries and shared-instance cooldown."""
import re
import threading
import time

from ddgs import DDGS
from ddgs.engines import ENGINES as DDGS_ENGINES
from ddgs.exceptions import DDGSException, TimeoutException, RatelimitException
from .base import SearchEngine
from .http import SearchError
from src.services.performance import count


class PublicWebSearch(SearchEngine):
    backend = 'duckduckgo'

    def __init__(self, timeout=10, retries=1):
        self.timeout, self.retries = timeout, retries
        self._lock = threading.Lock()
        self._blocked_until = 0
        self._blocked_category = ''
        self._next = 0

    @property
    def name(self):
        return self.backend

    @staticmethod
    def category(error):
        # Inspect locally, but never persist raw SDK text (URLs/proxies can contain secrets).
        message = str(error).lower()
        if isinstance(error, TimeoutException) or 'timed out' in message or 'timeout' in message:
            return 'timeout'
        if isinstance(error, RatelimitException) or re.search(r'\b429\b|ratelimit|rate.limit', message):
            return 'rate_limited'
        if re.search(r'\b403\b|captcha|forbidden', message):
            return 'access_denied'
        if any(word in message for word in ('connect', 'dns', 'certificate', 'resolve', 'tls')):
            return 'network_error'
        if message.strip() == 'no results found.':
            return 'no_results'
        return 'provider_error'

    def search(self, query, max_results=5):
        # DDGS silently switches unavailable backend names to auto. Reject it
        # before construction, so provenance and cooldown always match reality.
        if self.backend not in DDGS_ENGINES.get('text', {}):
            raise SearchError(self.name, 'backend_unavailable')
        with self._lock:
            if time.monotonic() < self._blocked_until:
                raise SearchError(self.name, self._blocked_category)
            for attempt in range(self.retries + 1):
                time.sleep(max(0, self._next - time.monotonic()))
                try:
                    count('provider_search_attempts.' + self.name)
                    with DDGS(timeout=self.timeout) as client:
                        rows = list(client.text(query, backend=self.backend, max_results=max_results,
                                                region='cn-zh' if re.search(r'[\u4e00-\u9fff]', query) else 'us-en'))
                    return [{'title': r.get('title', ''), 'url': r['href'], 'snippet': r.get('body', '')}
                            for r in rows if isinstance(r, dict) and isinstance(r.get('href'), str)]
                except DDGSException as error:
                    category = self.category(error)
                    if category == 'no_results':
                        return []
                    if category in {'timeout', 'network_error'} and attempt < self.retries:
                        self._next = time.monotonic() + 1
                        continue
                    self._blocked_until = time.monotonic() + (60 if category in {'rate_limited', 'access_denied'} else 20)
                    self._blocked_category = category
                    raise SearchError(self.name, category, error_type=type(error).__name__,
                                      detail=str(error)) from None
                finally:
                    self._next = max(self._next, time.monotonic() + .25)


class DuckDuckGoSearch(PublicWebSearch):
    backend = 'duckduckgo'


class YahooSearch(PublicWebSearch):
    backend = 'yahoo'


class BraveWebSearch(PublicWebSearch):
    backend = 'brave'


class GoogleWebSearch(PublicWebSearch):
    backend = 'google'


class MojeekSearch(PublicWebSearch):
    backend = 'mojeek'


class StartpageSearch(PublicWebSearch):
    backend = 'startpage'

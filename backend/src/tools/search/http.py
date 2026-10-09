"""Per-provider throttling and bounded retries. Never expose request URLs/keys in errors."""
import threading
import time
import re
import requests
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from src.services.performance import count


class SearchError(RuntimeError):
    def __init__(self, provider, category, *, error_type='', detail=''):
        self.provider, self.category = provider, category
        self.error_type = error_type
        detail = re.sub(r'https?://\S+', '<url>', str(detail))
        detail = re.sub(r'(?i)(api[_ -]?key|token|password|authorization)\s*[:= ]\s*\S+',
                        r'\1=<redacted>', detail)
        self.detail = ' '.join(detail.split())[:120]
        super().__init__(f'{provider}: {category}')


class SearchUnavailable(RuntimeError):
    """Safe, structured diagnostics survive query aggregation and task failure."""
    def __init__(self, warnings):
        self.warnings = warnings
        details = ', '.join(dict.fromkeys(
            f"{w['provider']}={w['category']}" +
            (f" ({w['error_type']}" + (f": {w['detail']}" if w.get('detail') else '') + ')'
             if w.get('error_type') or w.get('detail') else '')
            for w in warnings))
        super().__init__('搜索渠道未取得可用结果（' + details + '）；已保留研究进度。')


class ProviderHTTP:
    def __init__(self, name, interval=1.0, timeout=12, retries=1):
        self.name, self.interval, self.timeout, self.retries = name, interval, timeout, retries
        self._lock = threading.Lock()
        self._next = 0.0
        self._blocked_until = 0.0
        self._blocked_category = ''

    @staticmethod
    def retry_delay(value, default):
        try:
            return max(0, float(value))
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0, (date - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return default

    def request(self, method, url, **kwargs):
        # Keep retries and cooldown updates inside the provider lock. Other
        # queries must not slip in between a 429 and its retry/circuit update.
        with self._lock:
            for attempt in range(self.retries + 1):
                if time.monotonic() < self._blocked_until:
                    raise SearchError(self.name, self._blocked_category)
                time.sleep(max(0, self._next - time.monotonic()))
                try:
                    count('provider_http_requests.' + self.name)
                    response = requests.request(method, url, timeout=self.timeout, **kwargs)
                except requests.Timeout:
                    category = 'timeout'
                    response = None
                except requests.RequestException:
                    category = 'network_error'
                    response = None
                finally:
                    self._next = time.monotonic() + self.interval
                if response is not None:
                    status = response.status_code
                    if 200 <= status < 300:
                        return response
                    category = ('authentication_error' if status in (401, 403) else
                                'rate_limited' if status == 429 else
                                'not_found' if status == 404 else
                                'server_error' if status >= 500 else 'request_error')
                    retry_after = response.headers.get('Retry-After', '')
                    response.close()
                    if category not in {'rate_limited', 'server_error'}:
                        if category == 'authentication_error':
                            self._blocked_until = time.monotonic() + 60
                            self._blocked_category = category
                        raise SearchError(self.name, category)
                    delay = self.retry_delay(retry_after, (5 if category == 'rate_limited' else 1) * 2 ** attempt)
                    # Respect long cooldowns without holding up the research.
                    # SearchService will try a separate discovery channel.
                    if delay > 8:
                        self._blocked_until = time.monotonic() + delay
                        self._blocked_category = category
                        raise SearchError(self.name, category)
                else:
                    delay = 2 ** attempt
                if attempt < self.retries:
                    self._next = max(self._next, time.monotonic() + delay)
            self._blocked_until = max(self._blocked_until, time.monotonic() + (60 if category == 'rate_limited' else 30))
            self._blocked_category = category
            raise SearchError(self.name, category)

    def json(self, method, url, **kwargs):
        response = self.request(method, url, **kwargs)
        try:
            payload = response.json()
            if not isinstance(payload, dict) or payload.get('error'):
                raise ValueError('invalid envelope')
            return payload
        except ValueError:
            raise SearchError(self.name, 'invalid_response') from None
        finally:
            response.close()

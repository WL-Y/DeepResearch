from .base import SearchEngine
from .http import ProviderHTTP, SearchError


class TavilySearch(SearchEngine):
    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.http = ProviderHTTP('tavily', interval=.3)

    @property
    def name(self) -> str:
        return "tavily"

    def search(self, query: str, max_results: int = 5) -> list:
        if not self.api_key or self.api_key.startswith('your_'):
            raise SearchError(self.name, 'missing_api_key')
        data = self.http.json('POST',
            "https://api.tavily.com/search",
            json={
                "api_key": self.api_key,
                "query": query,
                "max_results": max_results,
            },
        )
        if not isinstance(data.get('results'), list):
            raise SearchError(self.name, 'invalid_response')
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
            for r in data.get("results", [])
        ]

    def extract(self, urls: list[str]) -> list[dict]:
        """Return cleaned page bodies for URLs the direct reader could not open."""
        if not self.api_key or self.api_key.startswith('your_'):
            raise SearchError(self.name, 'missing_api_key')
        data = self.http.json(
            'POST', 'https://api.tavily.com/extract',
            headers={'Authorization': f'Bearer {self.api_key}'},
            json={'urls': urls[:10], 'extract_depth': 'basic', 'format': 'markdown',
                  'include_images': False},
        )
        if not isinstance(data.get('results'), list):
            raise SearchError(self.name, 'invalid_response')
        return [record for record in data['results']
                if isinstance(record, dict) and record.get('url') and record.get('raw_content')]

from .duckduckgo import (DuckDuckGoSearch, YahooSearch, BraveWebSearch, GoogleWebSearch,
                         MojeekSearch, StartpageSearch)
from .tavily import TavilySearch
from .academic import ArxivSearch, SemanticScholarSearch, OpenAlexSearch

ENGINES = {
    "duckduckgo": DuckDuckGoSearch,
    "yahoo": YahooSearch,
    "brave_web": BraveWebSearch,
    "google": GoogleWebSearch,
    "mojeek": MojeekSearch,
    "startpage": StartpageSearch,
    "tavily": TavilySearch,
    "arxiv": ArxivSearch,
    "semantic_scholar": SemanticScholarSearch,
    "openalex": OpenAlexSearch,
}

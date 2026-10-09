from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Literal


class Settings(BaseSettings):
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"  # DeepSeek-V4.1-Flash
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 0
    llm_max_output_tokens: int = Field(default=5000, ge=256, le=16000)
    report_max_output_tokens: int = Field(default=12000, ge=256, le=16000)
    report_timeout_seconds: float = Field(default=180.0, ge=30, le=600)
    report_max_input_chars: int = Field(default=180000, ge=10000, le=500000)
    llm_max_input_chars: int = Field(default=70000, ge=10000, le=150000)
    deepseek_thinking: bool = False
    deepseek_reasoning_effort: Literal["low", "high", "max"] = "high"
    research_token_budget: int = Field(default=1500000, ge=10000)
    research_max_model_calls: int = Field(default=200, ge=1, le=500)
    report_token_budget: int = Field(default=100000, ge=20000)
    report_max_model_calls: int = Field(default=2, ge=1, le=8)
    research_task_concurrency: int = Field(default=2, ge=1, le=2)
    search_api: str = "duckduckgo"  # 单引擎模式（兼容）
    search_engines: str = ""  # 多引擎模式，逗号分隔，如 "duckduckgo,tavily"
    search_fallback_engines: str = 'yahoo,brave_web,google,mojeek,startpage'
    search_fallback_max_providers: int = Field(default=5, ge=0, le=5)
    search_web_timeout_seconds: int = Field(default=10, ge=3, le=30)
    search_web_retries: int = Field(default=1, ge=0, le=1)
    search_query_relaxation: bool = True
    tavily_api_key: str = ""
    academic_search_engines: str = "arxiv,openalex"
    arxiv_read_timeout_seconds: float = Field(default=30, ge=12, le=90)
    academic_web_fallback: bool = True
    search_profile: Literal['auto', 'web', 'academic'] = 'auto'
    semantic_scholar_api_key: str = ""
    openalex_api_key: str = ""
    crossref_enabled: bool = True
    crossref_mailto: str = ""
    search_cache_ttl_seconds: int = Field(default=86400, ge=0)
    search_fresh_cache_ttl_seconds: int = Field(default=3600, ge=0)
    source_cache_ttl_seconds: int = Field(default=604800, ge=0)
    source_max_bytes: int = Field(default=50_000_000, ge=1_000_000, le=100_000_000)
    source_pdf_max_pages: int = Field(default=500, ge=10, le=1000)
    source_max_extracted_chars: int = Field(default=5_000_000, ge=100_000, le=10_000_000)
    # Discovery can be broader than the 10-15 sources displayed in the report.
    # Full documents are cached outside the model context, so reads are not
    # equivalent to prompt-token spend.
    search_read_budget: int = Field(default=8, ge=1, le=12)
    search_read_attempts: int = Field(default=16, ge=1, le=24)
    fulltext_chunk_chars: int = Field(default=20000, ge=4000, le=30000)
    reading_optimization_enabled: bool = True
    analysis_source_limit: int = Field(default=4, ge=1, le=8)
    analysis_part_limit: int = Field(default=16, ge=1, le=100)
    concept_source_limit: int = Field(default=2, ge=1, le=8)
    concept_part_limit: int = Field(default=8, ge=1, le=100)
    tavily_extract_fallback: bool = True
    tavily_extract_max_urls: int = Field(default=5, ge=1, le=10)
    search_rerank_enabled: bool = False
    search_rerank_candidates: int = Field(default=16, ge=4, le=30)
    search_citation_expansion: bool = False
    search_expansion_seeds: int = Field(default=1, ge=1, le=2)
    search_expansion_results: int = Field(default=4, ge=1, le=8)
    report_materials_max_chars: int = Field(default=160000, ge=5000, le=480000)
    export_font_path: str = ""  # Optional TTF/TTC font for Chinese PDF exports.
    host: str = "0.0.0.0"
    port: int = 8000

    class Config:
        extra = "ignore"
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

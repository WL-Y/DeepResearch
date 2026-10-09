from abc import ABC, abstractmethod
from typing import List, Dict


class SearchEngine(ABC):
    """搜索引擎基类"""

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> List[Dict]:
        """执行搜索，返回结果列表"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """引擎名称"""
        pass

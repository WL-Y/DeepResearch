from pydantic import BaseModel, Field
from typing import List


class TodoItem(BaseModel):
    id: int
    title: str
    intent: str
    query: str
    verification_query: str = ""
    required_methods: List[str] = Field(default_factory=list)
    depends_on: List[int] = Field(default_factory=list)
    status: str = "pending"
    failure_reason: str | None = None


class TaskSummary(BaseModel):
    task: TodoItem
    summary: str
    sources: List[str] = Field(default_factory=list)
    source_titles: List[str] = Field(default_factory=list)
    source_details: List[dict] = Field(default_factory=list)
    retrieval: dict = Field(default_factory=dict)

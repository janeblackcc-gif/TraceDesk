from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    knowledge_base_id: UUID
    question: str = Field(min_length=1, max_length=1000)
    profile: Literal['evidence', 'ollama'] = 'evidence'
    method: Literal['bm25', 'dense', 'hybrid'] = 'hybrid'
    conversation_id: UUID | None = None

    @field_validator('question')
    @classmethod
    def question_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('Question cannot be blank')
        return value

    @model_validator(mode='after')
    def mode(self) -> QueryRequest:
        if self.profile == 'evidence' and self.method == 'dense':
            raise ValueError('Evidence mode has no dense model')
        return self


class Citation(BaseModel):
    chunk_id: UUID
    quote: str = Field(min_length=1, max_length=900)


class Claim(BaseModel):
    text: str
    citations: list[Citation]


class Source(BaseModel):
    id: UUID
    document_id: UUID
    revision_id: UUID
    parse_run_id: UUID
    filename: str
    collection: str
    version: str
    heading: str
    text: str
    page: int
    start_line: int
    end_line: int
    score: float
    rank: int
    context_origin: str = 'retrieved'


class QueryScope(BaseModel):
    kb_id: UUID
    data_epoch: int
    index_generation_id: UUID | None


class Timing(BaseModel):
    queue_ms: float = 0
    retrieval_ms: float = 0
    generation_ms: float = 0
    total_ms: float = 0


class QueryResponse(BaseModel):
    query_id: UUID
    query_job_id: UUID | None = None
    conversation_id: UUID | None = None
    status: Literal['queued', 'running', 'answered', 'evidence_found', 'no_evidence', 'needs_scope',
                    'clarify', 'partial', 'failed', 'cancelled', 'superseded']
    scope: QueryScope
    question: str
    effective_question: str
    claims: list[Claim] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    warning: str | None = None
    error_code: str | None = None
    timing: Timing = Field(default_factory=Timing)
    generation_assessment: dict[str, JsonValue] | None = None

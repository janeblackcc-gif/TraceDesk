from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingIdentity:
    provider: str
    tag: str
    digest: str
    dimension: int = 1024
    input_profile: str = 'rc2-document-text-query-instruct-v1'


class EmbeddingProvider(Protocol):
    def identity(self) -> EmbeddingIdentity: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...

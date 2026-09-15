from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Chunk, ChunkEmbedding, DocumentRevision, GenerationRevision, KnowledgeBase, LogicalDocument, ParseRun
from app.retrieval import search, search_context
from app.services.errors import DomainError


def candidates(session: Session, kb_id: UUID, generation_id: UUID | None = None) -> list[dict]:
    """Caller authorizes KB first. SQL visibility predicates apply to every mode."""
    kb = session.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        raise DomainError('NOT_FOUND', 404)
    statement = select(Chunk, DocumentRevision, LogicalDocument).join(DocumentRevision, DocumentRevision.id == Chunk.revision_id)
    statement = statement.join(LogicalDocument, LogicalDocument.id == DocumentRevision.document_id).where(
        LogicalDocument.kb_id == kb_id, LogicalDocument.deleted_at.is_(None), DocumentRevision.deleted_at.is_(None),
        DocumentRevision.status.not_in(['quarantined', 'deleted']))
    if generation_id is not None:
        statement = statement.join(GenerationRevision, (GenerationRevision.revision_id == Chunk.revision_id) &
            (GenerationRevision.parse_run_id == Chunk.parse_run_id)).where(GenerationRevision.generation_id == generation_id,
                GenerationRevision.kb_id == kb_id, LogicalDocument.active_revision_id == Chunk.revision_id)
    else:
        latest = select(ParseRun.id).where(ParseRun.revision_id == DocumentRevision.id, ParseRun.status == 'succeeded')
        latest = latest.order_by(ParseRun.finished_at.desc().nulls_last(), ParseRun.id).limit(1).correlate(DocumentRevision).scalar_subquery()
        statement = statement.where(Chunk.parse_run_id == latest, LogicalDocument.desired_revision_id == DocumentRevision.id)
    rows = session.execute(statement.order_by(Chunk.id).limit(50001)).all()
    if len(rows) > 50000:
        raise DomainError('CORPUS_CAPACITY_EXCEEDED', 429)
    return [{'id': str(chunk.id), 'chunk_id': str(chunk.id), 'doc_id': str(revision.id),
             'document_id': str(document.id), 'revision_id': str(revision.id), 'parse_run_id': str(chunk.parse_run_id),
             'filename': document.display_name, 'collection': kb.slug, 'version': revision.source_version_label or str(revision.revision_no),
             'heading': chunk.heading, 'text': chunk.text, 'page': chunk.page_start, 'start_line': chunk.line_start,
             'end_line': chunk.line_end, 'legacy_chunk_id': chunk.legacy_chunk_id} for chunk, revision, document in rows]


def dense_scores(session: Session, kb_id: UUID, generation_id: UUID, vector: list[float]) -> dict[str, float]:
    score = 1 - ChunkEmbedding.embedding.cosine_distance(vector)
    rows = session.execute(select(ChunkEmbedding.chunk_id, score)
        .join(GenerationRevision, (GenerationRevision.generation_id == ChunkEmbedding.generation_id) &
            (GenerationRevision.revision_id == ChunkEmbedding.revision_id) & (GenerationRevision.parse_run_id == ChunkEmbedding.parse_run_id))
        .join(LogicalDocument, LogicalDocument.id == GenerationRevision.document_id)
        .join(DocumentRevision, DocumentRevision.id == GenerationRevision.revision_id)
        .where(ChunkEmbedding.generation_id == generation_id, GenerationRevision.kb_id == kb_id,
               LogicalDocument.deleted_at.is_(None), DocumentRevision.deleted_at.is_(None),
               DocumentRevision.status.not_in(['deleted', 'quarantined']),
               LogicalDocument.active_revision_id == ChunkEmbedding.revision_id)).all()
    return {str(identifier): float(value) for identifier, value in rows}


def retrieve(session: Session, kb_id: UUID, generation_id: UUID | None, queries: list[str],
             method: str = 'hybrid', vectors: list[list[float]] | None = None, *, context: bool = True) -> list[dict]:
    rows = candidates(session, kb_id, generation_id)
    scores = None
    if vectors is not None:
        if generation_id is None or len(vectors) != len(queries):
            raise DomainError('INDEX_REQUIRED', 409)
        scores = [dense_scores(session, kb_id, generation_id, vector) for vector in vectors]
        expected = {row['id'] for row in rows}
        if any(set(values) != expected for values in scores):
            raise DomainError('INDEX_INCOMPLETE', 409)
    if context:
        return search_context(queries, rows, method, query_scores=scores)
    return search(queries[0], rows, method, dense_scores=scores[0] if scores else None)

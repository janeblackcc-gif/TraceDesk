from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import Chunk, DocumentRevision, KnowledgeBase, LegacyAlias, LogicalDocument


@dataclass(frozen=True)
class LegacyResolution:
    status: int
    target_id: UUID | None = None
    text: str | None = None
    text_sha256: str | None = None


def resolve_legacy(session: Session, kind: str, identifier: str, *, authorized_kb_id: UUID) -> LegacyResolution:
    """Internal resolver: the calling service must authorize this KB first.

    No public route is provided until T-022/T-051 supplies centralized ACL.
    Historical text is returned only for the original revision, never a replacement.
    """
    alias = session.get(LegacyAlias, (kind, identifier))
    if alias is None or alias.kb_id != authorized_kb_id:
        return LegacyResolution(404)
    kb = session.get(KnowledgeBase, alias.kb_id)
    revision = session.get(DocumentRevision, alias.revision_id)
    document = session.get(LogicalDocument, revision.document_id) if revision else None
    if (alias.deleted_at is not None or kb is None or kb.deleted_at is not None or
            revision is None or document is None or document.deleted_at is not None or
            revision.deleted_at is not None or revision.status in {'deleted', 'superseded'} or
            (document.active_revision_id is not None and document.active_revision_id != revision.id)):
        return LegacyResolution(410)
    if revision.status == 'quarantined':
        return LegacyResolution(404)
    if kind == 'document':
        return LegacyResolution(200, alias.target_id)
    chunk = session.get(Chunk, alias.target_id)
    if chunk is None:
        return LegacyResolution(410)
    return LegacyResolution(200, chunk.id, chunk.text, chunk.text_sha256)

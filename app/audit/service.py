from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import AuditEvent


def audit(session: Session, action: str, *, actor: UUID | None, request_id: str,
          resource_type: str = 'system', resource_id: str | None = None,
          workspace_id: UUID | None = None, kb_id: UUID | None = None, outcome: str = 'allowed') -> None:
    # Deliberately no arbitrary metadata argument: caller cannot accidentally log text/secrets.
    session.add(AuditEvent(actor_user_id=actor, workspace_id=workspace_id, kb_id=kb_id,
        action=action, resource_type=resource_type, resource_id=resource_id,
        outcome=outcome, request_id=request_id, details={}))
    session.flush()

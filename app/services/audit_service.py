"""Append-only record of consequential actions."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User


def record(
    db: AsyncSession,
    actor: User | None,
    action: str,
    entity: str,
    entity_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    **details,
) -> AuditLog:
    """Stage an audit row. The caller commits it with the change it describes, so the two
    cannot diverge."""
    entry = AuditLog(
        org_id=org_id if org_id is not None else (actor.org_id if actor else None),
        actor_id=actor.id if actor else None,
        # Denormalised so the trail stays readable after an account is deleted.
        actor_email=actor.email if actor else None,
        action=action,
        entity=entity,
        entity_id=entity_id,
        details=details,
    )
    db.add(entry)
    return entry


async def for_entity(db: AsyncSession, entity: str, entity_id: uuid.UUID, limit: int = 50) -> list[AuditLog]:
    rows = await db.scalars(
        select(AuditLog)
        .where(AuditLog.entity == entity, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    return list(rows)

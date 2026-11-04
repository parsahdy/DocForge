"""Compare-and-set state transitions (W8).

``UPDATE ... WHERE status = <expected>`` succeeds for exactly one of any number of
concurrent callers. The return value says whether *we* won. The caller commits.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.states import Status, check_transition
from app.db.models import File, utcnow


def transition(
    db: Session,
    tanent_id: str,
    file_id: uuid.UUID,
    src: Status,
    dst: Status,
    **fields: Any,
) -> bool:

    check_transition(src, dst)
    statement = (
        update(File)
        .where(File.tenant_id == tanent_id, File.file_id == file_id,
               File.status == src.value)
        .values(status=dst.value, updated_at=utcnow(), **fields)
        .execution_options(synchronize_session=False)
    )
    return db.execute(statement).rowcount == 1
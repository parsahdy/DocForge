"""The queue message contract (W4): small, ID-only and versioned.

* ``v`` lets a consumer recognise old messages and reject messages it cannot understand.
* Unknown extra fields are ignored, so a newer producer can add fields safely.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.errors import PermanentError

SUPPORTED_VERSIONS = frozenset({1})


class JobMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    file_id: uuid.UUID
    tenant_id: str

    def to_wire(self) -> dict[str, Any]:
        return {"v": self.v, "file_id": str(self.file_id), "tenant_id": self.tenant_id}

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> JobMessage:
        try:
            message = cls.model_validate(raw)
        except ValidationError as exc:
            raise PermanentError(
                "BAD_MESSAGE", f"invalid job message: {exc.error_count()} error(s)"
            ) from exc
        if message.v not in SUPPORTED_VERSIONS:
            raise PermanentError("UNSUPPORTED_MESSAGE_VERSION", f"message version {message.v} is not supported")
        return message
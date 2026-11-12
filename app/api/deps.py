import re
from functools import lru_cache
from typing import Annotated

from fastapi import Header, HTTPException

from app.core.config import get_settings
from app.services.queue import Publisher, publish_job
from app.services.storage import Storage

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


def require_tenant(x_tenant_id: Annotated[str, Header()]) -> str:
    """Placeholder for authentication: a real system derives the tenant from a verified token.

    The strict pattern also keeps the value safe to use inside object keys.
    """
    if not _TENANT_RE.fullmatch(x_tenant_id):
        raise HTTPException(
            status_code=422, detail="X-Tenant-Id must match [a-z0-9][a-z0-9_-]{1,62}"
        )
    return x_tenant_id


@lru_cache
def get_storage() -> Storage:
    return Storage(get_settings())


def get_publisher() -> Publisher:
    return publish_job
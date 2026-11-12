import logging
import uuid
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_publisher, get_storage, require_tenant
from app.api.schemas import CreateFileRequest, CreateFileResponse, FileListOut, FileOut, UploadInstruction
from app.core.config import MB, Settings, get_settings
from app.core.ids import uuid7
from app.core.keys import original_key
from app.core.messages import JobMessage
from app.core.routing import QUEUE_SMALL, route_queue
from app.core.states import Status
from app.db.models import File
from app.db.session import get_db
from app.db.transitions import transition
from app.pipeline.parse import SUPPORTED_EXTENSIONS
from app.services.queue import Publisher
from app.services.storage import Storage

logger = logging.getLogger("docforge.api")
router = APIRouter(prefix="/files", tags=["files"])

TenantId = Annotated[str, Depends(require_tenant)]
Db = Annotated[Session, Depends(get_db)]
StorageDep = Annotated[Storage, Depends(get_storage)]
PublisherDep = Annotated[Publisher, Depends(get_publisher)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _get_or_404(db: Session, tenant_id: str, file_id: uuid.UUID) -> File:
    # Always looked up by (tenant, file): another tenant's file is simply "not found"
    row = db.get(File, (tenant_id, file_id))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="file not found")
    return row


@router.post("", status_code=201, response_model=CreateFileResponse)
def create_file(
    body: CreateFileRequest,
    tenant_id: TenantId,
    db: Db,
    storage: StorageDep,
    settings: SettingsDep,
) -> CreateFileResponse:
    filename = PurePosixPath(body.filename.replace("\\", "/")).name
    if PurePosixPath(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, 
            detail=f"supported types: {sorted(SUPPORTED_EXTENSIONS)}"
        )
    if body.size_bytes > settings.max_file_mb * MB:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, 
            detail=f"maximum file size is {settings.max_file_mb} MB"
        )

    file_id = uuid7()
    key = original_key(tenant_id, file_id)
    url = storage.presign_put(key, body.size_bytes, settings.presign_expires_s)

    db.add(
        File(
            tenant_id=tenant_id,
            file_id=file_id,
            filename=filename,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            s3_key=key,
            status=Status.PENDING_UPLOAD.value,
        )
    )
    db.commit()

    return CreateFileResponse(
        file_id=file_id,
        status=Status.PENDING_UPLOAD.value,
        upload=UploadInstruction(
            url=url,
            headers={"Content-Lenght": str(body.size_bytes)},
            expires_in=settings.presign_expires_s,
        ),
    )


@router.post("/{file_id}/complete", status_code=202, response_model=FileOut)
def complete_upload(
    file_id: uuid.UUID,
    tenant_id: TenantId,
    db: Db,
    storage: StorageDep,
    publish: PublisherDep,
    settings: SettingsDep,
) -> File:
    """Idempotent: safe to call again after a timeout or a failure."""
    row = _get_or_404(db, tenant_id, file_id)

    if row.status == Status.PENDING_UPLOAD.value:
        head = storage.head(row.s3_key)
        if head is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, 
                detail="the file has not been uploaded yet"
            )
        actual = head["ContentLength"]
        if actual != row.size_bytes or actual > settings.max_file_mb * MB:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, 
                detail=f"uploaded {actual} bytes, declared {row.size_bytes}"
            )
        transition(db, tenant_id, file_id, Status.PENDING_UPLOAD.value, Status.UPLOADED)
        db.commit()  # commit 1: the upload is verified
        db.refresh(row)

    if row.status == Status.UPLOADED.value:
        queue = route_queue(row.size_bytes, settings.size_class_small_max_mb)
        transition(db, tenant_id, file_id, Status.UPLOADED, Status.QUEUED, queue=queue)
        db.commit()  # commit 2: ready to be published
        db.refresh(row)

    if row.status == Status.QUEUED.value:
        try:
            publish(JobMessage(file_id=file_id, tenant_id=tenant_id), row.queue or QUEUE_SMALL)
        except Exception as exc:  # the broker is down: the row stays QUEUED, the client may retry
            logger.error("publish failed", exc_info=exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="queue unavailable, retry", headers={"Retry-After": "5"}
            ) from exc

    return row    


@router.get("/{file_id}", response_model=FileOut)
def get_file(file_id: uuid.UUID, tenant_id: TenantId, db: Db) -> File:
    return _get_or_404(db, tenant_id, file_id)


@router.get("", response_model=FileListOut)
def list_files(
    tenant_id: TenantId,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    after: uuid.UUID | None = None,
    status: str | None = None,
) -> FileListOut:
    """Newest first. Keyset pagination: UUIDv7 is time-ordered, so the primary key is the cursor."""
    statement = select(File).where(File.tenant_id == tenant_id)
    if status is not None:
        try:
            statement = statement.where(File.status == Status(status).value)
        except ValueError:
            raise HTTPException(
                status_code=422, 
                detail=f"unknown status: {status}"
            ) from None
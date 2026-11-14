import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateFileRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, description="exact size of the file in bytes")
    content_type: str | None = Field(default=None, max_length=128)


class UploadInstruction(BaseModel):
    method: str = "PUT"
    url: str
    headers: dict[str, str]
    expires_in: int


class CreateFileResponse(BaseModel):
    file_id: uuid.UUID
    status: str
    upload: UploadInstruction


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    file_id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    queue: str | None
    chunk_count: int | None
    attempts: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class FileListOut(BaseModel):
    items: list[FileOut]
    next_after: uuid.UUID | None
    retry_at: datetime | None
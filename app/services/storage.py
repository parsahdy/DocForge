"""Object storage behind the S3 API only (D6): no vendor-specific calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import Settings

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}


class Storage:
    def __init__(self, settings: Settings) -> None:
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},  # http://host/bucket/key, works without DNS tricks
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
        )
        common: dict[str, Any] = {
            "aws_access_key_id": settings.s3_access_key,
            "aws_secret_access_key": settings.s3_secret_key,
            "region_name": settings.s3_region,
            "config": config,
        }
        # Internal client: workers and server-side checks. Public client: only to sign URLs.
        self._internal = boto3.client("s3", endpoint_url=settings.s3_endpoint_internal, **common)
        self._public = boto3.client("s3", endpoint_url=settings.s3_endpoint_public, **common)
        self.bucket = settings.s3_bucket

    def presign_put(self, key: str, content_length: int, expires_s: int) -> str:
        """URL for a direct upload (W5).

        ``ContentLength`` becomes a *signed* header: the storage rejects a body whose
        length differs from the declared size, so the size cap holds without any
        bytes passing through the API.
        """
        return self._public.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentLength": content_length},
            ExpiresIn=expires_s,
            HttpMethod="PUT",
        )

    def head(self, key: str) -> dict[str, Any] | None:
        try:
            return self._internal.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in _NOT_FOUND:
                return None
            raise

    def download(self, key: str, dest: Path) -> None:
        self._internal.download_file(self.bucket, key, str(dest))  # streams to disk, not memory

    def upload(self, src: Path, key: str, metadata: dict[str, str] | None = None) -> None:
        extra = {"Metadata": metadata} if metadata else None
        self._internal.upload_file(str(src), self.bucket, key, ExtraArgs=extra)

    def ensure_bucket(self) -> None:
        try:
            self._internal.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) not in _NOT_FOUND:
                raise
            self._internal.create_bucket(Bucket=self.bucket)
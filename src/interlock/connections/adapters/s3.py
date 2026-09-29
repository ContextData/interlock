"""S3 file listing adapter for ingestion.

aiobotocore is an optional dependency - the adapter degrades gracefully
when it is not installed, returning empty results instead of raising.
"""

from __future__ import annotations

import logging
from typing import Any

from interlock.models import S3FileInfo

logger = logging.getLogger(__name__)

try:
    from aiobotocore.session import get_session as _get_aio_session

    _HAS_AIOBOTOCORE = True
except ImportError:  # pragma: no cover - optional dep
    _HAS_AIOBOTOCORE = False


class S3Adapter:
    """Lists files from S3 buckets for ingestion."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._credentials: dict[str, Any] = {}
        if aws_access_key_id:
            self._credentials["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key:
            self._credentials["aws_secret_access_key"] = aws_secret_access_key
        self._region = region_name
        self._endpoint_url = endpoint_url
        self._available = False

    async def initialize(self) -> None:
        """Check if aiobotocore is available and mark adapter ready."""
        if _HAS_AIOBOTOCORE:
            self._available = True
            logger.info("S3Adapter initialized (aiobotocore available)")
        else:
            self._available = False
            logger.warning("S3Adapter: aiobotocore not installed - S3 operations disabled")

    async def list_files(
        self,
        extensions: set[str] | None = None,
        max_files: int = 10_000,
    ) -> list[S3FileInfo]:
        """List files in the bucket/prefix.

        Returns an empty list when aiobotocore is not available.
        """
        if not self._available:
            return []

        files: list[S3FileInfo] = []
        session = _get_aio_session()
        async with session.create_client(
            "s3",
            region_name=self._region,
            endpoint_url=self._endpoint_url,
            **self._credentials,
        ) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
                for obj in page.get("Contents", []):
                    key: str = obj["Key"]
                    if extensions and not any(key.endswith(ext) for ext in extensions):
                        continue
                    files.append(
                        S3FileInfo(
                            key=key,
                            size=obj["Size"],
                            last_modified=obj.get("LastModified"),
                            etag=obj.get("ETag"),
                        )
                    )
                    if len(files) >= max_files:
                        return files
        return files

    async def list_level(self, prefix: str, *, max_keys: int = 1000) -> tuple[list[str], int]:
        """The immediate sub-prefixes of `prefix`, and how many objects sit directly in it.

        One `Delimiter='/'` listing: nothing below the next level is read, and
        no object is fetched. Stops after `max_keys` entries of either kind.
        """
        if not self._available:
            return [], 0
        prefixes: list[str] = []
        objects = 0
        session = _get_aio_session()
        async with session.create_client(
            "s3",
            region_name=self._region,
            endpoint_url=self._endpoint_url,
            **self._credentials,
        ) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix, Delimiter="/"):
                for entry in page.get("CommonPrefixes", []) or []:
                    prefixes.append(str(entry["Prefix"]))
                objects += len(page.get("Contents", []) or [])
                if len(prefixes) + objects >= max_keys:
                    break
        return prefixes, objects

    async def download_file(self, key: str, local_path: str) -> str:
        """Download a file from S3 to *local_path*. Returns *local_path*."""
        if not self._available:
            raise RuntimeError("S3Adapter is not available (aiobotocore not installed)")

        session = _get_aio_session()
        async with session.create_client(
            "s3",
            region_name=self._region,
            endpoint_url=self._endpoint_url,
            **self._credentials,
        ) as client:
            resp = await client.get_object(Bucket=self._bucket, Key=key)
            async with resp["Body"] as stream:
                data = await stream.read()
            with open(local_path, "wb") as fh:
                fh.write(data)
        return local_path

    @property
    def available(self) -> bool:
        return self._available

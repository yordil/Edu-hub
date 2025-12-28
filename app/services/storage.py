"""Blob storage for uploaded files.

Two drivers behind one interface, chosen by `STORAGE_PROVIDER`:

- `local` writes under `LOCAL_STORAGE_DIR`. No credentials, so development, CI and the
  test suite need no AWS account.
- `s3` writes to `S3_BUCKET`. Credentials come from the standard boto3 chain, which in
  production means an IAM role on the task rather than static keys.

Uploads are held so a worker can parse them after the request returns, and so a failed
parse can be retried against the original bytes instead of asking the teacher to upload again.
"""

import asyncio
import functools
import logging
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings

log = logging.getLogger("eduhub.storage")


class StorageError(RuntimeError):
    """The object could not be stored or retrieved."""


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


def _safe(key: str) -> str:
    """Keys are built from ids and filenames, so refuse anything that could climb out of the root."""
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise StorageError(f"Unsafe storage key: {key!r}")
    return key


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / _safe(key)

    async def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> None:
        path = self._path(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except OSError as e:
            raise StorageError(f"Could not read {key}") from e

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, True)


class S3Storage:
    def __init__(self, bucket: str, region: str | None = None):
        self.bucket = bucket
        self.region = region
        self._client = None

    def _s3(self):
        # Imported lazily and cached: the local driver must not require boto3 to be configured.
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    async def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> None:
        call = functools.partial(
            self._s3().put_object,
            Bucket=self.bucket,
            Key=_safe(key),
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        try:
            await asyncio.to_thread(call)
        except Exception as e:  # noqa: BLE001 - botocore raises a wide family of client errors
            raise StorageError(f"Could not upload {key}: {e}") from e

    async def get(self, key: str) -> bytes:
        def read() -> bytes:
            return self._s3().get_object(Bucket=self.bucket, Key=_safe(key))["Body"].read()

        try:
            return await asyncio.to_thread(read)
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"Could not download {key}: {e}") from e

    async def delete(self, key: str) -> None:
        call = functools.partial(self._s3().delete_object, Bucket=self.bucket, Key=_safe(key))
        try:
            await asyncio.to_thread(call)
        except Exception as e:  # noqa: BLE001
            log.warning("could not delete %s: %s", key, e)


@functools.lru_cache
def get_storage() -> Storage:
    s = get_settings()
    if s.storage_provider == "s3":
        if not s.s3_bucket:
            raise StorageError("STORAGE_PROVIDER=s3 but S3_BUCKET is not set")
        return S3Storage(s.s3_bucket, s.s3_region)
    return LocalStorage(s.local_storage_dir)


def paper_key(paper_id, filename: str, kind: str = "exam") -> str:
    """One prefix per paper so the exam and its answer key are deleted together."""
    suffix = Path(filename).suffix.lower() or ".pdf"
    return f"papers/{paper_id}/{kind}{suffix}"

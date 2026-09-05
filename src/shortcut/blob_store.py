"""Where uploaded bytes actually live: a local folder, or a shared S3 bucket.

Everything above this module works in terms of a "key" (a short name, like a
filename) and gets bytes back. It never has to know which backend is in use.

Local disk is the default, so nobody needs AWS credentials just to run the
app or the test suite. Setting the SHORTCUT_S3_BUCKET environment variable
switches every store over to S3 - which is what actually makes an upload
visible to a teammate, instead of staying stuck on whoever's machine it was
added on.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

__all__ = ["BlobStore", "LocalBlobStore", "S3BlobStore", "build_blob_store"]


class BlobStore(ABC):
    """Read, write and delete bytes under a key. No filesystem assumptions."""

    @abstractmethod
    def read(self, key: str) -> bytes | None:
        """The bytes stored under ``key``, or None if there are none."""

    @abstractmethod
    def write(self, key: str, content: bytes) -> None:
        """Store ``content`` under ``key``, replacing whatever was there."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove whatever is stored under ``key``. Fine if there is nothing."""


class LocalBlobStore(BlobStore):
    """Backs onto an ordinary folder on this machine."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def read(self, key: str) -> bytes | None:
        path = self.directory / key
        if not path.exists():
            return None
        return path.read_bytes()

    def write(self, key: str, content: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / key
        # Written under a temporary name and moved into place, so a reader
        # never sees a half-written file.
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)

    def delete(self, key: str) -> None:
        (self.directory / key).unlink(missing_ok=True)


class S3BlobStore(BlobStore):
    """Backs onto one prefix inside a shared S3 bucket.

    boto3 is imported lazily, inside the methods that need it, so that
    nobody without it installed - or without AWS credentials - is affected
    unless SHORTCUT_S3_BUCKET is actually set.
    """

    def __init__(self, bucket: str, *, prefix: str = "") -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _client(self):
        import boto3

        return boto3.client("s3")

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client().get_object(
                Bucket=self.bucket, Key=self._full_key(key)
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return None
            raise
        return response["Body"].read()

    def write(self, key: str, content: bytes) -> None:
        self._client().put_object(
            Bucket=self.bucket, Key=self._full_key(key), Body=content
        )

    def delete(self, key: str) -> None:
        self._client().delete_object(Bucket=self.bucket, Key=self._full_key(key))


def build_blob_store(local_directory: str | Path, *, prefix: str) -> BlobStore:
    """The store to use: S3 when SHORTCUT_S3_BUCKET is set, local disk otherwise.

    ``local_directory`` only matters for the local backend. ``prefix`` only
    matters for S3, where it keeps photos and floorplans apart inside one
    shared bucket instead of mixing their filenames together.
    """
    bucket = os.environ.get("SHORTCUT_S3_BUCKET")
    if bucket:
        return S3BlobStore(bucket, prefix=prefix)
    return LocalBlobStore(local_directory)

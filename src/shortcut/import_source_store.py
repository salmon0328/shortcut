"""The drawings people upload, kept until nothing refers to them any more.

A floorplan candidate is a picture, and a picture does not belong in a JSON
queue: the plans this builds run to a megabyte each, and five of them
base64-encoded into ``import_candidates.json`` would make a file nobody can
open to see what is waiting.

So the *drawing* is kept instead, once, and the plan is rebuilt from it at the
moment somebody approves it. Rebuilding is safe because it is deterministic -
the same PDF gives the same plan, which is pinned by a test - and it buys
something worth having on its own: while candidates from an upload are still
waiting, the file they came out of is still there to look at.

Uses the same blob store as photos and floorplans, so an upload lands beside
them: on this machine when nothing says otherwise, in the shared bucket when
``SHORTCUT_S3_BUCKET`` does.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from shortcut.blob_store import BlobStore, build_blob_store

__all__ = ["ImportSourceStore", "ImportSourceError"]


class ImportSourceError(Exception):
    """An uploaded drawing could not be stored or read back."""


class ImportSourceStore:
    """Keeps uploaded drawings, addressed by an id."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._blobs: BlobStore = build_blob_store(self.directory, prefix="imports")

    def add(self, content: bytes) -> str:
        """Store one drawing and return the id it can be read back by."""
        source_id = uuid.uuid4().hex
        self._blobs.write(f"{source_id}.pdf", content)
        return source_id

    def read(self, source_id: str) -> bytes:
        """The drawing itself.

        Raises:
            ImportSourceError: it is not there. Which is recoverable and worth
                saying plainly: the candidates from it cannot be approved, and
                re-uploading the same file fixes it.
        """
        content = self._blobs.read(f"{source_id}.pdf")
        if content is None:
            raise ImportSourceError(
                f"The drawing this was read from ({source_id}) is no longer stored. "
                "Upload it again to approve what it found."
            )
        return content

    def remove(self, source_id: str) -> None:
        """Forget a drawing. Called once nothing is waiting on it."""
        self._blobs.delete(f"{source_id}.pdf")

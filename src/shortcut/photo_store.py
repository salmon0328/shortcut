"""Storage for photographs of the building.

A photo is always *of* something on the map and *facing* somewhere: "standing
at the lift lobby, looking towards the courtyard". That pairing is what lets
the turn-by-turn screen show the right picture for the direction someone is
actually walking, rather than a picture of the same corridor from the far end.

Both the image files and the JSON index describing them go through a
:class:`~shortcut.blob_store.BlobStore`, which is a local folder unless
``SHORTCUT_S3_BUCKET`` is set, in which case both live in S3 - so a photo one
teammate uploads shows up for everyone, not just on their machine.

Nothing here inspects image content. Deciding whether a photo actually shows
what it claims is an AI job for later; today a photo is trusted as uploaded.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from shortcut.blob_store import BlobStore, build_blob_store

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_PHOTO_BYTES",
    "Photo",
    "PhotoStore",
    "PhotoStoreError",
    "PhotoTargetKind",
]

PhotoTargetKind = Literal["node", "edge"]

# Only ordinary photograph formats. Anything else is refused rather than
# stored, since these files are served straight back to browsers.
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# A generous phone-photo ceiling. Large enough for a normal upload, small
# enough that a mistake cannot fill the disk.
MAX_PHOTO_BYTES = 10 * 1024 * 1024


class PhotoStoreError(Exception):
    """The photo index is unreadable, or a photo cannot be stored."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Photo:
    """One photograph, and everything needed to find it again."""

    id: str
    target_kind: PhotoTargetKind
    target_id: str
    # The node id being looked towards. For an edge this is the end you are
    # walking to, which is what makes a photo direction-aware. None means the
    # photo is not tied to a direction.
    facing: str | None
    # Where this was taken, given at upload time. Held on the photo itself so
    # a file is still identifiable if it is ever separated from the graph.
    building: str
    floor: str
    location: str
    filename: str
    content_type: str
    size_bytes: int
    uploaded_at: str
    caption: str = ""


class PhotoStore:
    """Reads and writes photo files and the index describing them."""

    INDEX_KEY = "photos.json"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._blobs: BlobStore = build_blob_store(self.directory, prefix="photos")

    # -- reading -----------------------------------------------------------

    def all(self) -> list[Photo]:
        return self._read()

    def get(self, photo_id: str) -> Photo | None:
        for photo in self._read():
            if photo.id == photo_id:
                return photo
        return None

    def for_target(
        self, target_kind: str, target_id: str, facing: str | None = None
    ) -> list[Photo]:
        """Photos of one node or edge, optionally only those facing one way."""
        matches = [
            photo
            for photo in self._read()
            if photo.target_kind == target_kind and photo.target_id == target_id
        ]
        if facing is None:
            return matches
        return [photo for photo in matches if photo.facing == facing]

    def find_best(
        self, target_kind: str, target_id: str, facing: str | None
    ) -> Photo | None:
        """The photo to show for one step of a route.

        A photo facing the way the walker is going is used if there is one.
        Otherwise nothing is returned: a picture looking back the way they came
        is more confusing than no picture at all.
        """
        return self.best_of(self._read(), target_kind, target_id, facing)

    @staticmethod
    def best_of(
        photos: list[Photo], target_kind: str, target_id: str, facing: str | None
    ) -> Photo | None:
        """The same choice :meth:`find_best` makes, against an already-read list.

        A route has several steps, each wanting its own photo lookup. Calling
        :meth:`find_best` per step would re-fetch the whole index that many
        times - cheap on local disk, but each one is a real network round trip
        against S3. Reading the index once and reusing it here is what keeps a
        multi-step route to a single fetch instead of one per step.
        """
        matches = [
            photo
            for photo in photos
            if photo.target_kind == target_kind and photo.target_id == target_id
        ]
        if facing is not None:
            facing_matches = [photo for photo in matches if photo.facing == facing]
            if facing_matches:
                return facing_matches[0]

        undirected = [photo for photo in matches if photo.facing is None]
        return undirected[0] if undirected else None

    def read_file(self, photo: Photo) -> bytes:
        """The bytes of a photo's file."""
        content = self._blobs.read(photo.filename)
        if content is None:
            raise PhotoStoreError(
                f"Photo {photo.id} is in the index but its file is missing."
            )
        return content

    # -- writing -----------------------------------------------------------

    def add(
        self,
        *,
        content: bytes,
        content_type: str,
        target_kind: PhotoTargetKind,
        target_id: str,
        building: str,
        floor: str,
        location: str,
        facing: str | None = None,
        caption: str = "",
    ) -> Photo:
        """Store one photo and record where it was taken.

        Raises:
            PhotoStoreError: the file is empty, too large, or not an image
                format this store accepts.
        """
        if not content:
            raise PhotoStoreError("The uploaded file is empty.")
        if len(content) > MAX_PHOTO_BYTES:
            megabytes = MAX_PHOTO_BYTES // (1024 * 1024)
            raise PhotoStoreError(f"Photos must be smaller than {megabytes} MB.")

        extension = ALLOWED_CONTENT_TYPES.get(content_type)
        if extension is None:
            allowed = ", ".join(sorted(ALLOWED_CONTENT_TYPES))
            raise PhotoStoreError(
                f"{content_type!r} is not a photo format this store accepts. "
                f"Use one of: {allowed}."
            )

        photo_id = uuid.uuid4().hex
        photo = Photo(
            id=photo_id,
            target_kind=target_kind,
            target_id=target_id,
            facing=facing,
            building=building,
            floor=floor,
            location=location,
            filename=f"{photo_id}{extension}",
            content_type=content_type,
            size_bytes=len(content),
            uploaded_at=_now(),
            caption=caption,
        )

        self._save_file(photo.filename, content)

        photos = self._read()
        photos.append(photo)
        self._write(photos)
        return photo

    def update_details(
        self,
        photo_id: str,
        *,
        caption: str | None = None,
        location: str | None = None,
        facing: str | None = None,
        clear_facing: bool = False,
    ) -> Photo | None:
        """Change what is recorded about a photo without re-uploading the file.

        ``facing`` is here because a bulk upload cannot know it. A file named
        ``photo_3_2026-09-03_13-41-36.jpg`` says nothing about which way the
        camera pointed, so photos arrive undirected and somebody says later.
        Without this the only way to label one was to delete it and upload it
        again, which for a whole walk is not a thing anybody would do.

        ``clear_facing`` exists because ``None`` already means "leave it
        alone"; setting a photo back to undirected needs its own word.
        """
        photos = self._read()
        for index, photo in enumerate(photos):
            if photo.id != photo_id:
                continue
            updated = replace(
                photo,
                caption=photo.caption if caption is None else caption,
                location=photo.location if location is None else location,
                facing=None if clear_facing else (photo.facing if facing is None else facing),
            )
            photos[index] = updated
            self._write(photos)
            return updated
        return None

    def delete(self, photo_id: str) -> bool:
        """Remove a photo and its file. Returns whether there was one."""
        photos = self._read()
        remaining = [photo for photo in photos if photo.id != photo_id]
        if len(remaining) == len(photos):
            return False

        gone = next(photo for photo in photos if photo.id == photo_id)
        # Write the index first: an orphaned file wastes space, but an index
        # entry with no file behind it breaks every page that shows it.
        self._write(remaining)
        self._blobs.delete(gone.filename)
        return True

    def delete_for_target(self, target_kind: str, target_id: str) -> int:
        """Remove every photo of one node or edge. Used when it is deleted."""
        doomed = [
            photo.id for photo in self.for_target(target_kind, target_id)
        ]
        for photo_id in doomed:
            self.delete(photo_id)
        return len(doomed)

    # -- storage -------------------------------------------------------------

    def _save_file(self, filename: str, content: bytes) -> None:
        self._blobs.write(filename, content)

    def _read(self) -> list[Photo]:
        raw_bytes = self._blobs.read(self.INDEX_KEY)
        if raw_bytes is None:
            return []

        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise PhotoStoreError(
                f"{self.INDEX_KEY} is not valid JSON: {error.msg} "
                f"(line {error.lineno}, column {error.colno})."
            ) from error

        if not isinstance(raw, list):
            raise PhotoStoreError(
                f"{self.INDEX_KEY}: expected a list, got {type(raw).__name__}."
            )
        return [self._parse(entry, index) for index, entry in enumerate(raw)]

    def _write(self, photos: list[Photo]) -> None:
        text = json.dumps([asdict(photo) for photo in photos], indent=2) + "\n"
        self._blobs.write(self.INDEX_KEY, text.encode("utf-8"))

    @staticmethod
    def _parse(entry: Any, index: int) -> Photo:
        if not isinstance(entry, dict):
            raise PhotoStoreError(
                f"photos[{index}]: expected an object, got {type(entry).__name__}."
            )
        try:
            return Photo(
                id=entry["id"],
                target_kind=entry["target_kind"],
                target_id=entry["target_id"],
                facing=entry.get("facing"),
                building=entry.get("building", ""),
                floor=entry.get("floor", ""),
                location=entry.get("location", ""),
                filename=entry["filename"],
                content_type=entry["content_type"],
                size_bytes=entry.get("size_bytes", 0),
                uploaded_at=entry["uploaded_at"],
                caption=entry.get("caption", ""),
            )
        except KeyError as error:
            raise PhotoStoreError(
                f"photos[{index}]: missing required field {error.args[0]!r}."
            ) from error

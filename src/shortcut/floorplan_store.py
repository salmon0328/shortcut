"""Storage for floorplan images, and how map coordinates sit on them.

Nothing here is used for routing. A floorplan is what turns a list of steps
into a picture: the image for one floor of one building, plus enough
information to work out where on that image a node with ``x``/``y`` sits.

None of that data has been collected yet, so this module exists so the space
is ready rather than because it is full. A floorplan can be uploaded before
anyone has measured anything: it simply stays *uncalibrated* until a scale is
set, and :meth:`Floorplan.to_pixels` says so instead of guessing.

The calibration is deliberately the simplest thing that works:

* ``origin_x_m`` / ``origin_y_m`` - the map coordinates at the image's
  top-left corner
* ``metres_per_pixel`` - how much ground one pixel covers

which is enough for ``pixel = (x - origin) / metres_per_pixel``. Rotated or
skewed plans would need more, and can have it when a real plan turns up.

Both the image files and the JSON index describing them go through a
:class:`~shortcut.blob_store.BlobStore`, which is a local folder unless
``SHORTCUT_S3_BUCKET`` is set, in which case both live in S3 - so a plan one
teammate uploads shows up for everyone, not just on their machine.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shortcut.blob_store import BlobStore, build_blob_store

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_FLOORPLAN_BYTES",
    "Floorplan",
    "FloorplanStore",
    "FloorplanStoreError",
]

ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}

# Floorplans are bigger than snapshots, and there is one per floor rather than
# one per direction, so the ceiling is higher than for photos.
MAX_FLOORPLAN_BYTES = 25 * 1024 * 1024


class FloorplanStoreError(Exception):
    """The floorplan index is unreadable, or a plan cannot be stored."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Floorplan:
    """One floor of one building, as a picture."""

    id: str
    building: str
    floor: str
    filename: str
    content_type: str
    size_bytes: int
    uploaded_at: str
    # Where the map's coordinates sit on the image. All None until somebody
    # measures the plan; see the module docstring.
    origin_x_m: float | None = None
    origin_y_m: float | None = None
    metres_per_pixel: float | None = None
    note: str = ""

    @property
    def is_calibrated(self) -> bool:
        """Whether node coordinates can be turned into positions on this image."""
        return (
            self.origin_x_m is not None
            and self.origin_y_m is not None
            and self.metres_per_pixel is not None
            and self.metres_per_pixel > 0
        )

    def to_pixels(self, x_m: float, y_m: float) -> tuple[float, float] | None:
        """Where a point on the map falls on this image.

        Returns ``None`` when the plan has not been calibrated, rather than a
        made-up position: a pin in the wrong place is worse than no pin.
        """
        if not self.is_calibrated:
            return None
        return (
            (x_m - self.origin_x_m) / self.metres_per_pixel,
            (y_m - self.origin_y_m) / self.metres_per_pixel,
        )


class FloorplanStore:
    """Reads and writes floorplan images and the index describing them."""

    INDEX_KEY = "floorplans.json"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._blobs: BlobStore = build_blob_store(self.directory, prefix="floorplans")

    # -- reading -----------------------------------------------------------

    def all(self) -> list[Floorplan]:
        return self._read()

    def get(self, floorplan_id: str) -> Floorplan | None:
        for plan in self._read():
            if plan.id == floorplan_id:
                return plan
        return None

    def for_floor(self, building: str, floor: str) -> Floorplan | None:
        """The plan for one floor, if one has been uploaded.

        The most recent wins, so replacing a plan is just uploading a newer
        one. Timestamps only go down to the second, so two uploads a moment
        apart can tie; the later position in the file breaks it, since plans
        are only ever appended.
        """
        matches = [
            (index, plan)
            for index, plan in enumerate(self._read())
            if plan.building == building and plan.floor == floor
        ]
        if not matches:
            return None
        return max(matches, key=lambda pair: (pair[1].uploaded_at, pair[0]))[1]

    def read_file(self, plan: Floorplan) -> bytes:
        content = self._blobs.read(plan.filename)
        if content is None:
            raise FloorplanStoreError(
                f"Floorplan {plan.id} is in the index but its file is missing."
            )
        return content

    # -- writing -----------------------------------------------------------

    def add(
        self,
        *,
        content: bytes,
        content_type: str,
        building: str,
        floor: str,
        origin_x_m: float | None = None,
        origin_y_m: float | None = None,
        metres_per_pixel: float | None = None,
        note: str = "",
    ) -> Floorplan:
        """Store one floorplan image, calibrated or not."""
        if not content:
            raise FloorplanStoreError("The uploaded file is empty.")
        if len(content) > MAX_FLOORPLAN_BYTES:
            megabytes = MAX_FLOORPLAN_BYTES // (1024 * 1024)
            raise FloorplanStoreError(
                f"Floorplans must be smaller than {megabytes} MB."
            )

        extension = ALLOWED_CONTENT_TYPES.get(content_type)
        if extension is None:
            allowed = ", ".join(sorted(ALLOWED_CONTENT_TYPES))
            raise FloorplanStoreError(
                f"{content_type!r} is not an image format this store accepts. "
                f"Use one of: {allowed}."
            )
        if metres_per_pixel is not None and metres_per_pixel <= 0:
            raise FloorplanStoreError("metres_per_pixel must be greater than zero.")

        plan_id = uuid.uuid4().hex
        plan = Floorplan(
            id=plan_id,
            building=building,
            floor=floor,
            filename=f"{plan_id}{extension}",
            content_type=content_type,
            size_bytes=len(content),
            uploaded_at=_now(),
            origin_x_m=origin_x_m,
            origin_y_m=origin_y_m,
            metres_per_pixel=metres_per_pixel,
            note=note,
        )

        self._save_file(plan.filename, content)
        plans = self._read()
        plans.append(plan)
        self._write(plans)
        return plan

    def calibrate(
        self,
        floorplan_id: str,
        *,
        origin_x_m: float | None = None,
        origin_y_m: float | None = None,
        metres_per_pixel: float | None = None,
        note: str | None = None,
    ) -> Floorplan | None:
        """Set or correct where the map sits on an image already uploaded."""
        if metres_per_pixel is not None and metres_per_pixel <= 0:
            raise FloorplanStoreError("metres_per_pixel must be greater than zero.")

        plans = self._read()
        for index, plan in enumerate(plans):
            if plan.id != floorplan_id:
                continue
            updated = replace(
                plan,
                origin_x_m=plan.origin_x_m if origin_x_m is None else origin_x_m,
                origin_y_m=plan.origin_y_m if origin_y_m is None else origin_y_m,
                metres_per_pixel=(
                    plan.metres_per_pixel
                    if metres_per_pixel is None
                    else metres_per_pixel
                ),
                note=plan.note if note is None else note,
            )
            plans[index] = updated
            self._write(plans)
            return updated
        return None

    def delete(self, floorplan_id: str) -> bool:
        plans = self._read()
        remaining = [plan for plan in plans if plan.id != floorplan_id]
        if len(remaining) == len(plans):
            return False

        gone = next(plan for plan in plans if plan.id == floorplan_id)
        self._write(remaining)
        self._blobs.delete(gone.filename)
        return True

    # -- storage -------------------------------------------------------------

    def _save_file(self, filename: str, content: bytes) -> None:
        self._blobs.write(filename, content)

    def _read(self) -> list[Floorplan]:
        raw_bytes = self._blobs.read(self.INDEX_KEY)
        if raw_bytes is None:
            return []

        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise FloorplanStoreError(
                f"{self.INDEX_KEY} is not valid JSON: {error.msg} "
                f"(line {error.lineno}, column {error.colno})."
            ) from error

        if not isinstance(raw, list):
            raise FloorplanStoreError(
                f"{self.INDEX_KEY}: expected a list, got {type(raw).__name__}."
            )
        return [self._parse(entry, index) for index, entry in enumerate(raw)]

    def _write(self, plans: list[Floorplan]) -> None:
        text = json.dumps([asdict(plan) for plan in plans], indent=2) + "\n"
        self._blobs.write(self.INDEX_KEY, text.encode("utf-8"))

    @staticmethod
    def _parse(entry: Any, index: int) -> Floorplan:
        if not isinstance(entry, dict):
            raise FloorplanStoreError(
                f"floorplans[{index}]: expected an object, got {type(entry).__name__}."
            )
        try:
            return Floorplan(
                id=entry["id"],
                building=entry["building"],
                floor=entry["floor"],
                filename=entry["filename"],
                content_type=entry["content_type"],
                size_bytes=entry.get("size_bytes", 0),
                uploaded_at=entry["uploaded_at"],
                origin_x_m=entry.get("origin_x_m"),
                origin_y_m=entry.get("origin_y_m"),
                metres_per_pixel=entry.get("metres_per_pixel"),
                note=entry.get("note", ""),
            )
        except KeyError as error:
            raise FloorplanStoreError(
                f"floorplans[{index}]: missing required field {error.args[0]!r}."
            ) from error

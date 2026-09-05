"""Upload the surveyed photos in bulk, instead of one at a time by hand.

    .venv/bin/python scripts/upload_photos.py ~/Downloads/Images
    .venv/bin/python scripts/upload_photos.py ~/Downloads/Images --write

The photo walk produced a folder per place, named the way the node map names
it, so the folder name is the only label needed: ``Images/B5/Hive-B5-G`` is
every photo taken standing at ``Hive_B5_G``. Uploading those through the admin
form is a hundred and twenty-five separate page loads.

**Which way each photo faces is left blank, deliberately.** The filenames
carry a number and a timestamp and nothing about direction, and
``PhotoStore.best_of`` already prefers a photo facing the way you are walking
and falls back to an undirected one rather than showing a picture looking back
the way you came. So an undirected photo is useful immediately and can be
sharpened later by anything that can tell where a camera was pointed; a
*guessed* direction could not be, because nothing downstream would know it was
a guess.

Re-running is safe: a place that already has as many photos as its folder does
is left alone, so this can be run again after another walk without uploading
the first batch twice.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import import_survey  # noqa: E402
from shortcut.dotenv import load_dotenv  # noqa: E402
from shortcut.photo_store import ALLOWED_CONTENT_TYPES, PhotoStore  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHOTOS = PROJECT_ROOT / "data" / "photos"


def node_id_for_folder(folder: Path, places: dict[str, dict]) -> str | None:
    """``Hive-B5-G`` -> ``Hive_B5_G``. The walk named its folders after places.

    Matched without regard to case. The walk produced ``SS-canteen-A`` where
    the map says ``SS-Canteen-A``, and losing seven photographs of a canteen
    to a capital letter would be a silly way to lose them.
    """
    wanted = folder.name.replace("-", "_").casefold()
    return next(
        (node_id for node_id in places if node_id.casefold() == wanted), None
    )


def images_in(folder: Path) -> list[Path]:
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file()
        and (mimetypes.guess_type(path.name)[0] or "") in ALLOWED_CONTENT_TYPES
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", type=Path, help="the folder the photo walk produced")
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    bucket = os.environ.get("SHORTCUT_S3_BUCKET")
    print(f"uploading to {'s3://' + bucket if bucket else 'local disk, ' + str(PHOTOS)}")

    survey = import_survey.load_survey()
    places = {node["id"]: node for node in survey["nodes"]}

    folders = sorted(
        path
        for path in arguments.images.expanduser().rglob("*")
        if path.is_dir() and images_in(path)
    )

    store = PhotoStore(PHOTOS)
    counts: dict[str, int] = defaultdict(int)
    for photo in store.all():
        counts[photo.target_id] += 1

    uploaded = skipped = 0
    unknown: list[str] = []

    for folder in folders:
        node_id = node_id_for_folder(folder, places)
        files = images_in(folder)

        place = places.get(node_id) if node_id else None
        if place is None:
            # A folder naming a place the survey does not have. Reported
            # rather than dropped: it usually means the survey is behind the
            # walk, which is worth knowing and is not this script's to fix.
            unknown.append(f"{folder.name} ({len(files)} photos)")
            continue

        if counts[node_id] >= len(files):
            skipped += len(files)
            continue

        print(f"  {node_id:<22} {len(files)} photos  ({place['name']})")
        if not arguments.write:
            uploaded += len(files)
            continue

        for path in files:
            store.add(
                content=path.read_bytes(),
                content_type=mimetypes.guess_type(path.name)[0] or "image/jpeg",
                target_kind="node",
                target_id=node_id,
                building=place["building"],
                floor=place["floor"],
                location=place["name"],
                # Left blank on purpose - see this file's docstring.
                facing=None,
                caption="",
            )
            uploaded += 1

    print()
    print(f"{uploaded} photos {'uploaded' if arguments.write else 'to upload'}")
    if skipped:
        print(f"{skipped} already there, left alone")

    if unknown:
        print(f"\n{len(unknown)} folder(s) name a place the survey does not have:")
        for name in unknown:
            print(f"  {name}")

    missing = sorted(
        node_id
        for node_id in places
        if not counts[node_id]
        and node_id not in {node_id_for_folder(folder, places) for folder in folders}
    )
    if missing:
        print(f"\n{len(missing)} place(s) in the survey have no photos at all:")
        for node_id in missing:
            print(f"  {node_id} ({places[node_id]['name']})")

    if not arguments.write:
        print("\nre-run with --write to upload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

Re-running is safe, and it is careful about what is already up there. A file
is matched to what the store holds by its exact byte size, not by counting
photos per place, because the two are not the same job: somebody uploading by
hand picks the best few of a folder and labels which way each one faces, and
counting would either re-upload their whole folder as duplicates or skip the
ones they left out. Matching by content adds only what is genuinely missing
and never touches a photo somebody has already labelled.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
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

#: The last commit before the survey was re-lettered. The folders were named
#: against the map as it stood then, so that is the map their names mean.
SURVEY_AT_THE_WALK = "94d8b4a^:data/campus_graph.json"


def survey_at_the_walk() -> dict[str, tuple[str, str, str]]:
    """Every place the survey held when the photo walk happened."""
    shown = subprocess.run(
        ["git", "show", SURVEY_AT_THE_WALK],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        node["id"]: (node["building"], node["floor"], node["name"])
        for node in json.loads(shown)["nodes"]
    }


#: Places whose *name* changed in the re-survey as well as their id, so the
#: rule below cannot find them. Each of these is taken from where a teammate
#: filed those same photographs by hand, matched to the file byte for byte -
#: not from reading the map and deciding what looks likely.
RENAMED = {
    "Hive_B4_E": "Hive_B4_G",  # "Side Entrance" was split into 1 and 2
    "Hive_B4_B": "Hive_B4_A",  # "Lift" folded into "Lift Lobby"
    "Hive_B5_B": "Hive_B5_A",  # the same, one floor up
    "Hive_B4_F": "Hive_B4_F",  # "Back Entrance" renamed, id unchanged
}


def folder_to_place(survey_at_the_walk: dict[str, tuple[str, str, str]],
                    places: dict[str, dict]) -> dict[str, str]:
    """Which place each photo folder belongs to, by name rather than by id.

    The folders were named on the walk, and the survey was re-lettered
    afterwards: ``Hive-B5-A`` was Staircase 1 then and is the Lift Lobby now.
    Taking the folder name as an id therefore files every photograph of that
    floor against the wrong place - silently, since both ids exist.

    So the folder's id is read against the survey *as it stood when the walk
    happened*, turned into the name of the place it meant, and matched to
    whatever carries that name today. Checked against the forty-three photos a
    teammate had already filed by hand: forty agree, and the three that do not
    are the places whose names changed too, listed in RENAMED above.
    """
    by_name = {
        (node["building"], node["floor"], node["name"]): node_id
        for node_id, node in places.items()
    }
    mapping = {}
    for old_id, key in survey_at_the_walk.items():
        today = by_name.get(key)
        if today:
            mapping[old_id] = today
    mapping.update(RENAMED)
    return mapping


def node_id_for_folder(
    folder: Path, places: dict[str, dict], relettered: dict[str, str]
) -> str | None:
    """``Hive-B5-G`` -> the place it was named after, whatever it is called now.

    Matched without regard to case: the walk produced ``SS-canteen-A`` where
    the map says ``SS-Canteen-A``, and losing seven photographs of a canteen to
    a capital letter would be a silly way to lose them.
    """
    wanted = folder.name.replace("-", "_")
    for old_id, new_id in relettered.items():
        if old_id.casefold() == wanted.casefold():
            return new_id
    return next(
        (node_id for node_id in places if node_id.casefold() == wanted.casefold()), None
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
    relettered = folder_to_place(survey_at_the_walk(), places)
    moved = {old: new for old, new in relettered.items() if old != new}
    if moved:
        print(f"{len(moved)} folder(s) were named before the survey was re-lettered:")
        for old, new in sorted(moved.items()):
            print(f"  {old:<14} is now {new:<14} ({places[new]['name']})")
        print()

    folders = sorted(
        path
        for path in arguments.images.expanduser().rglob("*")
        if path.is_dir() and images_in(path)
    )

    store = PhotoStore(PHOTOS)
    counts: dict[str, int] = defaultdict(int)
    # (place, exact byte size) of every photo already stored. Two different
    # photographs agreeing to the byte is not something worth worrying about;
    # re-uploading a hundred that are already there is.
    stored: set[tuple[str, int]] = set()
    for photo in store.all():
        counts[photo.target_id] += 1
        stored.add((photo.target_id, photo.size_bytes))

    uploaded = skipped = 0
    unknown: list[str] = []

    for folder in folders:
        node_id = node_id_for_folder(folder, places, relettered)
        files = images_in(folder)

        place = places.get(node_id) if node_id else None
        if place is None:
            # A folder naming a place the survey does not have. Reported
            # rather than dropped: it usually means the survey is behind the
            # walk, which is worth knowing and is not this script's to fix.
            unknown.append(f"{folder.name} ({len(files)} photos)")
            continue

        wanted = [
            path for path in files if (node_id, path.stat().st_size) not in stored
        ]
        skipped += len(files) - len(wanted)
        if not wanted:
            continue

        already = f", {len(files) - len(wanted)} already there" if len(wanted) != len(files) else ""
        print(f"  {node_id:<22} {len(wanted)} photos  ({place['name']}{already})")
        if not arguments.write:
            uploaded += len(wanted)
            continue

        for path in wanted:
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
        and node_id not in {
            node_id_for_folder(folder, places, relettered) for folder in folders
        }
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

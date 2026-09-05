"""Reading ``.env`` into the environment, once, when the server starts.

Hand-rolled rather than pulling in python-dotenv: it is a dozen lines, and the
core service deliberately has almost no dependencies.

Two rules, both there so that nobody is surprised:

* **A real environment variable always wins.** ``.env`` only fills in what is
  missing, so someone who sets ``SHORTCUT_S3_BUCKET`` in their shell for one
  run gets that, whatever the file says.
* **Missing is fine.** A fresh clone has no ``.env`` and must run exactly as
  before, so the absence of the file is not an error, or even a warning.
* **Blank means unset.** ``.env.example`` is full of ``NAME=`` lines waiting
  to be filled in. Setting those to an empty string is not harmless: the AWS
  library reads ``AWS_PROFILE=""`` as a profile called "" and refuses to
  start. So a blank value is skipped, exactly as if the line were absent.

This lives in the core rather than the AI package because the photo and
floorplan stores decide between local disk and S3 at startup, and that
decision has to see ``.env`` too. The AI settings read the same file through
this same function, so there is one place every setting comes from.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_dotenv"]


def load_dotenv(path: Path) -> list[str]:
    """Set every ``KEY=value`` line of ``path`` that is not already set.

    Returns the names that were actually set, which is what a log line or a
    test wants to know. Blank lines, ``#`` comments and empty values are
    skipped, and matching single or double quotes around a value are removed.
    """
    if not path.exists():
        return []

    applied: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied

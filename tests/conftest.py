"""Shared pytest setup and fixtures for the Shortcut test suite.

pytest imports this file automatically before collecting any tests, so this is
the right place to put ``src`` on the import path and to define fixtures that
more than one test file will want.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
CAMPUS_GRAPH_PATH = PROJECT_ROOT / "data" / "campus_graph.json"

# Make "import shortcut..." work without installing the package first.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from shortcut.graph_store import CampusGraph, load_graph  # noqa: E402


@pytest.fixture(autouse=True)
def stay_on_local_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every test away from S3, whatever the developer's own setup says.

    The photo and floorplan stores switch to a shared bucket when
    ``SHORTCUT_S3_BUCKET`` is set, and the server now reads ``.env`` at
    startup. Without this, a developer who keeps the bucket in their shell or
    their ``.env`` would have the suite writing test uploads into the shared
    bucket. So the variable is cleared, and the app is pointed at a ``.env``
    that does not exist.
    """
    monkeypatch.delenv("SHORTCUT_S3_BUCKET", raising=False)

    import shortcut.api as api  # noqa: PLC0415 - after sys.path is set up

    monkeypatch.setattr(api, "DOTENV_PATH", tmp_path / "no-such.env")

    # The same problem one layer down. A test can point app.state at its own
    # overrides file, but only after the lifespan has already run and built
    # the graph from the real one - so a developer who has approved anything
    # locally gets a different map under the suite than CI does, and tests
    # start passing or failing on state nobody wrote deliberately. Patching
    # the module constant moves the isolation to before startup, where it has
    # to be.
    monkeypatch.setattr(
        api, "GRAPH_OVERRIDES_PATH", tmp_path / "graph_overrides.json"
    )


@pytest.fixture(scope="session")
def graph_path() -> Path:
    """Path to the real, hand-made campus graph."""
    return CAMPUS_GRAPH_PATH


@pytest.fixture
def graph(graph_path: Path) -> CampusGraph:
    """A freshly loaded copy of the real campus graph.

    Loaded per test, so nothing one test does can leak into another.
    """
    return load_graph(graph_path)


@pytest.fixture(scope="session")
def graph_bytes_at_session_start(graph_path: Path) -> bytes:
    """The real graph file's exact content, captured once before any test runs.

    Used only to prove the file is untouched by the end of the run. Deliberately
    not a node/edge count: the graph is real survey data that keeps growing as
    more of the building is surveyed, so a hardcoded number goes stale the
    moment someone adds a node - a byte-for-byte snapshot never does.
    """
    return graph_path.read_bytes()

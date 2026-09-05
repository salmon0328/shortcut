"""Tests for reading ``.env`` at startup.

The point of the file is that a setting written there reaches the parts of
the server that decide things when they are built - the photo store's choice
between local disk and S3 above all. So as well as the loader on its own,
these tests start the app and look at what it saw.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import shortcut.api as api
from shortcut.blob_store import LocalBlobStore, S3BlobStore
from shortcut.dotenv import load_dotenv

# --------------------------------------------------------------------------
# The loader on its own
# --------------------------------------------------------------------------


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "absent.env") == []


def test_values_are_set_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOTENV_TEST_A", raising=False)
    monkeypatch.delenv("DOTENV_TEST_B", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "DOTENV_TEST_A=plain\n"
        "DOTENV_TEST_B = 'quoted value'\n"
        "not a setting\n",
        encoding="utf-8",
    )

    applied = load_dotenv(env)

    assert applied == ["DOTENV_TEST_A", "DOTENV_TEST_B"]
    assert os.environ["DOTENV_TEST_A"] == "plain"
    assert os.environ["DOTENV_TEST_B"] == "quoted value"

    monkeypatch.delenv("DOTENV_TEST_A")
    monkeypatch.delenv("DOTENV_TEST_B")


def test_a_blank_value_is_left_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AWS_PROFILE=`` copied from the example must not become a profile named ""."""
    monkeypatch.delenv("DOTENV_TEST_A", raising=False)
    env = tmp_path / ".env"
    env.write_text("DOTENV_TEST_A=\nDOTENV_TEST_B=''\n", encoding="utf-8")

    assert load_dotenv(env) == []
    assert "DOTENV_TEST_A" not in os.environ
    assert "DOTENV_TEST_B" not in os.environ


def test_the_real_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value set in the shell is deliberate, and the file must not undo it."""
    monkeypatch.setenv("DOTENV_TEST_A", "from the shell")
    env = tmp_path / ".env"
    env.write_text("DOTENV_TEST_A=from the file\n", encoding="utf-8")

    assert load_dotenv(env) == []
    assert os.environ["DOTENV_TEST_A"] == "from the shell"


# --------------------------------------------------------------------------
# Read at startup, in time to matter
# --------------------------------------------------------------------------


def test_startup_reads_the_bucket_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bucket named in .env has to reach the photo store as it is built.

    Only the choice of store is checked, never a real bucket: building an S3
    store makes no network call, and nothing here uploads.
    """
    env = tmp_path / ".env"
    env.write_text("SHORTCUT_S3_BUCKET=dotenv-test-bucket\n", encoding="utf-8")
    monkeypatch.setattr(api, "DOTENV_PATH", env)

    with TestClient(api.app):
        assert os.environ.get("SHORTCUT_S3_BUCKET") == "dotenv-test-bucket"
        store = api.app.state.photos._blobs
        assert isinstance(store, S3BlobStore)
        assert store.bucket == "dotenv-test-bucket"

    # Undo what startup set, so nothing leaks into the next test.
    monkeypatch.delenv("SHORTCUT_S3_BUCKET", raising=False)


def test_without_dotenv_photos_stay_on_disk() -> None:
    """A fresh clone has no .env, and must run exactly as it always has."""
    with TestClient(api.app):
        assert "SHORTCUT_S3_BUCKET" not in os.environ
        assert isinstance(api.app.state.photos._blobs, LocalBlobStore)
        assert isinstance(api.app.state.floorplans._blobs, LocalBlobStore)

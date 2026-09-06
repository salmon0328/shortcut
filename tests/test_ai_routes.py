"""The /ai endpoints.

Offline, like everything else here: the model is a :class:`MockLlm` handed in
through ``dependency_overrides``, never by assigning to ``app.state``. ``app``
is a module-level object and its lifespan does not reset ``ai_llm``, so a mock
left behind would go on answering for every later test in the session.

The test that matters most is
:func:`test_the_request_it_returns_is_accepted_by_route_unchanged`. Everything
else here checks a detail; that one checks the whole premise, which is that
typing a sentence and picking from the lists hand the router the same thing.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shortcut.ai.bedrock import MockLlm
from shortcut.ai.config import AiSettings
from shortcut.ai.prompts import PARSE_VERSION
from shortcut.ai.routes import get_ai_settings, get_llm
from shortcut.ai.schemas import ParsedIntent
from shortcut.api import AI_ROUTES_ENABLED, DEV_ALLOWED_ORIGINS, app


def intent(**overrides) -> ParsedIntent:
    fields = {"origin_phrase": "main entrance", "destination_phrase": "the courtyard"}
    fields.update(overrides)
    return ParsedIntent(**fields)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client whose model answers one parse call with a fixed intent."""
    app.dependency_overrides[get_llm] = lambda: MockLlm({PARSE_VERSION: intent()})
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def parse(client: TestClient, text: str = "main entrance to the courtyard", **extra):
    return client.post("/ai/parse", json={"text": text, **extra})


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_the_ai_routes_are_actually_mounted() -> None:
    """A swallowed ImportError would look exactly like nothing being wrong."""
    assert AI_ROUTES_ENABLED is True
    assert {"/ai/health", "/ai/parse"} <= set(app.openapi()["paths"])


def test_the_ai_routes_import_without_the_bedrock_client() -> None:
    """The guarded import in api.py claims this; here it is, checked.

    langchain is imported inside BedrockLlm.__init__, not at module scope, so
    the whole /ai surface loads on a machine that has none of it installed.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import shortcut.ai.routes, sys; "
            "print('langchain_aws' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        cwd="src",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def test_health_reports_which_model_and_which_mode(client: TestClient) -> None:
    """Asserted against an injected setting, never the ambient environment.

    ``load_settings`` reads a gitignored ``.env``, so a developer who has
    switched MOCK_MODE off would otherwise fail this on their machine.
    """
    app.dependency_overrides[get_ai_settings] = lambda: AiSettings(
        model_id="test-model", region="test-region", profile=None, mock_mode=True
    )

    body = client.get("/ai/health").json()

    assert body["available"] is True
    assert body["mock_mode"] is True
    assert body["model_id"] == "test-model"
    assert body["region"] == "test-region"


def test_health_says_when_it_is_only_pretending(client: TestClient) -> None:
    """A demo that silently ran on canned answers would be worse than useless."""
    app.dependency_overrides[get_ai_settings] = lambda: AiSettings(
        model_id="m", region="r", profile=None, mock_mode=True
    )

    assert "MOCK_MODE" in client.get("/ai/health").json()["detail"]


def test_health_reports_an_unreachable_model_rather_than_failing(
    client: TestClient,
) -> None:
    """Unreachable is a fact to report, not an error to raise.

    A profile that does not exist is the quickest honest way to be sure the
    probe fails without touching the network. Constructing the client alone
    would not have failed - ChatBedrockConverse accepts anything until you
    actually send it something, which is exactly why the probe checks
    credentials rather than trusting the constructor.
    """
    app.dependency_overrides[get_ai_settings] = lambda: AiSettings(
        model_id="m",
        region="ap-southeast-1",
        profile="definitely-not-a-real-profile",
        mock_mode=False,
    )

    response = client.get("/ai/health")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "credentials" in body["detail"].lower()


def test_health_costs_nothing() -> None:
    """It must not call the model: tests would need a network, and nobody
    runs a health check that shows up on the bill."""
    llm = MockLlm()
    app.dependency_overrides[get_llm] = lambda: llm
    with TestClient(app) as test_client:
        test_client.get("/ai/health")
    app.dependency_overrides.clear()

    assert llm.calls == []


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_a_sentence_becomes_a_real_route_request(client: TestClient) -> None:
    body = parse(client).json()

    assert body["needs_clarification"] is False
    assert body["request"]["origin"] == "Hive_B5_I"
    assert body["request"]["destination"] == "Hive_B5_G"


def test_the_request_it_returns_is_accepted_by_route_unchanged(
    client: TestClient,
) -> None:
    """The premise of the whole feature, in one assertion.

    Typing a sentence and picking from the lists must hand /route exactly the
    same thing, so every rule already written applies to both and there is no
    second routing path to keep in step.
    """
    parsed = parse(client).json()["request"]

    response = client.post("/route", json=parsed)

    assert response.status_code == 200


def test_what_the_controls_already_say_is_respected(client: TestClient) -> None:
    """A sentence silent about stairs must not reset the user's own toggle."""
    body = parse(
        client,
        current={
            "origin": "Hive_B5_I",
            "destination": "Hive_B5_G",
            "allow_stairs": False,
        },
    ).json()

    assert body["request"]["allow_stairs"] is False


def test_what_it_cost_comes_back_with_the_answer(client: TestClient) -> None:
    usage = parse(client).json()["usage"]

    assert usage["calls"] == 1
    assert usage["input_tokens"] > 0


# --------------------------------------------------------------------------
# Asking rather than guessing
# --------------------------------------------------------------------------


def test_an_ambiguous_place_asks_instead_of_guessing() -> None:
    """Every surveyed floor has a lift lobby, and they share one name."""
    app.dependency_overrides[get_llm] = lambda: MockLlm(
        {PARSE_VERSION: intent(destination_phrase="lift")}
    )
    with TestClient(app) as test_client:
        body = test_client.post("/ai/parse", json={"text": "take me to the lift"}).json()
    app.dependency_overrides.clear()

    assert body["request"] is None
    assert body["needs_clarification"] is True
    assert body["question"]
    # One button per floor that has one. Capping the list would leave a floor
    # nobody could choose, since the screen builds its buttons from this.
    offered = {place["floor"] for place in body["destination"]["alternatives"]}
    assert offered == {"B3", "B4", "B5"}


def test_the_places_it_offers_are_ones_you_could_have_picked() -> None:
    """The clarification screen turns these into buttons, so they must exist."""
    app.dependency_overrides[get_llm] = lambda: MockLlm(
        {PARSE_VERSION: intent(destination_phrase="lift")}
    )
    with TestClient(app) as test_client:
        body = test_client.post("/ai/parse", json={"text": "the lift"}).json()
        known = {node["id"] for node in test_client.get("/nodes").json()}
    app.dependency_overrides.clear()

    offered = {choice["node_id"] for choice in body["destination"]["alternatives"]}
    assert offered <= known


# --------------------------------------------------------------------------
# When it cannot answer
# --------------------------------------------------------------------------


def test_a_model_failure_is_a_503_and_points_at_the_lists() -> None:
    """Degrade to the pickers, which always work, and say so."""
    app.dependency_overrides[get_llm] = lambda: MockLlm()  # nothing registered
    with TestClient(app) as test_client:
        response = test_client.post("/ai/parse", json={"text": "anywhere"})
    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "lists" in response.json()["detail"]


def test_an_empty_request_is_rejected(client: TestClient) -> None:
    assert client.post("/ai/parse", json={"text": "   "}).status_code == 422


def test_an_invented_field_is_rejected(client: TestClient) -> None:
    response = client.post("/ai/parse", json={"text": "hello", "origin": "Hive_B5_A"})

    assert response.status_code == 422


# --------------------------------------------------------------------------
# From a browser
# --------------------------------------------------------------------------


def test_a_browser_page_may_call_the_ai_endpoints(client: TestClient) -> None:
    """Same app, same CORS rules as /route - worth pinning, not assuming."""
    response = client.options(
        "/ai/parse",
        headers={
            "Origin": DEV_ALLOWED_ORIGINS[0],
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_ALLOWED_ORIGINS[0]

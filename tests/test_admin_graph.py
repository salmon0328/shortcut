"""Tests for extending and editing the map from the admin screen.

The rule underneath all of these: the surveyed file is never written to. Every
change lands in the overrides file, so deleting that file returns the map to
exactly what somebody measured in the building.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app, get_photos
from shortcut.photo_store import PhotoStore

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend image data" * 4

NEW_NODE = {
    "id": "Hive_B5_J",
    "name": "Study Pods",
    "building": "Hive",
    "floor": "B5",
    "type": "junction",
}
CONNECTION = {
    "id": "Hive_B5_100",
    "from_id": "Hive_B5_J",
    "to_id": "Hive_B5_G",
    "distance_m": 12.0,
    "walk_seconds": 9.0,
    "covered": True,
}
SECOND_CONNECTION = {
    "id": "Hive_B5_101",
    "from_id": "Hive_B5_J",
    "to_id": "Hive_B5_H",
    "distance_m": 8.0,
    "walk_seconds": 6.0,
    "covered": True,
}


@pytest.fixture
def overrides_path(tmp_path: Path) -> Path:
    return tmp_path / "graph_overrides.json"


@pytest.fixture
def client(tmp_path: Path, overrides_path: Path) -> Iterator[TestClient]:
    photos = PhotoStore(tmp_path / "photos")
    app.dependency_overrides[get_photos] = lambda: photos
    with TestClient(app) as test_client:
        app.state.photos = photos
        app.state.overrides_path = overrides_path
        yield test_client
    app.dependency_overrides.clear()


def add_node(client: TestClient, **overrides):
    payload = {**NEW_NODE}
    payload.update(overrides)
    return client.post("/admin/nodes", json=payload)


def node_ids(client: TestClient) -> set[str]:
    return {node["id"] for node in client.get("/nodes").json()}


def edge_ids(client: TestClient) -> set[str]:
    return {edge["id"] for edge in client.get("/edges").json()}


# --------------------------------------------------------------------------
# Adding places
# --------------------------------------------------------------------------


def test_a_new_place_can_be_added(client: TestClient) -> None:
    response = add_node(client, connections=[CONNECTION])

    assert response.status_code == 201
    assert response.json()["created"] is True
    assert "Hive_B5_J" in node_ids(client)


def test_a_new_place_can_be_routed_to_immediately(client: TestClient) -> None:
    """Adding somewhere is pointless if the router cannot reach it."""
    add_node(client, connections=[CONNECTION])

    response = client.post(
        "/route", json={"origin": "Hive_B5_A", "destination": "Hive_B5_J"}
    )

    assert response.status_code == 200
    assert response.json()["nodes"][-1] == "Hive_B5_J"


def test_the_connecting_link_is_added_too(client: TestClient) -> None:
    response = add_node(client, connections=[CONNECTION])

    assert response.json()["edge_ids"] == ["Hive_B5_100"]
    assert "Hive_B5_100" in edge_ids(client)


def test_a_place_can_be_added_without_a_link(client: TestClient) -> None:
    """Allowed, but it will not be reachable until a link is added."""
    response = add_node(client)

    assert response.status_code == 201
    assert response.json()["edge_ids"] == []


def test_reusing_an_existing_place_id_is_refused(client: TestClient) -> None:
    response = add_node(client, id="Hive_B5_A")

    assert response.status_code == 409


def test_a_link_to_a_place_that_does_not_exist_is_refused(
    client: TestClient,
) -> None:
    response = add_node(
        client, connections=[{**CONNECTION, "to_id": "Ghost_Node"}]
    )

    assert response.status_code == 422
    assert "Ghost_Node" in response.json()["detail"]


def test_a_refused_change_leaves_nothing_behind(
    client: TestClient, overrides_path: Path
) -> None:
    """A bad edit must not sit in the overrides file breaking the next start."""
    add_node(client, connections=[{**CONNECTION, "to_id": "Ghost_Node"}])

    assert "Hive_B5_J" not in node_ids(client)
    if overrides_path.exists():
        saved = json.loads(overrides_path.read_text(encoding="utf-8"))
        assert "Hive_B5_J" not in saved.get("added_nodes", {})


def test_a_link_must_join_two_different_places(client: TestClient) -> None:
    response = add_node(
        client,
        connections=[{**CONNECTION, "from_id": "Hive_B5_G", "to_id": "Hive_B5_G"}],
    )

    assert response.status_code == 422


def test_a_place_can_be_added_with_several_links(client: TestClient) -> None:
    """A junction usually joins more than one corridor."""
    response = add_node(client, connections=[CONNECTION, SECOND_CONNECTION])

    assert response.status_code == 201
    assert set(response.json()["edge_ids"]) == {"Hive_B5_100", "Hive_B5_101"}
    assert {"Hive_B5_100", "Hive_B5_101"} <= edge_ids(client)


def test_every_link_works_after_adding_several(client: TestClient) -> None:
    add_node(client, connections=[CONNECTION, SECOND_CONNECTION])

    neighbours = {
        edge["to_id"] if edge["from_id"] == "Hive_B5_J" else edge["from_id"]
        for edge in client.get("/edges").json()
        if "Hive_B5_J" in (edge["from_id"], edge["to_id"])
    }

    assert neighbours == {"Hive_B5_G", "Hive_B5_H"}


def test_one_bad_link_refuses_the_whole_place(client: TestClient) -> None:
    """All of it or none: a place with only some of its links is worse."""
    response = add_node(
        client,
        connections=[CONNECTION, {**SECOND_CONNECTION, "to_id": "Ghost_Node"}],
    )

    assert response.status_code == 422
    assert "Hive_B5_J" not in node_ids(client)
    assert "Hive_B5_100" not in edge_ids(client)


def test_joining_the_same_pair_twice_is_refused(client: TestClient) -> None:
    response = add_node(
        client,
        connections=[CONNECTION, {**CONNECTION, "id": "Hive_B5_999"}],
    )

    assert response.status_code == 422


def test_a_link_id_is_made_from_its_ends_when_left_out(client: TestClient) -> None:
    """Two places can only be joined once, so their ids make a unique one."""
    connection = {key: value for key, value in CONNECTION.items() if key != "id"}

    response = add_node(client, connections=[connection])

    assert response.status_code == 201
    assert response.json()["edge_ids"] == ["Hive_B5_J--Hive_B5_G"]


# --------------------------------------------------------------------------
# Adding links on their own
# --------------------------------------------------------------------------


def test_a_link_between_unknown_places_is_refused(client: TestClient) -> None:
    response = client.post(
        "/admin/edges",
        json={
            "from_id": "Hive_B5_A",
            "to_id": "Ghost_Node",
            "distance_m": 10.0,
            "walk_seconds": 8.0,
        },
    )

    assert response.status_code == 404


def test_a_standalone_link_changes_routing(client: TestClient) -> None:
    """A new shortcut should be used when it really is shorter.

    Staircase 1 to the Main Entrance is the longest way round on B5 that the
    survey has no direct link for, which is what makes it a fair test of a
    shortcut. It used to use Lift Lobby to Pick Lockers, until the node-map
    import added a real link between exactly those two - at which point the
    "before" route was already direct and the shortcut had nothing to beat.
    """
    origin, destination = "Hive_B5_C", "Hive_B5_I"
    before = client.post(
        "/route", json={"origin": origin, "destination": destination}
    ).json()

    created = client.post(
        "/admin/edges",
        json={
            "from_id": origin,
            "to_id": destination,
            "distance_m": 1.0,
            "walk_seconds": 1.0,
            "covered": True,
        },
    )
    assert created.status_code == 201, created.json()

    after = client.post(
        "/route", json={"origin": origin, "destination": destination}
    ).json()
    assert after["total_walk_seconds"] < before["total_walk_seconds"]
    assert after["nodes"] == [origin, destination]


def test_a_link_can_be_added_between_two_existing_places(
    client: TestClient,
) -> None:
    response = client.post(
        "/admin/edges",
        json={
            "id": "Hive_B5_200",
            "from_id": "Hive_B5_A",
            "to_id": "Hive_B5_F",
            "distance_m": 20.0,
            "walk_seconds": 15.0,
            "covered": True,
        },
    )

    assert response.status_code == 201
    assert "Hive_B5_200" in edge_ids(client)


def test_reusing_an_existing_link_id_is_refused(client: TestClient) -> None:
    response = client.post(
        "/admin/edges",
        json={
            "id": "Hive_B5_002",
            "from_id": "Hive_B5_A",
            "to_id": "Hive_B5_F",
            "distance_m": 20.0,
            "walk_seconds": 15.0,
        },
    )

    assert response.status_code == 409


# --------------------------------------------------------------------------
# Editing what is already there
# --------------------------------------------------------------------------


def test_a_place_can_be_renamed(client: TestClient) -> None:
    response = client.patch("/admin/nodes/Hive_B5_G", json={"name": "The Courtyard"})

    assert response.status_code == 200
    names = {node["id"]: node["name"] for node in client.get("/nodes").json()}
    assert names["Hive_B5_G"] == "The Courtyard"


def test_editing_a_link_changes_routing(client: TestClient) -> None:
    """A walking time is not decoration: the router has to use the new one."""
    before = client.post(
        "/route", json={"origin": "Hive_B5_A", "destination": "Hive_B5_G"}
    ).json()
    assert "Hive_B5_002" in before["edges"]

    client.patch("/admin/edges/Hive_B5_002", json={"walk_seconds": 999})

    after = client.post(
        "/route", json={"origin": "Hive_B5_A", "destination": "Hive_B5_G"}
    ).json()
    assert after["edges"] != before["edges"], "expected a cheaper way round"


def test_editing_something_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert client.patch("/admin/nodes/Nope", json={"name": "x"}).status_code == 404
    assert client.patch("/admin/edges/Nope", json={"covered": True}).status_code == 404


def test_an_empty_edit_is_refused(client: TestClient) -> None:
    assert client.patch("/admin/nodes/Hive_B5_G", json={}).status_code == 422


def test_an_id_cannot_be_changed(client: TestClient) -> None:
    """Renaming an id would orphan every link and photo pointing at it."""
    response = client.patch("/admin/nodes/Hive_B5_G", json={"id": "Something_Else"})

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Removing additions
# --------------------------------------------------------------------------


def test_an_added_place_can_be_removed(client: TestClient) -> None:
    add_node(client, connections=[CONNECTION])

    response = client.delete("/admin/additions/node/Hive_B5_J")

    assert response.status_code == 200
    assert "Hive_B5_J" not in node_ids(client)


def test_removing_a_place_takes_its_links_with_it(client: TestClient) -> None:
    """A link to nowhere would break the next time the map is built."""
    add_node(client, connections=[CONNECTION])

    client.delete("/admin/additions/node/Hive_B5_J")

    assert "Hive_B5_100" not in edge_ids(client)


def test_removing_a_place_takes_its_photos_with_it(client: TestClient) -> None:
    add_node(client, connections=[CONNECTION])
    client.post(
        "/photos",
        files={"file": ("pods.png", PNG, "image/png")},
        data={"target_kind": "node", "target_id": "Hive_B5_J"},
    )
    assert len(client.get("/photos").json()) == 1

    client.delete("/admin/additions/node/Hive_B5_J")

    assert client.get("/photos").json() == []


def test_a_surveyed_place_cannot_be_removed(client: TestClient) -> None:
    """Deleting measured data here would put the map out of step with the
    building. Closing one is what 'blocked' is for."""
    response = client.delete("/admin/additions/node/Hive_B5_A")

    assert response.status_code == 404
    assert "Hive_B5_A" in node_ids(client)


# --------------------------------------------------------------------------
# The surveyed file
# --------------------------------------------------------------------------


def test_the_surveyed_graph_file_is_never_written_to(
    client: TestClient, graph_path: Path
) -> None:
    before = graph_path.read_text(encoding="utf-8")

    add_node(client, connections=[CONNECTION])
    client.patch("/admin/nodes/Hive_B5_G", json={"name": "The Courtyard"})
    client.patch("/admin/edges/Hive_B5_002", json={"walk_seconds": 30})

    assert graph_path.read_text(encoding="utf-8") == before


def test_deleting_the_overrides_returns_the_original_map(
    client: TestClient, overrides_path: Path
) -> None:
    """The whole point of keeping changes in their own file."""
    original_nodes = node_ids(client)
    add_node(client, connections=[CONNECTION])
    assert node_ids(client) != original_nodes

    overrides_path.unlink()
    # A fresh client reloads from the surveyed file plus what remains.
    with TestClient(app) as fresh:
        app.state.overrides_path = overrides_path
        assert node_ids(fresh) == original_nodes

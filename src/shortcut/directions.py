"""Turning one step of a route into words.

This module owns every sentence a user reads while walking. It is deliberately
the only place that decides wording, because it is the seam that changes when
Claude (via Bedrock) starts writing directions later:

* today  - :func:`describe_step` builds a sentence from the edge's own numbers
* later  - a human or an AI writes ``directions_forward`` / ``directions_reverse``
  onto the edge, and :func:`describe_step` uses that instead

Note the AI would write those fields *when an edge is added*, not while
answering a route request. Storing the text keeps route lookups fast and
offline; nothing here calls a model, the network, or AWS.
"""

from __future__ import annotations

from dataclasses import dataclass

from shortcut.graph_store import Edge, Node

__all__ = ["StepText", "describe_step"]


@dataclass(frozen=True)
class StepText:
    """The two pieces of text shown on one step card.

    ``instruction`` is the short bold line ("Take the lift to Level B4").
    ``detail`` is the longer sentence underneath it.
    """

    instruction: str
    detail: str


def _stored_text(edge: Edge, from_node: Node) -> str | None:
    """Any text already written for the direction being walked.

    ``directions_forward`` describes walking the edge as it is written in the
    graph (``from`` -> ``to``), so walking the other way needs the other field.
    """
    walking_forward = from_node.id == edge.from_id
    return edge.directions_forward if walking_forward else edge.directions_reverse


def _place(node: Node, *, with_building: bool) -> str:
    """Name a node, adding the building only when it is worth saying."""
    return f"{node.name} in {node.building}" if with_building else node.name


def _shelter_note(edge: Edge) -> str:
    return "" if edge.covered else " This stretch is not sheltered."


def describe_step(edge: Edge, from_node: Node, to_node: Node) -> StepText:
    """Describe walking ``edge``, starting at ``from_node``.

    Both endpoints are passed in rather than looked up, because the direction
    of travel decides the wording: the same corridor reads differently
    depending on which end you start from.
    """
    changing_building = from_node.building != to_node.building
    destination = _place(to_node, with_building=changing_building)

    # For a lift or a staircase the useful fact is the change of floor. The
    # node at the far end is usually called "Lift" or "Staircase 2", so naming
    # it here would only read as "take the lift ... to the lift". The next
    # step says where to walk once you are out.
    # No "up" or "down" anywhere below: floors are labels like "B4"/"B5", and
    # nothing in the graph says which way round they stack.
    if edge.lift:
        return StepText(
            instruction=f"Take the lift to Level {to_node.floor}",
            detail=(
                f"Take the lift from Level {from_node.floor} to "
                f"Level {to_node.floor}"
                + (f" in {to_node.building}." if changing_building else ".")
            ),
        )

    if edge.stairs:
        return StepText(
            instruction=f"Take the stairs to Level {to_node.floor}",
            detail=(
                f"Take the stairs from Level {from_node.floor} to "
                f"Level {to_node.floor}.{_shelter_note(edge)}"
            ),
        )

    metres = round(edge.distance_m)

    if changing_building:
        return StepText(
            instruction=f"Cross to {to_node.building}",
            detail=(
                f"From {from_node.name}, walk about {metres} m across to "
                f"{destination}.{_shelter_note(edge)}"
            ),
        )

    return StepText(
        instruction=f"Walk to {to_node.name}",
        detail=(
            f"From {from_node.name}, walk about {metres} m to "
            f"{to_node.name}.{_shelter_note(edge)}"
        ),
    )


def step_text(edge: Edge, from_node: Node, to_node: Node) -> StepText:
    """The text to show for one step: stored wording if any, else generated.

    Only ``detail`` can be replaced by stored text. The short ``instruction``
    line stays generated, so the bold heading keeps a predictable shape even
    once the descriptions underneath are written by hand or by a model.
    """
    generated = describe_step(edge, from_node, to_node)
    stored = _stored_text(edge, from_node)
    if stored is None:
        return generated
    return StepText(instruction=generated.instruction, detail=stored)

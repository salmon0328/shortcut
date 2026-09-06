"""The offline reader: rules standing in for a model.

What these pin is not "the regexes are clever". It is that the offline path
obeys the same contract as the real one - it returns phrases and never node
ids, it leaves anything unmentioned as ``None`` rather than guessing, and it
refuses work it cannot honestly do.

That contract is what lets the box degrade instead of breaking when Bedrock
is unreachable, and it is why the demo does not depend on the venue wifi.
"""

from __future__ import annotations

import pytest

from shortcut.ai.bedrock import LlmError
from shortcut.ai.offline import KeywordLlm, read_intent
from shortcut.ai.parser import build_result, parse_request
from shortcut.ai.prompts import PARSE_VERSION, PARSE_SYSTEM, parse_user_message
from shortcut.ai.schemas import ParsedIntent
from shortcut.graph_store import CampusGraph

ENTRANCE = "Hive_B5_I"
COURTYARD = "Hive_B5_G"


# --------------------------------------------------------------------------
# Finding the two places
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "main entrance to the courtyard",
        "from the main entrance to the courtyard",
        "how do I get to the courtyard from the main entrance",
        "I'm at the main entrance and I need to get to the courtyard",
        "take me to the courtyard from the main entrance",
    ],
)
def test_the_same_journey_said_five_ways(graph: CampusGraph, text: str) -> None:
    """People do not phrase this consistently, and should not have to."""
    result = build_result(graph, read_intent(text))

    assert result.request is not None, f"could not read: {text!r}"
    assert result.request.origin == ENTRANCE
    assert result.request.destination == COURTYARD


def test_a_destination_on_its_own_is_still_understood() -> None:
    """Typing one place into a box that asks where you are going means that."""
    assert read_intent("the courtyard").destination_phrase == "the courtyard"


def test_a_condition_is_not_mistaken_for_a_place() -> None:
    """'the courtyard, it's raining' names one place, not two."""
    assert read_intent("main entrance to the courtyard, it's raining") \
        .destination_phrase == "the courtyard"


def test_the_article_is_kept_because_it_carries_meaning() -> None:
    """'the lift' names one lift; bare 'lift' names one on every floor."""
    assert read_intent("main entrance to the lift").destination_phrase == "the lift"


# --------------------------------------------------------------------------
# Reading the conditions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a to b, it's raining", "sheltered"),
        ("a to b, I want to stay dry", "sheltered"),
        ("a to b, no stairs please", "prefer_lift"),
        ("a to b, I have a suitcase", "prefer_lift"),
        ("a to b, I'm on crutches", "prefer_lift"),
        ("a to b by shuttle", "least_walking"),
        ("a to b, quickest way, I'm late", "fastest"),
        ("a to b", None),
    ],
)
def test_what_they_said_becomes_a_preference(text: str, expected: str | None) -> None:
    assert read_intent(text).preference == expected


def test_refusing_stairs_is_recorded_separately_from_preferring_a_lift() -> None:
    """One is a hard requirement, the other is a leaning. Both are said here."""
    intent = read_intent("a to b, I can't use stairs")

    assert intent.avoid_stairs is True
    assert intent.preference == "prefer_lift"


def test_a_wish_to_stay_dry_is_not_a_requirement() -> None:
    """'it's raining' asks for cover; 'I can't get wet' insists on it."""
    assert read_intent("a to b, it's raining").wants_shelter is None
    assert read_intent("a to b, I can't get wet").wants_shelter is True


@pytest.mark.parametrize(
    "text",
    [
        "a to b, must be sheltered",
        "a to b, it needs to be covered",
        "a to b, has to be indoors",
        "a to b, sheltered corridors only",
        "a to b, I need to stay under cover",
    ],
)
def test_the_ways_people_insist_on_shelter(text: str) -> None:
    """All of these are requirements, and reading one as a mere preference
    is the difference between ruling a wet route out and merely pricing it."""
    assert read_intent(text).wants_shelter is True


def test_insisting_on_shelter_and_refusing_stairs_keeps_both() -> None:
    """Only one of them can be *the* preference, so the other has to be a filter.

    Shelter becomes the hard requirement and the lift becomes what the search
    optimises for, which is how both survive a single-valued preference field.
    """
    intent = read_intent("a to b, must be sheltered and no stairs")

    assert intent.wants_shelter is True
    assert intent.avoid_stairs is True


def test_a_time_budget_is_picked_up() -> None:
    assert read_intent("a to b, I have 10 minutes").max_minutes == 10


def test_vague_urgency_is_not_a_time_budget() -> None:
    """'quickly' is a preference, not a deadline, and inventing one would lie."""
    assert read_intent("a to b, quickly").max_minutes is None


def test_anything_unmentioned_stays_unmentioned() -> None:
    """The whole point of three-valued fields: silence is not a 'no'."""
    intent = read_intent("main entrance to the courtyard")

    assert intent.avoid_stairs is None
    assert intent.avoid_lift is None
    assert intent.avoid_shuttle is None
    assert intent.wants_shelter is None
    assert intent.preference is None


# --------------------------------------------------------------------------
# The contract it shares with the real model
# --------------------------------------------------------------------------


def test_it_returns_words_never_node_ids() -> None:
    """The guarantee the whole parser design rests on."""
    intent = read_intent("main entrance to the courtyard")

    assert "Hive_" not in (intent.origin_phrase or "")
    assert "Hive_" not in intent.destination_phrase


def test_it_plugs_into_the_parser_like_any_other_model(
    graph: CampusGraph,
) -> None:
    result = parse_request(
        graph, KeywordLlm(), "main entrance to the courtyard, it's raining"
    )

    assert result.request.destination == COURTYARD
    assert result.request.preference == "sheltered"


def test_it_reports_what_it_cost(graph: CampusGraph) -> None:
    """Nothing, which is the point, but the field still has to be there."""
    usage = parse_request(graph, KeywordLlm(), "a to b").usage

    assert usage.calls == 1
    assert usage.total_tokens == 0


def test_it_refuses_to_read_photographs() -> None:
    """A wrong fact about a corridor is worse than an admitted gap."""
    with pytest.raises(LlmError, match="real model"):
        KeywordLlm().vision(
            ParsedIntent, purpose="p", system="s", user="u", images=[]
        )


def test_it_refuses_work_it_was_not_built_for() -> None:
    class SomethingElse(ParsedIntent):
        pass

    with pytest.raises(LlmError, match="does not handle SomethingElse"):
        KeywordLlm().structured(
            SomethingElse, purpose=PARSE_VERSION, system=PARSE_SYSTEM, user="u"
        )


def test_it_reads_the_text_out_of_the_prompt_the_parser_sends() -> None:
    """Coupled to parse_user_message, so pin it rather than hope."""
    intent, _ = KeywordLlm().structured(
        ParsedIntent,
        purpose=PARSE_VERSION,
        system=PARSE_SYSTEM,
        user=parse_user_message("main entrance to the courtyard"),
    )

    assert intent.destination_phrase == "the courtyard"


# --------------------------------------------------------------------------
# Phrasings that broke it once
# --------------------------------------------------------------------------
#
# Each of these was a real failure found by typing at it, kept so it stays
# fixed. Regexes rot quietly, and a reader that silently starts treating half
# a sentence as a place name looks like the map is wrong.


def test_a_destination_stated_without_the_word_to(graph: CampusGraph) -> None:
    """'I need the courtyard' is as common as 'I need to get to it'."""
    result = build_result(
        graph, read_intent("I'm at the main entrance and I need the courtyard")
    )

    assert result.request is not None
    assert result.request.destination == COURTYARD


def test_a_deadline_is_not_part_of_the_place_name(graph: CampusGraph) -> None:
    """'the courtyard in 5 minutes' is one place and one deadline."""
    intent = read_intent("I'm at the lockers, I need the courtyard in 5 minutes")
    result = build_result(graph, intent)

    assert result.request is not None
    assert result.request.destination == COURTYARD
    assert result.max_minutes == 5


def test_an_article_finds_an_alias_that_was_written_without_one(
    graph: CampusGraph,
) -> None:
    """'the main entrance' and 'main entrance' are the same door."""
    result = build_result(graph, read_intent("the main entrance to the courtyard"))

    assert result.request is not None
    assert result.request.origin == ENTRANCE

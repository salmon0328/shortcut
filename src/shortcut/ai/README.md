# The agentic layer

Two agents, two extraction steps, one shared map — and this package is honest
about which is which, because the distinction is the claim the project makes.

| Component | Uses a model? | Plans, acts, adapts? | Honest label |
|---|---|---|---|
| `parser.py` | yes | **No.** Turns a sentence into a schema and decides nothing | Extraction step |
| `photo_reader.py` *(not built)* | yes | **No.** Reads images, returns facts | Extraction step |
| `ranker.py` *(not built)* | yes | **Yes.** Judges candidates, changes its own request, searches again | **Agent** |
| `verifier.py` | yes | **Yes.** Weighs evidence, prices the change, adapts its threshold, writes to the shared map | **Agent** |
| `tools/astar.py` | no | It is a tool | Tool |
| `impact.py` | no | It is a tool | Tool |

## What is built

| File | Does |
|---|---|
| `config.py` | Model id as a constant, never string-built. Every loop cap lives here, in code, where a prompt cannot argue with it |
| `bedrock.py` | `LlmPort` — the only interface the rest of the package sees. Plus `BedrockLlm` (real) and `MockLlm` (tests) |
| `offline.py` | `KeywordLlm`: pattern matching standing in for a model, so this works with no AWS at all |
| `schemas.py` | Every shape, all `extra="forbid"`. Choices are `Literal`, never free strings |
| `prompts.py` | Every prompt, versioned. When an accuracy number moves, the first question is whether the prompt changed |
| `places.py` | "the lift" → a node id. Aliases, typos, and ambiguity **reported rather than guessed at** |
| `parser.py` | One model call, no loop, no tools, no decisions |
| `impact.py` | What closing a place would cost: who gets stranded, how long the way round is. Arithmetic, no model |
| `verifier.py` | Rates reported problems, prices the change, decides or hands over |
| `routes.py` | `GET /ai/health`, `POST /ai/parse` |

The Verifier's own endpoints — `POST /reports/groups/{key}/verify` and
`POST /reports/verify-all` — live in `api.py` beside the manual approve and
reject, not here. Approving a report writes overrides and rebuilds the graph,
which is `api.py`'s job and nothing this package should learn how to do. The
agent decides; `_review_group` applies. So a report the agent approved is
indistinguishable afterwards from one a person approved: one way for the map
to change, not two that can drift apart.

## The Verifier, and why it escalates so much

It reads every submission about one problem and rates each (one model call),
adds the ratings up, asks `impact.py` what closing the place would actually
cost, **raises its own bar if the answer is bad enough**, then approves,
rejects, or hands the whole thing to a person.

Three rules hold it together:

**The model rates, the code decides.** Weights come back from the model; the
comparison against `AUTO_MERGE_THRESHOLD_BLOCKING` happens in Python, against
a number the model never sees. Report notes are text typed by strangers, and
a model asked to output "approve" will eventually be talked into outputting
"approve". Several tests hand it a reading arguing loudly for approval and
check the arithmetic still refuses.

**Closing something costs more than warning about it.** A wrongly closed
corridor sends somebody the long way round a building in the rain; a wrongly
flagged busy lobby costs them nothing. Hence 2.0 against 1.0.

**Escalating is a real outcome, not a failure.** Anything that would leave a
place with no way in goes to a person no matter how many people reported it.
That is the one threshold the agent cannot move, and it is what makes the rest
safe to automate.

## Two design decisions worth knowing

**The model never sees or returns a node id.** It returns the *phrases* it
found; `places.py` decides what they refer to. A model that emits ids will
eventually emit one that does not exist. This is why `ParsedIntent` has
`destination_phrase` and not `destination`.

**Nothing here is imported by `shortcut.*`.** `api.py` pulls the router in
behind a guarded import, so a machine without `requirements-ai.txt` still
serves every other endpoint and still passes the whole core suite. `routes.py`
takes the graph dependency as an argument rather than importing it, which
keeps that arrow pointing one way.

## Three-valued booleans

Every optional field in `ParsedIntent` means *"the person did not say"*, which
is different from *"the person said no"*. That is why they are `None` and not
`False`: a request that never mentions stairs must leave the user's existing
toggle alone rather than silently switching it off. `POST /ai/parse` takes a
`current` request for exactly this reason.

## Testing

Everything runs offline. Tests build a `MockLlm` and hand it in through
`app.dependency_overrides[get_llm]` — never by assigning to `app.state`, since
`app` is a module-level object whose lifespan does not reset `ai_llm`, and a
mock left behind would answer for every later test in the session.

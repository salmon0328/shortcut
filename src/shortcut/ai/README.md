# The agentic layer

Two agents, two extraction steps, one shared map — and this package is honest
about which is which, because the distinction is the claim the project makes.

| Component | Uses a model? | Plans, acts, adapts? | Honest label |
|---|---|---|---|
| `parser.py` | yes | **No.** Turns a sentence into a schema and decides nothing | Extraction step |
| `photo_reader.py` *(not built)* | yes | **No.** Reads images, returns facts | Extraction step |
| `ranker.py` *(not built)* | yes | **Yes.** Judges candidates, changes its own request, searches again | **Agent** |
| `verifier.py` *(not built)* | yes | **Yes.** Chooses checks, writes to the shared map, adapts its threshold | **Agent** |
| `tools/astar.py` | no | It is a tool | Tool |

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
| `routes.py` | `GET /ai/health`, `POST /ai/parse` |

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

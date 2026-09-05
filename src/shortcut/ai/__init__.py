"""The agentic layer: two agents, two extraction steps, one shared map.

Nothing in ``shortcut.*`` imports this package. ``shortcut.api`` pulls it in
behind a guarded import, so a machine without the AI dependencies installed
still serves every deterministic endpoint and still passes the whole core test
suite. See ``requirements-ai.txt``.

Honest labels, because the distinction is the claim this project makes:

===================  ==============================================
Component            Plans, acts, adapts?
===================  ==============================================
``parser``           No. Turns a sentence into a schema. Extraction.
``photo_reader``     No. Turns images into facts. Extraction.
``ranker``           Yes. Judges candidates, changes its own request
                     and searches again. **Agent.**
``verifier``         Yes. Chooses checks, acts on the shared map,
                     adapts its threshold to who is reporting.
                     **Agent.**
``shortcut.tools``   No. A* is a tool.
===================  ==============================================
"""

from __future__ import annotations

__all__: list[str] = []

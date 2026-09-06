# tests/

See the top-level [README](../README.md) for what this project is and how to
run it.

```bash
.venv/bin/python -m pytest
```

549 tests. Every one is offline and deterministic: nothing calls AWS, and
nothing depends on the network — `conftest.py` forces `MOCK_MODE` on and
clears any cached model client, so a developer who has switched Bedrock on for
real still gets an offline suite. Where a test needs a map the survey does not
contain — an uncovered corridor, a lift and a staircase side by side — it
builds a synthetic graph in `tmp_path` rather than relying on the survey
happening to contain one.

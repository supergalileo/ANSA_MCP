# Tests

Three layers, from cheapest to most expensive. Only the first one is safe to
run casually; the other two start ANSA and consume a licence.

| File | Starts ANSA | Duration | What it covers |
|---|---|---|---|
| `test_ledger.py` | no | <1 s | idempotency ledger, OUTCOME_UNKNOWN classification, strict booleans, output formatting (35 checks) |
| `test_mcp.py` | no | ~3 s | MCP stdio handshake, tool list, a read-only tool call |
| `test_server.py` | yes, batch | ~20 s | real meshing session: operation-id replay, entity-count delta, overwrite guards (8 assertions) |
| `manual/gui_regression.py` | yes, gui | ~25 s | every tool once, in a throwaway model |
| `manual/step_pipeline.py` | yes, gui | minutes | STEP import and meshing timing on a real CAD file |

Run from the repository root with the interpreter that has `mcp` installed:

```bash
python tests/test_ledger.py          # offline, no licence
python tests/test_mcp.py             # stdio contract, no licence
python tests/test_server.py batch    # launches and shuts down a batch instance
python tests/manual/gui_regression.py gui
python tests/manual/step_pipeline.py path/to/model.step
```

`test_ledger.py` and `test_server.py` print `N passed, M failed` and exit
non-zero on failure, so they work in CI without pytest.

## What "passing" does not mean

`test_ledger.py` and `test_mcp.py` prove the Python logic and the transport.
They do not prove that ANSA's API behaves as expected, that a mesh is any good,
or that a solver deck runs. `test_server.py` proves a handful of behaviours on
one ANSA release with one deck. Engineering validity is never asserted by any
test here.

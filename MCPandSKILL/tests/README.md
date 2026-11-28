# Tests

Layered, from cheapest to most expensive. Only the offline ones are safe to
run casually; the rest start ANSA and consume a licence.

| File | Starts ANSA | Duration | What it covers |
|---|---|---|---|
| `test_ledger.py` | no | <1 s | idempotency ledger, OUTCOME_UNKNOWN / PARTIAL / MCP_PRECONDITION classification, strict booleans, evidence formatting (47 checks) |
| `test_checks.py` | no | <1 s | snapshot/diff/verify generated-script contract: parses, ends with an expression, 3D area formula, no stray `%` (25 checks) |
| `test_mcp.py` | no | ~3 s | MCP stdio handshake, tool list, a read-only tool call |
| `test_baseline.py` | no | <1 s | executable baselines: `baselines/midsurface_540.json` (doc-derived, 7 checks) and `baselines/gemo_95.json` (measured here, 10 checks). Part/shell counts, Casting passes, hardware breakdown, PSHELL thickness read from the delivered decks, shared-output gates; `--selftest` (18 checks) proves the gates fire on drift |
| `test_server.py` | yes, batch | ~20 s | real meshing session: operation-id replay, entity-count delta, overwrite guards (8 assertions) |
| `test_gui_tools.py` | yes, gui | ~25 s | whole-window capture, gui state, module-menu switch, async job dispatch + live log |
| `manual/gui_regression.py` | yes, gui | ~25 s | every tool once, in a throwaway model |
| `manual/step_pipeline.py` | yes, gui | minutes | STEP import and meshing timing on a real CAD file |

Run from the repository root with the interpreter that has `mcp` installed:

```bash
python tests/test_ledger.py          # offline, no licence
python tests/test_mcp.py             # stdio contract, no licence
python tests/test_baseline.py        # baseline file sanity; add --selftest or an out_dir
python tests/test_baseline.py --baseline tests/baselines/gemo_95.json out/gemo_run2 --casting out/gemo_cast3
                                     # assert a finished gemo run against the measured baseline
python tests/test_server.py batch    # launches and shuts down a batch instance
python tests/test_gui_tools.py       # launches a gui instance, checks the GUI tools
python tests/manual/gui_regression.py gui
python tests/manual/step_pipeline.py path/to/model.step
```

`test_gui_tools.py` needs `ANSA_HOME` set, and it launches its own instance:
IAP accepts a single client, so it cannot share the connection the MCP server
already holds to a running session.

`test_ledger.py` and `test_server.py` print `N passed, M failed` and exit
non-zero on failure, so they work in CI without pytest.

## Measured baselines

`baselines/gemo_95.json` is measured on this machine: 95 real CATParts under
`gemo/`, ANSA 24.1.2, two workers, then a Casting second pass over the 12
Skin failures. Part classification is stable run to run (two runs: 95/95 same
status), shell-element counts drift about 0.8% between runs, so that assertion
allows 2%. `out/gemo_run2` plus `out/gemo_cast3` is the reference run; the file's
`known_issues` lists what the 38-part "hardware" bucket is really made of.

`baselines/midsurface_540.json` stays doc-derived (`reproduced_locally: false`)
because the 540-part source folder is not on this machine — its numbers are
copied from README/field-notes and the file says so.

## What "passing" does not mean

`test_ledger.py` and `test_mcp.py` prove the Python logic and the transport.
They do not prove that ANSA's API behaves as expected, that a mesh is any good,
or that a solver deck runs. `test_server.py` proves a handful of behaviours on
one ANSA release with one deck. Engineering validity is never asserted by any
test here.

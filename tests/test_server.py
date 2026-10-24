"""Direct smoke test of the MCP server logic (no MCP transport involved).

Covers the pre-existing tool surface plus the newer safety behaviour:

* an ``operation_id`` replay must not touch ANSA a second time
* a mutating call must report the entity-count delta it actually caused
* a refused overwrite must not write anything
* a non-boolean flag must be rejected instead of silently coerced

Run against a throwaway batch instance::

    python tests/test_server.py batch

The instance is launched and shut down by the script, so no licence is held
afterwards.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ansa_mcp_server as S  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def step(title: str, fn):
    print(f"\n{'=' * 70}\n## {title}\n{'=' * 70}")
    try:
        out = fn()
        print(out if isinstance(out, str) else repr(out))
        return out
    except Exception:
        print("!! EXCEPTION")
        traceback.print_exc()
        return None


def expect(title: str, actual: str | None, needle: str) -> None:
    ok = isinstance(actual, str) and needle in actual
    RESULTS.append((title, ok))
    print(f"\n>>> {'PASS' if ok else 'FAIL'}: {title}"
          + ("" if ok else f"  (expected {needle!r})"))


CREATE_NODE = (
    "deck = base.CurrentDeck()\n"
    "base.CreateEntity(deck, 'GRID', {'X1': 1.0, 'X2': 2.0, 'X3': 3.0})\n"
    "len(base.CollectEntities(deck, None, 'GRID'))\n"
)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "batch"
    step("launch", lambda: S.ansa_launch(mode=mode, wait_seconds=240))
    step("status", lambda: S.ansa_status())
    step("run_python: expression", lambda: S.ansa_run_python("1 + 41"))
    step("run_python: print + result",
         lambda: S.ansa_run_python("print('hello from ansa')\n[1,2,3]"))
    step("run_python: persistent vars",
         lambda: S.ansa_run_python("ansavars['x'] = 123\nansavars['x']"))
    step("run_python: read back", lambda: S.ansa_run_python("ansavars['x'] * 2"))
    step("run_python: error", lambda: S.ansa_run_python("1/0"))
    step("run_python: read-only deck query", lambda: S.ansa_run_python(
        "nodes = base.CollectEntities(constants.NASTRAN, None, 'NODE')\n"
        "{'n_nodes': len(nodes), 'deck': base.CurrentDeck()}"))
    step("model_info", lambda: S.ansa_model_info(detail=True))

    print("\n" + "=" * 70)
    print("## idempotency: a replayed operation must not re-run")
    print("=" * 70)
    first = S.ansa_run_python(CREATE_NODE, mutating=True)
    print(first)
    expect("mutating call reports a delta", first, "GRID +1")
    expect("mutating call reports an operation_id", first, "op `op_")

    op_id = ""
    if "op `op_" in first:
        op_id = first.split("op `op_")[1].split("`")[0]

    replay = S.ansa_run_python(CREATE_NODE, mutating=True,
                               operation_id=("op_" + op_id) if op_id else "")
    print(replay)
    expect("same id replays instead of re-running", replay, "replayed from ledger")

    after = S.ansa_run_python(
        "len(base.CollectEntities(base.CurrentDeck(), None, 'GRID'))")
    print(after)
    expect("the node exists exactly once", after, "result: `1`")

    print("\n" + "=" * 70)
    print("## guards")
    print("=" * 70)
    expect("non-boolean flag is rejected",
           S.ansa_run_python("1", mutating=1), "must be true or false")
    expect("merge=false without confirm is refused",
           S.ansa_open("out/simple.nas", merge=False), "refusing to replace")

    target = "out/e2e_export.nas"
    S.ansa_export(target, "nastran", overwrite=True)
    expect("existing target needs overwrite=true",
           S.ansa_export(target, "nastran"), "already exists")
    expect("overwrite=true is accepted",
           S.ansa_export(target, "nastran", overwrite=True), "ok ·")

    step("api_search", lambda: S.ansa_api_search("create node", top_n=2))
    step("step_history", lambda: S.ansa_step_history(limit=12))
    step("shutdown", lambda: S.ansa_shutdown())

    print("\n" + "=" * 70)
    failed = [t for t, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for title in failed:
        print(f"  FAILED: {title}")
    sys.exit(0 if not failed else 1)

"""Manual regression: exercise every MCP tool in one GUI-mode session.

Builds its own tiny model, so it needs no input file. Launches ANSA, runs the
whole tool surface, then shuts down - no licence is held afterwards.

    python tests/manual/gui_regression.py gui
    python tests/manual/gui_regression.py batch

This is a manual check, not a unit test: it takes ~20 s and consumes a licence
while it runs.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import ansa_mcp_server as S  # noqa: E402

BUILD = r"""
deck = base.CurrentDeck()
base.CreateEntity(deck, 'GRID', {'X1': 0.0,  'X2': 0.0, 'X3': 0.0})
base.CreateEntity(deck, 'GRID', {'X1': 10.0, 'X2': 0.0, 'X3': 0.0})
base.CreateEntity(deck, 'GRID', {'X1': 10.0, 'X2': 10.0, 'X3': 0.0})
base.CreateEntity(deck, 'GRID', {'X1': 0.0,  'X2': 10.0, 'X3': 0.0})
base.CreateEntity(deck, 'PSHELL', {'PID': 100, 'MID1': 1, 'T': 2.0})
{'grids': len(base.CollectEntities(deck, None, 'GRID')),
 'pshell': len(base.CollectEntities(deck, None, 'PSHELL'))}
"""


def show(title: str, out) -> None:
    print(f"\n{'=' * 66}\n## {title}\n{'=' * 66}")
    print(out)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "gui"
    work = Path(tempfile.mkdtemp(prefix="ansa_reg_"))

    show("status (before launch)", S.ansa_status())
    show("launch", S.ansa_launch(mode=mode, wait_seconds=300))
    show("build a tiny model", S.ansa_run_python(BUILD, mutating=True))
    show("status", S.ansa_status())
    show("model_info(detail)", S.ansa_model_info(detail=True))
    show("list GRID", S.ansa_list_entities("GRID", limit=4, fields="X1,X2,X3"))
    show("get PSHELL 100", S.ansa_get_entity_values("PSHELL", 100))
    show("run_python: rename + read back", S.ansa_run_python(
        "deck = base.CurrentDeck()\n"
        "p = base.GetEntity(deck, 'PSHELL', 100)\n"
        "base.SetEntityCardValues(deck, p, {'Name': 'panel', 'T': 2.5})\n"
        "vals = base.GetEntityCardValues(deck, p, ['Name', 'T'])\n"
        "{str(k): str(v) for k, v in vals.items()}\n",
        mutating=True))
    show("step_history", S.ansa_step_history(limit=10))
    # batch mesh has nothing to work on without faces; call it and accept the refusal
    show("mesh (expects no faces)", S.ansa_mesh("PSHELL", "batch"))
    shot = work / "regression.png"
    show("screenshot", S.ansa_screenshot(str(shot)))
    show("export abaqus", S.ansa_export(str(work / "regression.inp"), "abaqus",
                                        overwrite=True))
    show("export again (expects refusal)", S.ansa_export(str(work / "regression.inp"),
                                                         "abaqus"))
    show("api_search", S.ansa_api_search("mesh quality check", top_n=3))
    show("shutdown", S.ansa_shutdown())

    print(f"\nartefacts: {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

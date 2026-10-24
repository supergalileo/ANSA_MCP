"""Manual pipeline probe: STEP import -> faces -> batch mesh -> shell elements.

Measures import and meshing time on a real CAD file, which is useful when
comparing machines or checking a new ANSA release. Requires a STEP/IGES file.

    python tests/manual/step_pipeline.py <path/to/model.step> [output_dir]

Consumes a licence while it runs and writes a screenshot into output_dir.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import ansa_mcp_server as S  # noqa: E402

PROBE = r"""
deck = base.CurrentDeck()
out = {}
for t in ("FACE", "CONS", "SHELL", "SOLID", "GRID", "__PROPERTIES__"):
    try:
        out[t] = len(base.CollectEntities(deck, None, t))
    except Exception:
        out[t] = "ERR"
out
"""

MESH_THREE = r"""
deck = base.CurrentDeck()
props = [p for p in base.CollectEntities(deck, None, "__PROPERTIES__")
         if p.ansa_type(deck) in ("PSHELL", "PSOLID")][:3]
before = len(base.CollectEntities(deck, None, "SHELL"))
mesh.BatchGenerator(props)
after = len(base.CollectEntities(deck, None, "SHELL"))
{"targets": [p._id for p in props], "shells_before": before, "shells_after": after}
"""


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    step = sys.argv[1]
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(
        tempfile.mkdtemp(prefix="ansa_step_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    print(S.ansa_launch(mode="gui", wait_seconds=300))
    t0 = time.time()
    print(S.ansa_open(step, timeout=2400))
    print(f"[import] {time.time() - t0:.1f}s")

    print("\n--- what landed in the database ---")
    print(S.ansa_run_python(PROBE))

    t1 = time.time()
    print("\n--- batch mesh 3 properties ---")
    print(S.ansa_run_python(MESH_THREE, timeout=1800, mutating=True))
    print(f"[mesh] {time.time() - t1:.1f}s")

    print("\n--- after meshing ---")
    print(S.ansa_run_python(PROBE))

    print("\n--- screenshot ---")
    print(S.ansa_screenshot(str(out_dir / "meshed.png")))

    print("\n--- shutdown ---")
    print(S.ansa_shutdown())
    print(f"\nartefacts: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

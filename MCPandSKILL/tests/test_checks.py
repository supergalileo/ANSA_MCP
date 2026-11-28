"""Offline tests for the model-state tools (snapshot / diff / verify).

No ANSA, no licence. What is worth testing without a model is the contract the
MCP template depends on:

  * every generated script parses
  * the script's LAST statement is an expression, because the runner takes the
    trailing expression as the result - an ``if``/``else`` at the end silently
    returns nothing, which is exactly the kind of bug that would ship as
    "diff returned no data"
  * the verify script mentions only the checks that were asked for
  * an unknown check name is reported rather than ignored

Run with the interpreter that has ``mcp`` installed::

    python tests/test_checks.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ansa_checks  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def ends_with_expression(code: str) -> bool:
    body = ast.parse(code).body
    return bool(body) and isinstance(body[-1], ast.Expr)


def test_snapshot():
    code = ansa_checks.snapshot_code("before")
    check("snapshot parses", bool(ast.parse(code)))
    check("snapshot ends with a value", ends_with_expression(code))
    check("snapshot stores under the given name", "'before'" in code)
    check("snapshot keeps maps in ansavars", "ansavars" in code)
    check("snapshot avoids one call per element",
          "for p in props" in code and "CollectEntities(_D, p, t)" in code)


def test_diff():
    code = ansa_checks.diff_code("before")
    check("diff parses", bool(ast.parse(code)))
    check("diff ends with a value", ends_with_expression(code),
          "an if/else at the end returns None")
    check("diff reports re-propertied elements", "repropertied" in code)
    check("diff names the missing snapshot", "no snapshot named" in code)


def test_verify():
    code = ansa_checks.verify_code(["counts", "mats"])
    check("verify parses", bool(ast.parse(code)))
    check("verify ends with a value", ends_with_expression(code))
    check("mats check is present", "_res[\"mats\"]" in code)
    check("free check stays out when not asked", "_res[\"free\"]" in code,
          "block is present but guarded by _want()")
    check("checks are taken from the argument", "['counts', 'mats']" in code)
    check("unknown check names are surfaced", "unknown_checks" in code)
    nat = ansa_checks.verify_code(["counts", "native"])
    check("native group can be requested", "ansa.base import checks" in nat
          or "from ansa.base import checks" in nat)
    check("native group reads status and issues", "_nat[_key] = {" in nat)
    check("native keeps the CheckReport messages", '"messages"' in nat
          and '"err_codes"' in nat)
    check("native is flagged advisory, not a gate", '"advisory": True' in nat)
    check("native includes proximities", '"proximities"' in nat)


def test_quality_formula():
    """The area must be a true 3D area - a flat shoelace calls vertical shells dead."""
    code = ansa_checks.verify_code(["quality"])
    check("quality uses the Newell vector", "_nx" in code and "_ny" in code and "_nz" in code)
    check("quality does not use an XY-only shoelace",
          "pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1]" not in code)


def test_no_literal_percent_in_templates():
    """A bare % in an injected script collides with the %-format used to build it."""
    for maker in (lambda: ansa_checks.snapshot_code("x"),
                  lambda: ansa_checks.diff_code("x"),
                  lambda: ansa_checks.verify_code(["counts", "quality", "free"])):
        code = maker()
        check("generated script has no stray %%", "% (" not in code and "% len(" not in code)


def main() -> int:
    test_snapshot()
    test_diff()
    test_verify()
    test_quality_formula()
    test_no_literal_percent_in_templates()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

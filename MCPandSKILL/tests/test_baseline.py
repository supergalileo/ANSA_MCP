"""Assert a midsurface batch against the 540-part baseline - no ANSA needed.

The expected numbers live in ``tests/baselines/midsurface_540.json`` and are
copied from the docs; that file says ``reproduced_locally: false`` because this
machine holds neither the 540-part source folder nor that delivery. So this test
has three jobs:

    python tests/test_baseline.py                       # is the baseline sane?
    python tests/test_baseline.py <out_dir> [--casting <dir>]
                                                        # assert a real run
    python tests/test_baseline.py --selftest            # prove the checks fire

``--selftest`` builds a synthetic run that matches the baseline, then breaks it
on purpose. A baseline nobody has seen fail is not a test.

Input format is the same one ``scripts/make_summary.py`` reads: every
``_progress.jsonl`` line is an event, the last ``end`` record per part wins, and
a Casting second pass overrides the row through ``_casting_progress.jsonl``.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_PSHELL = re.compile(r"^PSHELL\s+\d+\s+\d+\s+([0-9.Ee+-]+)", re.M)
_ELEMENT = re.compile(r"^(CQUAD4|CQUAD8|CTRIA3|CTRIA6|CBEAM|CBAR|CONM2)\s", re.M)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "baselines" / "midsurface_540.json"

DELIVERED = ("done", "rescued", "rescued_var")
NOT_ISSUE = DELIVERED + ("hardware",)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  <- {detail}" if detail else ""))
    return cond


# ------------------------------------------------------------------- inputs --
def load_baseline(path: Path = BASELINE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sources(out: str) -> list[str]:
    """Merged progress file first, then per-worker files (same order as make_summary)."""
    paths = [os.path.join(out, "_progress.jsonl")]
    drv = os.path.join(out, "_driver")
    if os.path.isdir(drv):
        paths += [os.path.join(drv, f) for f in sorted(os.listdir(drv))
                  if f.startswith("w") and f.endswith(".jsonl")]
    return [p for p in paths if os.path.isfile(p)]


def read_ends(paths: list[str]) -> dict[str, dict]:
    last: dict[str, dict] = {}
    for path in paths:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") == "end" and rec.get("part"):
                    last[rec["part"]] = rec
    return last


def read_casting(folder: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not folder:
        return out
    path = os.path.join(folder, "_casting_progress.jsonl")
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("part"):
                out[rec["part"]] = rec
    return out


def analyze(out: str, casting: str = "", scan_nas: bool = False) -> dict:
    """Fold a finished run into the numbers the baseline talks about."""
    ends = read_ends(_sources(out))
    cast = read_casting(casting)

    rows: dict[str, dict] = {}
    for part, rec in ends.items():
        row = {"status": rec.get("status"), "shells": rec.get("n_SHELL") or 0,
               "ansa": rec.get("ansa") or "", "nas": rec.get("nas") or "",
               "std_part": rec.get("std_part"), "repaired": rec.get("repaired"),
               "skin_faces": rec.get("skin_faces")}
        c = cast.get(part)
        if c and c.get("status"):
            # The Casting record carries `shells`, not `n_SHELL`. It carries no
            # `.ansa` at all when the rescue failed, and that first-pass archive
            # is still on disk - dropping it would under-count the guard below.
            row = {"status": c["status"], "shells": c.get("shells") or 0,
                   "ansa": c.get("ansa") or row["ansa"],
                   "nas": c.get("nas") or row["nas"],
                   "std_part": None, "repaired": None, "skin_faces": None}
        rows[part] = row

    delivered = {k: v for k, v in rows.items() if v["status"] in DELIVERED}
    # .ansa is saved for every part that was opened, delivered or not; the docs
    # count it that way ("452 .ansa for 540 parts"). .nas only exists for a
    # delivered deck, so that one is counted over `delivered`.
    ansa = collections.Counter(v["ansa"].lower() for v in rows.values() if v["ansa"])
    nas = collections.Counter(v["nas"].lower() for v in delivered.values() if v["nas"])
    rescued = sum(1 for r in cast.values() if r.get("status") in DELIVERED)

    # A 40% "hardware" bucket is only acceptable if it is not one bucket: the
    # three paths that produce it mean different things, and a change to one of
    # them must move exactly one counter.
    hardware_kind: collections.Counter = collections.Counter()
    for v in rows.values():
        if v["status"] != "hardware":
            continue
        if v.get("std_part"):
            hardware_kind["by_part_number_code"] += 1
        elif v.get("repaired") and not (v.get("skin_faces") or 0):
            hardware_kind["skin_zero_faces_after_repair"] += 1
        else:
            hardware_kind["area_below_hardware_area"] += 1

    # The delivered files themselves, not the counters the run reported. This is
    # the one number a return value cannot fake.
    pshell = elements = None
    if scan_nas:
        thickness: list[float] = []
        elements = 0
        seen = 0
        for v in delivered.values():
            path = v["nas"]
            if not path or not os.path.isfile(path):
                continue
            text = open(path, encoding="ascii", errors="replace").read()
            thickness.extend(float(x) for x in _PSHELL.findall(text))
            elements += len(_ELEMENT.findall(text))
            seen += 1
        if seen:
            pshell = {"decks": seen, "cards": len(thickness),
                      "min": min(thickness) if thickness else None,
                      "max": max(thickness) if thickness else None}

    return {
        "parts": len(rows),
        "by_status": dict(collections.Counter(v["status"] for v in rows.values())),
        "skin_done": sum(1 for v in rows.values() if v["status"] == "done"),
        "hardware": sum(1 for v in rows.values() if v["status"] == "hardware"),
        "hardware_breakdown": dict(hardware_kind),
        "delivered": len(delivered),
        "unresolved": sum(1 for v in rows.values() if v["status"] not in NOT_ISSUE),
        "unresolved_parts": sorted(k for k, v in rows.items() if v["status"] not in NOT_ISSUE),
        "shell_elements": sum(int(v["shells"] or 0) for v in delivered.values()),
        "unique_ansa": len(ansa),
        "unique_nas": len(nas),
        "shared_ansa": {p: n for p, n in ansa.items() if n > 1},
        "shared_nas": {p: n for p, n in nas.items() if n > 1},
        "casting_attempted": len(cast),
        "casting_rescued": rescued,
        "casting_failed": len(cast) - rescued,
        "pshell": pshell,
        "nas_elements": elements,
    }


# ------------------------------------------------------------------- checks --
def check_baseline(base: dict) -> None:
    """The baseline file must be internally consistent before it can judge a run."""
    print("\nbaseline file")
    part = base.get("partition", {})
    exp = base.get("expect", {})
    if base.get("reproduced_locally") is True:
        mine = base.get("measured") or {}
        check("a locally measured baseline records date / ANSA / command",
              all(mine.get(k) for k in ("date", "ansa", "command")),
              str(sorted(mine)))
        check("it names the source folder it measured",
              bool(mine.get("source")), str(mine.get("source")))
    else:
        check("marked as not reproduced locally",
              base.get("reproduced_locally") is False
              and bool(base.get("not_reproduced_note")),
              "a doc-derived baseline must say so")
    check("evidence cites its sources",
          len(base.get("evidence") or []) >= 2,
          str(base.get("evidence")))
    total = (part.get("skin_done", 0) + part.get("hardware", 0)
             + part.get("casting_rescued", 0) + part.get("unresolved", 0))
    check("classification sums to the part count",
          total == exp.get("parts"), f"{total} != {exp.get('parts')}")
    statuses = ("skin_done", "hardware", "not_sheet_metal", "skin_failed")
    if all(exp.get(k) is not None for k in statuses):
        s = sum(exp[k] for k in statuses)
        check("per-status counts also sum to the part count",
              s == exp.get("parts"), f"{s} != {exp.get('parts')}")
    breakdown = base.get("hardware_breakdown") or {}
    if breakdown:
        check("hardware breakdown sums to the hardware count",
              sum(breakdown.values()) == exp.get("hardware"),
              f'{sum(breakdown.values())} != {exp.get("hardware")}')
    check("delivered_total is skin_done + casting_rescued",
          exp.get("delivered_total") == exp.get("skin_done", 0) + exp.get("casting_rescued", 0),
          str(exp.get("delivered_total")))
    check("casting splits into rescued + failed",
          exp.get("casting_attempted") == exp.get("casting_rescued", 0) + exp.get("casting_failed", 0))
    ratio = exp.get("skin_done", 0) / max(exp.get("parts", 1), 1)
    quoted = exp.get("skin_done_ratio")
    if quoted is not None:
        check(f"skin ratio matches the recorded {quoted * 100:.1f}%",
              abs(ratio - quoted) < 0.002, f"{ratio:.4f}")
    check("unique_nas equals delivered_total",
          exp.get("unique_nas") == exp.get("delivered_total"), str(exp.get("unique_nas")))


def check_run(base: dict, got: dict) -> None:
    """Every number that must not drift, plus the silent-data-loss gates."""
    exp = base.get("expect", {})
    tol = float(base.get("tolerance", {}).get("shell_elements_pct", 0.5)) / 100.0
    print("\nparts and outcomes")
    check("part count", got["parts"] == exp["parts"], f'{got["parts"]} vs {exp["parts"]}')
    check("skin done", got["skin_done"] == exp["skin_done"],
          f'{got["skin_done"]} vs {exp["skin_done"]}')
    check("hardware / non-sheet", got["hardware"] == exp["hardware"],
          f'{got["hardware"]} vs {exp["hardware"]}')
    breakdown = base.get("hardware_breakdown") or {}
    if breakdown:
        got_kind = got.get("hardware_breakdown") or {}
        for kind, want in breakdown.items():
            check(f"hardware by {kind}", got_kind.get(kind) == want,
                  f'{got_kind.get(kind)} vs {want}')
    check("delivered total", got["delivered"] == exp["delivered_total"],
          f'{got["delivered"]} vs {exp["delivered_total"]}')
    check("unresolved", got["unresolved"] == exp["unresolved"],
          f'{got["unresolved"]} vs {exp["unresolved"]}')
    if got["unresolved"]:
        print(f"       unresolved parts: {', '.join(got['unresolved_parts'][:15])}"
              + (" ..." if len(got["unresolved_parts"]) > 15 else ""))

    print("\ncasting second pass")
    check("casting attempted", got["casting_attempted"] == exp["casting_attempted"],
          f'{got["casting_attempted"]} vs {exp["casting_attempted"]}')
    check("casting rescued", got["casting_rescued"] == exp["casting_rescued"],
          f'{got["casting_rescued"]} vs {exp["casting_rescued"]}')
    check("casting failed", got["casting_failed"] == exp["casting_failed"],
          f'{got["casting_failed"]} vs {exp["casting_failed"]}')

    print("\nshell elements")
    want = exp["shell_elements"]
    dev = abs(got["shell_elements"] - want) / max(want, 1)
    check(f"shell element sum within {tol * 100:.1f}%",
          dev <= tol, f'{got["shell_elements"]} vs {want} ({dev * 100:.2f}% off)')
    if got.get("nas_elements") is not None:
        # Counted from the delivered decks, not from what the run reported: a
        # run that prints the right number while writing empty files fails here.
        dev = abs(got["nas_elements"] - want) / max(want, 1)
        check(f"elements counted in the .nas files within {tol * 100:.1f}%",
              dev <= tol, f'{got["nas_elements"]} vs {want} ({dev * 100:.2f}% off)')

    want_pshell = base.get("pshell") or {}
    got_pshell = got.get("pshell") or {}
    if want_pshell and got_pshell:
        print("\nPSHELL cards in the delivered decks")
        eps = float(want_pshell.get("tolerance", 0.01))
        for key, name in (("min", "thickness_min"), ("max", "thickness_max")):
            want_v, got_v = want_pshell.get(name), got_pshell.get(key)
            check(f"PSHELL {key} thickness", got_v is not None and abs(got_v - want_v) <= eps,
                  f"{got_v} vs {want_v} (tolerance {eps})")
        per_deck = want_pshell.get("cards_per_deck")
        if per_deck:
            got_per = got_pshell["cards"] / max(got_pshell["decks"], 1)
            check("one PSHELL card per delivered deck",
                  abs(got_per - per_deck) < 1e-9, f"{got_per:.3f} vs {per_deck}")

    print("\nsilent data loss gates")
    check("every part writes its own .ansa",
          got["unique_ansa"] == exp["unique_ansa"] and not got["shared_ansa"],
          f'{got["unique_ansa"]} unique, shared={list(got["shared_ansa"])[:5]}'
          if got["shared_ansa"] else f'{got["unique_ansa"]} unique')
    check("every delivered part has a unique .nas",
          got["unique_nas"] == exp["unique_nas"] and not got["shared_nas"],
          f'{got["unique_nas"]} unique, shared={list(got["shared_nas"])[:5]}'
          if got["shared_nas"] else f'{got["unique_nas"]} unique')


# ----------------------------------------------------------------- selftest --
def _write_ends(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps({"event": "end", **rec}, ensure_ascii=False) + "\n")


def _fixture(root: Path) -> tuple[Path, Path]:
    """A synthetic run that reproduces the baseline exactly: 540 = 382+129+20+9.

    Shell elements are split so the delivered sum is exactly 1 742 602:
    382 done parts carry 1 700 000, the 20 Casting-rescued parts carry 42 602.
    """
    out = root / "out"
    casting = root / "casting"
    recs = []

    def shells_to(total: int, n: int) -> list[int]:
        base, rem = divmod(total, n)
        return [base + 1 if i < rem else base for i in range(n)]

    done_shells = shells_to(1_700_000, 382)
    for i, n in enumerate(done_shells):
        recs.append({"part": f"B{i:04d}_SKIN", "status": "done", "n_SHELL": n,
                     "ansa": f"C:/out/B{i:04d}_SKIN.ansa",
                     "nas": f"C:/out/B{i:04d}_SKIN.nas"})
    for i in range(129):
        recs.append({"part": f"B{i:04d}_STD", "status": "hardware",
                     "ansa": f"C:/out/B{i:04d}_STD.ansa", "nas": ""})
    for i in range(29):
        recs.append({"part": f"B{i:04d}_HARD", "status": "skin_failed",
                     "ansa": f"C:/out/B{i:04d}_HARD.ansa", "nas": ""})
    _write_ends(out / "_progress.jsonl", recs)

    cast_shells = shells_to(42_602, 20)
    cast = []
    for i, n in enumerate(cast_shells):
        cast.append({"part": f"B{i:04d}_HARD", "status": "rescued", "shells": n,
                     "ansa": f"C:/out/B{i:04d}_HARD.ansa",
                     "nas": f"C:/out/B{i:04d}_HARD_cast.nas"})
    for i in range(20, 24):
        # A failed rescue still leaves the part's .ansa on disk.
        cast.append({"part": f"B{i:04d}_HARD", "status": "casting_failed",
                     "ansa": f"C:/out/B{i:04d}_HARD.ansa"})
    casting.mkdir(parents=True, exist_ok=True)
    with (casting / "_casting_progress.jsonl").open("w", encoding="utf-8") as fh:
        for rec in cast:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out, casting


def selftest(base: dict) -> bool:
    """The checks must pass on a matching run and fail on a drifted one."""
    global PASS, FAIL
    print("\nselftest: a matching run passes")
    with tempfile.TemporaryDirectory() as tmp:
        out, casting = _fixture(Path(tmp))
        got = analyze(str(out), str(casting))
        before = FAIL
        check_run(base, got)
        if FAIL != before:
            print("  -> the fixture does not match the baseline; fix the fixture "
                  "or the baseline, not the expectations")
            return False

    print("\nselftest: a drifted run fails")
    with tempfile.TemporaryDirectory() as tmp:
        out, casting = _fixture(Path(tmp))
        # 1) two parts now write the same .nas - the silent overwrite this guard exists for
        lines = (out / "_progress.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        second["nas"] = first["nas"]
        lines[1] = json.dumps(second, ensure_ascii=False)
        (out / "_progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        got = analyze(str(out), str(casting))
        pass_before, fail_before = PASS, FAIL
        check_run(base, got)
        drift_fails = FAIL - fail_before
        # This failure is the point of the exercise: report it, do not count it.
        PASS, FAIL = pass_before, fail_before
        print(f"  -> drift produced {drift_fails} failing assertion(s)"
              if drift_fails else "  -> BUG: drift was not detected")
        if not drift_fails:
            return False
    return True


# --------------------------------------------------------------------- main --
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", nargs="?", help="batch output folder holding _progress.jsonl")
    ap.add_argument("--casting", default="", help="folder holding _casting_progress.jsonl")
    ap.add_argument("--baseline", default=str(BASELINE))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    base = load_baseline(Path(args.baseline))
    ok = True
    check_baseline(base)

    if args.selftest:
        ok = selftest(base) and ok
    elif args.out:
        if not os.path.isdir(args.out):
            print(f"\nno such output folder: {args.out}")
            return 1
        got = analyze(args.out, args.casting, scan_nas=True)
        check_run(base, got)
        if not got["parts"]:
            print(f"\nno end records under {args.out} - nothing to assert")
            return 1
        print(f"\nmeasured: {json.dumps(got['by_status'], ensure_ascii=False)}")
        if got.get("pshell"):
            p = got["pshell"]
            print(f"         .nas {p['decks']} decks, PSHELL {p['cards']} cards, "
                  f"thickness {p['min']}-{p['max']} mm")
    else:
        exp = base["expect"]
        head = ("MEASURED ON THIS MACHINE" if base.get("reproduced_locally")
                else "EXPECTED (documented, NOT reproduced on this machine)")
        print(f"\n{head}:")
        print(f"  parts {exp['parts']} = skin done {exp['skin_done']} "
              f"+ hardware {exp['hardware']} + casting rescued {exp['casting_rescued']} "
              f"+ unresolved {exp['unresolved']}")
        print(f"  delivered {exp['delivered_total']} "
              f"({exp['delivered_total'] / exp['parts'] * 100:.1f}%), "
              f"shell elements {exp['shell_elements']:,}")
        print(f"  casting {exp['casting_attempted']} -> {exp['casting_rescued']} "
              f"({exp['casting_failed']} failed); .ansa {exp['unique_ansa']} unique, "
              f".nas {exp['unique_nas']} unique")
        print("\npass an out_dir to assert a real run, or --selftest to exercise the checks.")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

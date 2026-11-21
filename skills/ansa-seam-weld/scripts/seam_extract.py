"""Extract seam lines from weld-bead tubes, one STEP per bead.

Runs INSIDE ANSA (needs the geometry kernel):

    exec(open(r"<this file>", encoding="utf-8").read(), globals())
    lines = extract(TPARAMS)

Why one file at a time: `base.Open` on a CAD file replaces the whole database,
so a bead is opened, measured, and discarded before the next one. What survives
is the JSON - two to three points per bead in the assembly coordinate system
(the STEP splitter baked the transforms in, so no re-placement is needed).

`base.CreateCurvesAtTubesCenterLine` is ANSA's own tube axis finder: it pairs
the tube's faces and returns {curve: {"diameter": d}}. The curve is then sampled
with `EvaluateCurvePoint(u)` where u is the normalised arc length, so 0.0 and
1.0 are the ends and 0.5 tells us whether the bead is straight (deviation from
the chord midpoint) - a curved bead needs more than two points when the seam
line is rebuilt.

Output per bead:
    {"file", "open", "n_faces", "curves": [{"p0", "pm", "p1", "d"}]}

A bead with `n_faces` but no curve is reported, never silently dropped: those
are the ones that need a look.
"""

import glob
import json
import math
import os
import traceback


def _dist(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def extract(params):
    folder = params.get("folder")
    out_json = params.get("out_json")
    pattern = params.get("pattern", "*.stp")
    limit = int(params.get("limit", 0) or 0)
    n_pts = max(2, int(params.get("n_pts", 15)))

    files = sorted(glob.glob(os.path.join(folder, pattern)))
    if limit:
        files = files[:limit]
    print("beads: %d in %s" % (len(files), folder))

    rows = []
    for i, path in enumerate(files, 1):
        rec = {"file": os.path.basename(path), "curves": []}
        try:
            session.New("discard")
            rec["open"] = base.Open(path)
            faces = base.CollectEntities(constants.NASTRAN, None, "FACE")
            rec["n_faces"] = len(faces)
            if faces:
                found = base.CreateCurvesAtTubesCenterLine(faces)
                if found:
                    for curve, info in found.items():
                        try:
                            p0 = base.EvaluateCurvePoint(curve, 0.0)
                            pm = base.EvaluateCurvePoint(curve, 0.5)
                            p1 = base.EvaluateCurvePoint(curve, 1.0)
                            pts = [base.EvaluateCurvePoint(curve, i / (n_pts - 1.0))
                                   for i in range(n_pts)]
                        except Exception as exc:  # noqa: BLE001
                            rec["curves"].append({"error": str(exc)})
                            continue
                        chord = _dist(p0, p1) if p0 and p1 else 0.0
                        bulge = 0.0
                        if p0 and p1 and pm and chord > 1e-6:
                            mid = [(p0[k] + p1[k]) * 0.5 for k in range(3)]
                            bulge = _dist(pm, mid)
                        rec["curves"].append({
                            "p0": p0, "pm": pm, "p1": p1, "pts": pts,
                            "d": (info or {}).get("diameter"),
                            "chord": round(chord, 4),
                            "bulge": round(bulge, 4),
                        })
        except Exception as exc:  # noqa: BLE001
            rec["error"] = str(exc)
            rec["trace"] = traceback.format_exc(limit=2)
        rows.append(rec)
        if i % 10 == 0 or i == len(files):
            print("%d/%d %-28s faces=%s curves=%s"
                  % (i, len(files), rec["file"], rec.get("n_faces"), len(rec["curves"])))

    ok = [r for r in rows if r["curves"]]
    print("extracted %d/%d beads, %d with no curve"
          % (len(ok), len(rows), len(rows) - len(ok)))
    if out_json:
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print("wrote " + out_json)

    diams = {}
    for r in ok:
        for c in r["curves"]:
            key = round(c.get("d") or 0.0, 2)
            diams[key] = diams.get(key, 0) + 1
    print("diameters: " + ", ".join("O%s x%d" % (k, v) for k, v in sorted(diams.items())))
    straight = sum(1 for r in ok for c in r["curves"] if c.get("bulge", 0) < 0.5)
    total = sum(len(r["curves"]) for r in ok)
    print("straight (bulge<0.5mm): %d/%d" % (straight, total))

    return {"rows": rows, "n_ok": len(ok), "n_total": len(rows)}


TPARAMS = {
    "folder": r"D:/proj/beads",
    "out_json": r"D:/out/weld/_seam_lines.json",
    "n_pts": 15,
}
if globals().get("_SEAM_AUTORUN", True):
    RESULT = extract(TPARAMS)

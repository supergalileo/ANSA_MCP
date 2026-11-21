"""Assembly + seam-weld connections for the class5 model, one clean pass.

Runs INSIDE ANSA. The tube geometry and the shell model never share a database,
so this is stage 2 of two:

  stage 1  seam_extract.py   bead .stp -> centerline points (JSON)
  stage 2  this file         JSON -> CURVE -> SeamLine connection -> FE

The chain uses ANSA's connection manager rather than a hand-rolled node matcher:

    base.CreateCurve                    15 sampled points -> a 3D curve
    connections.CreateConnectionLine    "SeamLine_Type", connectivity left empty
    connections.AutoSetConnectivityInConnections   find the two plates
    connections.SetWeldPosition         "Automatic" - place the weld positions
    connections.RealizeConnections      "RBE2" - weld node + one spider per plate

Three things this script exists to avoid:

* **One radius is not enough.** `search_distance` is a single number for the
  whole model, but a lap-joint bead sits ~1 mm from both plates while a flange
  seam can be 6 mm away. So the unresolved ones are swept over
  `(distance, find_exactly)` pairs - and a *bigger* radius is not uniformly
  better: at 40 mm a dozen parts are in range and `find_exactly=2` becomes
  unsatisfiable, which is exactly how 21 seams stayed empty on the first try.
* **Self-welds need find_exactly=1.** A hem or a lapped edge inside one stamping
  has only one part in range; demanding two can never succeed.
* **Realise once, at the end.** `RealizeConnections` on a connection that already
  has a FE representation adds a second one, so the whole set is realised only
  after every connection has its plates.
"""

import glob
import json
import os


def assemble(params):
    nas_dir = params["nas_dir"]
    files = sorted(glob.glob(os.path.join(nas_dir, "*.nas")))
    session.New("discard")
    for f in files:
        rc = base.InputNastran(f)
        print("  + %-26s rc=%s" % (os.path.basename(f), rc))
    counts = {
        "files": len(files),
        "ANSAPART": len(base.CollectEntities(constants.NASTRAN, None, "ANSAPART")),
        "SHELL": len(base.CollectEntities(constants.NASTRAN, None, "SHELL")),
        "GRID": len(base.CollectEntities(constants.NASTRAN, None, "GRID")),
    }
    print("assembled: %s" % counts)
    return counts


def _load_seams(path):
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    seams = []
    for r in rows:
        for c in r.get("curves") or []:
            if c.get("p0") and c.get("p1"):
                seams.append({"file": r["file"], "p0": c["p0"], "pm": c.get("pm"),
                              "p1": c["p1"], "pts": c.get("pts"), "d": c.get("d"),
                              "chord": c.get("chord"), "bulge": c.get("bulge")})
    return seams


def make_connections(params):
    seams = _load_seams(params["seams_json"])
    limit = int(params.get("limit", 0) or 0)
    if limit:
        seams = seams[:limit]
    conns, fails = [], []
    for s in seams:
        pts = [p for p in (s.get("pts") or [s["p0"], s["p1"]]) if p]
        try:
            curve = base.CreateCurve(len(pts), [p[0] for p in pts],
                                     [p[1] for p in pts], [p[2] for p in pts])
            c = connections.CreateConnectionLine("SeamLine_Type", [curve], 0, [])
        except Exception as exc:  # noqa: BLE001
            s["error"] = str(exc)
            fails.append(s)
            continue
        if c is None:
            s["error"] = "CreateConnectionLine -> None"
            fails.append(s)
            continue
        s["id"] = c._id
        s["n_pts"] = len(pts)
        conns.append((s, c))
    print("connections: %d created, %d failed" % (len(conns), len(fails)))
    return conns, fails


def _attach(conns, dist, exact):
    connections.AutoSetConnectivityInConnections(
        conns,
        search_distance=float(dist),
        find_nearest=int(exact),
        find_up_to=int(exact),
        find_exactly=int(exact),
        between_parts=False,
        clear_connectivity=True,
        output_mode="pid",
        search_faces=True,
        search_shells=True,
        search_solids=False,
        search_tubes=False,
    )
    bad = connections.SetWeldPosition(conns, "Automatic")
    bad_ids = set(b._id for b in (bad or []))
    return [c for c in conns if c._id not in bad_ids], [c for c in conns if c._id in bad_ids]


def attach_sweep(conns, steps):
    """Position every connection, widening the search only for what failed."""
    log, pending = [], list(conns)
    for dist, exact in steps:
        if not pending:
            break
        ok, pending = _attach(pending, dist, exact)
        log.append({"distance": dist, "find_exactly": exact,
                    "positioned": len(ok), "still_empty": len(pending),
                    "still_ids": [c._id for c in pending]})
        print("d=%-4s n=%-2s -> %d positioned, %d empty"
              % (dist, exact, len(ok), len(pending)))
    return log, pending


def realise(conns, fe_type):
    before = len(base.CollectEntities(constants.NASTRAN, None, "RBE2"))
    n = connections.RealizeConnections(conns, {"SeamLine_Type": fe_type})
    after = len(base.CollectEntities(constants.NASTRAN, None, "RBE2"))
    print("realized %d/%d with %s (RBE2 +%d)" % (n or 0, len(conns), fe_type, after - before))
    return n, after - before


def run(params):
    report = {}
    if params.get("assemble", True):
        report["assembled"] = assemble(params)

    conns, fails = make_connections(params)
    pairs = [c for _, c in conns]
    report["n_conn"] = len(pairs)
    report["n_fail"] = len(fails)
    report["fail"] = fails

    steps = params.get("steps", [(3.0, 2), (5.0, 2), (6.0, 1), (10.0, 1)])
    log, unresolved = attach_sweep(pairs, steps)
    report["attach"] = log
    report["unresolved"] = [c._id for c in unresolved]
    report["n_unresolved"] = len(unresolved)

    done = [c for c in pairs if c._id not in set(u._id for u in unresolved)]
    if params.get("fe_type") and done:
        n, added = realise(done, params["fe_type"])
        report["realized"] = n
        report["rbe2_added"] = added

    if params.get("xml_out"):
        try:
            rc = connections.OutputConnections(pairs, "XML", params["xml_out"])
            report["xml_rc"] = rc
            print("connection list -> %s (rc=%s)" % (params["xml_out"], rc))
        except Exception as exc:  # noqa: BLE001
            report["xml_rc"] = "err %s" % exc

    if params.get("out_json"):
        with open(params["out_json"], "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print("wrote " + params["out_json"])
    return report


TPARAMS = {
    "nas_dir": r"D:/out/parts",
    "seams_json": r"D:/out/weld/_seam_lines.json",
    "fe_type": "RBE2",
    "xml_out": r"D:/out/weld/_conns.xml",
    "out_json": r"D:/out/weld/_seam_build.json",
}
if globals().get("_SEAM_AUTORUN", True):
    RESULT = run(TPARAMS)

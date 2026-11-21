"""Convert realisied seam-weld RBE2 into CBAR elements.

ANSA's seam realisation only emits RBE2: `RealizeConnections` ignores any FE name
it does not know and falls back to the type default, so `"RBAR"` and `"CWELD"`
both produce RBE2 without a warning (see references/api-verified.md). When the
weld has to be elastic rather than rigid, the fix is a post-pass over the model.

The seam pattern this script expects, one per weld position:

    plate node (GM)  --RBE2-->  weld node (GN)

so each RBE2 has exactly two nodes and converting it to one CBAR gives a chain
plate A -- bar -- weld node -- bar -- plate B. That chain is mechanically the same
as one bar of the full length: axial stiffness of two collinear bars in series is
EA/L1 + EA/L2 = EA/L, and the intermediate node adds no stiffness of its own.

Spiders with three or more nodes are left alone on purpose - that is the bolt
pattern, and collapsing a spider to one bar would connect only one of its slaves.

Cards go in through a temporary `.nas` because ANSA cannot create CBAR or PBARL
through `base.CreateEntity`; `DIM1` is then written back on the entity because
ANSA reads `SECTION='ROD'` but drops the diameter on import.
"""

import json
import math
import os

EID0 = 800001
PID0 = 5001


def _coords(grid, deck):
    v = grid.get_entity_values(deck, ["X1", "X2", "X3"]) or {}
    return [v.get("X1", 0.0), v.get("X2", 0.0), v.get("X3", 0.0)]


def _ref(axis):
    """A vector that is not parallel to the bar, for the CBAR X direction."""
    return [1.0, 0.0, 0.0] if abs(axis[0]) < 0.85 else [0.0, 0.0, 1.0]


def patch_pbarl_dims(nas_path, dmap):
    """Put the rod diameter back: ANSA writes PBARL without DIM1.

    The diameter survives in the database (the entity answers DIM1 = 4.0) but the
    exported card ends at `PBARL 5001 1 MSCBML0 ROD`, so the solver sees a rod of
    zero size. Columns 41-48 are where DIM1 belongs on the small-field card.
    """
    lines, n = [], 0
    with open(nas_path, encoding="utf-8", errors="ignore") as fh:
        for ln in fh:
            if ln.startswith("PBARL"):
                s = ln.rstrip("\n")
                try:
                    pid = int(s[8:16])
                except ValueError:
                    pid = None
                d = dmap.get(pid)
                if d is not None and s[40:48].strip() == "":
                    s = s.ljust(40) + "%8.2f" % d
                    n += 1
                lines.append(s + "\n")
            else:
                lines.append(ln)
    with open(nas_path, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))
    return n


def collect(deck, max_nodes=2):
    """(rbe2, gn, gm) for every RBE2 that is a two-node weld link."""
    out, skipped = [], 0
    for e in base.CollectEntities(deck, None, "RBE2"):
        vals = e.get_entity_values(deck, ["GN", "No.of.Nodes"]) or {}
        if vals.get("No.of.Nodes") != max_nodes:
            skipped += 1
            continue
        grids = base.CollectEntities(deck, e, "GRID") or []
        if len(grids) != 2:
            skipped += 1
            continue
        gn = vals.get("GN")
        gn_id = gn._id if hasattr(gn, "_id") else None
        ids = [g._id for g in grids]
        if gn_id is not None and gn_id in ids:
            gm = grids[1 - ids.index(gn_id)]
        else:
            gm = grids[1]
        out.append((e, grids[ids.index(gn_id)] if gn_id in ids else grids[0], gm))
    return out, skipped


def run(params):
    deck = constants.NASTRAN
    work = params["work_dir"]
    dia = float(params.get("diameter", 4.0))
    if not os.path.isdir(work):
        os.makedirs(work)

    pairs, skipped = collect(deck, int(params.get("max_nodes", 2)))
    print("RBE2: %d two-node links, %d skipped (spiders)" % (len(pairs), skipped))

    lines = ["BEGIN BULK"]
    lines.append("PBARL,%d,1,,ROD,%.3f" % (PID0, dia))
    eid, made, seen = EID0, [], set()
    for rbe2, gn, gm in pairs:
        key = (gn._id, gm._id)
        if key in seen:
            continue
        seen.add(key)
        a = _coords(gn, deck)
        b = _coords(gm, deck)
        d = [b[i] - a[i] for i in range(3)]
        n = math.sqrt(sum(x * x for x in d)) or 1.0
        axis = [x / n for x in d]
        ref = _ref(axis)
        lines.append("CBAR,%d,%d,%d,%d,%.4f,%.4f,%.4f"
                     % (eid, PID0, gn._id, gm._id, ref[0], ref[1], ref[2]))
        made.append((eid, gn._id, gm._id, round(n, 3)))
        eid += 1

    if not made:
        print("nothing to convert")
        return {"converted": 0}

    nas = os.path.join(work, "weld_bars.nas")
    with open(nas, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines + ["ENDDATA"]) + "\n")
    rc = base.InputNastran(nas)
    print("bars imported (rc=%s): %d" % (rc, len(made)))

    # ANSA drops PBARL DIM1 on import - put the diameter back on the entity
    patched = 0
    for c in base.CollectEntities(deck, None, "CBAR"):
        pid = (c.get_entity_values(deck, ["PID"]) or {}).get("PID")
        if pid is not None and getattr(pid, "_id", None) == PID0:
            pid.set_entity_values(deck, {"DIM1": dia})
            patched += 1
    print("PBARL DIM1 written back: %.3f mm" % dia)

    report = {
        "converted": len(made),
        "skipped_spiders": skipped,
        "diameter": dia,
        "pid": PID0,
        "bars": made[:20],
        "cbar_total": len(base.CollectEntities(deck, None, "CBAR")),
    }

    if params.get("delete_rbe2", True):
        base.DeleteEntity([p[0] for p in pairs])
        report["rbe2_left"] = len(base.CollectEntities(deck, None, "RBE2"))
        print("RBE2 deleted, %d left" % report["rbe2_left"])

    base.RedrawAll()

    if params.get("export_nas"):
        path = params["export_nas"]
        base.OutputNastran(filename=path)
        fixed = patch_pbarl_dims(path, {PID0: dia})
        report["export_nas"] = path
        report["pbar_dim_patched"] = fixed
        print("exported %s, DIM1 patched on %d PBARL card(s)" % (path, fixed))

    if params.get("save_ansa"):
        report["save_ansa"] = base.SaveAs(params["save_ansa"])
        print("saved " + params["save_ansa"])

    if params.get("out_json"):
        with open(params["out_json"], "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print("wrote " + params["out_json"])
    return report


TPARAMS = {
    "work_dir": r"D:/out/weld",
    "diameter": 4.0,
    "delete_rbe2": True,
    "export_nas": r"D:/out/weld/seam_welded_bar.nas",
    "save_ansa": "",
    "out_json": r"D:/out/weld/_rbe2_to_bar.json",
}
if globals().get("_SEAM_AUTORUN", True):
    RESULT = run(TPARAMS)

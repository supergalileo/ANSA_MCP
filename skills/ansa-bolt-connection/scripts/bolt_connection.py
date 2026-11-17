# -*- coding: utf-8 -*-
"""ANSA bolt connection builder — 2 RBE2 + 1 CBAR(PBARL ROD) per bolt.

Runs INSIDE the ANSA process. Drive it through the ansa MCP server:

    exec(open(r"<skill>/scripts/bolt_connection.py", encoding="utf-8").read(), globals())
    RESULT = run(PARAMS)

Two stages, because `base.Open()` on a CATPart REPLACES the model:

    PARAMS["stage"] = "extract"   # walk bolt CATParts, cache axes -> bolt_geo.json
    PARAMS["stage"] = "build"     # current model must already be the structure
    PARAMS["stage"] = "all"       # extract, then re-load structure, then build

PARAMS keys
-----------
stage        : "extract" | "build" | "all"
bolts        : list of CATPart paths (or a directory — all *.CATPart inside)
structure    : path to the structure .nas / .ansa  (needed by "all", optional otherwise)
work_dir     : scratch dir for json/nas exchange files
grab_grow    : node grab radius = hole_radius + this      (default 5.0)
on_axis_tol  : a hole belongs to a bolt if its centre is within this of the axis (default 3.0)
layer_gap    : axial gap that starts a new plate layer (default 3.0)
merge_gap    : adjacent layers closer than this merge into one RBE2 (default 4.0)
single_side  : part-number prefixes treated as single-plate (one RBE2, no beam)
export_nas   : optional NASTRAN output path
save_ansa    : optional .ansa output path
report       : optional json path for the summary

Returns a dict: {"bolts": n, "rbe2": n, "cbar": n, "sides": [...], "warnings": [...]}
"""

import json
import math
import os

from ansa import base, constants, connections

DECK = constants.NASTRAN


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def _unit(p0, p1):
    v = [p1[i] - p0[i] for i in range(3)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v], n


def _frame(v):
    """Two unit vectors orthogonal to v, for angular coverage checks."""
    ref = [1.0, 0.0, 0.0] if abs(v[0]) < 0.85 else [0.0, 0.0, 1.0]
    e1 = [v[1] * ref[2] - v[2] * ref[1],
          v[2] * ref[0] - v[0] * ref[2],
          v[0] * ref[1] - v[1] * ref[0]]
    n = math.sqrt(sum(x * x for x in e1))
    e1 = [x / n for x in e1]
    e2 = [v[1] * e1[2] - v[2] * e1[1],
          v[2] * e1[0] - v[0] * e1[2],
          v[0] * e1[1] - v[1] * e1[0]]
    return e1, e2


def _gap_deg(centre, v, ids, pos):
    """Largest angular hole in the ring. <= 45 deg means the ring is complete
    for a 5 mm mesh; > 70 deg means nodes were missed."""
    if len(ids) < 2:
        return 360.0
    e1, e2 = _frame(v)
    ang = []
    for nid in ids:
        d = [pos[nid][k] - centre[k] for k in range(3)]
        ang.append(math.degrees(math.atan2(
            sum(d[k] * e2[k] for k in range(3)),
            sum(d[k] * e1[k] for k in range(3)))) % 360.0)
    ang.sort()
    gaps = [ang[i + 1] - ang[i] for i in range(len(ang) - 1)]
    gaps.append(360.0 - ang[-1] + ang[0])
    return max(gaps)


def _node_positions():
    return {n._id: tuple(n.position)
            for n in base.CollectEntities(DECK, None, "GRID")}


def _grab(centre, radius, v, p0, s_lo, s_hi, pos):
    """Nodes within `radius` of the axis through `centre`, axially inside
    [s_lo, s_hi] measured from the bolt origin p0."""
    out = []
    for nid, (x, y, z) in pos.items():
        w = [x - p0[0], y - p0[1], z - p0[2]]
        s = sum(w[k] * v[k] for k in range(3))
        if s < s_lo or s > s_hi:
            continue
        wc = [x - centre[0], y - centre[1], z - centre[2]]
        sc = sum(wc[k] * v[k] for k in range(3))
        rr = [wc[k] - sc * v[k] for k in range(3)]
        rho = math.sqrt(sum(t * t for t in rr))
        if rho <= radius:
            out.append(nid)
    return out


# --------------------------------------------------------------------------- #
# stage 1 — bolt axes from the solid geometry
# --------------------------------------------------------------------------- #
def _bolt_files(bolts):
    files = []
    for b in bolts:
        if os.path.isdir(b):
            files += [os.path.join(b, f) for f in sorted(os.listdir(b))
                      if f.lower().endswith(".catpart")]
        else:
            files.append(b)
    return files


def extract_axes(PARAMS):
    """Walk every bolt CATPart, take the longest tube-centre-line as the shank
    axis, and its diameter as the bolt diameter. Writes bolt_geo.json."""
    work = PARAMS["work_dir"]
    os.makedirs(work, exist_ok=True)
    out = []
    for f in _bolt_files(PARAMS["bolts"]):
        base.Open(f)                                    # replaces the model
        faces = base.CollectEntities(DECK, None, "FACE")
        if not faces:
            continue
        crv = base.CreateCurvesAtTubesCenterLine(faces)
        best = None
        for c, info in (crv or {}).items():
            try:
                L = base.GetCurveLength(c)
            except Exception:
                L = 0.0
            if best is None or L > best[0]:
                best = (L, c, info)
        if not best:
            continue
        L, c, info = best
        p0 = base.EvaluateCurvePoint(c, 0.0)
        p1 = base.EvaluateCurvePoint(c, 1.0)
        out.append({"file": os.path.basename(f),
                    "L": round(L, 2),
                    "d": round(info.get("diameter", 0.0), 2),
                    "p0": [round(x, 3) for x in p0],
                    "p1": [round(x, 3) for x in p1]})
    path = os.path.join(work, "bolt_geo.json")
    json.dump(out, open(path, "w"), indent=1)
    return {"stage": "extract", "bolts": len(out), "path": path}


# --------------------------------------------------------------------------- #
# stage 2 — build the connections on the current (structure) model
# --------------------------------------------------------------------------- #
def _scan_holes():
    """One-shot hole survey. Creates Bolt_Type entities, reads them, deletes
    them again — nothing is left behind."""
    parts = base.CollectEntities(DECK, None, "ANSAPART")
    connections.DefineConnectionHoles(
        parts, create="Bolt_Type", output_mode="properties",
        parts_proximity_for_connection_merging=0.0, maximum_length=0.0,
        exclude_single_part_connections=False, match_hole_params="any",
        search_holes=True, use_hole_shape=True, use_hole_proximity=True,
        hole_proximity_distance=100.0, hole_proximity_angle=10.0,
        hole_minimum_diameter=3.0, hole_maximum_diameter=25.0,
        tube_minimum_diameter=0.0, tube_maximum_diameter=0.0,
        search_tubes=False, tubes_feature_line_angle_limit=30.0)
    bl = base.CollectEntities(DECK, None, "Bolt_Type")
    holes = [{"pos": [round(x, 3) for x in b.position],
              "D": round(b.get_entity_values(DECK, ["D"])["D"], 2)} for b in bl]
    for b in bl:
        base.DeleteEntity(b)
    return holes


def _plan(geo, holes, PARAMS):
    """Group holes per bolt into sides; return one entry per RBE2."""
    on_axis = PARAMS.get("on_axis_tol", 3.0)
    layer_gap = PARAMS.get("layer_gap", 3.0)
    merge_gap = PARAMS.get("merge_gap", 4.0)
    grow = PARAMS.get("grab_grow", 5.0)
    singles = tuple(PARAMS.get("single_side", ()))
    pos = _node_positions()
    plan = []

    for g in geo:
        p0, p1, d = g["p0"], g["p1"], g["d"]
        v, L = _unit(p0, p1)
        cand = []
        for h in holes:
            w = [h["pos"][k] - p0[k] for k in range(3)]
            s = sum(w[k] * v[k] for k in range(3))
            if s < -5 or s > L + 5:
                continue
            rr = [w[k] - s * v[k] for k in range(3)]
            if math.sqrt(sum(t * t for t in rr)) > on_axis:
                continue                                    # not this bolt
            cand.append({"s": round(s, 2), "pos": h["pos"], "D": h["D"]})
        cand.sort(key=lambda x: x["s"])

        clusters, cur = [], []
        for c in cand:
            if cur and c["s"] - cur[-1]["s"] > layer_gap:
                clusters.append(cur)
                cur = []
            cur.append(c)
        if cur:
            clusters.append(cur)

        single = g["file"].startswith(singles) or len(clusters) <= 1
        if single:
            sides = [sum(clusters, [])] if clusters else [[]]
        else:
            gaps = [(clusters[i + 1][0]["s"] - clusters[i][-1]["s"], i)
                    for i in range(len(clusters) - 1)]
            k = max(gaps)[1]
            sides = [sum(clusters[:k + 1], []), sum(clusters[k + 1:], [])]

        items = []
        for side in sides:
            if not side:
                items.append(None)
                continue
            s_lo = min(c["s"] for c in side) - 2.5
            s_hi = max(c["s"] for c in side) + 2.5
            centre = [sum(c["pos"][k] for c in side) / len(side) for k in range(3)]
            radius = max(c["D"] for c in side) / 2.0 + grow
            ids = _grab(centre, radius, v, p0, s_lo, s_hi, pos)
            items.append({"c": [round(x, 3) for x in centre], "R": round(radius, 2),
                          "n": len(ids), "nodes": ids,
                          "gap": round(_gap_deg(centre, v, ids, pos), 1),
                          "s": [round(s_lo, 1), round(s_hi, 1)],
                          "holes": len(side)})

        # layers the hole finder missed -> fit the ring from the nodes
        covered = [it["s"] for it in items if it]
        for lay in _scan_layers(pos, p0, v, L, d):
            s_lo, s_hi = lay[0][0], lay[-1][0]
            if len(lay) < 8:
                continue
            if any(not (s_hi < a[0] or s_lo > a[1]) for a in covered):
                continue
            cx = sum(pos[i][0] for _, _, i in lay) / len(lay)
            cy = sum(pos[i][1] for _, _, i in lay) / len(lay)
            cz = sum(pos[i][2] for _, _, i in lay) / len(lay)
            w = [cx - p0[0], cy - p0[1], cz - p0[2]]
            s0 = sum(w[k] * v[k] for k in range(3))
            centre = [p0[k] + v[k] * s0 for k in range(3)]     # snap onto the axis
            rs = sorted(math.dist(pos[i], centre) for _, _, i in lay)
            radius = rs[len(rs) // 2] + grow
            ids = [i for _, _, i in lay if math.dist(pos[i], centre) <= radius]
            items.append({"c": [round(x, 3) for x in centre], "R": round(radius, 2),
                          "n": len(ids), "nodes": ids,
                          "gap": round(_gap_deg(centre, v, ids, pos), 1),
                          "s": [round(s_lo - 2, 1), round(s_hi + 2, 1)],
                          "holes": 0, "fitted": True})
            covered.append([s_lo - 2, s_hi + 2])

        # merge neighbours that belong to the same stack
        items = [it for it in items if it]
        items.sort(key=lambda x: x["s"][0])
        merged = []
        for it in items:
            if merged and it["s"][0] - merged[-1]["s"][1] < merge_gap:
                a = merged[-1]
                a["nodes"] = sorted(set(a["nodes"]) | set(it["nodes"]))
                a["s"] = [min(a["s"][0], it["s"][0]), max(a["s"][1], it["s"][1])]
                a["holes"] += it["holes"]
                a["fitted"] = a.get("fitted", False) or it.get("fitted", False)
            else:
                merged.append(dict(it))
        for it in merged:
            c = [sum(pos[k][i] for k in it["nodes"]) / len(it["nodes"]) for i in range(3)]
            w = [c[k] - p0[k] for k in range(3)]
            s0 = sum(w[k] * v[k] for k in range(3))
            c = [p0[k] + v[k] * s0 for k in range(3)]
            it["c"] = [round(x, 3) for x in c]
            it["n"] = len(it["nodes"])
            it["gap"] = round(_gap_deg(c, v, it["nodes"], pos), 1)

        plan.append({"file": g["file"], "d": d, "p0": p0, "p1": p1,
                     "v": v, "L": round(L, 1), "sides": merged})

    return plan


def _scan_layers(pos, p0, v, L, d):
    raw = []
    for nid, (x, y, z) in pos.items():
        w = [x - p0[0], y - p0[1], z - p0[2]]
        s = sum(w[k] * v[k] for k in range(3))
        if s < -4 or s > L + 4:
            continue
        rr = [w[k] - s * v[k] for k in range(3)]
        rho = math.sqrt(sum(t * t for t in rr))
        if 0.5 < rho < d * 0.5 + 12:
            raw.append((s, rho, nid))
    raw.sort()
    layers, cur = [], []
    for it in raw:
        if cur and it[0] - cur[-1][0] > 1.2:
            layers.append(cur)
            cur = []
        cur.append(it)
    if cur:
        layers.append(cur)
    return layers


def build_connections(PARAMS):
    """Build 2 RBE2 + 1 CBAR per bolt on the model that is currently open."""
    work = PARAMS["work_dir"]
    geo_path = os.path.join(work, "bolt_geo.json")
    geo = json.load(open(geo_path))

    holes = _scan_holes()
    plan = _plan(geo, holes, PARAMS)
    json.dump(plan, open(os.path.join(work, "plan.json"), "w"), indent=1)

    # centre nodes -> nas -> import  (there is no API to create a GRID)
    lines = ["BEGIN BULK"]
    nid0 = 900001
    nid = nid0
    for p in plan:
        ids = []
        for it in p["sides"]:
            lines.append("GRID,%d,,%.4f,%.4f,%.4f" % (nid, it["c"][0], it["c"][1], it["c"][2]))
            ids.append(nid)
            nid += 1
        p["gids"] = ids
    centers = os.path.join(work, "bolt_centers.nas")
    open(centers, "w").write("\n".join(lines + ["ENDDATA"]) + "\n")
    json.dump(plan, open(os.path.join(work, "plan.json"), "w"), indent=1)
    base.InputNastran(centers)

    gs = {n._id: n for n in base.CollectEntities(DECK, None, "GRID")}

    # SET + RBE2
    built = 0
    for p in plan:
        tag = os.path.splitext(p["file"])[0]
        for si, it in enumerate(p["sides"]):
            gn = gs.get(p["gids"][si])
            if gn is None:
                continue
            nodes = [gs[i] for i in it["nodes"] if i in gs]
            if len(nodes) < 4:
                continue
            s = base.CreateEntity(DECK, "SET", {"Name": "BOLT_%s_%d" % (tag, si)})
            base.AddToSet(s, nodes)
            base.CreateEntity(DECK, "RBE2", {"GN": gn, "GM NSET": s, "CM": "123456"})
            base.RemoveFromSet(s, [gn])          # Nastran forbids GN inside GM
            built += 1

    # CBAR + PBARL(ROD) — both only via nas import
    lines = ["BEGIN BULK"]
    pid = 10001
    eid = 10001
    bars = []
    dmap = {}
    for p in plan:
        if len(p["sides"]) < 2:
            continue
        a, b = p["gids"][0], p["gids"][1]
        d = round(p["d"], 2)
        v = p["v"]
        ref = [1.0, 0.0, 0.0] if abs(v[0]) < 0.85 else [0.0, 0.0, 1.0]
        lines.append("PBARL,%d,1,,ROD,%.2f" % (pid, d))
        lines.append("CBAR,%d,%d,%d,%d,%.4f,%.4f,%.4f" % (eid, pid, a, b, ref[0], ref[1], ref[2]))
        dmap[pid] = d
        bars.append((pid, eid, a, b))
        pid += 1
        eid += 1
    barfile = os.path.join(work, "bars.nas")
    open(barfile, "w").write("\n".join(lines + ["ENDDATA"]) + "\n")
    if bars:
        base.InputNastran(barfile)
        # ANSA does not read DIM1 from PBARL -> write it through the entity
        for c in base.CollectEntities(DECK, None, "CBAR"):
            pe = c.get_entity_values(DECK, ["PID"])["PID"]
            d = dmap.get(pe._id)
            if d is not None:
                pe.set_entity_values(DECK, {"DIM1": d})

    base.RedrawAll()

    result = {"stage": "build", "bolts": len(plan), "rbe2": built,
              "cbar": len(bars), "sides": [], "warnings": []}
    for p in plan:
        for si, it in enumerate(p["sides"]):
            result["sides"].append({
                "bolt": p["file"], "side": si, "nodes": it["n"],
                "gap_deg": it["gap"], "holes": it["holes"],
                "fitted": bool(it.get("fitted"))})
            if it["gap"] > 70:
                result["warnings"].append(
                    "%s side%d: angular gap %.1f deg — ring not closed"
                    % (p["file"], si, it["gap"]))
            if it.get("fitted"):
                result["warnings"].append(
                    "%s side%d: no hole found by the finder, centre fitted from nodes"
                    % (p["file"], si))
    return result


# --------------------------------------------------------------------------- #
# export helpers
# --------------------------------------------------------------------------- #
def patch_pbarl_dims(nas_path, dmap):
    """ANSA writes PBARL without DIM1. Put the diameter back into columns 41-48."""
    lines, n = [], 0
    for ln in open(nas_path, encoding="utf-8", errors="ignore"):
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
    open(nas_path, "w", encoding="utf-8").write("".join(lines))
    return n


def export(PARAMS):
    """Export NASTRAN + save .ansa, then patch the PBARL diameters back in."""
    work = PARAMS["work_dir"]
    plan = json.load(open(os.path.join(work, "plan.json")))
    dmap = {}
    pid = 10001
    for p in plan:
        if len(p["sides"]) < 2:
            continue
        dmap[pid] = round(p["d"], 2)
        pid += 1

    out = {"patch": 0}
    if PARAMS.get("export_nas"):
        base.OutputNastran(PARAMS["export_nas"])
        out["patch"] = patch_pbarl_dims(PARAMS["export_nas"], dmap)
        out["nas"] = PARAMS["export_nas"]
    if PARAMS.get("save_ansa"):
        base.SaveAs(PARAMS["save_ansa"])
        out["ansa"] = PARAMS["save_ansa"]
    return out


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run(PARAMS):
    stage = PARAMS.get("stage", "all")
    result = {}
    if stage in ("extract", "all"):
        result["extract"] = extract_axes(PARAMS)
    if stage == "all":
        if PARAMS.get("structure"):
            base.InputNastran(PARAMS["structure"])
        else:
            raise RuntimeError("stage 'all' needs PARAMS['structure'] — "
                               "the bolt CATParts replaced the model")
    if stage in ("build", "all"):
        result["build"] = build_connections(PARAMS)
        if PARAMS.get("export_nas") or PARAMS.get("save_ansa"):
            result["export"] = export(PARAMS)
    if PARAMS.get("report"):
        json.dump(result, open(PARAMS["report"], "w"), indent=1, default=str)
    print(json.dumps(result, indent=1, default=str))
    return result

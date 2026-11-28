# -*- coding: utf-8 -*-
"""ANSA bushing (CBUSH) builder - zero-length point bushing or real-length bushing.

Runs INSIDE the ANSA process. Drive it through the ansa MCP server:

    exec(open(r"<skill>/scripts/bushing_connection.py", encoding="utf-8").read(), globals())
    RESULT = run(PARAMS)

Stages, because base.Open() on a part REPLACES the model:

    "extract"  walk the rubber CATPart/STEP files, cache axes -> bushing_geo.json
    "build"    current model must already be the structure deck
    "all"      extract, re-load PARAMS["structure"], then build

PARAMS keys
-----------
stage        : "extract" | "build" | "all"                      (default "all")
rubber       : list of rubber part paths, or a directory (extract/all)
structure    : .nas of the structure (needed by "all")
work_dir     : scratch dir for json / card exchange
geo          : bushing_geo.json from an earlier extract (build only)
bushings     : explicit list, overrides/supplements geo  (see below)
mode         : "point" | "spanning"                            (default "point")
spanning_fix : "physical" | "rigid_rocking"                     (spanning only)
k_rad        : target radial (face) stiffness, N/mm            (default 1000)
E            : rubber Young's modulus, MPa                     (default 4.0)
G            : rubber shear modulus, MPa                       (default 0.6)
ring_band    : [r_lo, r_hi] override for the slave-node ring
z_tol        : axial half-band that selects a plate ring, mm   (default 8.0)
min_ring     : refuse a side with fewer ring nodes             (default 6)
max_gap      : refuse a side whose angular gap exceeds this    (default 90)
axis_tol     : two rubber parts share an axis within this, mm  (default 5.0)
angle_tol    : ... and this many degrees                       (default 5.0)
export_nas   : output deck
save_ansa    : optional .ansa output
report       : optional json path for the summary
eid_base/pid_base/gid_base : optional id bases

A bushing spec (either from geo or given by hand):

    {"name": "BUSH_A",
     "origin": [x, y, z], "dir": [0, 0, 1], "span": [s1, s2],
     "ring_band": [29.5, 34.5], "k_rad": 1000.0, "E": 4.0, "G": 0.6,
     "r_in": 19.0, "r_out": 31.5}

"span" is in mm measured along "dir" from "origin"; the plates are at those two
positions. For an axis-aligned bushing "axis": [x, y] is accepted instead of
origin/dir (dir defaults to +Z).

Returns {"bushings": n, "cbush": n, "rbe2": n, "sides": [...], "warnings": [...]}
"""

import json
import math
import os

from ansa import base, constants, session

DECK = constants.NASTRAN

DEFAULT_K_RAD = 1000.0
DEFAULT_E = 4.0
DEFAULT_G = 0.6


# --------------------------------------------------------------------------- #
# small vector helpers
# --------------------------------------------------------------------------- #
def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _add(a, b):
    return [a[i] + b[i] for i in range(3)]


def _mul(a, s):
    return [a[i] * s for i in range(3)]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    return [x / n for x in a] if n > 1e-12 else [0.0, 0.0, 1.0]


def _perp(v):
    """A unit vector perpendicular to v."""
    ref = [1.0, 0.0, 0.0] if abs(v[0]) < 0.85 else [0.0, 1.0, 0.0]
    return _unit([v[1] * ref[2] - v[2] * ref[1],
                  v[2] * ref[0] - v[0] * ref[2],
                  v[0] * ref[1] - v[1] * ref[0]])


def _project(p, o, v):
    """Axial coordinate of p on the line (o, v)."""
    return _dot(_sub(p, o), v)


def _radial(p, o, v):
    """Distance of p from the line (o, v)."""
    w = _sub(p, o)
    return _norm(_sub(w, _mul(v, _dot(w, v))))


def _nodes():
    return {n._id: tuple(n.position)
            for n in base.CollectEntities(DECK, None, "GRID")}


def _gap_deg(pts, o, v):
    """Largest angular hole in a ring of points around the axis (o, v)."""
    if len(pts) < 2:
        return 360.0
    e1 = _perp(v)
    e2 = [v[1] * e1[2] - v[2] * e1[1],
          v[2] * e1[0] - v[0] * e1[2],
          v[0] * e1[1] - v[1] * e1[0]]
    ang = []
    for p in pts:
        d = _sub(p, o)
        ang.append(math.degrees(math.atan2(_dot(d, e2), _dot(d, e1))) % 360.0)
    ang.sort()
    gaps = [ang[i + 1] - ang[i] for i in range(len(ang) - 1)]
    gaps.append(360.0 - ang[-1] + ang[0])
    return max(gaps)


# --------------------------------------------------------------------------- #
# stage 1 - axes and sizes from the rubber parts
# --------------------------------------------------------------------------- #
def _part_files(items):
    if isinstance(items, str):
        items = [items]
    files = []
    for it in items:
        if os.path.isdir(it):
            files += [os.path.join(it, f) for f in sorted(os.listdir(it))
                      if f.lower().endswith((".catpart", ".stp", ".step", ".igs",
                                             ".iges", ".model", ".sldprt"))]
        else:
            files.append(it)
    return files


def _part_geo(path):
    """Longest tube-centre-line of one rubber solid: axis, length, diameter."""
    base.Open(path)                                  # replaces the model
    faces = base.CollectEntities(DECK, None, "FACE")
    if not faces:
        return None
    crv = base.CreateCurvesAtTubesCenterLine(faces)
    if not crv:
        return None
    best = None
    for c, info in crv.items():
        try:
            L = base.GetCurveLength(c)
        except Exception:
            L = 0.0
        if best is None or L > best[0]:
            best = (L, c, info)
    if not best:
        return None
    L, c, info = best
    p0 = base.EvaluateCurvePoint(c, 0.0)
    p1 = base.EvaluateCurvePoint(c, 1.0)
    d = float(info.get("diameter", 0.0) or 0.0)
    if d <= 0.0 and L > 0.0:
        d = 0.0
    return {"file": os.path.basename(path), "L": L, "d": d,
            "p0": [float(x) for x in p0], "p1": [float(x) for x in p1]}


def _group_axes(parts, axis_tol, angle_tol):
    """Group coaxial rubber parts; return merged bushing axes."""
    groups = []
    cos_tol = math.cos(math.radians(angle_tol))
    for p in parts:
        v = _unit(_sub(p["p1"], p["p0"]))
        placed = False
        for g in groups:
            if abs(_dot(g["v"], v)) < cos_tol:
                continue
            a = _radial(p["p0"], g["o"], g["v"])
            b = _radial(p["p1"], g["o"], g["v"])
            if max(a, b) <= axis_tol:
                if _dot(g["v"], v) < 0.0:
                    v = _mul(v, -1.0)
                g["v"] = _unit(_add(g["v"], v))
                g["parts"].append(p)
                placed = True
                break
        if not placed:
            groups.append({"o": list(p["p0"]), "v": v, "parts": [p]})
    out = []
    for g in groups:
        o, v = g["o"], _unit(g["v"])
        ss = []
        ds = []
        for p in g["parts"]:
            ss += [_project(p["p0"], o, v), _project(p["p1"], o, v)]
            if p["d"] > 0.0:
                ds.append(p["d"])
        s1, s2 = min(ss), max(ss)
        out.append({"origin": [round(x, 3) for x in o],
                    "dir": [round(x, 6) for x in v],
                    "span": [round(s1, 3), round(s2, 3)],
                    "L": round(s2 - s1, 3),
                    "r_in": round(min(ds) / 2.0, 3) if ds else 0.0,
                    "r_out_est": round(max(ds) / 2.0, 3) if ds else 0.0,
                    "parts": [p["file"] for p in g["parts"]],
                    "diameters": [round(d, 2) for d in ds]})
    out.sort(key=lambda b: (round(b["origin"][0], 1), round(b["origin"][1], 1)))
    for i, b in enumerate(out):
        b["name"] = "BUSH_%d" % (i + 1)
    return out


def _work(PARAMS):
    work = PARAMS.get("work_dir") or os.path.join(os.getcwd(), "bush_work")
    if not os.path.isdir(work):
        os.makedirs(work)
    return work


def extract_bushings(PARAMS):
    work = _work(PARAMS)
    parts = []
    for f in _part_files(PARAMS["rubber"]):
        g = _part_geo(f)
        if g:
            parts.append(g)
    bushes = _group_axes(parts, float(PARAMS.get("axis_tol", 5.0)),
                         float(PARAMS.get("angle_tol", 5.0)))
    path = os.path.join(work, "bushing_geo.json")
    json.dump({"bushings": bushes, "parts": parts}, open(path, "w"), indent=1)
    return {"stage": "extract", "parts": len(parts), "bushings": len(bushes),
            "path": path, "list": bushes}


# --------------------------------------------------------------------------- #
# stage 2 - build on the structure
# --------------------------------------------------------------------------- #
def _bushing_specs(PARAMS):
    specs = []
    geo = PARAMS.get("geo")
    if not geo:
        # A bare "build" run after "extract" should not need the path repeated:
        # extract wrote bushing_geo.json into work_dir, so look there first.
        work = PARAMS.get("work_dir") or os.path.join(os.getcwd(), "bush_work")
        candidate = os.path.join(work, "bushing_geo.json")
        if os.path.isfile(candidate):
            geo = candidate
    if geo and os.path.isfile(geo):
        specs += json.load(open(geo))["bushings"]
    specs += list(PARAMS.get("bushings") or [])
    if not specs:
        raise ValueError("MCP_PRECONDITION: no bushing geometry - pass PARAMS['geo'] "
                         "(bushing_geo.json from stage=extract) or "
                         "PARAMS['bushings']; nothing was built")
    out = []
    for s in specs:
        b = dict(s)
        if "origin" not in b:
            if "axis" in b:                          # convenience: [x, y] + Z
                b["origin"] = [float(b["axis"][0]), float(b["axis"][1]), 0.0]
                b.setdefault("dir", [0.0, 0.0, 1.0])
            else:
                raise ValueError("bushing %r needs origin or axis" % b.get("name"))
        b["dir"] = _unit([float(x) for x in b.get("dir", [0.0, 0.0, 1.0])])
        b["origin"] = [float(x) for x in b["origin"]]
        b["span"] = [float(x) for x in b["span"]]
        out.append(b)
    return out


def _plates(pos, o, v, s1, s2, r_max, pad=8.0):
    """Axial positions of the plates inside the rubber footprint.

    The rubber extent from the CAD is longer than the plate gap (sleeves stick
    out), so the build finds the plates on the structure: nodes of the hole ring
    and the plate face form a sharp peak in the axial histogram inside the bore
    footprint.  Returns [s_first, s_last] of the two strongest peaks, or [].
    """
    bins = {}
    for x, y, z in pos.values():
        p = [x, y, z]
        s = _project(p, o, v)
        if s < s1 - pad or s > s2 + pad:
            continue
        if _radial(p, o, v) > r_max:
            continue
        k = int(round(s))
        bins[k] = bins.get(k, 0) + 1
    if not bins:
        return []
    keys = sorted(bins)
    clusters = []
    cur = [keys[0]]
    for k in keys[1:]:
        if k - cur[-1] <= 2:
            cur.append(k)
        else:
            clusters.append(cur)
            cur = [k]
    clusters.append(cur)
    scored = [(sum(bins[k] for k in c), 1.0 * sum(bins[k] * k for k in c) / sum(bins[k] for k in c))
              for c in clusters]
    strong = [c for s, c in scored if s >= 0.35 * max(x[0] for x in scored)]
    strong.sort()
    if len(strong) < 2:
        return []
    return [strong[0], strong[-1]]


def _auto_band(pos, o, v, s, z_tol):
    """Pick the hole-edge radius band from the radial histogram of the slab."""
    radii = []
    for x, y, z in pos.values():
        p = [x, y, z]
        ds = _project(p, o, v) - s
        if abs(ds) > z_tol:
            continue
        r = _radial(p, o, v)
        if r < 60.0:
            radii.append(r)
    if len(radii) < 6:
        return None, 0
    radii.sort()
    # The hole edge is the inner boundary of the plate mesh: below it there are
    # no nodes at all, above it the plate fills the annulus.  Take the first
    # radius that already carries a ring of nodes.
    r_edge, n_edge = None, 0
    for r0 in radii:
        n = sum(1 for r in radii if r0 <= r <= r0 + 2.0)
        if n >= 6:
            r_edge, n_edge = r0, n
            break
    if r_edge is None:
        return None, 0
    return [r_edge - 1.0, r_edge + 4.0], n_edge


def _ring(pos, o, v, s, band, z_tol):
    ids = []
    for nid, (x, y, z) in pos.items():
        p = [x, y, z]
        if abs(_project(p, o, v) - s) > z_tol:
            continue
        r = _radial(p, o, v)
        if band[0] <= r <= band[1]:
            ids.append(nid)
    return ids


def _stiffness(b, mode, s1, s2):
    """K in CBUSH order: [axial, radial, radial, torsion, rocking, rocking].

    OptiStruct CBUSH is a *nodal* spring: it springs node A's six DOFs against
    node B's six DOFs and carries no length coupling, so K2 is already the
    force/relative-displacement the two plates see and K5/K6 already the
    moment/relative-rotation.  Verified on class5: identical models with a 2 mm
    and a 44 mm element and the same PBUSH gave the same solved stiffness to
    five digits (165.6 N/mm in +Z and 9913.9 N/mm in +Y).  There is therefore no lever term to
    compensate; the only reason to scale anything is a deliberately rigid joint.
    """
    L = abs(s2 - s1)
    k_rad = float(b.get("k_rad", DEFAULT_K_RAD))
    E = float(b.get("E", DEFAULT_E))
    G = float(b.get("G", DEFAULT_G))
    r_out = float(b.get("r_out", 0.0) or 0.0)
    r_in = float(b.get("r_in", 0.0) or 0.0)
    area = math.pi * (r_out ** 2 - r_in ** 2) if r_out > r_in > 0.0 else 0.0
    jpol = math.pi / 2.0 * (r_out ** 4 - r_in ** 4) if r_out > r_in > 0.0 else 0.0
    k_ax = E * area / L if area > 0.0 and L > 0.0 else 0.0
    k_tor = G * jpol / L if jpol > 0.0 and L > 0.0 else 0.0
    k_rock = k_rad * L ** 2 / 12.0
    k_el = k_rad
    warnings = []
    fix = b.get("spanning_fix", "physical") if mode == "spanning" else "physical"
    if fix == "rigid_rocking":
        k_rock = 1.0e5 * k_rad
        warnings.append("%s: rocking locked (K5=K6=1e5*k_rad), global model stiffer"
                        % b.get("name"))
    elif fix == "scale_radial":
        warnings.append("%s: spanning_fix='scale_radial' is obsolete - a CBUSH has "
                        "no lever term, scaling K2 only makes the joint stiff; using "
                        "the physical K" % b.get("name"))
    elif fix != "physical":
        raise ValueError("spanning_fix must be 'physical' or 'rigid_rocking'")
    if k_ax <= 0.0:
        warnings.append("%s: axial stiffness 0 - pass r_in/r_out" % b.get("name"))
    K = [k_ax, k_el, k_el, k_tor, k_rock, k_rock]
    return {"K": K, "L": L, "area": area, "jpol": jpol,
            "k_rad_nominal": k_rad, "k_rad_face": k_rad,
            "bounds": {"E": E, "G": G, "r_out": r_out, "r_in": r_in},
            "warnings": warnings}


def build(PARAMS):
    mode = PARAMS.get("mode", "point")
    if mode not in ("point", "spanning"):
        raise ValueError("mode must be 'point' or 'spanning'")
    pos = _nodes()
    if not pos:
        raise RuntimeError("no GRID in the current model - load the structure first")
    z_tol = float(PARAMS.get("z_tol", 8.0))
    min_ring = int(PARAMS.get("min_ring", 6))
    max_gap = float(PARAMS.get("max_gap", 90.0))
    band_override = PARAMS.get("ring_band")
    gid = int(PARAMS.get("gid_base", max(pos) + 1000))
    pid0 = int(PARAMS.get("pid_base", 960001))
    eid0 = int(PARAMS.get("eid_base", 900001))

    plan, cards, warnings = [], ["BEGIN BULK"], []
    for i, b in enumerate(_bushing_specs(PARAMS)):
        o, v = b["origin"], b["dir"]
        s1, s2 = b["span"]
        span_src = "given"
        if b.get("span_from", "plates") == "plates":
            r_max = float(b.get("r_out_est", 0.0) or 0.0) * 1.35
            if r_max < 20.0:
                r_max = 1e9
            det = _plates(pos, o, v, s1, s2, r_max)
            if det:
                s1, s2, span_src = det[0], det[1], "detected"
        bands = band_override
        if not bands:
            bands, _ = _auto_band(pos, o, v, s1, z_tol)
        if not bands:
            warnings.append("%s: no ring found at span %.1f - skipped" % (b["name"], s1))
            continue
        sides = []
        ok = True
        for s in (s1, s2):
            ids = _ring(pos, o, v, s, bands, z_tol)
            gap = _gap_deg([pos[n] for n in ids], o, v) if ids else 360.0
            sides.append({"s": s, "ids": ids, "gap": gap})
            if len(ids) < min_ring or gap > max_gap:
                ok = False
        if not ok:
            warnings.append("%s: ring too sparse (%s) - skipped"
                            % (b["name"], [(len(x["ids"]), round(x["gap"], 1)) for x in sides]))
            continue
        r_ring = sorted(_radial(pos[n], o, v) for n in sides[0]["ids"])
        r_mid = 0.5 * (r_ring[len(r_ring) // 2] + r_ring[(len(r_ring) - 1) // 2])
        if "r_out" not in b or not b.get("r_out"):
            b["r_out"] = r_mid
        st = _stiffness(b, mode, s1, s2)
        warnings += st["warnings"]

        if mode == "spanning":
            p_a, p_b = _add(o, _mul(v, s1)), _add(o, _mul(v, s2))
        else:
            mid = _add(o, _mul(v, 0.5 * (s1 + s2)))
            p_a = _sub(mid, _mul(v, 1.0))
            p_b = _add(mid, _mul(v, 1.0))
        p_g0 = _add(p_a, _mul(_perp(v), 2.0))
        ga, gb, g0 = gid, gid + 1, gid + 2
        gid += 3
        pid, eid = pid0 + i, eid0 + i
        for n, p in ((ga, p_a), (gb, p_b), (g0, p_g0)):
            cards.append("GRID,%d,,%.4f,%.4f,%.4f" % (n, p[0], p[1], p[2]))
        cards.append("PBUSH,%d,K,%s" % (pid, ",".join("%.1f" % x for x in st["K"])))
        cards.append("CBUSH,%d,%d,%d,%d,%d" % (eid, pid, ga, gb, g0))
        plan.append({"name": b["name"], "pid": pid, "eid": eid,
                     "ga": ga, "gb": gb, "g0": g0,
                     "origin": b["origin"], "dir": b["dir"],
                     "span": [s1, s2], "span_source": span_src, "elements": 1,
                     "sides": sides, "stiffness": st,
                     "r_ring": r_mid, "parts": b.get("parts", [])})

    if not plan:
        return {"bushings": 0, "cbush": 0, "rbe2": 0, "sides": [],
                "warnings": warnings + ["nothing built"]}

    work = _work(PARAMS)
    card_path = os.path.join(work, "bush_cards.nas")
    fh = open(card_path, "w")
    fh.write("\n".join(cards + ["ENDDATA"]) + "\n")
    fh.close()
    base.InputNastran(card_path)

    ent = {n._id: n for n in base.CollectEntities(DECK, None, "GRID")}
    built = 0
    for p in plan:
        for side, gn_id in ((0, p["ga"]), (1, p["gb"])):
            ids = [i for i in p["sides"][side]["ids"] if i in ent and i != gn_id]
            gn = ent.get(gn_id)
            if gn is None or len(ids) < 2:
                warnings.append("%s side%d: no spider" % (p["name"], side))
                continue
            s = base.CreateEntity(DECK, "SET",
                                  {"Name": "BUSH_%s_%d" % (p["name"], side)})
            base.AddToSet(s, [ent[i] for i in ids])
            base.CreateEntity(DECK, "RBE2", {"GN": gn, "GM NSET": s, "CM": "123456"})
            built += 1
    base.RedrawAll()

    out = {"bushings": len(plan), "cbush": len(plan), "rbe2": built,
           "mode": mode, "sides": plan, "warnings": warnings}
    if PARAMS.get("export_nas"):
        base.OutputNastran(PARAMS["export_nas"])
        out["export_nas"] = PARAMS["export_nas"]
        # ANSA re-exports PBUSH in 8-column fixed format and lets long values run
        # into the next field ("27429.8161333.3" = 27429.8 + 161333.3), which the
        # solver then mis-reads.  Rewrite the six K values from the plan.
        out["pbush_rewritten"] = _rewrite_pbush(PARAMS["export_nas"], plan)
    if PARAMS.get("save_ansa"):
        try:
            base.SaveAs(PARAMS["save_ansa"])
            out["save_ansa"] = PARAMS["save_ansa"]
        except Exception as exc:
            warnings.append("SaveAs failed: %s" % exc)
    path = PARAMS.get("report") or os.path.join(work, "bush_plan.json")
    json.dump(plan, open(path, "w"), indent=1)
    out["report"] = path
    return out


# --------------------------------------------------------------------------- #
def _rewrite_pbush(path, plan):
    """Rewrite each PBUSH as free-field cards.

    ANSA re-exports PBUSH in 8-column fixed format and lets long values run into
    the next field ("27429.8161333.3" = 27429.8 + 161333.3), so the solver and any
    later read-back lose K5/K6.  Free-field commas keep all six values exact.
    """
    k_by_pid = {p["pid"]: p["stiffness"]["K"] for p in plan}
    lines, done = [], 0
    for line in open(path, errors="ignore").read().splitlines():
        head = line.strip().upper()
        if head.startswith("PBUSH"):
            try:
                pid = int(line.split()[1].rstrip("Kk"))
            except (IndexError, ValueError):
                lines.append(line)
                continue
            if pid in k_by_pid:
                vals = ",".join("%.6g" % float(x) for x in k_by_pid[pid])
                lines.append("PBUSH,%d,K,%s" % (pid, vals))
                done += 1
                continue
        lines.append(line)
    open(path, "w").write("\n".join(lines) + "\n")
    return done


# --------------------------------------------------------------------------- #
def run(PARAMS):
    stage = PARAMS.get("stage", "all")
    out = {"stage": stage}
    if stage in ("extract", "all"):
        if not PARAMS.get("rubber"):
            raise ValueError("stage %s needs PARAMS['rubber']" % stage)
        out["extract"] = extract_bushings(PARAMS)
    if stage == "all":
        structure = PARAMS.get("structure")
        if not structure:
            raise ValueError("stage 'all' needs PARAMS['structure']")
        session.New("discard")
        base.InputNastran(structure)
    if stage in ("build", "all"):
        out["build"] = build(PARAMS)
    return out

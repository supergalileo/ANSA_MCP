"""Mesh surface geometry with either of ANSA's two meshing engines.

Both engines honour the mesh-parameter file and the quality-criteria file; they
differ in *how* the mesh is produced, and the difference is measurable.

    PARAMS = {
        "source":        r"D:/proj/part.ansa",      # or "catpart"
        "catpart":       r"D:/proj/part.CATPart",
        "engine":        "mesh",                    # "mesh" | "batchmesh"
        "target_suffix": "_MIDSURF",                # only mesh these parts
        "mpar":          r"D:/proj/5MM.ansa_mpar",
        "qual":          r"D:/proj/5MM.ansa_qual",
        "change_option": "macro perimeters",        # mesh engine only
        "drop_mesh":     True,
        "save":          r"D:/proj/part_mesh.ansa",
    }
    exec(open(r"<skill>/scripts/mesh_engine.py", encoding="utf-8").read(), globals())
    RESULT

Engine choice, measured on three stamped panels at a 5 mm target
---------------------------------------------------------------
`mesh.Mesh` gave a lower worst-case aspect ratio than a Batch Mesh session on
every part (2.84 / 2.65 / 2.35 versus 3.32 / 3.41 / 2.86) with the same element
count and the same measured thickness. Batch Mesh is the one to use when the
assembly has many parts and the run has to be parallelised and repeatable; use
`mesh.Mesh` when mesh quality on a handful of parts is what matters.

`change_option` is read from the ANSA defaults, not from the .ansa file:

    "macro perimeters"  perimeters may be joined/released and every feature
                        treatment applies, including the ones that alter the
                        geometry (fillet sharpening)
    "perimeter grids"   only the perimeter node distribution changes; feature
                        treatments apply except the geometry-altering ones
    "mesh"              perimeters and their grids stay untouched, only the
                        interior mesh is generated and no feature treatment runs

The value does not travel with the .ansa, so a session that opens a saved file
and re-meshes must set it again.
"""

import math
import os

try:
    base  # noqa: F821 - injected when run through the MCP server
except NameError:  # pragma: no cover - only when executed outside ANSA
    from ansa import base, constants, mesh, session, utils, batchmesh


def _pctl(v, frac):
    if not v:
        return None
    i = int(round(frac * (len(v) - 1)))
    return v[min(len(v) - 1, max(0, i))]


def _stat(v):
    if not v:
        return None
    return {"min": round(v[0], 3), "p10": round(_pctl(v, .10), 3),
            "p50": round(_pctl(v, .50), 3), "p90": round(_pctl(v, .90), 3),
            "max": round(v[-1], 3), "n": len(v)}


def _measure(deck):
    """Counts, edge lengths, quad ratio and the thickness on the mesh."""
    shells = base.CollectEntities(deck, None, "SHELL")
    grids = base.CollectEntities(deck, None, "GRID")
    out = {"n_shells": len(shells), "n_grids": len(grids),
           "n_faces": len(base.CollectEntities(deck, None, "FACE"))}

    coords = {}
    for g in grids:
        try:
            v = base.GetEntityCardValues(deck, g, ["X1", "X2", "X3"])
            coords[g._id] = (float(v["X1"]), float(v["X2"]), float(v["X3"]))
        except Exception:
            pass
    out["n_unique_nodes"] = len(set((round(a, 2), round(b, 2), round(c, 2))
                                    for a, b, c in coords.values()))

    edges, nq, nt = [], 0, 0
    for s in shells:
        try:
            pts = [coords[n._id] for n in
                   base.CollectEntities(deck, s, "GRID") if n._id in coords]
        except Exception:
            continue
        if len(pts) < 3:
            continue
        if len(pts) == 3:
            nt += 1
        else:
            nq += 1
        for i in range(len(pts)):
            edges.append(math.dist(pts[i], pts[(i + 1) % len(pts)]))
    edges.sort()
    out["n_quad"] = nq
    out["n_tria"] = nt
    out["quad_ratio"] = round(nq / max(1, nq + nt), 4)
    out["edge"] = _stat(edges)
    out["edge_lt2mm"] = sum(1 for e in edges if e < 2.0)
    out["edge_gt10mm"] = sum(1 for e in edges if e > 10.0)

    asp, skw, wrp = [], [], []
    for s in shells:
        try:
            q = base.ElementQuality(s, ["ASPECT", "SKEW", "WARP"])
        except Exception:
            continue
        for lst, idx in ((asp, 0), (skw, 1), (wrp, 2)):
            try:
                lst.append(float(q[idx]))
            except (TypeError, ValueError, IndexError):
                pass
    asp.sort(); skw.sort(); wrp.sort()
    out["aspect"] = _stat(asp)
    out["skew"] = _stat(skw)
    out["warp"] = _stat(wrp)

    # Link shells to properties through the element card: after a saved .ansa is
    # reopened, CollectEntities(deck, property, "SHELL") returns 0 even though
    # the elements are there.
    pid_n = {}
    for s in shells:
        try:
            pid = int(base.GetEntityCardValues(deck, s, ["PID"]).get("PID"))
            pid_n[pid] = pid_n.get(pid, 0) + 1
        except Exception:
            pass
    props = []
    for pr in base.CollectEntities(deck, None, "__PROPERTIES__"):
        try:
            if pr.ansa_type(deck) != "PSHELL":
                continue
        except Exception:
            continue
        pid = int(pr._id)
        if pid in pid_n:
            props.append({"pid": pid, "n_shells": pid_n[pid],
                          "T": str(base.GetEntityCardValues(
                              deck, pr, ["T"]).get("T"))})
    out["props"] = props
    out["placeholder_gauge"] = [p for p in props
                                if abs(float(p["T"]) - 1.0) < 1e-9]
    return out


def run(params):
    source = params.get("source")
    catpart = params.get("catpart")
    engine = str(params.get("engine", "mesh")).lower()
    suffix = params.get("target_suffix", "_MIDSURF")
    mpar = params.get("mpar")
    qual = params.get("qual")
    change = str(params.get("change_option", "macro perimeters"))
    drop = bool(params.get("drop_mesh", True))
    save = params.get("save")

    if engine not in ("mesh", "batchmesh"):
        return {"ok": False, "error": "engine must be 'mesh' or 'batchmesh'"}
    if not source and not catpart:
        return {"ok": False, "error": "PARAMS needs 'source' or 'catpart'"}
    if source and not os.path.isfile(source):
        return {"ok": False, "error": "source not found: %s" % source}
    if catpart and not os.path.isfile(catpart):
        return {"ok": False, "error": "catpart not found: %s" % catpart}

    session.New("discard")
    opened = catpart or source
    base.Open(opened)
    base.RedrawAll()
    deck = base.CurrentDeck()

    for p in base.CollectEntities(deck, None, "ANSAPART"):
        if not base.CollectEntities(deck, p, "FACE") and \
           not base.CollectEntities(deck, p, "SHELL"):
            base.DeleteEntity(p)

    log = {"ok": False, "engine": engine, "opened": opened}
    if drop:
        shells = base.CollectEntities(deck, None, "SHELL")
        if shells:
            try:
                base.DeleteEntity(shells)
                log["dropped_shells"] = len(shells)
            except Exception as exc:
                log["drop_err"] = str(exc)[:160]

    if mpar:
        if not os.path.isfile(mpar):
            return {"ok": False, "error": "mpar not found: %s" % mpar}
        log["read_mpar"] = mesh.ReadMeshParams(mpar)
    if qual:
        if not os.path.isfile(qual):
            return {"ok": False, "error": "qual not found: %s" % qual}
        log["read_qual"] = mesh.ReadQualityCriteria(qual)

    # ---- which parts / faces get meshed
    targets = []
    for p in base.CollectEntities(deck, None, "ANSAPART"):
        if suffix and not p._name.endswith(str(suffix)):
            continue
        if base.CollectEntities(deck, p, "FACE"):
            targets.append(p)
    if not targets:
        return {"ok": False, "error": "no part with faces matched target_suffix=%r"
                                      % suffix, "log": log}
    faces = []
    for p in targets:
        faces.extend(base.CollectEntities(deck, p, "FACE"))
    log["targets"] = [(int(p._id), p._name) for p in targets]
    log["n_target_faces"] = len(faces)

    # ---- mesh
    if engine == "mesh":
        try:
            base.SetANSAdefaultsValues({"mesh_change_option": change})
            log["change_option"] = change
        except Exception as exc:
            log["change_option_err"] = str(exc)[:160]
        log["mesh_ret"] = mesh.Mesh(faces)
    else:
        try:
            ses = batchmesh.GetNewSession("BM")
            log["session_mpar"] = batchmesh.ReadSessionMeshParams(ses, mpar) if mpar else None
            log["session_qual"] = batchmesh.ReadSessionQualityCriteria(ses, qual) if qual else None
            scen = batchmesh.GetNewMeshingScenario("SC", "PARTS")
            log["add_parts"] = batchmesh.AddPartToMeshingScenario(targets, scen)
            log["add_session"] = batchmesh.AddSessionToMeshingScenario([ses], scen)
            log["run"] = str(batchmesh.RunMeshingScenario(
                scen, time_limit=int(params.get("time_limit", 20))))
        except Exception as exc:
            return {"ok": False, "error": "batch mesh failed: %s" % str(exc)[:250],
                    "log": log}

    base.RedrawAll()
    log["mesh"] = _measure(deck)

    if save:
        parent = os.path.dirname(save)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.isfile(save):
            os.remove(save)
        base.SaveAs(save)
        log["saved"] = save
        log["saved_size"] = os.path.getsize(save) if os.path.isfile(save) else 0

    log["ok"] = (log.get("mesh") or {}).get("n_shells", 0) > 0
    return log


RESULT = run(globals().get("PARAMS", {}) or {})
RESULT

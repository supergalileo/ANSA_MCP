"""One part, start to finish: classify -> repair -> Skin -> name -> mesh -> save.

    PARAMS = {
        "catpart":  r"D:/proj/B0029_A0072600089.CATPart",
        "engine":   "mesh",              # "mesh" | "batchmesh" | "none"
        "mpar":     r"D:/proj/5MM.ansa_mpar",
        "qual":     r"D:/proj/5MM.ansa_qual",
        "repair":   "auto",              # "auto" | "always" | "never"
        "max_thickness": 6.0,
        "save":     r"D:/out/A0072600089.ansa",
    }
    exec(open(r"<skill>/scripts/process_part.py", encoding="utf-8").read(), globals())
    RESULT

Naming
------
The part code is taken from the file name: `B0029_A0072600089.CATPart` ->
`A0072600089` (drop everything before the first underscore). The original solid
geometry keeps the code plus `_origin`; the middle-surface property becomes
`<code>_T<gauge>` where the gauge is the measured wall in 0.01 mm, rounded half
up: 1.702 -> 170, 1.709 -> 171, 1.720 -> 172, 2.657 -> 266.

Classification
--------------
`base.DetectSolidDescription(estimate_thickness=True)` returns a list whose first
element is `{thickness: area_fraction}`. A dominant entry above
`sheet_metal_fraction` means the faces form a thin-walled body and the same call
already gives the gauge, so the verdict and the thickness come from one call.
`estimate_thickness` and `return_percentage` are mutually exclusive - passing both
raises.
"""

import json
import math
import os
import re
import statistics

try:
    base  # noqa: F821 - injected when run through the MCP server
except NameError:  # pragma: no cover
    from ansa import base, constants, mesh, session, utils, batchmesh

CHECK_OPTS = ["UNCHECKED FACES", "COLLAPSED CONS", "TRIPLE CONS",
              "NEEDLE FACES", "OVERLAPS", "CRACKS"]
CODE_RE = re.compile(r"([A-Za-z]\d{7,10})")


def part_code(path, override=None):
    """B0029_A0072600089.CATPart -> A0072600089

    The code is the **second underscore token**, not everything after the first
    underscore and not a fixed digit pattern:
        B0029_A0072600089        -> A0072600089
        R007_A0072600643_WELD    -> A0072600643   (drop the suffix)
        O004_A007S010414         -> A007S010414   (letters inside the number)
    A purely numeric regex misses the `A007S...` family; splitting once keeps the
    `_WELD` suffix. Falls back to the whole stem when there is no underscore.
    """
    if override:
        return str(override)
    stem = os.path.splitext(os.path.basename(path))[0]
    parts = stem.split("_")
    return parts[1] if len(parts) > 1 and parts[1] else stem


def gauge_tag(t):
    """1.702 -> '170' (0.01 mm, half up)"""
    return str(int(math.floor(float(t) * 100.0 + 0.5)))


def classify(deck, faces, fraction=0.5, max_T=4.0):
    """Sheet-metal verdict + estimated wall from one DetectSolidDescription call.

    Area fraction alone is not enough: a 7.45 mm weld gun body reports 100% of
    its area at one thickness and looks like a perfect panel. A wall thicker than
    `max_T` is not sheet metal whatever the fraction says.
    """
    try:
        r = base.DetectSolidDescription(
            faces=faces, thickness=0.0, return_percentage=False,
            estimate_thickness=True, separate_connectivity=True,
            separate_connectivity_stop_at_pid_bound=False,
            fix_unchecked_faces=False, estimate_thickness_num_decimals=3)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240]}
    table = {}
    if isinstance(r, list) and r and isinstance(r[0], dict):
        table = {float(k): float(v) for k, v in r[0].items()}
    elif isinstance(r, dict):
        table = {float(k): float(v) for k, v in r.items()}
    if not table:
        return {"ok": False, "error": "unexpected return: %r" % (r,)}
    t_best = max(table, key=table.get)
    frac = table[t_best]
    top = sorted(((round(k, 3), round(v, 4)) for k, v in table.items()),
                 key=lambda kv: -kv[1])[:5]
    if t_best <= 0:
        # DetectSolidDescription returns a negative sentinel when it cannot find a
        # consistent offset pair (open body, mixed wall, failed estimate). That is
        # "unknown", not "thick" - calling it not-sheet-metal silently drops a real
        # panel. Carry on and let Skin decide; it measures the gauge itself.
        return {"ok": True, "is_sheet_metal": True, "estimated_T": None,
                "thickness_estimated": False, "fraction": round(frac, 4),
                "max_sheet_thickness": max_T,
                "reason": "厚度估计失败（返回 %s），按薄壁件继续尝试抽中面" % t_best,
                "table_top": top}
    thin = t_best <= max_T
    thick_enough = frac >= fraction
    reason = None
    if not thick_enough:
        reason = ("主导厚度 %.3f mm 只覆盖 %.1f%% 的面积，低于 %.0f%% 的薄壁阈值"
                  % (t_best, 100 * frac, 100 * fraction))
    elif not thin:
        reason = ("主导厚度 %.3f mm 超过钣金上限 %.1f mm，判为非钣金"
                  % (t_best, max_T))
    return {"ok": True, "is_sheet_metal": bool(thin and thick_enough),
            "estimated_T": round(t_best, 3), "fraction": round(frac, 4),
            "thickness_estimated": True,
            "max_sheet_thickness": max_T, "reason": reason,
            "table_top": top}


def pids_of(deck, faces):
    out = set()
    for f in faces:
        try:
            out.add(int(base.GetEntityCardValues(deck, f, ["PID"]).get("PID")))
        except Exception:
            pass
    return out


def rename_prop(deck, pid, name):
    try:
        pr = base.GetEntity(deck, "PSHELL", int(pid))
        if pr is None:
            return False
        base.SetEntityCardValues(deck, pr, {"Name": name})
        return True
    except Exception:
        return False


def measure(deck):
    shells = base.CollectEntities(deck, None, "SHELL")
    grids = base.CollectEntities(deck, None, "GRID")
    coords = {}
    for g in grids:
        try:
            v = base.GetEntityCardValues(deck, g, ["X1", "X2", "X3"])
            coords[g._id] = (float(v["X1"]), float(v["X2"]), float(v["X3"]))
        except Exception:
            pass
    edges = []
    for s in shells:
        try:
            pts = [coords[n._id] for n in
                   base.CollectEntities(deck, s, "GRID") if n._id in coords]
        except Exception:
            continue
        for i in range(len(pts)):
            edges.append(math.dist(pts[i], pts[(i + 1) % len(pts)]))
    edges.sort()
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
            props.append({"pid": pid, "name": pr._name, "n_shells": pid_n[pid],
                          "T": str(base.GetEntityCardValues(deck, pr, ["T"]).get("T"))})
    return {"n_FACE": len(base.CollectEntities(deck, None, "FACE")),
            "n_SHELL": len(shells), "n_GRID": len(grids),
            "edge_p50": round(edges[len(edges) // 2], 3) if edges else None,
            "edge_min": round(edges[0], 3) if edges else None,
            "edge_max": round(edges[-1], 3) if edges else None,
            "n_edge_lt2mm": sum(1 for e in edges if e < 2.0),
            "props": props,
            "placeholder_gauge": [p for p in props
                                  if abs(float(p["T"]) - 1.0) < 1e-9]}


def run(params):
    cat = params.get("catpart") or params.get("source")
    if not cat or not os.path.isfile(cat):
        return {"ok": False, "error": "catpart not found: %s" % cat}
    code = part_code(cat, params.get("code"))
    engine = str(params.get("engine", "mesh")).lower()
    repair = str(params.get("repair", "auto")).lower()
    maxt = float(params.get("max_thickness", 6.0))
    mpar, qual = params.get("mpar"), params.get("qual")
    change = str(params.get("change_option", "macro perimeters"))
    save = params.get("save")

    log = {"part": os.path.basename(cat), "code": code, "engine": engine,
           "repair": repair, "ok": False}

    session.New("discard")
    base.Open(cat)
    base.RedrawAll()
    deck = base.CurrentDeck()
    for p in base.CollectEntities(deck, None, "ANSAPART"):
        if not base.CollectEntities(deck, p, "FACE") and \
           not base.CollectEntities(deck, p, "SHELL"):
            base.DeleteEntity(p)
    solid_parts = [p for p in base.CollectEntities(deck, None, "ANSAPART")
                   if base.CollectEntities(deck, p, "FACE")]
    if not solid_parts:
        return {"ok": False, "error": "no geometry imported", "log": log}
    solid_part = solid_parts[0]
    solid_faces = base.CollectEntities(deck, solid_part, "FACE")

    # ---------------------------------------------------------- 1. classify
    log["classify"] = classify(deck, solid_faces,
                               float(params.get("sheet_metal_fraction", 0.5)),
                               float(params.get("max_sheet_thickness", 4.0)))
    if not log["classify"].get("ok"):
        return {"ok": False, "error": "classification failed", "log": log}
    if not log["classify"]["is_sheet_metal"] and not params.get("force"):
        log["status"] = "not_sheet_metal"
        log["note"] = (log["classify"].get("reason")
                       or "dominant wall fraction below threshold")
        if save:
            base.SaveAs(save)
            log["saved"] = save
        return log

    # ------------------------------------------------------------ 2. repair
    origin_pids = pids_of(deck, solid_faces)

    def geometry_checkfix(max_rounds=3):
        """Check and fix with the v24 API.

        `base.CheckAndFixGeometry` was deprecated in 24.0 and only knows nine
        options; it has no `Problematic Surfaces` and no `Needle Areas`. The
        replacement, `base.checks.general.Geometry()`, defaults to exactly the
        set of checks the ANSA Geometry panel ships with: problematic surfs,
        unchecked faces, collapsed cons, triple cons, needle faces, overlap
        faces, needle areas, cracks - and `single_cons`/`sharp_edges` off.

        `try_fix()` returns nothing even when it repairs something, so the loop
        watches the FACE count instead: keep fixing until a round changes
        nothing (or the report comes back empty).
        """
        steps = []
        for i in range(int(max_rounds)):
            try:
                res = base.checks.general.Geometry().execute()
            except Exception as exc:                      # pre-24 release
                steps.append({"round": i + 1, "error": str(exc)[:120]})
                return steps, False
            n = len(res) if res is not None else 0
            before = len(base.CollectEntities(deck, None, "FACE"))
            for r in (res or []):
                try:
                    r.try_fix()
                except Exception:
                    pass
            base.RedrawAll()
            after = len(base.CollectEntities(deck, None, "FACE"))
            steps.append({"round": i + 1, "reports": n, "faces": [before, after]})
            if n == 0 or after == before:
                break
        return steps, True

    def geometry_checkfix_legacy():
        """Fallback for releases without `base.checks`."""
        try:
            base.CheckAndFixGeometry(input=solid_part, options=CHECK_OPTS,
                                     fix_options=[1] * len(CHECK_OPTS),
                                     return_one_matrix_for_every_error=False,
                                     remaining_errors=True)
            return True
        except Exception as exc:
            log["repair_err"] = str(exc)[:200]
            return False

    def skin():
        """Skin the solid -> (faces_skinned, faces_check, new_parts, ret).

        `base.Skin` reports three buckets: `skinned`, `check`, `failed`. A group
        in `check` has a real middle surface, ANSA only wants it eyeballed -
        counting `skinned` alone writes off perfectly good parts. Every call also
        leaves new parts behind (one per group), so a retry must drop the previous
        attempt's parts or the mesh ends up on three stacked surfaces.
        """
        before = {p._id for p in base.CollectEntities(deck, None, "ANSAPART")}
        ret = base.Skin(apply_thickness=True, new_pid=True, offset_type=2,
                        ok_to_offset=True, max_thickness=maxt, delete=False,
                        entities=base.CollectEntities(deck, solid_part, "FACE"),
                        treat_chamfers=True, new_part=True,
                        fail_on_all_errors=False)
        n_ok = n_chk = 0
        if isinstance(ret, dict):
            for g in (ret.get("skinned") or []):
                n_ok += len(g.get("skin") or [])
            for g in (ret.get("check") or []):
                n_chk += len(g.get("skin") or [])
        new_parts = [p for p in base.CollectEntities(deck, None, "ANSAPART")
                     if p._id not in before]
        return n_ok, n_chk, new_parts, ret

    def drop_parts(parts):
        for p in parts:
            try:
                base.DeleteEntity(p)
            except Exception:
                pass

    n_ok, n_chk, new_parts, ret = skin()
    n_skin = n_ok + n_chk
    log["skin"] = {"faces": n_skin, "skinned": n_ok, "check": n_chk,
                   "repaired": False}
    log["repair_steps"] = []
    if n_skin == 0:
        drop_parts(new_parts)

    if n_skin == 0 and repair in ("auto", "always"):
        steps, modern = geometry_checkfix()
        log["repair_steps"].append("geometry_checkfix" if modern else "checkfix_legacy")
        log["geometry_fix"] = steps
        if not modern:
            geometry_checkfix_legacy()
        n_ok, n_chk, new_parts, ret = skin()
        n_skin = n_ok + n_chk
        log["skin"] = {"faces": n_skin, "skinned": n_ok, "check": n_chk,
                       "repaired": True,
                       "faces_after_repair": len(
                           base.CollectEntities(deck, None, "FACE"))}
        if n_skin == 0:
            drop_parts(new_parts)

    if n_skin == 0 and repair in ("auto", "always"):
        # second attempt: rebuild topology. Do not run this after a successful
        # checkfix (it undoes it), only as a fallback.
        try:
            base.RebuildTopology(entities=[solid_part],
                                 paste_diff_parts=False,
                                 paste_diff_pids=True)
            log["repair_steps"].append("rebuild")
        except Exception as exc:
            log["rebuild_err"] = str(exc)[:200]
        n_ok, n_chk, new_parts, ret = skin()
        n_skin = n_ok + n_chk
        log["skin"] = {"faces": n_skin, "skinned": n_ok, "check": n_chk,
                       "repaired": True,
                       "faces_after_rebuild": len(
                           base.CollectEntities(deck, None, "FACE"))}
        if n_skin == 0:
            drop_parts(new_parts)

    if n_skin == 0:
        log["status"] = "skin_failed"
        log["note"] = ("Skin produced no middle-surface faces after repair steps: %s"
                       % (", ".join(log["repair_steps"]) or "none"))
        if save:
            base.SaveAs(save)
            log["saved"] = save
        return log

    # ------------------------------------------------- 3. name the properties
    mid_parts = [p for p in new_parts if base.CollectEntities(deck, p, "FACE")]
    mid_faces = []
    for p in mid_parts:
        mid_faces.extend(base.CollectEntities(deck, p, "FACE"))
        try:
            base.SetEntityCardValues(deck, p, {"Name": code + "_midsurf"})
        except Exception:
            pass
    mid_pids = pids_of(deck, mid_faces)
    if n_chk:
        log["skin_check_note"] = ("ANSA 将 %d 个中面面片归入 check 桶"
                                  "（已保留，建议人工复核）" % n_chk)

    naming = []
    for pid in sorted(origin_pids - mid_pids):
        naming.append({"pid": pid, "name": code + "_origin",
                       "role": "origin", "ok": rename_prop(deck, pid, code + "_origin")})
    for pid in sorted(mid_pids):
        try:
            t = float(base.GetEntityCardValues(
                deck, base.GetEntity(deck, "PSHELL", pid), ["T"]).get("T"))
        except Exception:
            t = float(log["classify"].get("estimated_T") or 1.0)
        nm = "%s_T%s" % (code, gauge_tag(t))
        naming.append({"pid": pid, "name": nm, "role": "midsurface",
                       "T": round(t, 3), "ok": rename_prop(deck, pid, nm)})
    log["naming"] = naming

    # ---------------------------------------------------------- 4. mesh
    if engine != "none":
        if mpar and os.path.isfile(mpar):
            log["read_mpar"] = mesh.ReadMeshParams(mpar)
        if qual and os.path.isfile(qual):
            log["read_qual"] = mesh.ReadQualityCriteria(qual)
        if engine == "mesh":
            base.SetANSAdefaultsValues({"mesh_change_option": change})
            log["mesh_ret"] = mesh.Mesh(mid_faces)
        else:
            ses = batchmesh.GetNewSession("BM")
            if mpar:
                batchmesh.ReadSessionMeshParams(ses, mpar)
            if qual:
                batchmesh.ReadSessionQualityCriteria(ses, qual)
            scen = batchmesh.GetNewMeshingScenario("SC", "PARTS")
            batchmesh.AddPartToMeshingScenario(mid_parts, scen)
            batchmesh.AddSessionToMeshingScenario([ses], scen)
            log["run"] = str(batchmesh.RunMeshingScenario(
                scen, time_limit=int(params.get("time_limit", 20))))

    base.RedrawAll()
    log["result"] = measure(deck)
    log["status"] = "done" if log["result"]["n_SHELL"] else "geometry_only"

    if save:
        parent = os.path.dirname(save)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.isfile(save):
            os.remove(save)
        base.SaveAs(save)
        log["saved"] = save
        log["saved_size"] = os.path.getsize(save) if os.path.isfile(save) else 0

    nas = params.get("nas")
    if nas:
        # A CATPart arrives with a placeholder material whose DEFINED flag is NO;
        # ANSA then drops MAT1 from the solver deck while PSHELL still points at
        # MID 1. Flip the flag before exporting and re-save so the .ansa agrees.
        mats = []
        for m in base.CollectEntities(deck, None, "__MATERIALS__"):
            try:
                base.SetEntityCardValues(deck, m, {"DEFINED": "YES"})
                mats.append({"id": int(m._id), "name": m._name})
            except Exception:
                pass
        log["materials_defined"] = mats
        if save:
            base.SaveAs(save)
        parent = os.path.dirname(nas)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.isfile(nas):
            os.remove(nas)
        log["nas_ret"] = base.OutputNastran(filename=nas)
        log["nas"] = nas
        log["nas_size"] = os.path.getsize(nas) if os.path.isfile(nas) else 0

    log["ok"] = log["status"] == "done"
    return log


RESULT = run(globals().get("PARAMS", {}) or {})
RESULT

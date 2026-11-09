"""Second pass: give the parts `Skin` refuses a middle surface with Casting.

    PARAMS = {
        "folder":  r"D:/proj/hard_parts",
        "out_dir": r"D:/out_casting",
        "engine":  "mesh",        # "mesh" | "batchmesh"  - only these two mesh here
        "mpar":    r"D:/proj/5MM.ansa_mpar",
        "qual":    r"D:/proj/5MM.ansa_qual",
        "thickness_tolerance": 2.0,      # 1.0 silently drops walls that differ by more
        "max_thickness": 6.0,
        "limit":   0,             # parts this invocation, 0 = all
        "resume":  True,
    }
    exec(open(r"<skill>/scripts/casting_midsurf.py", encoding="utf-8").read(), globals())
    RESULT

Two paths, and the default changed on 2026-11-09
-----------------------------------------------
**`autosurf` (default)** - `base.MidSurfAuto` extracts the surface *and* meshes it
*and* stitches it, in one call. On a rear crash absorber (203 faces, 3.9 mm walls)
it returned **one connected patch of 24 017 shells in 65 s**.

**`middle`** - `base.MiddleGeometry(faces, thickness_tolerance)` with the meshing
stripped out, then the project's own engine (`mesh.Mesh` / Batch Mesh) driven by
the handed `.ansa_mpar` / `.ansa_qual`. Faster (8 s) and every element comes from
a controlled engine, but **the surface is discrete**. The same part came out as
**5 disconnected blocks**, and `perimeters_success_rate` was 24.8% - the faces
never really joined. Stitching afterwards made it worse: `RebuildTopology` gave
10 blocks, and rebuilding topology before Casting dropped the perimeter rate to
19.5%.

So the trade is connectivity against control. A single-block mesh is worth more
than a parameter-exact mesh that cannot be assembled, so `autosurf` is the default.
Reach for `"casting": "middle"` only when the mpar/qual must be provably honoured
and the part is known to survive as separate patches.

`thick` is the one parameter that decides whether `MidSurfAuto` returns at all:
1.0 on a 3.9 mm wall ran past 240 s with no result, 3.5 returned in 65 s. It is
therefore taken from `DetectSolidDescription(estimate_thickness=True)` - the
dominant bin of that histogram *is* the answer. `connect_weldings` did not change
the block count on the test part (1 either way) but is left on.

Thickness
---------
Casting measures the wall while it builds the faces, and the mesher carries it onto
the elements, so `base.GetShellThickness(deck, shell_entity)` returns the real gauge
right after meshing - no `mesh.CalculateThickness` call is needed (that function
additionally requires a `maximum_thickness` argument, which is a third positional
parameter, not a keyword). `property="auto_create"`-style defaults do not apply
here, so the median has to be written into the PSHELL by hand.

Status values per part:
    rescued       Casting produced faces, mesh produced shells, one gauge written
    rescued_var   gauges spread beyond tolerance - median written, flagged
    casting_failed Casting produced no faces: a real solid, needs no surface
    mesh_failed   faces produced but the engine returned no shells
    error         exception
"""

import csv
import json
import os
import time
import traceback

try:
    base  # noqa: F821 - injected when run through the MCP server
except NameError:  # pragma: no cover
    from ansa import base, constants, mesh, session, batchmesh


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[min(len(sorted_vals) - 1, max(0, i))]


def solid_faces(deck):
    """Faces of the real solid, after dropping empty imported parts."""
    for ap in base.CollectEntities(deck, None, "ANSAPART"):
        if not base.CollectEntities(deck, ap, "FACE") and \
           not base.CollectEntities(deck, ap, "SHELL"):
            base.DeleteEntity(ap)
    faces = []
    for ap in base.CollectEntities(deck, None, "ANSAPART"):
        faces.extend(base.CollectEntities(deck, ap, "FACE"))
    return faces


def measure(deck, sample=400):
    """(median, p2, p98, n_shells, n_sampled) read off the elements themselves."""
    shells = base.CollectEntities(deck, None, "SHELL")
    step = max(1, len(shells) // sample) if shells else 1
    vals = []
    for s in shells[::step]:
        try:
            vals.append(float(base.GetShellThickness(deck, s)))
        except Exception:
            pass
    vals = sorted(v for v in vals if v and v > 0)
    if not vals:
        return None, None, None, len(shells), 0
    return (_pct(vals, 0.5), _pct(vals, 0.02), _pct(vals, 0.98), len(shells), len(vals))


def write_gauge(deck, t, name=None):
    """Put `t` on every PSHELL in the deck; Casting/meshing leave the 1.0 default."""
    done = 0
    for pr in base.CollectEntities(deck, None, "__PROPERTIES__"):
        try:
            if pr.ansa_type(deck) != "PSHELL":
                continue
            base.SetEntityCardValues(deck, pr, {"T": t})
            if name:
                base.SetEntityCardValues(deck, pr, {"Name": name})
            done += 1
        except Exception:
            pass
    return done


def count_blocks(deck):
    """Number of connected components in the shell mesh (union-find on grid ids)."""
    shells = base.CollectEntities(deck, None, "SHELL")
    if not shells:
        return 0
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    roots = set()
    for s in shells:
        try:
            v = base.GetEntityCardValues(deck, s, ["G1", "G2", "G3", "G4"])
        except Exception:
            continue
        gs = []
        for k in ("G1", "G2", "G3", "G4"):
            val = v.get(k)
            if not val:
                continue
            if isinstance(val, (list, tuple)):
                val = val[0]
            try:
                gs.append(int(val))
            except Exception:
                pass
        if len(gs) < 3:
            continue
        for g in gs:
            parent.setdefault(g, g)
        r0 = find(gs[0])
        for g in gs[1:]:
            r = find(g)
            if r != r0:
                parent[r] = r0
        roots.add(find(gs[0]))
    return len({find(g) for g in list(parent)})


def autosurf_thick(faces, params):
    """`thick` for MidSurfAuto.

    It is the minimum wall thickness the algorithm searches for, and it is
    **not** a free parameter: 1.0 on a 3.9 mm crash box makes MidSurfAuto run
    past 240 s without returning; 3.5 returns a single connected patch in 65 s.
    So take the dominant wall from DetectSolidDescription, which is exactly the
    histogram MidSurfAuto wants.
    """
    if params.get("autosurf_thick"):
        return float(params["autosurf_thick"])
    try:
        d = base.DetectSolidDescription(faces, estimate_thickness=True)
        hist = d[0] if isinstance(d, (list, tuple)) and d else {}
        if isinstance(hist, dict) and hist:
            return float(max(hist.items(), key=lambda kv: kv[1])[0])
    except Exception:
        pass
    return float(params.get("autosurf_thick_fallback", 3.0))


def cast_autosurf(cat, params):
    """MidSurfAuto path: one continuous middle-surface mesh in a single call.

    Differs from the MiddleGeometry path in what it buys and what it costs:

     * buys **one connected patch**. MiddleGeometry returns discrete faces whose
       perimeters are mostly unpaired (24.8% on a rear crash absorber), so the
       mesh breaks into 5 blocks that no stitching recovers - RebuildTopology
       after the fact made it 10.
     * costs **control over the mesher**. MidSurfAuto emits SHELL only, no FACE,
       and meshes with its own algorithm, so `.ansa_mpar` / `.ansa_qual` do not
       drive it. `length` is the one knob it exposes.
     * costs **time**: 65 s for a 203-face, 20 k-element part, against 8 s for
       MiddleGeometry.
    """
    name = os.path.basename(cat)
    stem = os.path.splitext(name)[0]
    code = stem.split("_")[1] if "_" in stem else stem
    log = {"part": name, "code": code, "path": "autosurf"}

    session.New("discard")
    base.Open(cat)
    deck = base.CurrentDeck()
    faces = solid_faces(deck)
    log["in_faces"] = len(faces)
    if not faces:
        log.update({"status": "casting_failed", "note": "没有实体几何"})
        return log

    thick = autosurf_thick(faces, params)
    log["autosurf_thick"] = thick

    t0 = time.time()
    try:
        base.MidSurfAuto(thick=thick, faces=faces, part=str(params.get("part_action", "use_existing")),
                         ret_ents=True,
                         length=float(params.get("length", 5.0)),
                         connect_weldings=bool(params.get("connect_weldings", True)))
    except Exception as exc:
        log.update({"status": "casting_failed",
                    "note": "MidSurfAuto 失败: %s" % str(exc)[:250]})
        return log
    log["secs_autosurf"] = round(time.time() - t0, 1)

    shells = base.CollectEntities(deck, None, "SHELL")
    log["shells"] = len(shells)
    if not shells:
        log.update({"status": "casting_failed",
                    "note": "MidSurfAuto 未产生壳（thick=%.2f 可能不在壁厚范围内）" % thick})
        return log

    log["blocks"] = count_blocks(deck)

    # MidSurfAuto leaves the source solids in the deck. Keeping them makes the
    # saved database unreadable (the grey faces hide the mesh) and doubles its size.
    orig = base.CollectEntities(deck, None, "FACE")
    if orig:
        try:
            base.DeleteEntity(orig)
            log["deleted_orig"] = len(orig)
        except Exception as exc:
            log["del_err"] = str(exc)[:120]

    med, p2, p98, n_shells, n_sampled = measure(deck)
    log.update({"sampled": n_sampled,
                "T_median": round(med, 3) if med else None,
                "T_p2": round(p2, 3) if p2 else None,
                "T_p98": round(p98, 3) if p98 else None})
    if not med:
        log.update({"status": "mesh_failed", "note": "出了壳但读不到厚度"})
        return log

    spread = (p98 - p2) / med if med else 1.0
    log["spread"] = round(spread, 3)
    t_write = round(med, 2)
    if spread <= float(params.get("tolerance", 0.15)):
        log["props_written"] = write_gauge(deck, t_write, "%s_T%d" % (code, round(med * 100)))
        log["status"] = "rescued"
        log["note"] = ("MidSurfAuto 出壳 %d（%d 块），单一厚度 %.2f mm（离散 %.1f%%），用时 %.0f s"
                       % (n_shells, log["blocks"], t_write, spread * 100, log["secs_autosurf"]))
    else:
        log["props_written"] = write_gauge(deck, t_write, "%s_T%d_suspect" % (code, round(med * 100)))
        log["status"] = "rescued_var"
        log["note"] = ("MidSurfAuto 出壳 %d（%d 块），厚度 %.2f-%.2f mm（中位 %.2f，离散 %.1f%%）"
                       % (n_shells, log["blocks"], p2, p98, med, spread * 100))

    out_dir = params.get("out_dir") or os.path.dirname(cat)
    ansa_path = os.path.join(out_dir, "%s.ansa" % code)
    nas_path = os.path.join(out_dir, "%s.nas" % code)
    for m in base.CollectEntities(deck, None, "__MATERIALS__"):
        base.SetEntityCardValues(deck, m, {"DEFINED": "YES"})
    base.SaveAs(ansa_path)
    log["ansa"] = ansa_path
    if params.get("nas", True):
        base.OutputNastran(filename=nas_path)
        log["nas"] = nas_path
        try:
            log["nas_size"] = os.path.getsize(nas_path)
        except OSError:
            pass
    return log


def cast_one(cat, params):
    name = os.path.basename(cat)
    stem = os.path.splitext(name)[0]
    code = stem.split("_")[1] if "_" in stem else stem
    # Default to MidSurfAuto: it is the only path that yields one connected patch.
    # Set "casting": "middle" to get the old MiddleGeometry + own-mesher behaviour.
    if str(params.get("casting", "autosurf")).lower() == "autosurf":
        return cast_autosurf(cat, params)
    log = {"part": name, "code": code, "path": "middle"}
    engine = str(params.get("engine", "mesh")).lower()

    session.New("discard")
    base.Open(cat)
    deck = base.CurrentDeck()
    faces = solid_faces(deck)
    log["in_faces"] = len(faces)
    if not faces:
        log.update({"status": "casting_failed", "note": "没有实体几何"})
        return log

    ret = base.MiddleGeometry(faces, float(params.get("thickness_tolerance", 2.0)))
    d = ret[0] if isinstance(ret, list) and ret else (ret if isinstance(ret, dict) else {})
    mid = list(d.get("result_faces") or [])
    log.update({"mid_faces": len(mid),
                "face_rate": d.get("faces_success_rate"),
                "perim_rate": d.get("perimeters_success_rate"),
                "problem_faces": len(d.get("problematic_faces") or [])})
    if not mid:
        log.update({"status": "casting_failed", "note": "Casting 未产生中面，判定为真实体"})
        return log

    # A face rate below 90% means Casting dropped walls, not that the part is
    # simple. Measured: a crash box at thickness_tolerance=1.0 returned 14 of 84
    # faces (16.6%) and meshed into 12 disconnected patches; the same part at 2.0
    # returned 99.9%. Without this gate the part still looks "rescued".
    rate = d.get("faces_success_rate")
    if rate is not None and float(rate) < float(params.get("min_face_rate", 90.0)):
        log.update({"status": "casting_partial",
                    "note": "Casting 只抽出 %.1f%% 的面，阈值 %.0f%%，调 thickness_tolerance 重跑"
                            % (float(rate), float(params.get("min_face_rate", 90.0)))})
        return log

    mpar, qual = params.get("mpar"), params.get("qual")
    if mpar and os.path.isfile(mpar):
        log["read_mpar"] = mesh.ReadMeshParams(mpar)
    if qual and os.path.isfile(qual):
        log["read_qual"] = mesh.ReadQualityCriteria(qual)

    if engine == "batchmesh":
        ses = batchmesh.GetNewSession("BM")
        if mpar:
            batchmesh.ReadSessionMeshParams(ses, mpar)
        if qual:
            batchmesh.ReadSessionQualityCriteria(ses, qual)
        scen = batchmesh.GetNewMeshingScenario("SC", "PARTS")
        parts = [p for p in base.CollectEntities(deck, None, "ANSAPART")
                 if base.CollectEntities(deck, p, "FACE")]
        batchmesh.AddPartToMeshingScenario(parts, scen)
        batchmesh.AddSessionToMeshingScenario([ses], scen)
        log["run"] = str(batchmesh.RunMeshingScenario(
            scen, time_limit=int(params.get("time_limit", 20))))
    else:
        try:
            base.SetANSAdefaultsValues({"mesh_change_option": params.get(
                "change_option", "macro perimeters")})
        except Exception as exc:
            log["change_option_err"] = str(exc)[:120]
        log["mesh_ret"] = mesh.Mesh(mid)

    med, p2, p98, n_shells, n_sampled = measure(deck)
    log.update({"shells": n_shells, "sampled": n_sampled,
                "T_median": round(med, 3) if med else None,
                "T_p2": round(p2, 3) if p2 else None,
                "T_p98": round(p98, 3) if p98 else None})
    if not n_shells or not med:
        log.update({"status": "mesh_failed", "note": "中面已生成但引擎未出壳"})
        return log

    spread = (p98 - p2) / med if med else 1.0
    log["spread"] = round(spread, 3)
    t_write = round(med, 2)
    if spread <= float(params.get("tolerance", 0.15)):
        log["props_written"] = write_gauge(deck, t_write, "%s_T%d" % (code, round(med * 100)))
        log["status"] = "rescued"
        log["note"] = ("Casting 出中面 %d 面、%s 出壳 %d，单一厚度 %.2f mm（离散 %.1f%%）"
                       % (len(mid), engine, n_shells, t_write, spread * 100))
    else:
        log["props_written"] = write_gauge(deck, t_write, "%s_T%d_suspect" % (code, round(med * 100)))
        log["status"] = "rescued_var"
        log["note"] = ("Casting 出中面 %d 面、%s 出壳 %d，厚度离散 %.1f%%（%.2f-%.2f），"
                       "暂写中位数 %.2f，需人工确认"
                       % (len(mid), engine, n_shells, spread * 100, p2, p98, t_write))

    out = params.get("out_dir")
    if out:
        os.makedirs(out, exist_ok=True)
        ansa_path = os.path.join(out, code + ".ansa")
        base.SaveAs(ansa_path)
        log["ansa"] = ansa_path
        if params.get("nas"):
            nas = os.path.join(out, code + ".nas")
            try:
                # Same trap as in process_part: a CATPart import leaves a
                # placeholder material with DEFINED=NO, and OutputNastran then
                # writes PSHELL -> MID 1 with no MAT1 and no warning.
                for m in base.CollectEntities(deck, None, "__MATERIALS__"):
                    try:
                        base.SetEntityCardValues(deck, m, {"DEFINED": "YES"})
                    except Exception:
                        pass
                base.SaveAs(ansa_path)
                if os.path.isfile(nas):
                    os.remove(nas)
                base.OutputNastran(filename=nas)
                log["nas"] = nas
                log["nas_size"] = os.path.getsize(nas) if os.path.isfile(nas) else 0
            except Exception as exc:
                log["nas_err"] = str(exc)[:120]
    return log


def run(params):
    # Two entry points: a folder (walked in one ANSA session) or a single
    # `catpart`. The single-part form is what `casting_batch.py` drives, one
    # process per part, so a part that hangs cannot take the queue with it.
    folder = params.get("folder")
    one = params.get("catpart")
    if one:
        if not os.path.isfile(one):
            return {"ok": False, "error": "catpart not found: %s" % one}
        files = [one]
    elif not folder or not os.path.isdir(folder):
        return {"ok": False, "error": "folder not found: %s" % folder}
    else:
        files = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(folder) for f in fs
                       if f.lower().endswith(".catpart"))
    out = params.get("out_dir")
    if out:
        os.makedirs(out, exist_ok=True)
    progress = os.path.join(out, "_casting_progress.jsonl") if out else None
    done = {}
    if progress and os.path.isfile(progress) and params.get("resume", True):
        for line in open(progress, encoding="utf-8", errors="ignore"):
            try:
                rec = json.loads(line)
                done[rec["part"]] = rec
            except Exception:
                pass
    limit = int(params.get("limit", 0) or 0)
    todo = [f for f in files if os.path.basename(f) not in done]
    batch = todo[:limit] if limit > 0 else todo
    rows = list(done.values())
    pf = open(progress, "a", encoding="utf-8") if progress else None
    for i, cat in enumerate(batch, 1):
        t0 = time.time()
        try:
            rec = cast_one(cat, params)
        except Exception as exc:
            rec = {"part": os.path.basename(cat), "status": "error",
                   "error": str(exc)[:240], "tb": traceback.format_exc()[-600:]}
        rec["seconds"] = round(time.time() - t0, 1)
        rows.append(rec)
        if pf:
            pf.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            pf.flush()
        print("CAST %d/%d %5.1fs %-40s %-14s %s" % (
            i, len(batch), rec["seconds"], rec.get("part", ""), rec.get("status", ""),
            rec.get("note") or rec.get("error") or ""))
    if pf:
        pf.close()

    by = {}
    for r in rows:
        by[r.get("status")] = by.get(r.get("status"), 0) + 1
    csv_path = None
    if out:
        csv_path = os.path.join(out, "抢救清单.csv")
        cols = ["part", "code", "status", "in_faces", "mid_faces", "face_rate",
                "perim_rate", "shells", "T_median", "T_p2", "T_p98", "spread",
                "seconds", "note", "error"]
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["零件", "编号", "状态", "输入面数", "中面数", "面成功率",
                        "周界成功率", "壳单元", "T中位数", "T_p2", "T_p98",
                        "离散度", "耗时s", "说明", "错误"])
            for r in rows:
                w.writerow([r.get(k, "") for k in cols])
    return {"ok": by.get("error", 0) == 0, "n_total": len(rows), "by_status": by,
            "n_todo": len(todo), "n_left": len(todo) - len(batch),
            "rows": rows, "summary_csv": csv_path}


RESULT = run(globals().get("PARAMS", {}) or {})
RESULT

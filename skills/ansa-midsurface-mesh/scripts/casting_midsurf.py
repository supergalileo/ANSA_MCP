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

Why Casting and not MidSurfAuto
-------------------------------
`base.Skin` gives up on parts that are still perfectly shellable, so a second pass
is needed. `base.MidSurfAuto` can do it, but it is a **mesher**: it emits a part of
shells and no faces at all, and it meshes with its own algorithm. On one bracket it
produced "Middle Skin Shells" - 1306 SHELL / 0 FACE - so its result can neither be
re-meshed nor inspected as geometry.

`base.MiddleGeometry(faces, thickness_tolerance)` is the same Casting algorithm with
the meshing stripped out. ANSA logs `Middle-Casting time` during MidSurfAuto, and
MiddleGeometry returns `faces_success_rate`, `perimeters_success_rate`,
`problematic_faces` and the created `result_faces`. Measured on the parts Skin
rejected for a 553-part BIW folder: 100% face success in 1-3 s, against 13-324 s
for MidSurfAuto on the same parts.

So the split is: Casting extracts the surface, then the project's own meshing engine
(`mesh.Mesh` or a Batch Mesh session, both driven by the handed `.ansa_mpar` and
`.ansa_qual`) produces the elements. That keeps every element in this skill coming
out of one of those two engines.

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


def cast_one(cat, params):
    name = os.path.basename(cat)
    stem = os.path.splitext(name)[0]
    code = stem.split("_")[1] if "_" in stem else stem
    log = {"part": name, "code": code}
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
                if os.path.isfile(nas):
                    os.remove(nas)
                base.OutputNastran(filename=nas)
                log["nas"] = nas
                log["nas_size"] = os.path.getsize(nas) if os.path.isfile(nas) else 0
            except Exception as exc:
                log["nas_err"] = str(exc)[:120]
    return log


def run(params):
    folder = params.get("folder")
    if not folder or not os.path.isdir(folder):
        return {"ok": False, "error": "folder not found: %s" % folder}
    out = params.get("out_dir")
    if out:
        os.makedirs(out, exist_ok=True)
    files = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(folder) for f in fs
                   if f.lower().endswith(".catpart"))
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

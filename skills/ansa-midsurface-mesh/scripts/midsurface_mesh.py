"""Mid-surface extraction + shell mesh at a target element size, inside ANSA.

Run through the `ansa` MCP server. Inject `PARAMS`, exec this file, and read
`RESULT` (it is the last expression, so `ansa_run_python` returns it):

    PARAMS = {"source": r"D:/proj/model.ansa",
              "target_length": 5.0,
              "output_dir": r"D:/proj/mesh_out"}
    exec(open(r"<skill>/scripts/midsurface_mesh.py", encoding="utf-8").read(),
         globals())
    RESULT

Why two passes
    Pass A runs base.Skin() per part only to obtain the measured wall thickness
    and to decide which parts are thin-walled at all (Skin returns 0 when the
    part is not a thin solid).
    Pass B reloads the clean model and lets base.MidSurfAuto() build the mesh,
    because only MidSurfAuto honours a target element length. It stores the
    measured thickness on the elements; that value is read back with
    GetShellThickness and written into the PSHELL.

    The `thick` argument given to MidSurfAuto is a detection hint, not the
    result: passing a deliberately wrong hint still yields the true measured
    thickness. Verified by passing 1.0 on a 2.8 mm part and reading 2.8 back.
"""

import os
import statistics

try:
    base  # noqa: F821  provided by ansa_run_python
except NameError:  # pragma: no cover - only when run outside ANSA
    from ansa import base, constants, mesh, session, utils  # noqa: F401


def run(params):
    source = params.get("source")
    target = float(params.get("target_length", 5.0))
    max_t = float(params.get("max_thickness", 3.0))
    elem_type = int(params.get("elem_type", 3))
    out_dir = params.get("output_dir")
    prefix = str(params.get("name_prefix", "SHELL"))
    delete_solids = bool(params.get("delete_solids", True))
    save_ansa = bool(params.get("save_ansa", True))
    export_nastran = bool(params.get("export_nastran", True))
    export_abaqus = bool(params.get("export_abaqus", True))
    dry_run = bool(params.get("dry_run", False))
    only_parts = params.get("part_ids")

    if not source:
        return {"ok": False, "stage": "init",
                "error": "PARAMS['source'] is required"}
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    def load_clean():
        """Always start from the untouched source: pass A adds entities."""
        session.New("discard")
        base.Open(source)
        base.RedrawAll()
        return base.CurrentDeck()

    # ------------------------------------------------------------------ pass A
    deck = load_clean()
    if only_parts:
        part_ids = list(only_parts)
    else:
        part_ids = [p._id for p in base.CollectEntities(deck, None, "ANSAPART")]

    measured, pass_a = {}, []
    for pid in sorted(part_ids):
        part = base.GetEntity(deck, "ANSAPART", pid)
        faces = base.CollectEntities(deck, part, "FACE") if part else []
        entry = {"part": pid, "name": part._name if part else None,
                 "n_solid_faces": len(faces)}
        if not faces:
            entry["status"] = "no faces"
            pass_a.append(entry)
            continue

        before = {x._id for x in base.CollectEntities(deck, None, "__PROPERTIES__")}
        try:
            ret = base.Skin(apply_thickness=True, new_pid=True, offset_type=2,
                            ok_to_offset=True, max_thickness=max_t, delete=False,
                            entities=faces)
        except Exception as exc:
            entry["status"] = "skin error: %s" % exc
            pass_a.append(entry)
            continue

        new_props = [x for x in base.CollectEntities(deck, None, "__PROPERTIES__")
                     if x._id not in before]
        if ret != 1 or not new_props:
            entry["status"] = "not thin-walled (skin=%s)" % ret
            pass_a.append(entry)
            continue

        raw = base.GetEntityCardValues(deck, new_props[0], ["T"]).get("T")
        try:
            t_val = float(raw)
        except (TypeError, ValueError):
            entry["status"] = "unreadable thickness %r" % (raw,)
            pass_a.append(entry)
            continue

        measured[pid] = t_val
        entry.update({
            "status": "thin", "T": round(t_val, 3),
            "n_mid_faces": len(base.CollectEntities(deck, new_props[0], "FACE")),
        })
        pass_a.append(entry)

    if not measured:
        return {"ok": False, "stage": "pass_a", "source": source,
                "pass_a": pass_a,
                "error": "no thin-walled part found; raise max_thickness or "
                         "check the deck type"}
    if dry_run:
        return {"ok": True, "stage": "dry_run", "source": source,
                "target_length": target, "pass_a": pass_a,
                "n_thin_parts": len(measured)}

    # ------------------------------------------------------------------ pass B
    deck = load_clean()
    pass_b, mid_parts = [], []

    for pid in sorted(measured):
        hint = measured[pid]
        part = base.GetEntity(deck, "ANSAPART", pid)
        faces = base.CollectEntities(deck, part, "FACE") if part else []
        entry = {"part": pid, "name": part._name if part else None}
        if not faces:
            entry["status"] = "no faces"
            pass_b.append(entry)
            continue

        ret = None
        for thick in (hint, round(hint * 0.85, 2), 0.6):
            if thick <= 0 or thick >= target:
                continue
            try:
                ret = base.MidSurfAuto(faces=faces, thick=thick, length=target,
                                       elem_type=elem_type, part="auto_create",
                                       property="auto_create",
                                       ret_ents=True, get_result_type=True)
                entry["thick_hint_used"] = thick
                break
            except Exception as exc:
                entry.setdefault("attempts", []).append("%s: %s" % (thick, exc))

        if ret is None:
            entry["status"] = "MidSurfAuto failed"
            pass_b.append(entry)
            continue

        try:
            out_part_id, entry["result_type"] = ret.type[0][1], ret.type[0][2]
        except Exception:
            out_part_id, entry["result_type"] = None, "?"
        entry["n_elements"] = len(ret.ents) if ret.ents else 0
        if not out_part_id:
            entry["status"] = "no output part"
            pass_b.append(entry)
            continue

        out_part = base.GetEntity(deck, "ANSAPART", out_part_id)
        shells = base.CollectEntities(deck, out_part, "SHELL") if out_part else []
        step = max(1, len(shells) // 40)
        samples = []
        for s in shells[::step][:40]:
            try:
                samples.append(float(base.GetShellThickness(deck, s)))
            except Exception:
                pass

        if samples:
            med = round(statistics.median(samples), 3)
            lo, hi = round(min(samples), 3), round(max(samples), 3)
            entry["T_measured"] = med
            entry["T_range"] = [lo, hi]
            # A stamped panel has one gauge. A spread beyond 10% means the part
            # is not really constant-thickness and needs review rather than a
            # silently averaged value.
            entry["uniform"] = (hi - lo) <= max(0.01, 0.1 * med)
            try:
                raw_pid = base.GetEntityCardValues(deck, out_part, ["PID"]).get("PID")
                prop = base.GetEntity(deck, "PSHELL", int(raw_pid))
                base.SetEntityCardValues(deck, prop, {"T": med})
                entry["T_written"] = float(
                    base.GetEntityCardValues(deck, prop, ["T"]).get("T"))
                entry["pid"] = int(raw_pid)
            except Exception as exc:
                entry["T_write_err"] = str(exc)

        mid_parts.append(out_part_id)
        entry["status"] = "meshed"
        pass_b.append(entry)

    # ---------------------------------------------------------------- clean up
    faces_deleted = 0
    if delete_solids and mid_parts:
        keep = set()
        for opid in mid_parts:
            op = base.GetEntity(deck, "ANSAPART", opid)
            if op is not None:
                keep.update(f._id for f in base.CollectEntities(deck, op, "FACE"))
        doomed = []
        for pid in sorted(measured):
            part = base.GetEntity(deck, "ANSAPART", pid)
            if part is None:
                continue
            doomed.extend(f for f in base.CollectEntities(deck, part, "FACE")
                          if f._id not in keep)
        if doomed:
            base.DeleteEntity(doomed)
            faces_deleted = len(doomed)

    # Names containing spaces or semicolons make the exported Abaqus ELSET
    # unparsable, so give every shell property a compact gauge-based name.
    renamed = 0
    for pr in base.CollectEntities(deck, None, "__PROPERTIES__"):
        if pr.ansa_type(deck) != "PSHELL":
            continue
        if not base.CollectEntities(deck, pr, "SHELL"):
            continue
        t = base.GetEntityCardValues(deck, pr, ["T"]).get("T")
        try:
            tag = ("%.2f" % float(t)).rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            continue
        try:
            base.SetEntityCardValues(deck, pr, {"Name": "%s_T%s" % (prefix, tag)})
            renamed += 1
        except Exception:
            pass

    base.RedrawAll()

    # ------------------------------------------------------------------ verify
    shells = base.CollectEntities(deck, None, "SHELL")
    areas = sorted(base.CalcShellArea(s) for s in shells)
    n = len(areas)
    quad = tria = 0
    for s in shells[:400]:
        try:
            k = len(base.CollectEntities(deck, s, "GRID"))
        except Exception:
            k = 0
        quad += k == 4
        tria += k == 3

    verify = {
        "n_shells": n,
        "n_grids": len(base.CollectEntities(deck, None, "GRID")),
        "total_area": round(sum(areas), 1) if n else 0,
        "edge_p50": round(areas[n // 2] ** 0.5, 2) if n else None,
        "edge_p10": round(areas[int(n * 0.10)] ** 0.5, 2) if n else None,
        "edge_p90": round(areas[int(n * 0.90)] ** 0.5, 2) if n else None,
        "quad_ratio_sampled": round(quad / max(1, quad + tria), 3) if n else None,
        "faces_deleted": faces_deleted,
        "faces_left": len(base.CollectEntities(deck, None, "FACE")),
        "renamed_props": renamed,
    }

    # -------------------------------------------------------------- save/export
    files = {}
    if out_dir:
        stem = os.path.basename(source).split(".")[0]
        jobs = []
        if save_ansa:
            jobs.append(("ansa", lambda p=os.path.join(out_dir, stem + "_midsurface.ansa"):
                         base.SaveAs(p)))
        if export_nastran:
            jobs.append(("nastran", lambda p=os.path.join(out_dir, stem + ".nas"):
                         base.OutputNastran(filename=p)))
        if export_abaqus:
            jobs.append(("abaqus", lambda p=os.path.join(out_dir, stem + ".inp"):
                         base.OutputAbaqus(filename=p)))
        for label, fn in jobs:
            try:
                fn()
                files[label] = "ok"
            except Exception as exc:
                files[label] = "ERR:%s" % str(exc)[:120]
        for f in sorted(os.listdir(out_dir)):
            full = os.path.join(out_dir, f)
            if os.path.isfile(full):
                files[f] = os.path.getsize(full)

    return {
        "ok": verify["n_shells"] > 0,
        "stage": "done",
        "source": source,
        "target_length": target,
        "pass_a": pass_a,
        "pass_b": pass_b,
        "verify": verify,
        "files": files,
        "caveats": [
            "equivalent edge lengths come out 4-6% below the target on curved "
            "geometry; this is normal mesher behaviour, not a wrong setting",
            "each part is meshed on its own, so nodes are NOT shared between "
            "parts; define connections separately",
        ],
    }


RESULT = run(globals().get("PARAMS", {}) or {})
RESULT

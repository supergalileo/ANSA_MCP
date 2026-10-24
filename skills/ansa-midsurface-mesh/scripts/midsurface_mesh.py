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
    Pass A runs base.Skin() per part to obtain the measured wall thickness and
    to get a first opinion on whether the part is thin-walled.
    Pass B reloads the clean model and lets base.MidSurfAuto() build the mesh,
    because only MidSurfAuto honours a target element length. It stores the
    measured thickness on the elements; that value is read back with
    GetShellThickness and written into the PSHELL.

    The `thick` argument given to MidSurfAuto is a detection hint, not the
    result: passing a deliberately wrong hint still yields the true measured
    thickness. Verified by passing 1.0 on a 2.8 mm part and reading 2.8 back.

Why Skin alone cannot decide
    On a real BIW assembly (class5.stp, 68 parts) Skin returned 0 for 13 large
    panels with max_thickness raised as far as 40 mm, so the rejection was not a
    thickness limit. MidSurfAuto built a uniform mesh on 10 of those same parts
    (T 2.0-2.5 mm, element thickness spread < 1%). Gating the build on Skin
    therefore discards legitimate sheet metal.

    So a Skin failure is treated as "unknown", not "not sheet metal", and the
    part is retried with MidSurfAuto in pass B. A part is only reported as
    non-shellable when MidSurfAuto also produces no output. Set
    ``"fallback_to_midsurf": False`` to restore the old Skin-only behaviour.

max_thickness is a hard filter, not a clamp
    When the true wall is thicker than ``max_thickness``, Skin returns 0 and the
    part looks exactly like a non-sheet-metal part. It does not clamp T to the
    cap. Two 3.5 mm panels in class5.stp were silently dropped at a 3.0 mm cap
    and recovered at 6.0 mm. Use a cap above the thickest expected gauge.
"""

import os
import statistics

try:
    base  # noqa: F821  provided by ansa_run_python
except NameError:  # pragma: no cover - only when run outside ANSA
    from ansa import base, constants, mesh, session, utils  # noqa: F401


def _pctl(sorted_vals, frac):
    """Linear-free percentile on an already sorted list.

    Min/max are the wrong statistics for this job: MidSurfAuto occasionally
    mis-pairs two faces and produces a single element at 20 mm on a 3 mm panel.
    One such element must not condemn the whole part, so the verdict uses
    p2/p98 and the raw extremes are reported alongside for inspection.
    """
    if not sorted_vals:
        return None
    i = int(round(frac * (len(sorted_vals) - 1)))
    return sorted_vals[min(len(sorted_vals) - 1, max(0, i))]


def run(params):
    source = params.get("source")
    target = float(params.get("target_length", 5.0))
    max_t = float(params.get("max_thickness", 4.0))
    elem_type = int(params.get("elem_type", 3))
    out_dir = params.get("output_dir")
    prefix = str(params.get("name_prefix", "SHELL"))
    delete_solids = bool(params.get("delete_solids", True))
    save_ansa = bool(params.get("save_ansa", True))
    export_nastran = bool(params.get("export_nastran", True))
    export_abaqus = bool(params.get("export_abaqus", True))
    dry_run = bool(params.get("dry_run", False))
    only_parts = params.get("part_ids")
    fallback = bool(params.get("fallback_to_midsurf", True))
    # What to do when a part's element thickness is genuinely not constant:
    #   "block"        keep the shells, leave T unwritten (placeholder 1.0)
    #   "write_median" write the median T and say so
    #   "drop"         delete the shells, keep the solid faces for manual work
    var_policy = str(params.get("variable_thickness_policy", "block")).lower()
    if var_policy not in ("block", "write_median", "drop"):
        return {"ok": False, "stage": "init",
                "error": "variable_thickness_policy must be block | write_median | drop"}
    uniform_tol = float(params.get("uniform_tolerance", 0.10))

    if not source:
        return {"ok": False, "stage": "init",
                "error": "PARAMS['source'] is required"}
    # base.Open() does NOT raise on a missing or unreadable file - it writes
    # "File does not exist !" to the ANSA log and returns, leaving an empty deck.
    # Without this check the run then reports "no thin-walled part found", which
    # sends you looking at max_thickness for a problem that is a wrong path.
    if not os.path.isfile(source):
        return {"ok": False, "stage": "init",
                "error": "source file not found: %s" % source}
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
    n_faces = len(base.CollectEntities(deck, None, "FACE"))
    if not n_faces:
        return {"ok": False, "stage": "load",
                "source": source,
                "error": "loaded deck has no FACE entities - the import failed "
                         "silently (wrong deck type, missing translator, or an "
                         "unreadable file). Check the ANSA log."}
    if only_parts:
        part_ids = list(only_parts)
    else:
        part_ids = [p._id for p in base.CollectEntities(deck, None, "ANSAPART")]

    measured, unknown, pass_a = {}, [], []
    for pid in sorted(part_ids):
        part = base.GetEntity(deck, "ANSAPART", pid)
        faces = base.CollectEntities(deck, part, "FACE") if part else []
        entry = {"part": pid, "name": part._name if part else None,
                 "n_solid_faces": len(faces)}
        if not faces:
            entry.update({"status": "no_faces", "reason": "part has no faces"})
            pass_a.append(entry)
            continue

        before = {x._id for x in base.CollectEntities(deck, None, "__PROPERTIES__")}
        try:
            ret = base.Skin(apply_thickness=True, new_pid=True, offset_type=2,
                            ok_to_offset=True, max_thickness=max_t, delete=False,
                            entities=faces)
        except Exception as exc:
            entry.update({"status": "skin_error", "reason": str(exc)[:160]})
            pass_a.append(entry)
            continue

        new_props = [x for x in base.CollectEntities(deck, None, "__PROPERTIES__")
                     if x._id not in before]
        if ret != 1 or not new_props:
            # Skin says nothing useful here. Two very different causes look
            # identical: the wall may exceed max_thickness, or Skin may simply
            # fail on this topology. Never call that "not sheet metal".
            entry.update({
                "status": "unknown",
                "reason": "skin returned %s with max_thickness=%g; cause is "
                          "either a wall thicker than the cap or a topology "
                          "Skin cannot offset - not proof of a solid" % (ret, max_t),
            })
            unknown.append(pid)
            pass_a.append(entry)
            continue

        raw = base.GetEntityCardValues(deck, new_props[0], ["T"]).get("T")
        try:
            t_val = float(raw)
        except (TypeError, ValueError):
            entry.update({"status": "unknown", "reason":
                          "unreadable thickness %r" % (raw,)})
            unknown.append(pid)
            pass_a.append(entry)
            continue

        measured[pid] = t_val
        entry.update({
            "status": "thin", "T": round(t_val, 3),
            "n_mid_faces": len(base.CollectEntities(deck, new_props[0], "FACE")),
        })
        pass_a.append(entry)

    if not fallback:
        unknown = []

    if not measured and not unknown:
        return {"ok": False, "stage": "pass_a", "source": source,
                "pass_a": pass_a,
                "error": "no thin-walled and no undecided part found; check the "
                         "deck type or raise max_thickness"}

    if dry_run:
        return {"ok": True, "stage": "dry_run", "source": source,
                "target_length": target, "pass_a": pass_a,
                "n_thin_parts": len(measured),
                "n_unknown_parts": len(unknown),
                "unknown_parts": [
                    {"part": r["part"], "name": r["name"],
                     "faces": r["n_solid_faces"]}
                    for r in pass_a if r.get("status") == "unknown"],
                "note": "unknown parts are retried with MidSurfAuto in a full "
                        "run; a dry run cannot tell a thick wall from a "
                        "topology Skin rejects"}

    # ------------------------------------------------------------------ pass B
    deck = load_clean()
    pass_b, mid_parts, meshed_src = [], [], set()
    written_pids = set()      # PSHELL ids we set T on explicitly

    def measure_output(out_id):
        """Thickness verdict for one MidSurfAuto output part."""
        op = base.GetEntity(deck, "ANSAPART", out_id)
        sh = base.CollectEntities(deck, op, "SHELL") if op is not None else []
        if not sh:
            return None
        # Sample broadly: a 40-element min/max window gave two different
        # verdicts on two instances of the same part number, so the gate was
        # not reproducible. 400 evenly spread elements plus p2/p98 fixes both
        # the sample size and the single-outlier sensitivity.
        step = max(1, len(sh) // 400)
        vals = []
        for s in sh[::step][:400]:
            try:
                vals.append(float(base.GetShellThickness(deck, s)))
            except Exception:
                pass
        if not vals:
            return {"out_id": out_id, "n_shells": len(sh), "no_data": True}
        vals.sort()
        med = round(statistics.median(vals), 3)
        lo, hi = round(_pctl(vals, 0.02), 3), round(_pctl(vals, 0.98), 3)
        sp = (hi - lo) / max(med, 1e-9)
        try:
            raw_pid = base.GetEntityCardValues(deck, op, ["PID"]).get("PID")
            raw_pid = int(raw_pid) if raw_pid else None
        except Exception:
            raw_pid = None
        return {"out_id": out_id, "n_shells": len(sh), "shells": sh,
                "T": med, "T_range": [lo, hi],
                "T_range_raw": [round(vals[0], 3), round(vals[-1], 3)],
                "spread": sp, "spread_pct": round(100.0 * sp, 1),
                "n_sampled": len(vals), "pid": raw_pid,
                "uniform": sp <= uniform_tol, "no_data": False}

    def write_T(item):
        if not item.get("pid"):
            item["T_write_err"] = "output part has no PID"
            return False
        try:
            prop = base.GetEntity(deck, "PSHELL", int(item["pid"]))
            base.SetEntityCardValues(deck, prop, {"T": item["T"]})
            item["T_written"] = float(
                base.GetEntityCardValues(deck, prop, ["T"]).get("T"))
            written_pids.add(int(item["pid"]))
            return True
        except Exception as exc:
            item["T_write_err"] = str(exc)
            return False

    for pid in sorted(measured) + [p for p in sorted(unknown) if p not in measured]:
        hint = measured.get(pid)
        part = base.GetEntity(deck, "ANSAPART", pid)
        faces = base.CollectEntities(deck, part, "FACE") if part else []
        entry = {"part": pid, "name": part._name if part else None,
                 "T_source": "skin" if hint is not None else "midsurf"}
        if not faces:
            entry.update({"status": "no_faces", "reason": "part has no faces"})
            pass_b.append(entry)
            continue

        if hint is not None:
            hints = [hint, round(hint * 0.85, 2), 0.6]
        else:
            # No Skin thickness to hint with: try the common automotive gauges
            # and let MidSurfAuto correct them. The hint is not the result.
            hints = [1.0, 0.8, 1.5, 2.0, 0.6]

        ret = None
        # Snapshot the part set: ret.type does NOT reliably list every part the
        # call creates. In the class5 run 71 reported output parts held 66,084
        # shells while the model ended up with 69,352 and three properties were
        # never measured at all - so the created parts are found by diffing, and
        # ret.type is kept only as diagnostics.
        before_parts = {p._id for p in base.CollectEntities(deck, None, "ANSAPART")}
        for thick in hints:
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
            entry.update({"status": "midsurf_failed",
                          "reason": "MidSurfAuto raised on every hint"})
            pass_b.append(entry)
            continue

        reported = []
        for row in (ret.type or []):
            try:
                oid, rtype = row[1], row[2]
            except Exception:
                continue
            if oid and oid not in reported:
                reported.append(oid)
                entry.setdefault("output_types", []).append(rtype)
        created = [p._id for p in base.CollectEntities(deck, None, "ANSAPART")
                   if p._id not in before_parts]
        all_out = list(dict.fromkeys(reported + created))
        entry["n_reported_outputs"] = len(reported)
        entry["n_created_parts"] = len(created)
        entry["n_elements"] = len(ret.ents) if ret.ents else 0

        if not all_out:
            entry.update({
                "status": "not_shellable",
                "reason": "both Skin and MidSurfAuto produced nothing; treat as "
                          "a solid/casting, not a panel",
            })
            pass_b.append(entry)
            continue

        items = [m for m in (measure_output(oid) for oid in all_out) if m]
        if not items:
            entry.update({"status": "no_shells",
                          "reason": "MidSurfAuto created part(s) but they carry "
                                    "no shells"})
            pass_b.append(entry)
            continue
        if all(i.get("no_data") for i in items):
            entry.update({"status": "no_thickness_read",
                          "reason": "mesh created but GetShellThickness returned "
                                    "nothing on any output part"})
            mid_parts.extend(all_out)
            meshed_src.add(pid)
            pass_b.append(entry)
            continue

        for i in items:
            i.pop("shells", None)
        entry["n_outputs"] = len(items)
        entry["outputs"] = items
        # The input part is uniform only if every one of its skins is.
        worst = max(items, key=lambda i: i.get("spread", 0))
        entry["T_measured"] = worst.get("T")
        entry["T_range"] = worst.get("T_range")
        entry["T_range_raw"] = worst.get("T_range_raw")
        entry["n_sampled"] = sum(int(i.get("n_sampled") or 0) for i in items)
        entry["spread_pct"] = worst.get("spread_pct")
        entry["uniform"] = all(bool(i.get("uniform")) for i in items)
        spread = float(worst.get("spread") or 0.0)
        raw_spread = 0.0
        for i in items:
            lo_r, hi_r = i.get("T_range_raw") or [0, 0]
            t_i = max(float(i.get("T") or 1e-9), 1e-9)
            raw_spread = max(raw_spread, (hi_r - lo_r) / t_i)

        if entry["uniform"]:
            results = [write_T(i) for i in items]
            entry["T_written"] = items[0].get("T_written")
            entry["pid"] = items[0].get("pid")
            if all(results):
                entry["status"] = "meshed"
            else:
                # A write that silently fails must not leave the part reporting
                # success - that is how a default gauge reaches the export.
                entry["status"] = "t_write_failed"
                entry["reason"] = ("thickness is uniform but writing T failed on "
                                   "%d of %d output part(s)"
                                   % (results.count(False), len(items)))
        elif var_policy == "write_median":
            results = [write_T(i) for i in items]
            entry["T_written"] = items[0].get("T_written")
            entry["pid"] = items[0].get("pid")
            entry["status"] = ("meshed_median" if all(results)
                               else "t_write_failed")
            entry["reason"] = ("thickness spread %.1f%% over tolerance; median "
                               "written for %d output part(s) on request - check "
                               "this part" % (100.0 * spread, len(items)))
        elif var_policy == "drop":
            # Leave the part out of the shell deliverable and keep its solid
            # faces, so it can be modelled by hand instead of shipping a
            # default 1.0 mm gauge that looks legitimate.
            n_del = 0
            for oid in all_out:
                op = base.GetEntity(deck, "ANSAPART", oid)
                sh = base.CollectEntities(deck, op, "SHELL") if op is not None else []
                if not sh:
                    continue
                try:
                    base.DeleteEntity(sh)
                    n_del += len(sh)
                except Exception as exc:
                    entry["drop_err"] = str(exc)
            entry["status"] = "deferred_manual"
            entry["n_elements_dropped"] = n_del
            entry["reason"] = ("thickness spread %.1f%% over tolerance; "
                               "%d shell(s) dropped, solid faces kept for manual "
                               "modelling" % (100.0 * spread, n_del))
            pass_b.append(entry)
            continue
        else:
            # block: keep every shell, write T only where it is defensible
            for i in items:
                if i.get("uniform"):
                    write_T(i)
            entry["status"] = "meshed_variable"
            entry["reason"] = ("thickness spread %.1f%% (p2-p98) vs raw %.1f%%; "
                               "T left unwritten on the parts that vary, shells "
                               "still exported with the default 1.0 mm gauge"
                               % (100.0 * spread, 100.0 * raw_spread))

        mid_parts.extend(all_out)
        meshed_src.add(pid)
        pass_b.append(entry)

    # ------------------------------------------------------------------ sweep
    # Belt and braces: MidSurfAuto does not reliably report every part it
    # creates, so any part with shells that no entry claimed is still sitting on
    # the mesher's default gauge. Resolve it here rather than discovering it in
    # the exported deck.
    claimed = set()
    for r in pass_b:
        for o in (r.get("outputs") or []):
            if o.get("out_id") is not None:
                claimed.add(o["out_id"])
    strays = []
    for p in base.CollectEntities(deck, None, "ANSAPART"):
        if p._id in claimed:
            continue
        sh = base.CollectEntities(deck, p, "SHELL")
        if sh:
            strays.append((p._id, sh))
    strays_fixed = 0
    for sid, sh in strays:
        item = measure_output(sid)
        if not item or item.get("no_data"):
            continue
        item.pop("shells", None)
        if item.get("uniform") and write_T(item):
            strays_fixed += 1
        elif var_policy == "drop":
            try:
                base.DeleteEntity(sh)
                strays_fixed += 1
            except Exception:
                pass
    strays_found = len(strays)
    strays_reason = ("MidSurfAuto created %d part(s) it did not report; %d were "
                     "given a measured T or dropped" % (len(strays), strays_fixed)
                     if strays else None)

    # ---------------------------------------------------------------- clean up
    faces_deleted = 0
    if delete_solids and mid_parts:
        keep = set()
        for opid in mid_parts:
            op = base.GetEntity(deck, "ANSAPART", opid)
            if op is not None:
                keep.update(f._id for f in base.CollectEntities(deck, op, "FACE"))
        doomed = []
        for pid in sorted(meshed_src):
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

    by_status = {}
    for r in pass_b:
        by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1

    review = [{"part": r["part"], "name": r["name"], "status": r.get("status"),
               "reason": r.get("reason"), "T_range": r.get("T_range"),
               "T_range_raw": r.get("T_range_raw"),
               "spread_pct": r.get("spread_pct")}
              for r in pass_b
              if r.get("status") in ("meshed_variable", "meshed_median",
                                     "deferred_manual", "not_shellable",
                                     "midsurf_failed", "no_thickness_read",
                                     "t_write_failed", "no_shells")]

    # Shells that survived with no measured T, counted from the actual PSHELL
    # state rather than from the status bookkeeping: a part can report "meshed"
    # and still leave a second skin on the mesher's default gauge, so the deck
    # has to be measured, not inferred.
    elements_without_T = 0
    untracked_props = []
    for pr in base.CollectEntities(deck, None, "__PROPERTIES__"):
        try:
            if pr.ansa_type(deck) != "PSHELL":
                continue
        except Exception:
            continue
        sh = base.CollectEntities(deck, pr, "SHELL")
        if not sh or int(pr._id) in written_pids:
            continue
        try:
            t = base.GetEntityCardValues(deck, pr, ["T"]).get("T")
        except Exception:
            t = None
        elements_without_T += len(sh)
        untracked_props.append({"pid": int(pr._id), "n_shells": len(sh),
                                "T": t, "name": pr._name})
        # Make the hazard visible in the saved model too: a property called
        # SHELL_T1 is indistinguishable from a real 1.0 mm gauge, one called
        # SHELL_UNVERIFIED_T is not.
        try:
            base.SetEntityCardValues(deck, pr,
                                     {"Name": "%s_UNVERIFIED_T" % prefix})
        except Exception:
            pass
    deferred = [{"part": r["part"], "name": r["name"], "T_median": r.get("T_measured"),
                 "T_range": r.get("T_range"), "spread_pct": r.get("spread_pct"),
                 "n_outputs": r.get("n_outputs")}
                for r in pass_b if r.get("status") == "deferred_manual"]

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
        "parts_by_status": by_status,
        "n_parts_needing_review": len(review),
        "elements_without_T": elements_without_T,
        "props_without_T": untracked_props,
        "strays_found": strays_found,
        "strays_resolved": strays_fixed,
        "strays_note": strays_reason,
        "n_parts_deferred_manual": len(deferred),
        "variable_thickness_policy": var_policy,
        "from_midsurf_fallback": sum(1 for r in pass_b
                                     if r.get("T_source") == "midsurf"
                                     and r.get("status", "").startswith("meshed")),
    }
    warnings = []
    if elements_without_T:
        warnings.append(
            "%d shell(s) on %d property(ies) have no measured T and still carry "
            "the mesher default gauge in every export - do not run an analysis "
            "on this deck until they are resolved"
            % (elements_without_T, len(untracked_props)))

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
        "max_thickness": max_t,
        "pass_a": pass_a,
        "pass_b": pass_b,
        "review": review,
        "deferred_manual": deferred,
        "warnings": warnings,
        "verify": verify,
        "files": files,
        "caveats": [
            "equivalent edge lengths come out 4-6% below the target on curved "
            "geometry; this is normal mesher behaviour, not a wrong setting",
            "each part is meshed on its own, so nodes are NOT shared between "
            "parts; define connections separately",
            "max_thickness is a hard filter: a wall thicker than the cap makes "
            "Skin return 0, which is indistinguishable from a solid",
            "parts listed in review were NOT given a trustworthy thickness; do "
            "not ship them without a decision",
        ],
    }


RESULT = run(globals().get("PARAMS", {}) or {})
RESULT

"""Realise seam welds as node-shared QUAD strips — the weld that carries load.

The connection manager can build the weld out of shell elements instead of rigid
links, bars or bricks. The pattern that matters is `OVERLAP-SHELL`: the quads it
creates sit on **existing plate nodes**, so the weld bridges the two parts through
a shared mesh rather than through spiders. Measured on the reference model:
4 073 new shells, of which 3 339 span two different parts, quad short edge
~4.7 mm against W = 4 mm, long edge following the 5 mm plate mesh.

Other joint-type patterns in the same family (`T-JOINT-SHELL`, `EDGEWELD-SHELL`,
`LASERWELD-SHELL`, `CRIMPWELD-SHELL`, `Y-JOINT-SHELL`) only build a toe strip -
about 90 shells for the whole model - so they are not weld bridges. `OVERLAP-SHELL`
also only builds where the joint really is an overlap: 108 of 133 seams on the
reference model, and the rest are reported, not silently skipped.

**Two side effects must be counted, not discovered later:**

  * it locally re-meshes the plate around the seam - new shells appear that are
    not weld quads
  * it re-assigns a band of plate shells to the per-weld property: 1 601 elements
    on the reference model kept their thickness but changed material id

`snapshot()`/`compare()` measure both by reading element -> property before and
after; grouping by property keeps it to a couple of hundred API calls instead of
one per element.
"""

import json

KINDS = ("SHELL", "RBE2", "RBE3", "SOLID", "CBAR")


def snapshot(deck):
    """{element id: (property id, property entity)} for every shell.

    The entity is kept as well as the id because restoring an assignment needs the
    property entity, not its number, and rebuilding 2 000 of them one by one from
    ids is slower than holding the handles.
    """
    m = {}
    for p in base.CollectEntities(deck, None, "__PROPERTIES__"):
        for e in base.CollectEntities(deck, p, "SHELL") or []:
            m[e._id] = (p._id, p)
    return m


def compare(before, after):
    new_ids = [e for e in after if e not in before]
    reprop = [e for e in after if e in before and after[e][0] != before[e][0]]
    lost = [e for e in before if e not in after]
    return {"new_shells": len(new_ids), "repropertied": len(reprop),
            "removed": len(lost)}


def restore(snapshot_before, deck):
    """Put the original property back on every shell the pattern re-assigned.

    Nothing in the connection manager does this: `EraseConnectionFeRep` removes the
    FE entities it owns and leaves the re-propertied plate shells carrying the weld
    property. Undoing it means writing the old PID back, which is only possible if
    the assignment was snapshotted first.
    """
    fixed = 0
    for eid, (pid, pend) in snapshot_before.items():
        e = base.GetEntity(deck, "SHELL", eid)
        if e is None:
            continue
        cur = (e.get_entity_values(deck, ["PID"]) or {}).get("PID")
        if cur is None or getattr(cur, "_id", None) == pid:
            continue
        e.set_entity_values(deck, {"PID": pend})
        fixed += 1
    return fixed


def census(deck):
    return {t: len(base.CollectEntities(deck, None, t)) for t in KINDS}


def run(params):
    deck = constants.NASTRAN
    if params.get("model"):
        base.Open(params["model"])
    conns = base.CollectEntities(deck, None, "SeamLine_Type")
    if not conns:
        print("no seam connections in the model")
        return {"connections": 0}
    print("%d seam connections" % len(conns))

    fe_rep = params.get("fe_rep", "OVERLAP-SHELL")
    width = params.get("width", 4.0)
    # Single row is the default here on purpose: the pattern's own default is
    # "Double Row", which builds one quad row per plate and meets at a ridge -
    # correct for a lap joint but it reads as duplicated elements. Pass
    # "Double Row" to get the shipped behaviour back.
    root = params.get("root_shells", "Single Row")
    if root:
        rc = connections.SetDefault(("SeamLine_%s_RootShells" % fe_rep.replace("-", "_"), root))
        print("  RootShells = %s (rc=%s)" % (root, rc))
    if params.get("step_length") not in (None, ""):
        rc = connections.SetDefault(("SeamLine_%s_StepLength" % fe_rep.replace("-", "_"),
                                     str(params["step_length"])))
        print("  StepLength = %s (rc=%s)" % (params["step_length"], rc))

    try:
        connections.EraseConnectionFeRep(conns)
    except Exception as exc:  # noqa: BLE001
        print("  erase FE rep: %s" % exc)
    for c in conns:
        try:
            c.set_entity_values(deck, {"W": float(width)})
        except Exception:  # noqa: BLE001
            pass
    print("W = %.2f mm" % float(width))

    bad = connections.SetWeldPosition(conns, "Automatic")
    print("weld positions failed: %d" % len(bad or []))

    before = snapshot(deck)
    c0 = census(deck)
    n = connections.RealizeConnections(conns, {"SeamLine_Type": fe_rep})
    after = snapshot(deck)
    c1 = census(deck)

    delta = compare(before, after)
    print("realized %s/%d with %s" % (n, len(conns), fe_rep))
    print("  shells +%d (new %d, re-meshed/removed %d)"
          % (c1["SHELL"] - c0["SHELL"], delta["new_shells"], delta["removed"]))
    print("  re-propertied plate shells: %d" % delta["repropertied"])
    print("  RBE2 %d, RBE3 %d" % (c1["RBE2"], c1["RBE3"]))

    failed = []
    for c in conns:
        try:
            fe = connections.GetFirstFeRep(c)
        except Exception:  # noqa: BLE001
            fe = None
        if not fe:
            failed.append(c._id)
    print("  connections without FE: %d %s" % (len(failed), failed[:10]))

    # Second pass: the joint patterns only accept their own joint, and no search
    # tolerance changes that - `SearchDist` 5/10/20 mm and `Parameters_Gap`
    # 0.5-5 mm all made it worse on the reference model. What does cover the rest
    # is the spider-tied strip, which has no root-geometry requirement: 22 of 22
    # on the seams the quad pattern refused.
    fb_rep = params.get("fallback_rep")
    fallback_done = 0
    if fb_rep and failed:
        fset = set(failed)
        todo = [c for c in conns if c._id in fset]
        connections.SetWeldPosition(todo, "Automatic")
        fallback_done = connections.RealizeConnections(todo, {"SeamLine_Type": fb_rep}) or 0
        print("  fallback %s realised %d of %d" % (fb_rep, fallback_done, len(todo)))
        failed = []
        for c in todo:
            try:
                fe = connections.GetFirstFeRep(c)
            except Exception:  # noqa: BLE001
                fe = None
            if not fe:
                failed.append(c._id)
        print("  still without FE after fallback: %d %s" % (len(failed), failed[:10]))

    report = {
        "fe_rep": fe_rep,
        "fallback_rep": fb_rep,
        "fallback_realized": fallback_done,
        "width": float(width),
        "connections": len(conns),
        "realized": n,
        "shell_delta": c1["SHELL"] - c0["SHELL"],
        "new_shells": delta["new_shells"],
        "removed_shells": delta["removed"],
        "repropertied": delta["repropertied"],
        "rbe2": c1["RBE2"],
        "rbe3": c1["RBE3"],
        "weld_positions_failed": len(bad or []),
        "no_fe_connections": failed,
    }

    if params.get("restore_properties"):
        fixed = restore(before, deck)
        d2 = compare(before, snapshot(deck))
        c1 = census(deck)
        print("restored %d plate shells to their original property (%d still changed)"
              % (fixed, d2["repropertied"]))
        report["restored"] = fixed
        report["repropertied_after_restore"] = d2["repropertied"]

    if params.get("export_nas"):
        base.OutputNastran(filename=params["export_nas"])
        report["export_nas"] = params["export_nas"]
        print("exported " + params["export_nas"])
    if params.get("save_ansa"):
        report["save_ansa_rc"] = base.SaveAs(params["save_ansa"])
        print("saved " + params["save_ansa"])

    base.RedrawAll()
    if params.get("out_json"):
        with open(params["out_json"], "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print("wrote " + params["out_json"])
    return report


TPARAMS = {
    "model": r"D:/out/parts/seam_welded.ansa",
    "fe_rep": "OVERLAP-SHELL",
    "width": 4.0,
    "root_shells": "Single Row",
    "fallback_rep": "RBE3-SHELL-RBE3",
    "step_length": None,
    "restore_properties": True,
    "export_nas": r"D:/out/weld/seam_welded_quad.nas",
    "save_ansa": r"D:/out/weld/seam_welded_quad.ansa",
    "out_json": r"D:/out/weld/_shell_rep.json",
}
if globals().get("_SEAM_AUTORUN", True):
    RESULT = run(TPARAMS)

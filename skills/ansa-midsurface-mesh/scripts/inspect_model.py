"""Read-only inspection of the active ANSA model.

Run inside ANSA through the `ansa` MCP server:

    exec(open(r"<skill>/scripts/inspect_model.py", encoding="utf-8").read(),
         globals())
    RESULT

Answers the questions that decide the whole strategy: is this a closed
thin-walled CAD description or an existing shell model, and how are the parts
organised. It never modifies the database.

Optional `PARAMS` keys (injected before the exec):
    bbox_top_n   how many parts to list by area, default 20
"""

import json

try:
    base  # noqa: F821  provided by ansa_run_python
except NameError:  # pragma: no cover - only when run outside ANSA
    from ansa import base, constants, mesh, session, utils  # noqa: F401

PARAMS = globals().get("PARAMS", {}) or {}
TOP_N = int(PARAMS.get("bbox_top_n", 20))

deck = base.CurrentDeck()

PROBE_TYPES = ("FACE", "CONS", "POINT", "CURVE", "SHELL", "SOLID", "GRID",
               "NODE", "ANSAPART", "PART", "SET", "GROUP",
               "PSHELL", "PSOLID", "MAT1", "__PROPERTIES__", "__MATERIALS__")

counts = {}
for t in PROBE_TYPES:
    try:
        counts[t] = len(base.CollectEntities(deck, None, t))
    except Exception as exc:
        counts[t] = "ERR:%s" % exc

parts = []
for p in base.CollectEntities(deck, None, "ANSAPART"):
    faces = base.CollectEntities(deck, p, "FACE")
    shells = base.CollectEntities(deck, p, "SHELL")
    entry = {"part": p._id, "name": p._name,
             "faces": len(faces), "shells": len(shells)}
    if faces:
        try:
            bb = base.BoundBox(faces)
            entry["dims"] = [round(bb[3] - bb[0], 2),
                             round(bb[4] - bb[1], 2),
                             round(bb[5] - bb[2], 2)]
            entry["area"] = round(sum(base.GetFaceArea(f) for f in faces), 1)
        except Exception as exc:
            entry["measure_err"] = str(exc)
        # ANSA's own opinion. Useful as a hint only: the result is not
        # monotonic in the tolerance, so never make a decision from it alone.
        for tol in (0.3,):
            try:
                entry["is_part_for_skin"] = base.IsPartForSkin(p, tol)
            except Exception:
                pass
    parts.append(entry)

parts.sort(key=lambda r: -(r.get("area") or 0))

props = []
for pr in base.CollectEntities(deck, None, "__PROPERTIES__"):
    t = base.GetEntityCardValues(deck, pr, ["T"]).get("T") \
        if pr.ansa_type(deck) == "PSHELL" else None
    props.append({"pid": pr._id, "type": pr.ansa_type(deck),
                  "name": pr._name,
                  "T": str(t) if t is not None else None,
                  "shells": len(base.CollectEntities(deck, pr, "SHELL"))})

mats = []
for m in base.CollectEntities(deck, None, "__MATERIALS__"):
    fields = [f for f in ("E", "NU", "RHO") if f in list(m.card_fields(deck))]
    v = base.GetEntityCardValues(deck, m, fields) if fields else {}
    mats.append({"mid": m._id, "name": m._name,
                 "values": {k: str(x) for k, x in v.items()}})

RESULT = {
    "deck": deck,
    "counts": counts,
    "verdict": {
        "closed_cad_description": counts.get("SOLID", 0) == 0
                                   and counts.get("SHELL", 0) == 0
                                   and counts.get("FACE", 0) > 0,
        "already_shell_model": counts.get("SHELL", 0) > 0,
    },
    "n_parts": len(parts),
    "parts": parts[:TOP_N],
    "properties": props,
    "materials": mats,
}
RESULT

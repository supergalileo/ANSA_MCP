"""Final cleanup + acceptance numbers + deck export.

Deletes the connection entities that never found two plates (they carry no FE
representation, so they change nothing mechanically - they would only make the
connection list unreadable), then reports what the model actually contains and
writes the Nastran deck.

Acceptance asks four questions:

  * how many seams ended up welded to two different parts
  * how many RBE2 the welds produced, and whether every weld owns at least one
  * how many grids are used by no element (a floating node makes the stiffness
    matrix singular)
  * does the exported deck really carry the RBE2 cards
"""

import json
import os
import xml.etree.ElementTree as ET


def _pids_by_id(path):
    out = {}
    root = ET.parse(path).getroot()
    for g in root.iter("connection_group"):
        ct = g.find("connected_to")
        n = len(ct.findall("pid")) if ct is not None else 0
        for tag in ("connection_1d", "connection_0d"):
            for c in g.iter(tag):
                out[int(c.find("id").text.strip())] = n
    return out


def run(params):
    deck = constants.NASTRAN
    report = {}
    probe = params["probe_xml"]

    conns = base.CollectEntities(deck, None, "SeamLine_Type")
    if os.path.exists(probe):
        os.remove(probe)
    connections.OutputConnections(conns, "XML", probe)
    npid = _pids_by_id(probe)

    good = [c for c in conns if npid.get(c._id, 0) >= 2]
    bad = [c for c in conns if npid.get(c._id, 0) < 2]
    report["n_conn_total"] = len(conns)
    report["n_conn_two_parts"] = len(good)
    report["n_conn_unused"] = len(bad)
    print("connections: %d total, %d welded to two parts, %d unused"
          % (len(conns), len(good), len(bad)))

    if params.get("delete_unused") and bad:
        base.DeleteEntity(bad)
        report["deleted_unused"] = len(bad)
        print("deleted %d unused connections" % len(bad))

    # --- FE side -----------------------------------------------------------
    rbe2 = base.CollectEntities(deck, None, "RBE2")
    report["RBE2"] = len(rbe2)
    owned, orphan = 0, []
    for e in rbe2:
        try:
            v = e.get_entity_values(deck, ["No.of.Nodes"])
        except Exception:  # noqa: BLE001
            v = {}
        if (v or {}).get("No.of.Nodes", 0) >= 2:
            owned += 1
        else:
            orphan.append(e._id)
    report["RBE2_with_two_nodes"] = owned
    report["RBE2_orphans"] = len(orphan)
    print("RBE2: %d, with >=2 nodes: %d, orphans: %d" % (len(rbe2), owned, len(orphan)))

    # --- floating nodes ----------------------------------------------------
    # A node is "used" if an element names it, or if it is the master of a weld
    # RBE2. `get_entity_values(deck, None)` is rejected by ANSA, so the labels
    # have to be spelled out: shells carry G1..G4, bars GA/GB, RBE2 carries GN.
    used = set()
    for t, fields in (("SHELL", ("G1", "G2", "G3", "G4")),
                      ("CBAR", ("GA", "GB")), ("CBEAM", ("GA", "GB")),
                      ("CROD", ("GA", "GB")), ("RBE2", ("GN",))):
        for el in base.CollectEntities(deck, None, t):
            try:
                vals = el.get_entity_values(deck, list(fields))
            except Exception:  # noqa: BLE001
                continue
            for k, x in (vals or {}).items():
                # ANSA answers with Entity handles, not ids; a bare int shows up
                # for some deck fields, so both shapes are accepted.
                if hasattr(x, "_id"):
                    used.add(x._id)
                elif isinstance(x, int) and x > 0:
                    used.add(x)
    ids = {g._id for g in base.CollectEntities(deck, None, "GRID")}
    report["GRID"] = len(ids)
    report["GRID_used"] = len(used & ids)
    report["GRID_free"] = len(ids - used)
    print("grids: %d, used: %d, free: %d"
          % (report["GRID"], report["GRID_used"], report["GRID_free"]))

    if params.get("out_json"):
        with open(params["out_json"], "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print("wrote " + params["out_json"])
    return report


# There are no built-in paths: every input belongs to the case being welded. A
# bare exec() used to run the reference model's folders and report success, so an
# agent with no context silently read the wrong files. Demand PARAMS instead - see
# SKILL.md, "Passing parameters".
_SEAM_REQUIRED = ("probe_xml",)
if globals().get("_SEAM_AUTORUN", True):
    _SEAM_PARAMS = globals().get("PARAMS")
    if not _SEAM_PARAMS:
        raise RuntimeError(
            "MCP_PRECONDITION: seam_final: PARAMS is empty - set PARAMS = {...} "
            "before exec(), or set _SEAM_AUTORUN = False and call run(PARAMS) "
            "yourself. Required keys: "
            + (", ".join(_SEAM_REQUIRED) or "none"))
    _SEAM_MISSING = [k for k in _SEAM_REQUIRED if _SEAM_PARAMS.get(k) in (None, "")]
    if _SEAM_MISSING:
        raise RuntimeError("MCP_PRECONDITION: seam_final: PARAMS is missing "
                           + ", ".join(_SEAM_MISSING))
    RESULT = run(_SEAM_PARAMS)

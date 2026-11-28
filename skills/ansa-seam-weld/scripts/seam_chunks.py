"""Last resort for seam lines ANSA refuses to weld: cut them into short chunks.

A 118 mm bead that doubles back on itself is one continuous curve, and ANSA's
automatic weld positioning gives up on the whole thing when one stretch of it
cannot be projected. Cutting the same line into ~20 mm pieces localises the
problem: the pieces that can be welded are welded, and only the truly bad stretch
stays empty.

This is still a SeamLine connection through the connection manager - the curve
just arrives in more than one piece. Order matters: the original connection is
deleted only after its chunks are built, so a failed split cannot lose the seam
entirely.
"""

import json
import math
import os
import xml.etree.ElementTree as ET

CHUNK = 20.0


def _pts_of(conn, n=40):
    pts = []
    try:
        curves = connections.CollectConnectionCurves(conn) or []
    except Exception:  # noqa: BLE001
        curves = []
    for c in curves:
        for i in range(n):
            try:
                p = base.EvaluateCurvePoint(c, i / (n - 1.0))
            except Exception:  # noqa: BLE001
                p = None
            if p and (not pts or math.dist(p, pts[-1]) > 1e-3):
                pts.append(p)
    return pts


def _chunks(pts, size):
    out, cur, acc = [], [pts[0]], 0.0
    for a, b in zip(pts, pts[1:]):
        acc += math.dist(a, b)
        cur.append(b)
        if acc >= size:
            out.append(cur)
            cur = [b]
            acc = 0.0
    if len(cur) > 1:
        out.append(cur)
    return out


def _pids_by_id(path):
    out = {}
    if not os.path.exists(path):
        return out
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
    ids = json.load(open(params["bad_json"], encoding="utf-8"))
    size = float(params.get("chunk", CHUNK))
    report = {"input": len(ids), "built": 0, "chunks": 0, "deleted": 0}

    made, originals = [], []
    for cid in ids:
        conn = base.GetEntity(deck, "SeamLine_Type", int(cid))
        if conn is None:
            continue
        pts = _pts_of(conn, int(params.get("samples", 40)))
        if len(pts) < 2:
            continue
        pieces = _chunks(pts, size)
        new = []
        for piece in pieces:
            if len(piece) < 2 or math.dist(piece[0], piece[-1]) < 0.5:
                continue
            try:
                curve = base.CreateCurve(len(piece), [p[0] for p in piece],
                                         [p[1] for p in piece], [p[2] for p in piece])
                c = connections.CreateConnectionLine("SeamLine_Type", [curve], 0, [])
            except Exception:  # noqa: BLE001
                continue
            if c is not None:
                new.append(c)
        if new:
            made.extend(new)
            originals.append(conn)
            report["built"] += 1
            report["chunks"] += len(new)
    print("chunked %d seams into %d pieces" % (report["built"], report["chunks"]))

    if made:
        steps = params.get("steps", [(3.0, 2), (5.0, 2)])
        pending = list(made)
        for dist, exact in steps:
            if not pending:
                break
            connections.AutoSetConnectivityInConnections(
                pending,
                search_distance=float(dist),
                find_nearest=int(exact),
                find_up_to=int(exact),
                find_exactly=int(exact),
                between_parts=False,
                clear_connectivity=True,
                output_mode="pid",
                search_faces=True,
                search_shells=True,
                search_solids=False,
                search_tubes=False,
            )
            probe = params["probe_xml"]
            if os.path.exists(probe):
                os.remove(probe)
            connections.OutputConnections(pending, "XML", probe)
            npid = _pids_by_id(probe)
            ok = [c for c in pending if npid.get(c._id, 0) >= exact]
            pending = [c for c in pending if npid.get(c._id, 0) < exact]
            print("chunks d=%-4s n=%-2s -> %d attached, %d left"
                  % (dist, exact, len(ok), len(pending)))
            report["attach_%s_%s" % (dist, exact)] = len(ok)
        left = pending

        good = [c for c in made if c not in left]
        if good:
            before = len(base.CollectEntities(deck, None, "RBE2"))
            n = connections.RealizeConnections(good, {"SeamLine_Type": "RBE2"})
            after = len(base.CollectEntities(deck, None, "RBE2"))
            report["realized"] = n
            report["rbe2_added"] = after - before
            print("chunks realized %d/%d, RBE2 +%d" % (n or 0, len(good), after - before))

    if params.get("delete_originals") and originals:
        base.DeleteEntity(originals)
        report["deleted"] = len(originals)
        print("deleted %d originals" % len(originals))

    if params.get("xml_out"):
        allc = base.CollectEntities(deck, None, "SeamLine_Type")
        report["xml_rc"] = connections.OutputConnections(allc, "XML", params["xml_out"])

    if params.get("out_json"):
        with open(params["out_json"], "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print("wrote " + params["out_json"])
    return report


# There are no built-in paths: every input belongs to the case being welded. A
# bare exec() used to run the reference model's folders and report success, so an
# agent with no context silently read the wrong files. Demand PARAMS instead - see
# SKILL.md, "Passing parameters".
_SEAM_REQUIRED = ("bad_json", "probe_xml")
if globals().get("_SEAM_AUTORUN", True):
    _SEAM_PARAMS = globals().get("PARAMS")
    if not _SEAM_PARAMS:
        raise RuntimeError(
            "MCP_PRECONDITION: seam_chunks: PARAMS is empty - set PARAMS = {...} "
            "before exec(), or set _SEAM_AUTORUN = False and call run(PARAMS) "
            "yourself. Required keys: "
            + (", ".join(_SEAM_REQUIRED) or "none"))
    _SEAM_MISSING = [k for k in _SEAM_REQUIRED if _SEAM_PARAMS.get(k) in (None, "")]
    if _SEAM_MISSING:
        raise RuntimeError("MCP_PRECONDITION: seam_chunks: PARAMS is missing "
                           + ", ".join(_SEAM_MISSING))
    RESULT = run(_SEAM_PARAMS)

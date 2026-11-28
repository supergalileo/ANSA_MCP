"""Model-state tools: snapshot, diff and a structural verification checklist.

These three exist because every skill in this repo was re-deriving the same
questions by hand: *what did that operation actually change* and *is the model
still sound*. The answers were written as throw-away loops each time, which means
they were forgotten, and one of them (1 971 plate shells silently re-assigned to a
weld property) went unnoticed until somebody happened to look.

All three run inside ANSA and keep the bulky maps in ``ansavars`` so a 65 000-element
model never crosses the wire: only counts and a few sample ids come back.

They are read-only. Snapshot stores state; diff compares; verify reports. None of
them edits the model, so none of them is idempotent-tracked as a mutation.
"""

import json

# Entity types worth counting and comparing. CBAR is how ANSA reports beam
# elements; SOLID covers CHEXA/CPENTA/CTETRA alike.
ELEM_TYPES = ("SHELL", "SOLID", "CBAR")
CENSUS_TYPES = ("GRID", "SHELL", "SOLID", "CBAR", "RBE2", "RBE3", "MAT1",
                "__PROPERTIES__", "ANSAPART")

# Checks whose cost grows with the element count. Measured on 63 000 shells:
# `free` 0.6 s, `quality` 1.5 s - slow in theory, affordable in practice, which is
# why the server documents them as "walk every element" rather than warning people
# off them.
SLOW_CHECKS = ("free", "quality")

_HELPERS = r'''
import json as _json
_D = constants.NASTRAN

def _census():
    out = {}
    for t in %(census)s:
        try:
            out[t] = len(base.CollectEntities(_D, None, t))
        except Exception:
            out[t] = -1
    return out

def _elem_pid_map():
    """{type: {eid: pid}} - built per property, so 20 calls instead of 65 000."""
    out = {}
    props = base.CollectEntities(_D, None, "__PROPERTIES__")
    for t in %(elem_types)s:
        m = {}
        for p in props:
            for e in base.CollectEntities(_D, p, t) or []:
                m[e._id] = p._id
        if m:
            out[t] = m
    return out

def _prop_map():
    out = {}
    for p in base.CollectEntities(_D, None, "__PROPERTIES__"):
        v = p.get_entity_values(_D, ["PID", "T", "MID"]) or {}
        out[str(p._id)] = {"T": v.get("T"),
                           "MID": getattr(v.get("MID"), "_id", None)}
    return out
''' % {"census": repr(list(CENSUS_TYPES)), "elem_types": repr(list(ELEM_TYPES))}


def snapshot_code(name: str) -> str:
    """Capture element->property, counts and property table under a name."""
    return _HELPERS + r'''
_store = ansavars.setdefault("_snapshots", {})
_cur = {"counts": _census(), "elems": _elem_pid_map(), "props": _prop_map()}
_store[%(name)s] = _cur
{
 "snapshot": %(name)s,
 "counts": _cur["counts"],
 "elements": {k: len(v) for k, v in _cur["elems"].items()},
 "properties": len(_cur["props"]),
 "grids": _cur["counts"]["GRID"],
}
''' % {"name": repr(name)}


def diff_code(name: str) -> str:
    """Compare the live model against a stored snapshot."""
    return _HELPERS + r'''
_store = ansavars.setdefault("_snapshots", {})
if %(name)s not in _store:
    _out = {"error": "no snapshot named %(name)s", "known": sorted(_store)}
else:
    _old = _store[%(name)s]
    _new = {"counts": _census(), "elems": _elem_pid_map(), "props": _prop_map()}
    _per_type = {}
    for _t in set(list(_old["elems"]) + list(_new["elems"])):
        _a = _old["elems"].get(_t, {})
        _b = _new["elems"].get(_t, {})
        _added = [e for e in _b if e not in _a]
        _removed = [e for e in _a if e not in _b]
        _repid = [e for e in _b if e in _a and _b[e] != _a[e]]
        if _added or _removed or _repid:
            _per_type[_t] = {
                "before": len(_a), "after": len(_b),
                "added": len(_added), "removed": len(_removed),
                "repropertied": len(_repid),
                "added_sample": sorted(_added)[:5],
                "repropertied_sample": [
                    [e, _a[e], _b[e]] for e in sorted(_repid)[:5]],
            }

    _props_added = [p for p in _new["props"] if p not in _old["props"]]
    _props_removed = [p for p in _old["props"] if p not in _new["props"]]
    _count_delta = {k: _new["counts"].get(k, 0) - _old["counts"].get(k, 0)
                    for k in _new["counts"]
                    if _new["counts"].get(k, 0) != _old["counts"].get(k, 0)}
    _out = {
     "snapshot": %(name)s,
     "counts_before": _old["counts"],
     "counts_after": _new["counts"],
     "count_delta": _count_delta,
     "by_type": _per_type,
     "properties_added": _props_added,
     "properties_removed": _props_removed,
    }
_out
''' % {"name": repr(name)}


_NATIVE_BLOCK = r'''
if _want("native"):
    import time as _t
    try:
        from ansa.base import checks as _bc
    except Exception:
        _bc = None
    if _bc is None:
        _res["native"] = {"advisory": True,
                          "status": "unavailable",
                          "detail": "ansa.base.checks not importable"}
    else:
        _specs = [
            ("free_nodes", "general", "FreeNodes"),
            ("connections", "general", "Connections"),
            ("undefined_materials", "general", "UndefinedMaterials"),
            ("undefined_properties", "general", "UndefinedProperties"),
            ("duplicate_elements", "general", "DuplicateElements"),
            ("rbe3_definition", "general", "Rbe3Definition"),
            ("mesh_quality", "mesh", "MeshQuality"),
            ("non_manifold", "mesh", "NonManifoldNodes"),
            ("intersections", "penetration", "Intersections"),
            ("proximities", "penetration", "Proximities"),
        ]
        _nat = {}
        for _key, _cat, _name in _specs:
            _t0 = _t.time()
            try:
                _chk = getattr(getattr(_bc, _cat), _name)()
                _reps = _chk.execute()
            except Exception as _e:
                _nat[_key] = {"status": "error", "detail": str(_e)[:160]}
                continue
            _rep = _reps[0] if _reps else None
            if _rep is None:
                _nat[_key] = {"status": "no-report",
                              "seconds": round(_t.time() - _t0, 2)}
                continue
            _issues = list(getattr(_rep, "issues", []) or [])
            _msgs, _codes, _ents = {}, {}, []
            for _it in _issues:
                _m = str(getattr(_it, "message", ""))[:90]
                _msgs[_m] = _msgs.get(_m, 0) + 1
                _c = str(getattr(_it, "err_code", ""))
                if _c:
                    _codes[_c] = _codes.get(_c, 0) + 1
                for _en in (getattr(_it, "entities", None) or []):
                    if len(_ents) < 12:
                        _ents.append(_en._id)
            _nat[_key] = {
                "status": str(getattr(_rep, "status", "")),
                "issues": len(_issues),
                "messages": dict(sorted(_msgs.items(), key=lambda kv: -kv[1])[:5]),
                "err_codes": dict(sorted(_codes.items(), key=lambda kv: -kv[1])[:5]),
                "entities": _ents,
                "seconds": round(_t.time() - _t0, 2),
            }
        _res["native"] = {
            "advisory": True,
            "note": "ANSA's own thresholds, and they are strict. Treat this group "
                    "as reference: an 'error' here is a place to look, not a "
                    "verdict that the model is unusable.",
            "checks": _nat,
        }
'''


def verify_code(checks) -> str:
    """Structural checklist. Returns {check: {status, detail...}}."""
    return _HELPERS + (r'''
_checks = %(checks)s
_res = {}

def _want(c):
    return c in _checks

# -- counts -------------------------------------------------------------------
_res["counts"] = {"status": "info", "detail": _census()}

# -- properties: orphaned properties and elements without one ------------------
if _want("props"):
    _used = {}
    for _t, _m in _elem_pid_map().items():
        for _pid in set(_m.values()):
            _used[_pid] = _used.get(_pid, 0) + 1
    _all = {p._id: (p.get_entity_values(_D, ["PID"]) or {}).get("PID")
            for p in base.CollectEntities(_D, None, "__PROPERTIES__")}
    _orphan = sorted(p for p in _all if p not in _used)
    _res["props"] = {
        "status": "warn" if _orphan else "pass",
        "orphan": _orphan[:20],
        "orphan_count": len(_orphan),
        "used_properties": len(_used),
    }

# -- materials: every property MID must exist ---------------------------------
if _want("mats"):
    _mats = {m._id for m in base.CollectEntities(_D, None, "MAT1")}
    _missing = []
    for p in base.CollectEntities(_D, None, "__PROPERTIES__"):
        v = p.get_entity_values(_D, ["PID", "MID"]) or {}
        mid = getattr(v.get("MID"), "_id", None)
        if mid is not None and abs(mid) not in _mats:
            _missing.append([p._id, mid])
    _res["mats"] = {"status": "fail" if _missing else "pass",
                    "missing": _missing[:20], "missing_count": len(_missing),
                    "mat1": len(_mats)}

# -- rigid elements: fewer than two nodes welds nothing -----------------------
if _want("rbe"):
    _bad = {}
    for _t in ("RBE2", "RBE3"):
        _n = []
        for e in base.CollectEntities(_D, None, _t):
            v = e.get_entity_values(_D, ["No.of.Nodes"]) or {}
            k = v.get("No.of.Nodes")
            if not k or k < 2:
                _n.append([e._id, k])
        if _n:
            _bad[_t] = {"count": len(_n), "sample": _n[:10]}
    _res["rbe"] = {"status": "fail" if _bad else "pass", "bad": _bad}

# -- free grids: referenced by nothing ----------------------------------------
if _want("free"):
    _used = set()
    for _t, _f in (("SHELL", ("G1", "G2", "G3", "G4")),
                   ("SOLID", ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")),
                   ("CBAR", ("GA", "GB"))):
        for e in base.CollectEntities(_D, None, _t):
            v = e.get_entity_values(_D, list(_f)) or {}
            for x in v.values():
                if hasattr(x, "_id"):
                    _used.add(abs(x._id))
    for _t in ("RBE2", "RBE3"):
        for e in base.CollectEntities(_D, None, _t):
            for g in base.CollectEntities(_D, e, "GRID") or []:
                _used.add(abs(g._id))
    _all = {abs(g._id) for g in base.CollectEntities(_D, None, "GRID")}
    _free = sorted(_all - _used)
    _res["free"] = {"status": "warn" if _free else "pass",
                    "free": len(_free), "total": len(_all),
                    "sample": _free[:10]}

# -- quality: zero-area shells and extreme aspect -----------------------------
if _want("quality"):
    import math as _math
    _coords = {}
    for g in base.CollectEntities(_D, None, "GRID"):
        c = g.get_entity_values(_D, ["X1", "X2", "X3"]) or {}
        _coords[abs(g._id)] = (c.get("X1", 0.0), c.get("X2", 0.0), c.get("X3", 0.0))
    _zero, _aspect = [], []
    _limit = 10.0
    for e in base.CollectEntities(_D, None, "SHELL"):
        v = e.get_entity_values(_D, ["G1", "G2", "G3", "G4"]) or {}
        pts = [_coords.get(abs(x._id)) for x in v.values() if hasattr(x, "_id")]
        pts = [p for p in pts if p is not None]
        if len(pts) < 3:
            continue
        # Newell vector: its magnitude is twice the true 3D area. A flat XY
        # shoelace would call every vertical shell degenerate.
        _nx = _ny = _nz = 0.0
        for i in range(len(pts)):
            j = i + 1 if i + 1 < len(pts) else 0
            _nx += (pts[i][1] - pts[j][1]) * (pts[i][2] + pts[j][2])
            _ny += (pts[i][2] - pts[j][2]) * (pts[i][0] + pts[j][0])
            _nz += (pts[i][0] - pts[j][0]) * (pts[i][1] + pts[j][1])
        area = 0.5 * _math.sqrt(_nx * _nx + _ny * _ny + _nz * _nz)
        if area < 1e-8:
            _zero.append(e._id)
        edges = [_math.dist(pts[i], pts[i + 1 if i + 1 < len(pts) else 0])
                 for i in range(len(pts))]
        lo = min(edges)
        if lo > 0 and max(edges) / lo > _limit:
            _aspect.append([e._id, round(max(edges) / lo, 1)])
    _res["quality"] = {
        "status": "warn" if (_zero or _aspect) else "pass",
        "zero_area": len(_zero), "zero_area_sample": _zero[:10],
        "aspect_over": len(_aspect), "aspect_sample": _aspect[:10],
        "aspect_limit": _limit,
    }

# -- native ANSA checks -------------------------------------------------------
__NATIVE__

_missing = [c for c in _checks if c not in _res and c != "counts"]
if _missing:
    _res["unknown_checks"] = _missing
_res
''' % {"checks": repr(list(checks))}).replace("__NATIVE__", _NATIVE_BLOCK)

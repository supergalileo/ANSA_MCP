"""Realise seam welds as solid elements + RBE3 spiders (weld that carries load).

The connection manager can build the weld itself as a solid: FE representation
`RBE3-HEXA-RBE3` produces a CHEXA brick through the joint with an RBE3 spider on
each plate. This is the representation to use when the weld must transmit stress,
where the RBE2 default (two-node rigid links) or a CBAR is too crude.

Two things decide success, and both look like "the feature does not exist":

1. **`W` (weld width) must be non-zero.** The SeamLine card defaults to `W = 0`,
   and with a zero width the solid cannot be built: `RealizeConnections` returns
   **0** - not the RBE2 fallback that an unknown FE name produces, a silent
   nothing. Set `W` to the bead diameter before realising.
2. **The exported deck references a material that was never written.** The weld
   bricks get a new PSOLID whose MID points at a material created on the fly;
   that MAT1 exists in the database but `OutputNastran` does not write it, so the
   deck ships with `PSOLID 23 24` and no `MAT1 24`. `patch_materials()` appends
   the missing cards.

Behaviour worth knowing before reading the numbers: with a 4 mm weld on a 5 mm
mesh, 118 of 133 seams built as solids and the other 15 fell back to RBE2 in the
same call. Report the split; do not report a flat success.
"""

import json
import os
import re

KINDS = ("SOLID", "RBE2", "RBE3", "CBAR", "PSOLID", "MAT1")


def census(deck):
    return {t: len(base.CollectEntities(deck, None, t)) for t in KINDS}


_MAT = "MAT1,{mid},210000.,,0.3,,7.85-9"


def _rho_field(rho):
    """Nastran small-field wants `7.85-9`, not `7.85e-09`."""
    mant, exp = ("%.2e" % rho).split("e")
    return "%s%+d" % (mant, int(exp))


def _mat_line(mid, e_mod, nu, rho):
    """One MAT1 in small-field layout: six 8-column fields, MID first.

    Columns matter more than looks, but `%.6g` also keeps a space between the id
    and the modulus, which stops the card from reading like a typo.
    """
    return "MAT1    %8d%8.6g%8s%8.3f%8s\n" % (
        mid, e_mod, "", nu, _rho_field(rho))


def patch_materials(nas_path, e_mod=210000.0, nu=0.3, rho=7.85e-9):
    """Append a MAT1 for every MID a PSOLID points at but the deck never wrote.

    ANSA creates the weld material while realising and then leaves it out of the
    export, so the property dangles. A solver either errors or silently uses a
    default, which is worse.
    """
    with open(nas_path, encoding="utf-8", errors="ignore") as fh:
        lines = fh.readlines()

    mat_ids = set()
    solid_mids = set()
    for ln in lines:
        if ln.startswith("MAT1"):
            try:
                mat_ids.add(int(ln[8:16]))
            except ValueError:
                pass
        elif ln.startswith("PSOLID"):
            try:
                solid_mids.add(int(ln[16:24]))
            except ValueError:
                pass

    missing = sorted(solid_mids - mat_ids)
    if not missing:
        return 0

    new = [_mat_line(mid, e_mod, nu, rho) for mid in missing]

    out, done = [], False
    for ln in lines:
        if not done and ln.startswith("ENDDATA"):
            out.extend(new)
            done = True
        out.append(ln)
    if not done:
        out.extend(new)

    with open(nas_path, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    print("materials appended for MID %s" % missing)
    return len(missing)


def _apply_pattern_defaults(params):
    """Push pattern settings into the session defaults.

    The seam-weld pattern owns its own geometry parameters, named
    `<Type>_<Pattern>_<Field>` and stored in the ANSA profile defaults:

        SeamLine_RBE3_HEXA_RBE3_Height        weld thickness (mm)
        SeamLine_RBE3_HEXA_RBE3_StepLength    weld position spacing (mm)
        SeamLine_RBE3_HEXA_RBE3_NumOfLayers   elements through the thickness

    `connections.SetDefault` is the only way in, and it is **session-global**: the
    value survives `session.New()` and every later realisation, so a run that
    "leaves it alone" still inherits whatever the previous run set.
    """
    applied = {}
    for field, value in (("Height", params.get("height")),
                         ("StepLength", params.get("step_length")),
                         ("NumOfLayers", params.get("num_layers"))):
        if value in (None, ""):
            continue
        key = "SeamLine_RBE3_HEXA_RBE3_" + field
        rc = connections.SetDefault((key, str(value)))
        applied[key] = {"value": str(value), "rc": rc}
        print("  %s = %s (rc=%s)" % (key, value, rc))
    if params.get("height") in (None, ""):
        print("  Height not set: bricks follow the plate gap and collapse where "
              "the two mid-surfaces coincide")
    else:
        print("  weld thickness fixed at %s mm - bricks no longer follow the gap"
              % params["height"])
    return applied


def run(params):
    deck = constants.NASTRAN
    width = float(params.get("width", 4.0))
    fe_rep = params.get("fe_rep", "RBE3-HEXA-RBE3")
    conns = base.CollectEntities(deck, None, "SeamLine_Type")
    if not conns:
        print("no seam connections in the model")
        return {"connections": 0}

    before = census(deck)
    defaults = _apply_pattern_defaults(params)
    for c in conns:
        try:
            c.set_entity_values(deck, {"W": width})
        except Exception as exc:  # noqa: BLE001
            print("  W not set on %s: %s" % (c._id, exc))
    print("W = %.2f mm on %d connections" % (width, len(conns)))

    bad = connections.SetWeldPosition(conns, "Automatic")
    print("weld positions: %d failed" % len(bad or []))

    n = connections.RealizeConnections(conns, {"SeamLine_Type": fe_rep})
    after = census(deck)
    solids = after["SOLID"] - before["SOLID"]
    spiders = after["RBE3"] - before["RBE3"]
    rigid = after["RBE2"]
    print("realized %s/%d as %s: +%d solid, +%d RBE3" % (n, len(conns), fe_rep, solids, spiders))
    if rigid:
        print("  %d RBE2 left in the model - the connections the solid pattern "
              "could not build fall back to rigid links" % rigid)

    report = {
        "fe_rep": fe_rep,
        "width": width,
        "pattern_defaults": defaults,
        "connections": len(conns),
        "realized": n,
        "solid_added": solids,
        "rbe3_added": spiders,
        "rbe2_left": rigid,
        "weld_positions_failed": len(bad or []),
        "census": after,
    }

    if params.get("export_nas"):
        path = params["export_nas"]
        base.OutputNastran(filename=path)
        fixed = patch_materials(path)
        report["export_nas"] = path
        report["materials_patched"] = fixed
        print("exported %s (materials patched: %d)" % (path, fixed))

    if params.get("save_ansa"):
        report["save_ansa_rc"] = base.SaveAs(params["save_ansa"])
        print("saved " + params["save_ansa"])

    base.RedrawAll()
    if params.get("out_json"):
        with open(params["out_json"], "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print("wrote " + params["out_json"])
    return report


# There are no built-in paths: every input belongs to the case being welded. A
# bare exec() used to run the reference model's folders and report success, so an
# agent with no context silently read the wrong files. Demand PARAMS instead - see
# SKILL.md, "Passing parameters".
# No key is mandatory here (the model may already be open), but PARAMS must name
# the outputs - an empty dict is refused rather than silently realising nothing.
_SEAM_REQUIRED = ()
if globals().get("_SEAM_AUTORUN", True):
    _SEAM_PARAMS = globals().get("PARAMS")
    if not _SEAM_PARAMS:
        raise RuntimeError(
            "MCP_PRECONDITION: solid_rep: PARAMS is empty - set PARAMS = {...} "
            "before exec(), or set _SEAM_AUTORUN = False and call run(PARAMS) "
            "yourself. Required keys: "
            + (", ".join(_SEAM_REQUIRED) or "none"))
    _SEAM_MISSING = [k for k in _SEAM_REQUIRED if _SEAM_PARAMS.get(k) in (None, "")]
    if _SEAM_MISSING:
        raise RuntimeError("MCP_PRECONDITION: solid_rep: PARAMS is missing "
                           + ", ".join(_SEAM_MISSING))
    RESULT = run(_SEAM_PARAMS)

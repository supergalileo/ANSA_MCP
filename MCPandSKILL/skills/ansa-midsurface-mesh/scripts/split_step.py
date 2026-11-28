"""Split an assembly STEP into one STEP per part, and classify each part.

Why this exists
---------------
The mid-surface pipeline (`process_part.py`) takes one CAD file per part. A
supplier hands you one 51 MB STEP holding a whole tailgate: 45 PRODUCT,
177 MANIFOLD_SOLID_BREP, 79 assembly instances. Everything downstream needs
those 45 products as 45 files.

This is a **pure-Python STEP subsetter**: no ANSA, no license, no CAD kernel,
24 s for the 51 MB model. Verified against ANSA's own import on that model:
33 parts, 7 800 faces, face count identical to the source and every part
landing at its assembly position.

How it works
------------
1. index PRODUCT -> formation -> definition -> PDS -> SDR -> SHAPE_REPRESENTATION
   (CATIA uses `PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS` for parts that
   carry geometry and plain `PRODUCT_DEFINITION` for assembly nodes)
2. walk NAUO / CONTEXT_DEPENDENT_SHAPE_REPRESENTATION, accumulating one rigid
   transform per instance from ITEM_DEFINED_TRANSFORMATION
3. for each unique product take the dependency closure, **bake** the transform
   into every CARTESIAN_POINT / DIRECTION, and write a single-product STEP

Three things that are easy to get wrong, all of them cost a crash or an empty
import when missed:

- `VECTOR` must not be rewritten. Its arguments are `(name, #direction,
  magnitude)`, not coordinates. Transforming it corrupts the entity and ANSA
  dies with 0xC0000005 at ~40% of the import.
- `SHAPE_REPRESENTATION_RELATIONSHIP` is the bridge from the product's shell
  rep to the BREP rep and **nothing references it**. Leave it out and the
  importer sees a product with a placement and no geometry: 0 faces, no error.
- `APPLICATION_PROTOCOL_DEFINITION` has in-degree 0 and is mandatory. Drop it
  and CATIA-style readers refuse the file.

Bounding boxes come from `VERTEX_POINT` only. A CATIA B-spline carries control
points far outside its own geometry: on one 15 mm weld nut all-points gave
783 x 82 x 138 mm while the real body is 15 x 12 x 16 mm, which would classify
a weld nut as a panel.

Classification
--------------
Name first (fastener / weld vocabulary, when the CAD carries Chinese names),
then size. Thresholds are the ones measured on this tailgate plus the 540-part
body-in-white run in `references/field-notes.md`:

| class       | rule                                                        |
| ----------- | ----------------------------------------------------------- |
| `weld_spot` | largest bbox edge <= 15 mm                                  |
| `fastener`  | largest bbox edge <= 60 mm and area < 4 000 mm2              |
| `weld_seam` | slenderness >= 8 and area < 4 000 mm2                        |
| `sheet`     | everything else                                             |

These are a **first cut, not a verdict**: `process_part.py` re-decides with a
real thickness estimate, and a part it rejects lands on the exception list
where it belongs. The CSV carries area, bbox and face count so a human can
overrule any row.

Usage
-----
    python scripts/split_step.py --src tailgate.stp --out out/00_split
    python scripts/split_step.py --src tailgate.stp --out out/00_split --dry-run

Output
------
    out/00_split/01_sheet/<part>.stp
    out/00_split/02_fastener/<part>.stp
    out/00_split/03_weld_seam/<part>.stp
    out/00_split/04_weld_spot/<part>.stp
    out/00_split/09_unknown/<part>.stp
    out/00_split/_parts.csv
    out/00_split/_split.json

Then feed a class folder to the normal pipeline:

    python scripts/parallel_batch.py --src out/00_split/01_sheet --out out \
        --mpar 5MM.ansa_mpar --qual 5MM.ansa_qual --workers 4
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import time

# --------------------------------------------------------------------- parsing

_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def read_step(path):
    """Read `#id=TYPE(...)` lines. CATIA V5 writes one entity per line.

    Returns (entities, order). A line without a leading `#` (header, comments,
    the ISO wrapper) is skipped - the DATA section is the only thing indexed.
    """
    ents, order = {}, []
    with open(path, encoding="latin-1") as fh:
        for line in fh:
            s = line.strip()
            if not s.startswith("#"):
                continue
            i = s.find("=")
            if i < 0:
                continue
            try:
                eid = int(s[1:i])
            except ValueError:
                continue
            body = s[i + 1:].strip()
            if body.endswith(";"):
                body = body[:-1]
            ents[eid] = body
            order.append(eid)
    return ents, order


def etype(raw):
    if raw.startswith("("):
        return "COMPLEX"
    m = re.match(r"([A-Z0-9_]+)", raw)
    return m.group(1) if m else "?"


def refs(raw):
    return [int(x) for x in re.findall(r"#(\d+)", raw)]


def arg_split(raw):
    """Split top-level arguments: `TYPE(a,b,(c,d),.E.)` -> ["a","b","(c,d)",".E."]"""
    i = raw.find("(")
    if i < 0:
        return []
    depth, instr, out, cur = 0, False, [], []
    for ch in raw[i:]:
        if instr:
            cur.append(ch)
            if ch == "'":
                instr = False
            continue
        if ch == "'":
            instr = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            if depth > 1:
                cur.append(ch)
        elif ch == ")":
            depth -= 1
            if depth == 0:
                out.append("".join(cur))
                break
            cur.append(ch)
        elif ch == "," and depth == 1:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    return [x.strip() for x in out]


def first_str(raw):
    m = re.search(r"'((?:[^']|'')*)'", raw)
    return m.group(1).replace("''", "'") if m else ""


def numbers(text):
    return [float(x) for x in _NUM.findall(text or "")]


def point_of(ents, pid):
    if etype(ents.get(pid, "")) != "CARTESIAN_POINT":
        return None
    a = arg_split(ents[pid])
    if len(a) < 2:
        return None
    v = numbers(a[1])
    while len(v) < 3:
        v.append(0.0)
    return (v[0], v[1], v[2])


def direction_of(ents, pid):
    if etype(ents.get(pid, "")) != "DIRECTION":
        return None
    a = arg_split(ents[pid])
    if len(a) < 2:
        return None
    v = numbers(a[1])
    while len(v) < 3:
        v.append(0.0)
    return (v[0], v[1], v[2])


def closure(ents, seeds):
    seen, stack = set(), list(seeds)
    while stack:
        i = stack.pop()
        if i in seen or i not in ents:
            continue
        seen.add(i)
        stack.extend(refs(ents[i]))
    return seen


def fmt(x):
    s = "%.9g" % x
    return "0" if s == "-0" else s


# ---------------------------------------------------------------------- matrix

def norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def placement_matrix(ents, pid):
    """AXIS2_PLACEMENT_3D -> 4x4 with the axes as columns."""
    r = refs(ents.get(pid, ""))
    if not r:
        return None
    pt = point_of(ents, r[0])
    if pt is None:
        return None
    z = direction_of(ents, r[1]) if len(r) > 1 else None
    x = direction_of(ents, r[2]) if len(r) > 2 else None
    z = norm(z) if z else (0.0, 0.0, 1.0)
    x = norm(x) if x else (1.0, 0.0, 0.0)
    d = sum(a * b for a, b in zip(x, z))
    x = norm(tuple(a - d * b for a, b in zip(x, z)))
    y = (z[1] * x[2] - z[2] * x[1],
         z[2] * x[0] - z[0] * x[2],
         z[0] * x[1] - z[1] * x[0])
    return [[x[0], y[0], z[0], pt[0]],
            [x[1], y[1], z[1], pt[1]],
            [x[2], y[2], z[2], pt[2]],
            [0.0, 0.0, 0.0, 1.0]]


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def mat_inv_rigid(m):
    r = [[m[j][i] for j in range(3)] for i in range(3)]
    t = [m[i][3] for i in range(3)]
    ti = [-sum(r[i][k] * t[k] for k in range(3)) for i in range(3)]
    return [[r[0][0], r[0][1], r[0][2], ti[0]],
            [r[1][0], r[1][1], r[1][2], ti[1]],
            [r[2][0], r[2][1], r[2][2], ti[2]],
            [0.0, 0.0, 0.0, 1.0]]


def apply_pt(m, p):
    return (m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
            m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
            m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3])


def apply_dir(m, d):
    return norm((m[0][0] * d[0] + m[0][1] * d[1] + m[0][2] * d[2],
                 m[1][0] * d[0] + m[1][1] * d[1] + m[1][2] * d[2],
                 m[2][0] * d[0] + m[2][1] * d[1] + m[2][2] * d[2]))


# -------------------------------------------------------------------- indexing

PD_TYPES = ("PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS", "PRODUCT_DEFINITION")
PDF_TYPES = ("PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE",
             "PRODUCT_DEFINITION_FORMATION")
BREP_REPS = ("ADVANCED_BREP_SHAPE_REPRESENTATION",
             "MANIFOLD_SURFACE_SHAPE_REPRESENTATION", "FACETED_BREP_SHAPE_REPRESENTATION")
GLOBAL_TYPES = ("APPLICATION_PROTOCOL_DEFINITION",)


def index_model(ents):
    prod_name = {k: first_str(v) for k, v in ents.items() if etype(v) == "PRODUCT"}

    pdf_prod = {}
    for k, v in ents.items():
        if etype(v) in PDF_TYPES:
            r = refs(v)
            if r:
                pdf_prod[k] = r[0]

    pd_prod = {}
    for k, v in ents.items():
        if etype(v) in PD_TYPES:
            r = refs(v)
            if len(r) >= 2:
                pd_prod[k] = pdf_prod.get(r[0])

    pds_pd = {}
    for k, v in ents.items():
        if etype(v) == "PRODUCT_DEFINITION_SHAPE":
            r = refs(v)
            if r:
                pds_pd[k] = r[0]

    pd_chain = {}                 # pd -> (pds, sdr, shell_rep)
    for k, v in ents.items():
        if etype(v) != "SHAPE_DEFINITION_REPRESENTATION":
            continue
        r = refs(v)
        if len(r) >= 2 and r[0] in pds_pd:
            pd_chain.setdefault(pds_pd[r[0]], (r[0], k, r[1]))

    srr_geo = {}                  # shell rep -> (srr id, brep rep)
    for k, v in ents.items():
        if etype(v) == "SHAPE_REPRESENTATION_RELATIONSHIP":
            r = refs(v)
            if len(r) >= 2 and etype(ents.get(r[1], "")) in BREP_REPS:
                srr_geo[r[0]] = (k, r[1])

    cdsr = {}                     # child rep -> [(parent rep, item_1, item_2)]
    for k, v in ents.items():
        if etype(v) != "CONTEXT_DEPENDENT_SHAPE_REPRESENTATION":
            continue
        r = refs(v)
        if not r:
            continue
        rel = refs(ents.get(r[0], ""))
        if len(rel) < 2:
            continue
        t1 = t2 = None
        for x in rel[2:]:
            if etype(ents.get(x, "")) == "ITEM_DEFINED_TRANSFORMATION":
                tr = refs(ents[x])
                if len(tr) >= 2:
                    t1, t2 = tr
        cdsr.setdefault(rel[0], []).append((rel[1], t1, t2))

    nauo_child = {}
    for k, v in ents.items():
        if etype(v) == "NEXT_ASSEMBLY_USAGE_OCCURRENCE":
            r = refs(v)
            if len(r) >= 2:
                nauo_child[r[1]] = nauo_child.get(r[1], 0) + 1

    return {"prod_name": prod_name, "pd_prod": pd_prod, "pd_chain": pd_chain,
            "srr_geo": srr_geo, "cdsr": cdsr, "nauo_child": nauo_child}


def instance_transforms(ents, idx):
    """rep -> (cumulative 4x4, depth). One entry per rep; later instances reuse it."""
    ident = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
    cdsr = idx["cdsr"]
    down = {}
    for child, lst in cdsr.items():
        for parent, t1, t2 in lst:
            down.setdefault(parent, []).append((child, t1, t2))
    roots = [v[2] for v in idx["pd_chain"].values() if v[2] not in cdsr]

    inst = {}

    def walk(rep, m, depth):
        if rep in inst:
            return
        inst[rep] = (m, depth)
        for child, t1, t2 in down.get(rep, []):
            local = ident
            if t1 and t2:
                m1 = placement_matrix(ents, t1)
                m2 = placement_matrix(ents, t2)
                if m1 and m2:
                    local = mat_mul(m2, mat_inv_rigid(m1))
            walk(child, mat_mul(m, local), depth + 1)

    for r in roots:
        walk(r, ident, 0)
    for rep in idx["srr_geo"]:
        inst.setdefault(rep, (ident, 0))
    for v in idx["pd_chain"].values():
        inst.setdefault(v[2], (ident, 0))
    return inst


# ------------------------------------------------------------------ geometry io

# VECTOR is deliberately absent: args are (name, #direction, magnitude) and
# rewriting it as coordinates crashes the importer.
TRANSFORMABLE = ("CARTESIAN_POINT", "DIRECTION")


def bake(ents, ids, m):
    """Copy the entity table with one rigid transform folded into the coordinates."""
    out = dict(ents)
    for i in ids:
        raw = ents[i]
        t = etype(raw)
        if t not in TRANSFORMABLE:
            continue
        a = arg_split(raw)
        if len(a) < 2:
            continue
        v = numbers(a[1])
        while len(v) < 3:
            v.append(0.0)
        p = (v[0], v[1], v[2])
        q = apply_pt(m, p) if t == "CARTESIAN_POINT" else apply_dir(m, p)
        out[i] = "%s(%s,(%s,%s,%s))" % (t, a[0], fmt(q[0]), fmt(q[1]), fmt(q[2]))
    return out


def write_step(path, ents, ids, title):
    head = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('%s'),'2;1');" % title.replace("'", ""),
        "FILE_NAME('%s','%s',(''),(''),'','','');"
        % (os.path.basename(path).replace("'", ""), time.strftime("%Y-%m-%dT%H:%M:%S")),
        "FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));",
        "ENDSEC;",
        "DATA;",
    ]
    body = ["#%d=%s;" % (i, ents[i]) for i in sorted(ids)]
    tail = ["ENDSEC;", "END-ISO-10303-21;", ""]
    with open(path, "w", encoding="latin-1", newline="\n") as fh:
        fh.write("\n".join(head + body + tail))


PLACEMENT_TYPES = ("AXIS2_PLACEMENT_3D", "AXIS1_PLACEMENT", "AXIS2_PLACEMENT_2D")


def restrict_rep(ents, rep_id, keep):
    """Keep only the solids in `keep` inside one BREP shape representation.

    A PRODUCT with three solids is one file with three bodies in it. To hand a
    single tube to the mid-surface pipeline the representation has to be
    trimmed as well - the closure alone would still carry the other two solids
    and the importer would build all three parts again.
    """
    raw = ents.get(rep_id, "")
    a = arg_split(raw)
    if len(a) < 3:
        return ents
    kept = [r for r in refs(a[1]) if r in keep]
    if not kept:
        return ents
    out = dict(ents)
    out[rep_id] = "%s(%s,(%s)%s)" % (etype(raw), a[0],
                                     ",".join("#%d" % x for x in kept),
                                     ("," + ",".join(a[2:])) if a[2:] else "")
    return out


def bbox_of_part(ents, ids, prof=None):
    """Real extent of one part, in three corrections that all matter.

    1. **Placement origins are not geometry.** Every `AXIS2_PLACEMENT_3D` drags
       in its own origin point, and those sit wherever the datum was defined -
       on one 15 mm weld nut the raw point cloud reached (0, 0, 0) and
       (280, 532, 69), giving a box of 783 x 82 x 138 mm for a 15 x 12 x 16 mm
       body. Excluding them brings the same part to 14.9 x 12.4 x 15.6.
    2. **A cylinder's diameter is not on any vertex.** A tube's VERTEX_POINTs
       all sit on the seam plane: a 24 x 23 x 55 mm tube measured 1.0 mm across
       in the radial direction. Any axis smaller than twice the modal cylinder
       radius is widened to it.
    3. B-spline control points stay in - they only ever push the box outward,
       and an over-estimate only costs a wasted `Skin` attempt, never a lost
       part.

    Returns None when the part has no usable point at all.
    """
    excl = set()
    for i in ids:
        if etype(ents[i]) in PLACEMENT_TYPES:
            r = refs(ents[i])
            if r:
                excl.add(r[0])
    pts = [i for i in ids if i not in excl and etype(ents[i]) == "CARTESIAN_POINT"]
    xs, ys, zs = [], [], []
    for i in pts:
        p = point_of(ents, i)
        if p:
            xs.append(p[0])
            ys.append(p[1])
            zs.append(p[2])
    if not xs:
        return None
    lo = [min(xs), min(ys), min(zs)]
    hi = [max(xs), max(ys), max(zs)]
    r = (prof or {}).get("radius")
    if r:
        c = [0.5 * (lo[k] + hi[k]) for k in range(3)]
        for k in range(3):
            if hi[k] - lo[k] < 2.0 * r:
                lo[k] = c[k] - r
                hi[k] = c[k] + r
    return {"dx": hi[0] - lo[0], "dy": hi[1] - lo[1], "dz": hi[2] - lo[2],
            "x0": lo[0], "y0": lo[1], "z0": lo[2],
            "cx": 0.5 * (lo[0] + hi[0]), "cy": 0.5 * (lo[1] + hi[1]),
            "cz": 0.5 * (lo[2] + hi[2])}


# ------------------------------------------------------------------ classifying

FASTENER_WORDS = ("螺母", "螺栓", "螺柱", "垫圈", "螺钉", "铆钉", "卡扣", "衬套",
                  "导套", "螺套", "堵塞", "堵盖", "clip", "bolt", "nut", "screw",
                  "rivet", "washer", "stud", "fastener", "bushing", "plug")
WELD_WORDS = ("焊缝", "焊点", "焊接", "weld", "solder")
SEAM_WORDS = ("seam", "焊缝", "涂胶", "胶条", "adhesive", "mastic")
GLASS_WORDS = ("玻璃", "glass")
NONMETAL_WORDS = ("塑料", "泡沫", "nvh", "绝缘", "贴条", "密封", "橡胶",
                  "plastic", "foam", "rubber", "seal")

CLASS_DIR = {"sheet": "01_sheet", "fastener": "02_fastener",
             "weld_seam": "03_weld_seam", "weld_spot": "04_weld_spot",
             "glass": "05_glass", "nonmetal": "06_nonmetal",
             "unknown": "09_unknown"}


def name_kind(name):
    blob = (name or "").lower()
    if not blob:
        return ""
    for w in SEAM_WORDS:
        if w in blob:
            return "weld_seam"
    for w in WELD_WORDS:
        if w in blob:
            return "weld_spot"
    for w in FASTENER_WORDS:
        if w in blob:
            return "fastener"
    for w in GLASS_WORDS:
        if w in blob:
            return "glass"
    for w in NONMETAL_WORDS:
        if w in blob:
            return "nonmetal"
    return ""


def _closed_revolve(raw):
    """True when a surface's parameter range spans a whole turn.

    CATIA writes a cylinder as a b-spline as often as it writes it as
    `CYLINDRICAL_SURFACE`. The giveaway is the knot range: `(0., 4*pi)` means
    the surface closes on itself, which a flat patch never does. Measured on a
    4-face body that is really a Ø14 x 25 mm pin: u = (0, 25), v = (0, 12.566),
    and 12.566 / 2*pi = 2.000 exactly.

    The tolerance has to be tight. The linear direction of the same surface is
    (0, 25), and 25 / 2*pi = 3.979 - close enough to 4 to pass any loose test.
    """
    for m in re.finditer(r"\(([-+0-9.eE, ]*)\)", raw):
        v = numbers(m.group(1))
        if len(v) != 2 or v[1] <= v[0] or v[0] < -1e-9:
            continue
        k = (v[1] - v[0]) / (2.0 * math.pi)
        if k >= 0.5 and abs(k - round(k)) < 5e-3:
            return True
    return False


def decode_catia(text):
    """Resolve CATIA's `\\X2\\<hex>\\X0\\` UTF-16BE escapes.

    CATIA writes non-Latin names as `\\X2\\` + 4 hex digits per character +
    `\\X0\\`. The solid names in a body-in-white STEP are the design tree:
    decoding them turns `6A6180F68F74627F` into 橡胶轴承 and finds 127 bodies
    literally named `welding` inside one cross-member.

    A name is worth more than any geometric test - a hex nut and a weld bead
    are both bodies of revolution, and only the name separates them.
    """
    def repl(m):
        try:
            return bytes.fromhex(m.group(1)).decode("utf-16-be")
        except Exception:
            return m.group(0)
    return re.sub(r"\\X2\\([0-9A-Fa-f]+)\\X0\\", repl, text or "")


WELD_NAME = ("welding", "weld", "焊缝", "焊点", "焊道", "seam")
FASTENER_NAME = ("螺母", "螺栓", "螺柱", "垫圈", "螺钉", "铆钉", "卡扣",
                 "螺套", "堵盖", "堵塞", "nut", "bolt", "screw", "rivet")
NONMETAL_NAME = ("橡胶", "塑料", "泡沫", "衬套", "轴承", "密封", "nvh", "胶")
SHEET_NAME = ("板", "钣金", "panel", "sheet")


def solid_names(ents, ids):
    """Decoded MANIFOLD_SOLID_BREP names of one part."""
    out = []
    for i in ids:
        if etype(ents[i]) != "MANIFOLD_SOLID_BREP":
            continue
        out.append(decode_catia(first_str(ents[i])).strip())
    return out


def name_class(names):
    """(class, why) from the decoded solid names, or (None, None)."""
    if not names:
        return None, None
    blob = " ".join(names).lower()
    weld = [n for n in names if any(w in n.lower() for w in WELD_NAME)]
    if weld and len(weld) == len(names):
        return "weld_seam", "实体名称为「%s」" % "/".join(sorted(set(weld))[:3])
    for words, cls, label in ((FASTENER_NAME, "fastener", "紧固件"),
                              (NONMETAL_NAME, "nonmetal", "非金属件")):
        hit = [n for n in names if any(w in n.lower() for w in words)]
        if hit:
            return cls, "实体名称「%s」→ %s" % (hit[0], label)
    if any(w in blob for w in SHEET_NAME):
        return "sheet", "实体名称「%s」" % names[0]
    return None, None


def surface_profile(ents, ids):
    """Count the analytic surface types of one part - the shape fingerprint.

    A weld bead, a hinge barrel and a stamped panel can have the same bounding
    box and the same face count; what separates them is *what the faces are*.
    A weld run is a revolve: cylindrical faces, no b-spline, and a small
    radius. A panel is b-spline dominated. A nut is planes plus a torus.

    `radius` is the modal cylindrical radius - the wall of the tube.
    """
    counts = {}
    radii = []
    for i in ids:
        if etype(ents[i]) != "ADVANCED_FACE":
            continue
        for r in refs(ents[i]):
            t = etype(ents.get(r, ""))
            if t in ("PLANE", "CYLINDRICAL_SURFACE", "CONICAL_SURFACE",
                     "SPHERICAL_SURFACE", "TOROIDAL_SURFACE",
                     "B_SPLINE_SURFACE_WITH_KNOTS", "SURFACE_OF_LINEAR_EXTRUSION",
                     "SURFACE_OF_REVOLUTION", "COMPLEX"):
                counts[t] = counts.get(t, 0) + 1
                if t in ("B_SPLINE_SURFACE_WITH_KNOTS", "COMPLEX",
                         "SURFACE_OF_REVOLUTION") and _closed_revolve(ents[r]):
                    counts["CLOSED"] = counts.get("CLOSED", 0) + 1
                if t == "CYLINDRICAL_SURFACE":
                    v = numbers(ents[r])
                    if v:
                        radii.append(v[-1])
                break
    modal = None
    if radii:
        modal = max(set(round(x, 3) for x in radii),
                    key=lambda x: sum(1 for r in radii if round(r, 3) == x))
    return {"counts": counts, "radius": modal,
            "total": sum(v for k, v in counts.items() if k != "CLOSED")}


def _dims(bb):
    if not bb:
        return [0.0, 0.0, 0.0]
    return sorted((bb["dx"], bb["dy"], bb["dz"]), reverse=True)


def revolve_kind(prof, bb, max_radius=50.0, min_radius=1.5):
    """(class, why) when the part is a body of revolution - a weld bead or a tube.

    Measured on the tailgate: two weld runs came out as `6 faces = 4 cylinders
    + 2 planes`, radii 7.5 and 12.5 mm, no b-spline anywhere. They were being
    classified `sheet` and sent to `Skin`, which cannot produce a middle surface
    from a 1 mm thick arc and fails.

    The b-spline term is what keeps this from firing on panels: a stamped part
    is b-spline dominated even when it has dozens of cylindrical faces (a hinge
    barrel with 7 cylinders and 4 tori is a bracket, not a weld).
    """
    if not prof or not prof["total"]:
        return None, None
    c = prof["counts"]
    cyl = c.get("CYLINDRICAL_SURFACE", 0)
    spline = c.get("B_SPLINE_SURFACE_WITH_KNOTS", 0)
    closed = c.get("CLOSED", 0)
    # a b-spline cylinder: no analytic cylinder, but the surface wraps a full
    # turn and the body has almost nothing else on it
    if closed and prof["total"] <= 8 and cyl < 2:
        return ("weld_spot" if _dims(bb)[0] <= 15.0 else "weld_seam",
                "回转体（闭合 B 样条面 %d / 共 %d 面）" % (closed, prof["total"]))
    if cyl < 2 or spline or prof["total"] < 4:
        return None, None
    if cyl / float(prof["total"]) < 0.4:
        return None, None
    r = prof["radius"]
    if r is not None and r > max_radius:
        return None, None          # a 60 mm hinge eye, not a weld run
    if r is not None and r < min_radius:
        return None, None          # a 1 mm fillet, not a tube
    if r is not None and r < min_radius:
        return None, None          # a 1 mm fillet, not a tube
    dims = sorted((bb["dx"], bb["dy"], bb["dz"]), reverse=True) if bb else []
    small = dims[-1] if dims else 0.0
    tag = "半径 %s mm" % ("%.2f" % r if r is not None else "?")
    if dims and dims[0] <= 15.0:
        return "weld_spot", "回转体 %d 圆柱面，%s，最大尺寸 %.1f mm" % (cyl, tag, dims[0])
    if len(dims) > 1 and small <= 3.0:
        return "weld_seam", "焊道：%d 圆柱面 / %d 面，%s，厚 %.1f mm" % (
            cyl, prof["total"], tag, small)
    return "weld_seam", "回转体：%d 圆柱面 / %d 面，%s" % (cyl, prof["total"], tag)


def solid_profiles(ents, ids):
    """Per-solid surface fingerprint inside one part.

    A stamped bracket with two welded tubes is one PRODUCT and three
    MANIFOLD_SOLID_BREP. The product-level profile is b-spline dominated and
    says `sheet`, which is right for the stamping and wrong for the tubes -
    `Skin` cannot pull a middle surface out of a Ø23 x 55 tube. Counting the
    revolve-like solids here is what makes that visible.
    """
    out = []
    for i in ids:
        if etype(ents[i]) != "MANIFOLD_SOLID_BREP":
            continue
        sub = closure(ents, [i])
        prof = surface_profile(ents, sub)
        excl = set()
        for k in sub:
            if etype(ents[k]) in PLACEMENT_TYPES:
                r = refs(ents[k])
                if r:
                    excl.add(r[0])
        pts = [point_of(ents, k) for k in sub
               if k not in excl and etype(ents[k]) == "CARTESIAN_POINT"]
        pts = [p for p in pts if p]
        if not pts:
            continue
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        bb = {"dx": hi[0] - lo[0], "dy": hi[1] - lo[1], "dz": hi[2] - lo[2]}
        kind, why = revolve_kind(prof, bb)
        out.append({"solid": i, "faces": prof["total"], "kind": kind or "other",
                    "why": why, "radius": prof["radius"], "bbox": bb})
    return out


def size_kind(bb, area=0.0, hw_area=4000.0):
    """Coarse class from the vertex bbox.

    Deliberately conservative: calling a panel a fastener loses the part
    silently, calling a fastener a panel only costs one wasted `Skin` attempt
    that `process_part.py` resolves on its own. So `sheet` is the default and
    a fastener needs a compact, block-like body - not merely a small number.

    A 1 x 23 x 55 mm shim is a panel, not a bolt, and the flatness term is what
    separates them.
    """
    if not bb:
        return "unknown", "无 bbox"
    dims = sorted((bb["dx"], bb["dy"], bb["dz"]), reverse=True)
    big, mid, small = dims
    if big <= 15.0:
        return "weld_spot", "最大尺寸 %.1f mm ≤ 15" % big
    flat = small / max(big, 1e-6)
    blocky = mid / max(big, 1e-6)
    if big <= 60.0 and flat >= 0.15 and blocky >= 0.35:
        return "fastener", "块状体 %.1f×%.1f×%.1f mm（≤60 且非薄片）" % (big, mid, small)
    if area and area < hw_area and flat <= 0.05 and big >= 20.0:
        return "weld_seam", "细长条 厚/长=%.3f，面积 %.0f" % (flat, area)
    return "sheet", "非块状（厚/长=%.2f），按钣金件处理" % flat

def safe_name(name, used):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "part").strip()) or "part"
    if s in used:
        used[s] += 1
        s = "%s__%d" % (s, used[s])
    else:
        used[s] = 1
    return s


# ------------------------------------------------------------------------- main

def split(src, out_dir, dry_run=False, keep_local=False, min_solids=1,
          hw_area=4000.0, verbose=True, split_solids=False, split_mixed=True):
    t0 = time.time()
    ents, _ = read_step(src)
    idx = index_model(ents)
    etype_f, refs_f = etype, refs

    globals_ids = set()
    for k, v in ents.items():
        if etype_f(v) in GLOBAL_TYPES:
            globals_ids |= closure(ents, [k])

    inst = instance_transforms(ents, idx)

    parts = []
    for pd, (pds, sdr, shell) in idx["pd_chain"].items():
        prod = idx["pd_prod"].get(pd)
        name = idx["prod_name"].get(prod, "") or "part_%d" % shell
        srr = idx["srr_geo"].get(shell)
        geo = srr[1] if srr else shell
        geo_ids = closure(ents, [geo])

        # the product chain keeps the file a valid CATIA-style subset;
        # the SRR and every global definition are unreferenced and must be
        # added by hand or the importer silently returns no geometry.
        keep = closure(ents, [prod] if prod else [shell])
        keep |= closure(ents, [pd, pds, sdr, shell])
        if srr:
            keep |= closure(ents, [srr[0]])
        keep |= geo_ids | globals_ids

        n_sol = sum(1 for i in geo_ids if etype_f(ents[i]) == "MANIFOLD_SOLID_BREP")
        n_face = sum(1 for i in geo_ids if etype_f(ents[i]) == "ADVANCED_FACE")
        prof = surface_profile(ents, geo_ids)
        m, depth = inst.get(shell, (None, 0))
        bb = bbox_of_part(ents, geo_ids, prof)
        # transformed bbox: the classifier and the report must talk about the
        # position the part really sits at
        if bb and m and not keep_local:
            xs, ys, zs = [], [], []
            for dx in (0, bb["dx"]):
                for dy in (0, bb["dy"]):
                    for dz in (0, bb["dz"]):
                        q = apply_pt(m, (bb["x0"] + dx, bb["y0"] + dy, bb["z0"] + dz))
                        xs.append(q[0]); ys.append(q[1]); zs.append(q[2])
            bb = {"dx": max(xs) - min(xs), "dy": max(ys) - min(ys),
                  "dz": max(zs) - min(zs),
                  "x0": min(xs), "y0": min(ys), "z0": min(zs),
                  "cx": (max(xs) + min(xs)) / 2, "cy": (max(ys) + min(ys)) / 2,
                  "cz": (max(zs) + min(zs)) / 2}
        sprof = solid_profiles(ents, geo_ids)
        snames = solid_names(ents, geo_ids)
        n_inst = idx["nauo_child"].get(pd, 0)
        solids = [i for i in geo_ids if etype_f(ents[i]) == "MANIFOLD_SOLID_BREP"]
        # A part that mixes a stamped panel with welded tubes cannot be handed
        # to Skin as one body: the panel shells fine, the tubes fail, and the
        # failure is reported against the whole part. Split those apart by
        # default; a part made of one kind stays one file.
        # The decoded solid names decide first: 127 bodies literally called
        # `welding` next to a `10#板` panel is a mixed part whatever the
        # surface types say. Geometry is the fallback for unnamed bodies.
        weld_n = sum(1 for n in snames
                     if any(w in n.lower() for w in WELD_NAME))
        mixed = 0 < weld_n < len(snames)
        if not mixed:
            mixed = len({x["kind"] for x in sprof}) > 1
        do_split = split_solids or (split_mixed and mixed)

        if do_split and len(solids) > 1:
            # one body per file: the only way to keep a welded tube out of the
            # Skin run on the stamping it is welded to
            base_keep = keep - geo_ids
            # The representation's own context (units, uncertainty) lives in
            # the geometry closure that `base_keep` just subtracted, and the
            # solid closure does not reach it either - nothing points forward.
            # Leaving it out produces a file whose every reference resolves
            # except the two that name the context, and the importer answers
            # `no geometry imported`.
            ctx = closure(ents, [r for r in refs(ents.get(geo, ""))
                                 if r not in set(solids)])
            for n, sid in enumerate(solids, 1):
                sub = closure(ents, [sid])
                sprof1 = surface_profile(ents, sub)
                sbb = bbox_of_part(ents, sub, sprof1)
                rk1, rwhy1 = revolve_kind(sprof1, sbb)
                parts.append({
                    "name": "%s__s%02d" % (name, n), "pd": pd, "shell": shell,
                    "geo": geo, "rep_edit": (geo, sid),
                    "n_solids": 1,
                    "n_faces": sum(1 for i in sub if etype_f(ents[i]) == "ADVANCED_FACE"),
                    "n_entities": len(base_keep | sub | globals_ids | ctx | {geo}),
                    "geo_ids": sub, "profile": sprof1,
                    "solid_profiles": [{"solid": sid, "kind": rk1 or "other",
                                        "why": rwhy1,
                                        "radius": sprof1["radius"]}],
                    "solid_names": [decode_catia(first_str(ents[sid])).strip()],
                    "n_instances": n_inst, "depth": depth, "bbox": sbb,
                    # the parent BREP representation has to travel with the
                    # solid: it is the only entity that turns a
                    # MANIFOLD_SOLID_BREP into something an importer reads.
                    "ids": base_keep | sub | globals_ids | ctx | {geo}, "mat": m})
            continue

        parts.append({"name": name, "pd": pd, "shell": shell, "geo": geo,
                      "n_solids": n_sol, "n_faces": n_face, "n_entities": len(keep),
                      "geo_ids": geo_ids, "profile": prof,
                      "solid_profiles": sprof, "solid_names": snames,
                      "n_instances": n_inst, "depth": depth, "bbox": bb,
                      "ids": keep, "mat": m})

    parts.sort(key=lambda r: (r["name"]))
    geo_parts = [p for p in parts if p["n_solids"] >= min_solids]

    if verbose:
        print("entities %d  products %d  parts with geometry %d / %d  solids %d"
              % (len(ents), len(idx["prod_name"]), len(geo_parts), len(parts),
                 sum(p["n_solids"] for p in geo_parts)))

    used = {}
    for p in geo_parts:
        p["file"] = safe_name(p["name"], used)
        prof = p["profile"]
        # Order matters. The decoded solid name beats everything: a hex nut and
        # a weld bead are both bodies of revolution, and a 66 mm hex flange nut
        # scored 61% cylindrical - geometry alone called it a weld run.
        ncls, nwhy = name_class(p.get("solid_names") or [])
        kw = name_kind(p["name"])
        rk, rwhy = revolve_kind(prof, p["bbox"])
        sk, swhy = size_kind(p["bbox"], 0.0, hw_area)
        if ncls:
            p["class"], p["class_reason"] = ncls, nwhy
        elif kw:
            p["class"], p["class_reason"] = kw, "名称关键词"
        elif rk:
            p["class"], p["class_reason"] = rk, rwhy
        else:
            p["class"], p["class_reason"] = sk, swhy or "默认按钣金件"

    if verbose:
        print("\n%-22s %-11s %5s %5s %4s %4s | %8s %8s %8s"
              % ("name", "class", "sol", "faces", "inst", "管", "dx", "dy", "dz"))
        for p in geo_parts:
            bb = p["bbox"] or {}
            nrev = sum(1 for x in (p.get("solid_profiles") or [])
                       if x["kind"] != "other")
            print("%-22s %-11s %5d %5d %4d %4s | %8.1f %8.1f %8.1f"
                  % (p["name"][:22], p["class"], p["n_solids"], p["n_faces"],
                     p["n_instances"], ("管%d" % nrev) if nrev else "",
                     bb.get("dx", 0), bb.get("dy", 0), bb.get("dz", 0)))

    if dry_run:
        return {"parts": geo_parts, "entities": len(ents), "seconds": time.time() - t0}

    os.makedirs(out_dir, exist_ok=True)
    for p in geo_parts:
        sub = os.path.join(out_dir, CLASS_DIR.get(p["class"], "09_unknown"))
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, p["file"] + ".stp")
        src_table = ents
        if p.get("rep_edit"):
            src_table = restrict_rep(ents, p["rep_edit"][0], p["geo_ids"])
        table = bake(src_table, p["ids"], None if keep_local else (p["mat"] or [[1.0, 0, 0, 0],
                                                                         [0, 1.0, 0, 0],
                                                                         [0, 0, 1.0, 0],
                                                                         [0, 0, 0, 1.0]]))
        write_step(path, table, p["ids"], p["name"])
        p["step"] = os.path.relpath(path, out_dir).replace("\\", "/")
        p["bytes"] = os.path.getsize(path)

    csv_path = os.path.join(out_dir, "_parts.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["件号", "分类", "分类依据", "实体数", "面数", "装配实例",
                    "实体名称", "圆柱面", "平面", "样条面", "主半径", "回转体实体",
                    "圆管直径", "dx", "dy", "dz",
                    "cx", "cy", "cz", "entities", "step", "字节"])
        for p in geo_parts:
            bb = p["bbox"] or {}
            pr = p.get("profile") or {}
            pc = pr.get("counts") or {}
            w.writerow([p["name"], p["class"], p["class_reason"], p["n_solids"],
                        p["n_faces"], p["n_instances"],
                        "|".join(p.get("solid_names") or [])[:120],
                        pc.get("CYLINDRICAL_SURFACE", 0), pc.get("PLANE", 0),
                        pc.get("B_SPLINE_SURFACE_WITH_KNOTS", 0),
                        pr.get("radius"),
                        sum(1 for x in (p.get("solid_profiles") or [])
                            if x["kind"] != "other"),
                        ",".join("%.0f" % (2 * x["radius"])
                                 for x in (p.get("solid_profiles") or [])
                                 if x["kind"] != "other" and x["radius"]),
                        round(bb.get("dx", 0), 1), round(bb.get("dy", 0), 1),
                        round(bb.get("dz", 0), 1), round(bb.get("cx", 0), 1),
                        round(bb.get("cy", 0), 1), round(bb.get("cz", 0), 1),
                        p["n_entities"], p["step"], p["bytes"]])

    meta = {"src": os.path.abspath(src), "out": os.path.abspath(out_dir),
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "entities": len(ents), "products": len(idx["prod_name"]),
            "parts": [{k: v for k, v in p.items()
                       if k not in ("ids", "geo_ids")} for p in geo_parts]}
    with open(os.path.join(out_dir, "_split.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1, default=str)

    by_class = {}
    for p in geo_parts:
        by_class[p["class"]] = by_class.get(p["class"], 0) + 1
    if verbose:
        print("\n%s" % "  ".join("%s:%d" % kv for kv in sorted(by_class.items())))
        print("wrote %d steps in %.1fs -> %s" % (len(geo_parts), time.time() - t0, out_dir))
    return {"parts": geo_parts, "by_class": by_class, "csv": csv_path,
            "seconds": time.time() - t0}


def main():
    ap = argparse.ArgumentParser(description="Split an assembly STEP per part.")
    ap.add_argument("--src", required=True, help="assembly .stp/.step")
    ap.add_argument("--out", required=True, help="output root")
    ap.add_argument("--dry-run", action="store_true", help="report only")
    ap.add_argument("--keep-local", action="store_true",
                    help="write local coordinates instead of assembly position")
    ap.add_argument("--min-solids", type=int, default=1,
                    help="skip products with fewer solids (assembly nodes)")
    ap.add_argument("--hardware-area", type=float, default=4000.0,
                    help="below this area a part is treated as a standard part")
    ap.add_argument("--split-solids", action="store_true",
                    help="one file per solid body, always")
    ap.add_argument("--no-split-mixed", action="store_true",
                    help="keep a mixed part (panel + welded tubes) as one file")
    a = ap.parse_args()
    split(a.src, a.out, a.dry_run, a.keep_local, a.min_solids, a.hardware_area,
          split_solids=a.split_solids, split_mixed=not a.no_split_mixed)


if __name__ == "__main__":
    main()

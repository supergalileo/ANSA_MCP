"""Per-deck statistics straight off a Nastran shell deck - no ANSA needed.

    python scripts/deck_stats.py a.nas b.nas ...            # per file, one line
    python scripts/deck_stats.py --dir <out> --blocks       # whole folder + split report
    python scripts/deck_stats.py --dir <out> --csv stats.csv

Fills the two gates in SKILL.md 0f that nothing else measured:

    quad ratio   typically 0.8-0.95; a low ratio means the mesher triangled a
                 region it could not quad, which usually marks a geometry defect
    free edges   edges belonging to one element only. On an open shell the outer
                 boundary is legitimately free, so the meaningful number is
                 free edges **inside** the part, i.e. a crack; on a closed-ish
                 panel per_1k in the single digits is normal, hundreds is a mesh
                 that came apart

`--blocks` additionally splits each deck into connected components (union-find on
node ids, plus rounded coordinates so coincident-but-separately-numbered nodes
still count as joined) and reports **area and bbox per block**. That is the
answer to "is this part fragmented, or is its middle surface genuinely made of
inner wall / outer wall / bulkhead?" - a 9-block crash box whose blocks are each
0.5-3 m2 with adjacent bboxes is a normal automotive body part; 9 blocks of 40 mm2 each is
debris.
"""

import argparse
import collections
import csv
import math
import os
import sys

TAGS = ("CQUAD4", "CTRIA3", "GRID")


def parse(path):
    nodes, quads, tris = {}, [], []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            tag = line[:8].strip()
            if tag == "GRID":
                try:
                    nodes[int(line[8:16])] = (float(line[24:32]), float(line[32:40]),
                                              float(line[40:48]))
                except Exception:
                    pass
            elif tag == "CQUAD4":
                try:
                    quads.append([int(line[24:32]), int(line[32:40]),
                                  int(line[40:48]), int(line[48:56])])
                except Exception:
                    pass
            elif tag == "CTRIA3":
                try:
                    tris.append([int(line[24:32]), int(line[32:40]), int(line[40:48])])
                except Exception:
                    pass
    return nodes, quads, tris


def polygon_area(pts):
    """Newell's method - works for a non-planar quad too."""
    if len(pts) < 3:
        return 0.0
    nx = ny = nz = 0.0
    for i in range(len(pts)):
        x1, y1, z1 = pts[i]
        x2, y2, z2 = pts[(i + 1) % len(pts)]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)


def analyse(path, blocks=False, decimals=2):
    nodes, quads, tris = parse(path)
    elems = quads + tris
    out = {"file": os.path.basename(path), "elems": len(elems),
           "quads": len(quads), "tris": len(tris)}
    if not elems:
        out["error"] = "no shell elements"
        return out
    out["quad_ratio"] = round(len(quads) / float(len(elems)), 4)

    edge = collections.Counter()
    for e in elems:
        for i in range(len(e)):
            a, b = e[i], e[(i + 1) % len(e)]
            edge[(min(a, b), max(a, b))] += 1
    free = sum(1 for v in edge.values() if v == 1)
    out["free_edges"] = free
    out["free_per_1k"] = round(1000.0 * free / len(elems), 1)

    area = 0.0
    for e in elems:
        pts = [nodes[n] for n in e if n in nodes]
        area += polygon_area(pts)
    out["area_mm2"] = round(area, 1)

    if not blocks:
        return out

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def key(nid):
        p = nodes.get(nid)
        if p is None:
            return ("id", nid)
        return ("xyz", round(p[0], decimals), round(p[1], decimals), round(p[2], decimals))

    for e in elems:
        ks = [key(n) for n in e]
        for i in range(len(ks)):
            union(ks[i], ks[(i + 1) % len(ks)])

    per = collections.defaultdict(lambda: {"elems": 0, "area": 0.0,
                                           "lo": [1e18] * 3, "hi": [-1e18] * 3})
    for e in elems:
        r = find(key(e[0]))
        pts = [nodes[n] for n in e if n in nodes]
        per[r]["elems"] += 1
        per[r]["area"] += polygon_area(pts)
        for p in pts:
            for i in range(3):
                per[r]["lo"][i] = min(per[r]["lo"][i], p[i])
                per[r]["hi"][i] = max(per[r]["hi"][i], p[i])

    bl = []
    for r, d in per.items():
        bl.append({"elems": d["elems"], "area_mm2": round(d["area"], 1),
                   "bbox": [round(d["hi"][i] - d["lo"][i], 1) for i in range(3)]})
    bl.sort(key=lambda b: -b["area_mm2"])
    out["n_blocks"] = len(bl)
    out["blocks"] = bl
    if bl:
        out["largest_area_share"] = round(bl[0]["area_mm2"] / area, 3) if area else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--dir", default="", help="folder of .nas to walk")
    ap.add_argument("--blocks", action="store_true", help="per-component area and bbox")
    ap.add_argument("--csv", default="", help="write the table here")
    ap.add_argument("--min-blocks", type=int, default=1,
                    help="only print decks with at least this many components")
    args = ap.parse_args()

    files = list(args.paths)
    if args.dir:
        files += sorted(os.path.join(args.dir, f) for f in os.listdir(args.dir)
                        if f.lower().endswith(".nas"))
    if not files:
        print("no .nas given")
        return 2

    rows = []
    for f in files:
        r = analyse(f, blocks=args.blocks)
        rows.append(r)

    multi = [r for r in rows if r.get("n_blocks", 0) >= max(2, args.min_blocks)]
    if args.blocks:
        for r in multi:
            print("%s  blocks=%d  elems=%d  area=%.0f mm2  largest share=%.0f%%"
                  % (r["file"], r["n_blocks"], r["elems"], r["area_mm2"],
                     100 * (r.get("largest_area_share") or 0)))
            for i, b in enumerate(r["blocks"], 1):
                print("    #%-2d elems=%-6d area=%-9.0f mm2  bbox=%s"
                      % (i, b["elems"], b["area_mm2"], b["bbox"]))
    else:
        for r in rows:
            if "error" in r:
                print("%-24s %s" % (r["file"], r["error"]))
                continue
            print("%-24s elems=%-7d quad=%.3f free=%-6d per1k=%-6s area=%.0f"
                  % (r["file"], r["elems"], r["quad_ratio"], r["free_edges"],
                     r["free_per_1k"], r["area_mm2"]))

    good = [r for r in rows if "quad_ratio" in r]
    if len(good) > 1:
        q = sorted(r["quad_ratio"] for r in good)
        f1k = sorted(r["free_per_1k"] for r in good)
        print("\n%d decks: quad ratio min %.3f / p10 %.3f / median %.3f / max %.3f"
              % (len(good), q[0], q[len(q) // 10], q[len(q) // 2], q[-1]))
        print("            free edges per 1k: median %s / p90 %s / max %s"
              % (f1k[len(f1k) // 2], f1k[int(len(f1k) * 0.9)], f1k[-1]))
        print("            decks with quad<0.8: %d, with per1k>50: %d"
              % (sum(1 for r in good if r["quad_ratio"] < 0.8),
                 sum(1 for r in good if r["free_per_1k"] > 50)))

    if args.csv:
        cols = ["file", "elems", "quads", "tris", "quad_ratio", "free_edges",
                "free_per_1k", "area_mm2", "n_blocks", "largest_area_share"]
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print("\nwrote %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

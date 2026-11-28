"""Offline geometry audit for weld beads - no ANSA, no license.

Answers the two questions that decide whether the connection manager can weld a
given bead, before any ANSA time is spent:

  1. how far is the bead centerline from the nearest shell node
  2. how many DIFFERENT parts are within a radius of it

A bead 300 mm from the nearest node welds a part that is not in this model - no
search radius will fix it. A bead with only one part in range is either a
self-weld (a hem inside one stamping) or a weld to a part that was never meshed.

Both keys must include the file: every per-part `.nas` restarts its node and
property numbering at 1, so keying by id alone collapses 22 parts into one and
every seam then looks like it touches a single part.

    python seam_audit.py --nas-dir <dir> --seams <_seam_lines.json> [--radius 3]

Writes `<seams>_audit.csv` next to the JSON unless --csv is given.
"""

import argparse
import csv
import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict

CELL = 10.0


def load(nas_dir):
    nodes = {}
    node_pids = defaultdict(set)
    files = sorted(glob.glob(os.path.join(nas_dir, "*.nas")))
    for fi, f in enumerate(files):
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                tag = line[:8].strip()
                if tag == "GRID":
                    try:
                        nodes[(fi, int(line[8:16]))] = (float(line[24:32]),
                                                        float(line[32:40]),
                                                        float(line[40:48]))
                    except ValueError:
                        pass
                elif tag in ("CQUAD4", "CTRIA3"):
                    try:
                        pid = int(line[16:24])
                        g = [int(line[a:a + 8]) for a in (24, 32, 40, 48)]
                    except ValueError:
                        continue
                    for nid in g:
                        if nid > 0:
                            node_pids[(fi, nid)].add((fi, pid))
    return files, nodes, node_pids


def build_index(nodes):
    idx = defaultdict(list)
    for key, p in nodes.items():
        idx[(int(p[0] // CELL), int(p[1] // CELL), int(p[2] // CELL))].append(key)
    return idx


def _ring(idx, c, r):
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            for dz in range(-r, r + 1):
                if max(abs(dx), abs(dy), abs(dz)) != r:
                    continue
                for k in idx.get((c[0] + dx, c[1] + dy, c[2] + dz), ()):
                    yield k


def nearest(idx, nodes, q, max_ring=4):
    c = (int(q[0] // CELL), int(q[1] // CELL), int(q[2] // CELL))
    best = None
    for r in range(max_ring + 1):
        for k in _ring(idx, c, r):
            p = nodes[k]
            d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2
            if best is None or d < best:
                best = d
        if best is not None:
            return math.sqrt(best)
    return None


def parts_near(idx, nodes, node_pids, q, radius):
    c = (int(q[0] // CELL), int(q[1] // CELL), int(q[2] // CELL))
    out = set()
    for r in (0, 1):
        for k in _ring(idx, c, r) if r else idx.get(c, ()):
            p = nodes[k]
            if (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2 <= radius ** 2:
                out |= node_pids[k]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nas-dir", required=True)
    ap.add_argument("--seams", required=True)
    ap.add_argument("--radius", type=float, default=3.0)
    ap.add_argument("--csv", default="")
    a = ap.parse_args()

    files, nodes, node_pids = load(a.nas_dir)
    print("decks: %d, nodes: %d, pid-carrying nodes: %d"
          % (len(files), len(nodes), len(node_pids)))
    idx = build_index(nodes)

    rows = json.load(open(a.seams, encoding="utf-8"))
    out = []
    for r in rows:
        for c in r.get("curves") or []:
            pts = [p for p in (c.get("pts") or [c.get("p0"), c.get("p1")]) if p]
            if not pts:
                continue
            dists = [nearest(idx, nodes, p) for p in pts]
            known = [d for d in dists if d is not None]
            pids = set()
            for p in pts:
                pids |= parts_near(idx, nodes, node_pids, p, a.radius)
            out.append({
                "file": r["file"],
                "chord": c.get("chord"),
                "bulge": c.get("bulge"),
                "diameter": c.get("d"),
                "dist_to_node": round(min(known), 3) if known else "",
                "n_parts": len(pids),
                "parts": "|".join(str(p) for p in sorted(pids)),
            })

    d = sorted(x["dist_to_node"] for x in out if x["dist_to_node"] != "")
    print("distance to nearest node: min %.2f  p50 %.2f  max %.1f" % (d[0], d[len(d) // 2], d[-1]))
    hist = Counter(x["n_parts"] for x in out)
    print("parts within %.1f mm: %s" % (a.radius, dict(sorted(hist.items()))))

    bad = [x for x in out if x["n_parts"] < 2]
    print("seams that cannot reach two parts at this radius: %d" % len(bad))
    for x in bad[:15]:
        print("   %-26s parts=%d  chord=%6.1f  d=%s"
              % (x["file"], x["n_parts"], x["chord"] or 0, x["dist_to_node"]))

    csv_path = a.csv or os.path.splitext(a.seams)[0] + "_audit.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "chord", "bulge", "diameter",
                                           "dist_to_node", "n_parts", "parts"])
        w.writeheader()
        w.writerows(out)
    print("wrote " + csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Turn a finished batch's `_progress.jsonl` into the deliverable tables - no ANSA needed.

    python scripts/make_summary.py --out D:/proj/out [--target 5.0] [--connectivity]

Why this is separate from `process_folder.py`
---------------------------------------------
`process_folder` writes `汇总.csv` as it goes, but it has to run **inside** ANSA
(`base` is imported at module level). After `parallel_batch.py` - the only sane
way to run hundreds of parts - there is no ANSA process left to make the final
table with, and the driver deliberately does not write one (each worker would
overwrite the others).

This script reads `<out>/_progress.jsonl`, which the driver appends to, and
applies the verification gates from SKILL.md *before* calling anything delivered:

    shell count   > 0 and within tolerance of area / target^2
    edge_p50      within --p50-tol of the target length (default 6%)
    gauge         PSHELL T not the 1.0 placeholder
    deck          the .nas exists, is non-empty, and carries MAT1
    artefacts     .ansa and .nas both on disk

Every failure is written into the row's 校验 column, so a deck that "exists" but
has no MAT1 cannot pass as delivered.

`--connectivity` additionally parses each `.nas` and reports the number of
disjoint components per part (union-find over rounded coordinates, so coincident
but separately numbered nodes still count as joined). That is how the fragmented
Casting results were found; it costs a few minutes over a few hundred decks.
"""

import argparse
import collections
import csv
import json
import os
import re
import sys

ORDER = {"done": 0, "rescued": 1, "rescued_var": 2, "hardware": 3,
         "geometry_only": 4, "skin_failed": 5, "not_sheet_metal": 6,
         "casting_partial": 7, "mesh_failed": 8, "casting_failed": 9,
         "crash": 10, "timeout": 11, "died": 12, "error": 13, "unfinished": 14}
DELIVERED = ("done", "rescued", "rescued_var")
COLS = ["零件", "中文名称", "编号", "状态", "是否钣金", "薄壁面积占比", "表面积mm2",
        "判定厚度mm", "是否修复", "中面面数", "其中check桶", "总面数", "壳单元",
        "边长P50", "property", "ansa", "nas", "耗时s", "校验", "备注", "原因"]


# --------------------------------------------------------------------- input
def _sources(out):
    """Every progress file worth reading, in the order their records should win.

    `<out>/_progress.jsonl` is the merged file, but `parallel_batch.py` only
    appends to it **after** all workers exit - if the driver is killed, or the
    summary is wanted while the run is still going, the per-worker
    `_driver/w*.jsonl` files are the only complete record. Read the merged file
    first and the workers after, so a later re-run's record wins either way.
    """
    paths = [os.path.join(out, "_progress.jsonl")]
    drv = os.path.join(out, "_driver")
    if os.path.isdir(drv):
        paths += [os.path.join(drv, f) for f in sorted(os.listdir(drv))
                  if f.startswith("w") and f.endswith(".jsonl")]
    return [p for p in paths if os.path.isfile(p)]


def read_ends(paths):
    """part -> last `end` record. Later records win, which is what a re-run means."""
    last = {}
    for path in paths:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") == "end" and rec.get("part"):
                    last[rec["part"]] = rec
    return last


def read_begins(paths):
    n = collections.Counter()
    for path in paths:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") == "begin" and rec.get("part"):
                    n[rec["part"]] += 1
    return n


# -------------------------------------------------------------------- gates
def check_deck(nas_path):
    """(ok, note) - a deck is only delivered when MAT1 is really in the file."""
    if not nas_path or not os.path.isfile(nas_path):
        return False, "缺 .nas"
    if os.path.getsize(nas_path) == 0:
        return False, ".nas 为空"
    has_mat1 = False
    with open(nas_path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line.startswith("MAT1"):
                has_mat1 = True
                break
    if not has_mat1:
        return False, ".nas 无 MAT1（PSHELL 指向空材料）"
    return True, ""


def gates(rec, target, p50_tol):
    """Verification gates for one `done` row -> {"fails": [...], "notes": [...]}."""
    bad, notes = [], []
    n = rec.get("n_SHELL") or 0
    if not n:
        bad.append("无壳单元")
    p50 = rec.get("edge_p50")
    if target and p50:
        dev = abs(p50 - target) / target
        # Two tiers: >20% means the target length never reached the mesher, which
        # is a real defect. 6-20% is normal on small or highly curved parts - the
        # 5 mm target simply does not fit, and 25 of 308 decks sat there. Failing
        # them all would bury the two that are actually wrong.
        if dev > 0.20:
            bad.append("边长P50 %.2f 偏离目标 %.0f%%（目标尺寸未生效）" % (p50, 100 * dev))
        elif dev > p50_tol:
            notes.append("边长P50 %.2f（偏离 %.0f%%，小件受限）" % (p50, 100 * dev))
    props = rec.get("props") or []
    if not props:
        bad.append("无 PSHELL 属性")
    for p in props:
        try:
            if abs(float(p[1]) - 1.0) < 1e-9:
                bad.append("属性 %s 仍是占位 T=1.0" % p[0])
        except Exception:
            bad.append("属性 %s 厚度不可读" % (p[0] if p else "?"))
    ok, note = check_deck(rec.get("nas"))
    if not ok:
        bad.append(note)
    if not (rec.get("ansa") and os.path.isfile(rec["ansa"])):
        bad.append("缺 .ansa")
    return {"fails": bad, "notes": notes}


# --------------------------------------------------------- connectivity scan
def nas_components(path, decimals=2):
    nodes, elems = {}, []
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
                    elems.append([int(line[24:32]), int(line[32:40]),
                                  int(line[40:48]), int(line[48:56])])
                except Exception:
                    pass
            elif tag == "CTRIA3":
                try:
                    elems.append([int(line[24:32]), int(line[32:40]), int(line[40:48])])
                except Exception:
                    pass
    if not elems:
        return None
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

    sizes = collections.Counter()
    for e in elems:
        ks = [key(n) for n in e]
        for i in range(len(ks)):
            union(ks[i], ks[(i + 1) % len(ks)])
    for e in elems:
        sizes[find(key(e[0]))] += 1
    big = [c for c in sizes.values() if c > 1]
    return {"elems": len(elems), "components": len(sizes),
            "largest": max(sizes.values()) if sizes else 0,
            "big_components": len(big)}


# ------------------------------------------------------------ casting overlay
RESCUED = ("rescued", "rescued_var")


def read_casting(folder):
    """part -> last casting record. Empty when the second pass never ran."""
    path = os.path.join(folder, "_casting_progress.jsonl")
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("part"):
                out[rec["part"]] = rec
    return out


def overlay_casting(row, rec, target, p50_tol):
    """Fold a Casting result into the main-pass row for the same part.

    The casting record carries no `edge_p50` (the mesh is measured on elements,
    not on a face outline), so the size gate cannot run on it - that is stated in
    备注 rather than silently passed.
    """
    status = rec.get("status")
    row["状态"] = status
    row["中面面数"] = rec.get("mid_faces")
    row["壳单元"] = rec.get("shells")
    row["property"] = ("%s_T%s%s" % (rec.get("code"), rec.get("T_median"),
                                     "_suspect" if status == "rescued_var" else ""))
    row["ansa"] = rec.get("ansa") or ""
    row["nas"] = rec.get("nas") or ""
    row["耗时s"] = rec.get("seconds")
    notes = [rec.get("note") or ""]
    fails = []
    if status in RESCUED:
        ok, why = check_deck(row["nas"])
        if not ok:
            fails.append(why)
        notes.append("Casting 第二遍：面成功率 %s%%" % rec.get("face_rate"))
        notes.append("边长未测（Casting 产物）")
        if rec.get("spread") is not None:
            notes.append("厚度离散 %.0f%%" % (100 * float(rec["spread"])))
    row["校验"] = "；".join(x for x in fails if x)
    row["备注"] = "；".join(x for x in notes if x)
    row["原因"] = row["备注"]
    return row


# --------------------------------------------------------------------- write
def row_of(rec, gate=None, conn=None):
    gate = gate or {"fails": [], "notes": []}
    props = rec.get("props") or []
    row = {
        "零件": rec.get("part", ""),
        "中文名称": rec.get("part_name") or "",
        "编号": rec.get("code", ""),
        "状态": rec.get("status", ""),
        "是否钣金": "是" if rec.get("is_sheet_metal") else "否",
        "薄壁面积占比": rec.get("fraction"),
        "表面积mm2": rec.get("part_area"),
        "判定厚度mm": rec.get("estimated_T"),
        "是否修复": "是" if rec.get("repaired") else "否",
        "中面面数": rec.get("skin_faces"),
        "其中check桶": rec.get("skin_check"),
        "总面数": rec.get("n_FACE"),
        "壳单元": rec.get("n_SHELL"),
        "边长P50": rec.get("edge_p50"),
        "property": "; ".join("%s(T=%s)" % (p[0], p[1]) for p in props if p),
        "ansa": rec.get("ansa") or "",
        "nas": rec.get("nas") or "",
        "耗时s": rec.get("seconds"),
        "校验": "；".join(gate["fails"]),
        "备注": "；".join(gate["notes"]),
    }
    if conn is not None:
        row["连通块"] = conn.get("components")
        row["最大块单元"] = conn.get("largest")
    reason = rec.get("reason") or rec.get("note") or ""
    row["原因"] = reason
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="batch output folder holding _progress.jsonl")
    ap.add_argument("--target", type=float, default=5.0, help="target element length (mm)")
    ap.add_argument("--p50-tol", type=float, default=0.06, help="allowed edge_p50 deviation")
    ap.add_argument("--connectivity", action="store_true", help="scan each .nas for disjoint blocks")
    ap.add_argument("--casting", default="", help="folder of the Casting second pass to fold in")
    ap.add_argument("--prefix-only", default="", help="limit to parts whose file starts with this")
    args = ap.parse_args()

    out = args.out
    srcs = _sources(out)
    ends = read_ends(srcs)
    begins = read_begins(srcs)
    if not ends:
        print("no end records under %s" % out)
        return 1
    casting = read_casting(args.casting) if args.casting else {}

    rows, failed_gate = [], []
    for part in sorted(ends):
        rec = ends[part]
        if args.prefix_only and not part.upper().startswith(args.prefix_only.upper()):
            continue
        gate = {"fails": [], "notes": []}
        conn = None
        if rec.get("status") == "done":
            gate = gates(rec, args.target, args.p50_tol)
            if args.connectivity and rec.get("nas") and os.path.isfile(rec["nas"]):
                try:
                    conn = nas_components(rec["nas"])
                except Exception as exc:
                    gate["notes"].append("连通块分析失败 %s" % str(exc)[:60])
                if conn and conn["big_components"] > 1:
                    note = ("中面分裂为 %d 块（最大 %d 单元）"
                            % (conn["components"], conn["largest"]))
                    if conn["big_components"] > 3:
                        gate["fails"].append(note)
                    else:
                        gate["notes"].append(note)
            if gate["fails"]:
                failed_gate.append((rec.get("code"), gate["fails"]))
        row = row_of(rec, gate, None)
        cast = casting.get(part)
        if cast:
            overlay_casting(row, cast, args.target, args.p50_tol)
            # Fragmentation is the failure mode of the Casting path (a
            # `MiddleGeometry` result is not stitched), so the scan has to run on
            # the deck that was actually delivered, not on the main-pass one -
            # a `skin_failed` part has no main-pass deck at all.
            if args.connectivity and row.get("nas") and os.path.isfile(row["nas"]):
                try:
                    conn = nas_components(row["nas"])
                except Exception as exc:
                    row["备注"] = (row["备注"] + "；连通块分析失败 %s" % str(exc)[:60]).strip("；")
                if conn:
                    row["连通块"] = conn["components"]
                    row["最大块单元"] = conn["largest"]
                    if conn["big_components"] > 1:
                        note = ("中面分裂为 %d 块（最大 %d 单元）"
                                % (conn["components"], conn["largest"]))
                        if conn["big_components"] > 3:
                            row["校验"] = (row["校验"] + "；" + note).strip("；")
                        else:
                            row["备注"] = (row["备注"] + "；" + note).strip("；")
        elif conn is not None:
            row.update({"连通块": conn["components"], "最大块单元": conn["largest"]})
        rows.append(row)

    # A part that was begun and never ended is only a crash once it has burned
    # its retries. Six workers mean six parts are in flight at any moment, so a
    # live run always shows a few "begun, no end" rows - calling those crashes
    # would report the currently-running parts as failures.
    for part, n in begins.items():
        if part in ends:
            continue
        if args.prefix_only and not part.upper().startswith(args.prefix_only.upper()):
            continue
        if n >= 2:
            rows.append({"零件": part, "编号": "", "状态": "crash", "是否钣金": None,
                         "校验": "记录到 %d 次开始、0 次结束" % n,
                         "原因": "ANSA 在该零件上中断 %d 次，未产出" % n,
                         "ansa": "", "nas": ""})
        else:
            rows.append({"零件": part, "编号": "", "状态": "unfinished", "是否钣金": None,
                         "校验": "",
                         "原因": "只记录到 1 次开始、0 次结束：仍在处理，或驱动被中断",
                         "ansa": "", "nas": ""})

    # A shared output path is silent data loss: two parts write the same .ansa and
    # the last finisher wins, while both rows look complete because the file does
    # exist. On the 540-part BIW body 44 part numbers sat at 2-4 assembly
    # positions and 115 files were overwritten without any error anywhere.
    shared = collections.Counter()
    for r in rows:
        for col in ("ansa", "nas"):
            if r.get(col) and r.get("状态") in DELIVERED:
                shared[r[col].lower()] += 1
    for r in rows:
        if r.get("状态") not in DELIVERED:
            continue
        clash = [c for c in ("ansa", "nas")
                 if r.get(c) and shared[r[c].lower()] > 1]
        if clash:
            msg = "输出文件被 %d 件共用（%s）" % (shared[r[clash[0]].lower()],
                                                 "/".join(clash))
            r["校验"] = (r.get("校验", "") + "；" + msg).strip("；")

    cols = list(COLS)
    if args.connectivity:
        cols.insert(cols.index("校验"), "连通块")
        cols.insert(cols.index("校验"), "最大块单元")
    for r in rows:
        for c in cols:
            r.setdefault(c, "")

    rows.sort(key=lambda r: (ORDER.get(r["状态"], 20), str(r["零件"])))
    by_status = collections.Counter(r["状态"] for r in rows)
    done_bad = [r for r in rows if r["状态"] in DELIVERED and r["校验"]]
    n_delivered = sum(by_status.get(k, 0) for k in DELIVERED)

    def dump(name, subset):
        path = os.path.join(out, name)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(subset)
        return path

    p_all = dump("汇总.csv", rows)
    p_done = dump("交付清单.csv", [r for r in rows if r["状态"] in DELIVERED])
    p_iss = dump("未解决清单.csv", [r for r in rows if r["状态"] not in DELIVERED + ("hardware",)])
    p_gate = dump("校验不通过.csv", done_bad)

    print("parts recorded: %d" % len(rows))
    for k, v in sorted(by_status.items(), key=lambda kv: ORDER.get(kv[0], 20)):
        print("  %-16s %d" % (k, v))
    print("delivered decks: %d, of which failed a gate: %d" % (n_delivered, len(done_bad)))
    if done_bad:
        print("\n-- gate failures (first 15) --")
        for r in done_bad[:15]:
            print("  %-14s %s" % (r["编号"], r["校验"]))
    issues = [r for r in rows if r["状态"] not in DELIVERED + ("hardware",)]
    if issues:
        print("\n-- needs a human: %d --" % len(issues))
        for r in issues[:30]:
            print("  %-14s %-16s %s" % (r["编号"], r["状态"], (r["原因"] or "")[:70]))
    print("\nwrote:\n  %s\n  %s\n  %s\n  %s" % (p_all, p_done, p_iss, p_gate))
    return 0


if __name__ == "__main__":
    sys.exit(main())

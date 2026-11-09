"""Batch-process a folder of CATParts and summarise what could not be shelled.

    PARAMS = {
        "folder":   r"D:/proj/parts",
        "out_dir":  r"D:/out",
        "engine":   "mesh",                  # "mesh" | "batchmesh" | "none"
        "mpar":     r"D:/proj/5MM.ansa_mpar",
        "qual":     r"D:/proj/5MM.ansa_qual",
        "nas":      True,
        "repair":   "auto",
        "sheet_metal_fraction": 0.5,
        "max_sheet_thickness": 4.0,
        "hardware_area": 4000.0,   # below this a rejected part is a bolt, not a panel
        "name_rules": True,        # standard parts are identified by name/part number
        "standard_code_pattern": r"S\\d{6}$",
        "limit":    25,        # parts this invocation, 0 = no cap (chunking)
        "resume":   True,      # skip parts already finished in _progress.jsonl
        "order":    "size",    # "size" (small first) | "name"
        "max_attempts": 2,     # a part that kills ANSA twice is recorded as crash
    }
    exec(open(r"<skill>/scripts/process_folder.py", encoding="utf-8").read(), globals())
    RESULT

The summary is the point of this script: a run over 200 parts must end with an
explicit list of the parts that were **not sheet metal**, the parts whose middle
surface could **not be extracted**, and the parts that failed outright. Never
report only the successes - a silently skipped part looks like a clean run.

A long run is chunked by the caller: every invocation processes at most `limit`
parts and appends one JSON line per part to `<out_dir>/_progress.jsonl`. Kill the
process, run it again, and it resumes where it stopped. A part that begins but
never ends (ANSA crashed on it) is retried `max_attempts` times and then written
off as `crash` so a single poisonous file cannot stall the queue.

Status values per part:
    done              shell mesh written
    geometry_only     Skin produced no faces after repair; geometry kept
    hardware          standard part: the part-number block or the Chinese
                      Nomenclature says so, or (when unnamed) the surface area is
                      below `hardware_area` - not a failure, needs nobody
    not_sheet_metal   dominant wall covers less than `sheet_metal_fraction`
    skin_failed       Skin refused and repair did not help
    crash             ANSA died on this part `max_attempts` times
    error             exception while processing

`hardware` is deliberately absent from `issues`: a run that lists 121 bolts next
to 12 real failures buries the 12. The count is reported as `n_hardware` instead.
"""

import csv
import json
import os
import re
import time
import traceback

try:
    base  # noqa: F821 - injected when run through the MCP server
except NameError:  # pragma: no cover
    from ansa import base, constants, mesh, session, utils, batchmesh

def _scripts_dir(override=None):
    """Where process_part.py lives.

    `__file__` is the *caller's* path when this module is exec'd from a runner
    script, so trusting it looks for process_part.py in the wrong folder. Probe
    the candidates and return the first that actually holds the file.
    """
    here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ""
    cands = [override, here,
             r"C:/Users/Dongmiao/.workbuddy/skills/ansa-midsurface-mesh/scripts",
             r"D:/workbuddySpace/hm/ANSA_MCP/skills/ansa-midsurface-mesh/scripts"]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "process_part.py")):
            return c
    raise FileNotFoundError("process_part.py not found; looked in %r" % (cands,))

PUBLISHED = {"done", "geometry_only", "not_sheet_metal", "skin_failed",
             "error", "crash", "hardware"}
CODE_RE = re.compile(r"([A-Za-z]\d{7,10})")
BINDEX_RE = re.compile(r"^([A-Za-z]\d+)_")     # B0029_ / O004_ / R007_


def load_pipeline(scripts_dir=None):
    """Reuse process_part so both scripts share one implementation."""
    ns = {"PARAMS": {}}
    exec(open(os.path.join(_scripts_dir(scripts_dir), "process_part.py"),
              encoding="utf-8").read(), ns)
    return ns["run"], ns["part_code"]


def collect(folder, pattern=".catpart", recursive=True):
    hits = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(pattern):
                    hits.append(os.path.join(root, f))
    else:
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(pattern):
                hits.append(p)
    return sorted(hits)


def unique_stems(files, part_code):
    """Distinct output stem per input file.

    The rule is `B0029_A0072600089.CATPart -> A0072600089`, but the folder holds
    several files that share one part number (the same stamping used at four
    assembly positions). Keep the plain code for the first one and suffix the
    rest with their B-index, otherwise they overwrite each other's `.ansa`.
    """
    seen, out = {}, {}
    for cat in files:
        code = part_code(cat)
        if code not in seen:
            seen[code] = 1
            out[cat] = code
        else:
            seen[code] += 1
            m = BINDEX_RE.match(os.path.basename(cat))
            tag = m.group(1) if m else str(seen[code])
            out[cat] = "%s__%s" % (code, tag)
    return out


def read_progress(path):
    """part -> {'ends': [...], 'begins': n}"""
    state = {}
    if not os.path.isfile(path):
        return state
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            p = rec.get("part")
            if not p:
                continue
            s = state.setdefault(p, {"ends": [], "begins": 0})
            if rec.get("event") == "begin":
                s["begins"] += 1
            elif rec.get("event") == "end":
                s["ends"].append(rec)
    return state


NOT_ISSUE = ("done", "hardware")


def summarise(rows):
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    issues = [r for r in rows if r["status"] not in NOT_ISSUE]
    return {"n_total": len(rows), "by_status": by_status,
            "n_ok": by_status.get("done", 0),
            "n_hardware": by_status.get("hardware", 0),
            "n_issues": len(issues),
            "issues": [{"part": r["part"], "code": r["code"], "status": r["status"],
                        "reason": r.get("reason") or r.get("note") or r.get("error"),
                        "estimated_T": r.get("estimated_T"),
                        "part_area": r.get("part_area"),
                        "part_name": r.get("part_name"),
                        "fraction": r.get("fraction")} for r in issues]}


def write_summary(out_dir, rows, folder, extra=None):
    """Rewrite 汇总.csv / 汇总.json from every row known so far."""
    order = {"done": 0, "hardware": 1, "geometry_only": 2, "skin_failed": 3,
             "not_sheet_metal": 4, "crash": 5, "error": 6}
    rows = sorted(rows, key=lambda r: (order.get(r["status"], 9), r["part"]))
    summary = summarise(rows)
    result = {"ok": summary["n_ok"] == summary["n_total"], "rows": rows,
              "summary": summary, "folder": folder}
    if extra:
        result.update(extra)
    csv_path = os.path.join(out_dir, "汇总.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["零件", "中文名称", "编号", "状态", "是否钣金", "薄壁面积占比", "表面积mm2",
                    "判定厚度mm", "是否修复", "中面面数", "其中check桶", "总面数",
                    "壳单元", "边长P50", "property", "ansa", "nas", "耗时s", "原因"])
        for r in rows:
            w.writerow([r["part"], r.get("part_name") or "", r["code"], r["status"],
                        "是" if r["is_sheet_metal"] else "否",
                        r["fraction"], r.get("part_area"), r["estimated_T"],
                        "是" if r["repaired"] else "否",
                        r["skin_faces"], r.get("skin_check"), r["n_FACE"], r["n_SHELL"],
                        r["edge_p50"],
                        "; ".join("%s(T=%s)" % (n, t) for n, t in (r["props"] or [])),
                        r["ansa"], r["nas"], r.get("seconds"), r["reason"] or ""])
    json_path = os.path.join(out_dir, "汇总.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
    result["summary_csv"] = csv_path
    result["summary_json"] = json_path
    return result


def run(params):
    folder = params.get("folder")
    out_dir = params.get("out_dir")
    if not folder or not os.path.isdir(folder):
        return {"ok": False, "error": "folder not found: %s" % folder}
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    all_files = collect(folder, str(params.get("pattern", ".catpart")).lower(),
                        bool(params.get("recursive", True)))
    if not all_files:
        return {"ok": False, "error": "no CATPart under %s" % folder}
    files = list(all_files)

    # A parallel run hands each worker an explicit list so the groups cannot
    # overlap; runners that do not need it leave `only` unset.
    only = params.get("only")
    if only and os.path.isfile(only):
        keep = {ln.strip() for ln in open(only, encoding="utf-8") if ln.strip()}
        files = [f for f in files if os.path.basename(f) in keep]
        if not files:
            return {"ok": True, "error": "nothing left for this worker",
                    "summary": {"n_total": 0, "n_ok": 0, "by_status": {}}}

    if str(params.get("order", "size")).lower() == "size":
        files = sorted(files, key=lambda p: os.path.getsize(p))

    run_part, part_code = load_pipeline(params.get("scripts_dir"))
    # Stems must be decided on the whole folder, not on this worker's slice,
    # otherwise a duplicate part number lands in two groups and both write the
    # same file name. Under `parallel_batch` the caller hands the map in, because
    # a worker only ever sees its own slice - the defect that produced 361 `.nas`
    # for 382 shells on the 540-part BIW run: 44 part numbers sit at 2-4 assembly
    # positions, so 115 files were silently overwritten by a sibling.
    stems_file = params.get("stems_file")
    if stems_file and os.path.isfile(stems_file):
        with open(stems_file, encoding="utf-8") as fh:
            shared = json.load(fh)
        stems = {cat: shared.get(os.path.basename(cat)) or part_code(cat)
                 for cat in all_files}
        dup = {}
        for cat, st in stems.items():
            dup.setdefault(st, []).append(os.path.basename(cat))
        collisions = {k: v for k, v in dup.items() if len(v) > 1}
        if collisions:
            print("STEM_COLLISION %d stems shared by several parts: %s"
                  % (len(collisions), list(collisions.items())[:3]))
    else:
        stems = unique_stems(all_files, part_code)
    progress_path = (params.get("progress") or
                     (os.path.join(out_dir, "_progress.jsonl") if out_dir else None))
    hw_area = float(params.get("hardware_area", 4000.0))

    # ------------------------------------------------------------- resume state
    state = read_progress(progress_path) if (progress_path and params.get("resume", True)) else {}
    rows, todo = [], []
    attempts_max = int(params.get("max_attempts", 2))
    for cat in files:
        name = os.path.basename(cat)
        st = state.get(name) or {}
        ends = st.get("ends") or []
        ok_end = None
        for e in ends:
            if e.get("status") in PUBLISHED and e.get("status") != "crash":
                ok_end = e
        if ok_end and not (params.get("redo_failed")
                           and ok_end.get("status") in ("skin_failed", "geometry_only",
                                                        "error", "crash", "hardware")):
            rows.append({k: ok_end.get(k) for k in
                         ("part", "code", "status", "is_sheet_metal", "fraction",
                          "estimated_T", "repaired", "skin_faces", "skin_check",
                          "n_FACE", "n_SHELL", "edge_p50", "props", "ansa", "nas",
                          "seconds", "reason", "part_area", "part_name")})
            rows[-1]["part"] = name
            continue
        if not ends and st.get("begins", 0) >= attempts_max:
            rows.append({"part": name, "code": part_code(cat), "status": "crash",
                         "is_sheet_metal": None, "fraction": None,
                         "estimated_T": None, "repaired": None, "skin_faces": None,
                         "part_area": None, "part_name": None,
                         "n_FACE": None, "n_SHELL": None, "edge_p50": None,
                         "props": [], "ansa": None, "nas": None, "seconds": None,
                         "reason": "ANSA 在该零件上中断 %d 次，已跳过" % st["begins"]})
            continue
        todo.append(cat)

    limit = int(params.get("limit", 0) or 0)
    batch = todo[:limit] if limit > 0 else todo

    # --------------------------------------------------------------- process
    pf = open(progress_path, "a", encoding="utf-8") if progress_path else None
    for i, cat in enumerate(batch, 1):
        name = os.path.basename(cat)
        code = part_code(cat)
        stem = stems[cat]
        t0 = time.time()
        if pf:
            pf.write(json.dumps({"event": "begin", "part": name, "ts": t0}) + "\n")
            pf.flush()
        p = {"catpart": cat, "engine": params.get("engine", "mesh"),
             "repair": params.get("repair", "auto"),
             "mpar": params.get("mpar"), "qual": params.get("qual"),
             "max_thickness": params.get("max_thickness", 6.0),
             "sheet_metal_fraction": params.get("sheet_metal_fraction", 0.5),
             "max_sheet_thickness": params.get("max_sheet_thickness", 4.0),
             "hardware_area": hw_area,
             "change_option": params.get("change_option", "macro perimeters")}
        if out_dir:
            p["save"] = os.path.join(out_dir, stem + ".ansa")
            if params.get("nas"):
                p["nas"] = os.path.join(out_dir, stem + ".nas")
        try:
            r = run_part(p)
        except Exception as exc:
            r = {"part": name, "code": code, "status": "error",
                 "error": str(exc), "tb": traceback.format_exc()[-800:]}
        secs = round(time.time() - t0, 1)
        cls = r.get("classify") or {}
        res = r.get("result") or {}
        row = {
            "part": name, "code": code, "tag": stem,
            "status": r.get("status", "error"),
            "is_sheet_metal": r.get("status") != "not_sheet_metal"
                              and bool(cls.get("is_sheet_metal")),
            "fraction": cls.get("fraction"),
            "estimated_T": cls.get("estimated_T"),
            "part_area": r.get("part_area"),
            "part_name": r.get("part_name"),
            "std_part": r.get("std_part"),
            "repaired": (r.get("skin") or {}).get("repaired"),
            "skin_faces": (r.get("skin") or {}).get("faces"),
            "skin_check": (r.get("skin") or {}).get("check"),
            "n_FACE": res.get("n_FACE"),
            "n_SHELL": res.get("n_SHELL"),
            "edge_p50": res.get("edge_p50"),
            "props": [(q.get("name"), q.get("T")) for q in (res.get("props") or [])],
            "ansa": r.get("saved"), "nas": r.get("nas"),
            "seconds": secs,
            "reason": r.get("error") or r.get("note")
                      or ("Skin produced no middle-surface faces" if
                          r.get("status") == "skin_failed" else None),
        }
        rows.append(row)
        if pf:
            pf.write(json.dumps(dict(row, event="end", ts=time.time()),
                                ensure_ascii=False, default=str) + "\n")
            pf.flush()
        if out_dir and params.get("write_summary", True):
            # A locked 汇总.csv (open in Excel/WPS) must not kill a long run.
            try:
                write_summary(out_dir, rows, folder,
                              {"n_todo": len(todo), "n_left": len(todo) - i,
                               "hardware_area": hw_area, "running": True})
            except Exception as exc:
                print("SUMMARY_WRITE_SKIPPED %s" % str(exc)[:120])
        print("FOLDER %d/%d %.1fs %-42s %-16s shells=%s T=%s" % (
            i, len(batch), secs, name, row["status"], row["n_SHELL"],
            row["estimated_T"]))
    if pf:
        pf.close()

    if out_dir and params.get("write_summary", True):
        result = write_summary(out_dir, rows, folder,
                               {"n_todo": len(todo), "n_left": len(todo) - len(batch),
                                "hardware_area": hw_area, "running": False})
    else:
        result = {"ok": False, "rows": rows, "summary": summarise(rows),
                  "folder": folder}
    # `ok` means nothing is left for a human: hardware is not an issue, so a
    # folder of 121 bolts and 104 panels is a clean run, not a 225-part failure.
    result["ok"] = result["summary"]["n_issues"] == 0
    result["n_done_this_run"] = len(batch)
    result["n_remaining"] = len(todo) - len(batch)
    return result


RESULT = run(globals().get("PARAMS", {}) or {})
RESULT

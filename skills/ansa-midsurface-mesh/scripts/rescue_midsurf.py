"""Rescue the parts `Skin` refuses by falling back to `MidSurfAuto`.

    PARAMS = {
        "folder":  r"D:/proj/hard_parts",
        "out_dir": r"D:/out_rescue",
        "length":  5.0,          # target element length, mm
        "thick":   1.0,          # detection hint only - the measurement overrides it
        "tolerance": 0.15,       # p2-p98 spread over median that still counts as one gauge
        "limit":   0,            # parts this invocation, 0 = all
        "resume":  True,
    }
    exec(open(r"<skill>/scripts/rescue_midsurf.py", encoding="utf-8").read(), globals())
    RESULT

Why this exists
---------------
`base.Skin` is the first choice but it gives up on parts that are still perfectly
shellable - on one assembly it refused 13 of 68 parts and `MidSurfAuto` went on to
mesh 11 of them. The rule is therefore: try `Skin`, and only call a part
non-shellable when **both** fail. `process_part.py` stops at Skin; this script is
the second half of that sentence, and it is deliberately separate so a part that
kills ANSA cannot take a whole batch with it.

Two traps that the API notes in `references/ansa-api-verified.md` spell out:

* `property="auto_create"` writes a PSHELL with `T` left at the **1.0 default**.
  A run that reports "1318 shells, T=1.0" looks successful and is not - the
  measured wall lives on the elements, so it has to be read back with
  `base.GetShellThickness(deck, shell_entity)` (Entity, not id) and written into
  the property.
* min/max is not a verdict: one mis-paired element at 20 mm condemns a 3.2 mm part.
  Decide uniformity on p2/p98 over several hundred sampled elements.

Status values per part:
    rescued       MidSurfAuto produced shells, a single gauge written
    rescued_var   shells produced, but the spread says variable thickness -
                  median written, every element's own thickness kept in the log
    mid_failed    MidSurfAuto produced nothing: the part is a real solid
    error         exception
"""

import csv
import json
import math
import os
import time
import traceback

try:
    base  # noqa: F821 - injected when run through the MCP server
except NameError:  # pragma: no cover
    from ansa import base, constants, mesh, session, utils, batchmesh


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[min(len(sorted_vals) - 1, max(0, i))]


def measure(deck, created, sample=400):
    """(median, p2, p98, n_shells, n_sampled) from the elements' own thickness."""
    vals = []
    n_shells = 0
    for cp in created:
        shells = base.CollectEntities(deck, cp, "SHELL")
        n_shells += len(shells)
        step = max(1, len(shells) // sample) if shells else 1
        for s in shells[::step]:
            try:
                vals.append(float(base.GetShellThickness(deck, s)))
            except Exception:
                pass
    vals = [v for v in vals if v and v > 0]
    vals.sort()
    if not vals:
        return None, None, None, n_shells, 0
    return (_pct(vals, 0.5), _pct(vals, 0.02), _pct(vals, 0.98), n_shells, len(vals))


def write_gauge(deck, created, t, name=None):
    """Put `t` on every shell property MidSurfAuto created."""
    done = 0
    for cp in created:
        if not base.CollectEntities(deck, cp, "SHELL"):
            continue
        try:
            pid = base.GetEntityCardValues(deck, cp, ["PID"]).get("PID")
            pr = base.GetEntity(deck, "PSHELL", int(pid))
            base.SetEntityCardValues(deck, pr, {"T": t})
            if name:
                base.SetEntityCardValues(deck, pr, {"Name": name})
            done += 1
        except Exception:
            pass
    return done


def rescue_one(cat, params):
    name = os.path.basename(cat)
    code = os.path.splitext(name)[0].split("_")[1] if "_" in name else os.path.splitext(name)[0]
    log = {"part": name, "code": code}
    session.New("discard")
    base.Open(cat)
    deck = base.CurrentDeck()
    for ap in base.CollectEntities(deck, None, "ANSAPART"):
        if not base.CollectEntities(deck, ap, "FACE") and \
           not base.CollectEntities(deck, ap, "SHELL"):
            base.DeleteEntity(ap)
    solids = [ap for ap in base.CollectEntities(deck, None, "ANSAPART")
              if base.CollectEntities(deck, ap, "FACE")]
    if not solids:
        log.update({"status": "mid_failed", "note": "no solid geometry"})
        return log
    faces = []
    for s in solids:
        faces.extend(base.CollectEntities(deck, s, "FACE"))
    log["in_faces"] = len(faces)

    before = {x._id for x in base.CollectEntities(deck, None, "ANSAPART")}
    ret = base.MidSurfAuto(faces=faces, thick=float(params.get("thick", 1.0)),
                           length=float(params.get("length", 5.0)), elem_type=3,
                           part="auto_create", property="auto_create",
                           ret_ents=True, get_result_type=True)
    created = [x for x in base.CollectEntities(deck, None, "ANSAPART")
               if x._id not in before]
    log["created_parts"] = len(created)
    try:
        log["result_type"] = [str(r[2]) for r in (ret.type or [])]
    except Exception:
        log["result_type"] = []

    med, p2, p98, n_shells, n_sampled = measure(deck, created)
    log.update({"shells": n_shells, "sampled": n_sampled,
                "T_median": round(med, 3) if med else None,
                "T_p2": round(p2, 3) if p2 else None,
                "T_p98": round(p98, 3) if p98 else None})

    if not n_shells or not med:
        log["status"] = "mid_failed"
        log["note"] = "MidSurfAuto 未产生壳单元，判定为真实体"
        return log

    spread = (p98 - p2) / med if med else 1.0
    t_write = round(med, 2)
    log["spread"] = round(spread, 3)
    if spread <= float(params.get("tolerance", 0.15)):
        written = write_gauge(deck, created, t_write, "%s_T%d" % (code, round(med * 100)))
        log["status"] = "rescued"
        log["note"] = "MidSurfAuto 出壳 %d，单一厚度 %.2f mm（p2-p98 差 %.1f%%）" % (
            n_shells, t_write, spread * 100)
    else:
        written = write_gauge(deck, created, t_write, "%s_T%d_suspect" % (code, round(med * 100)))
        log["status"] = "rescued_var"
        log["note"] = ("MidSurfAuto 出壳 %d，厚度离散 %.1f%%（%.2f-%.2f mm），"
                       "暂写中位数 %.2f，需人工确认" % (n_shells, spread * 100, p2, p98, t_write))
    log["props_written"] = written

    out = params.get("out_dir")
    if out:
        os.makedirs(out, exist_ok=True)
        ansa_path = os.path.join(out, code + ".ansa")
        base.SaveAs(ansa_path)
        log["ansa"] = ansa_path
        nas = os.path.join(out, code + ".nas")
        try:
            if os.path.isfile(nas):
                os.remove(nas)
            base.OutputNastran(filename=nas)
            log["nas"] = nas
            log["nas_size"] = os.path.getsize(nas) if os.path.isfile(nas) else 0
        except Exception as exc:
            log["nas_err"] = str(exc)[:120]
    return log


def run(params):
    folder = params.get("folder")
    if not folder or not os.path.isdir(folder):
        return {"ok": False, "error": "folder not found: %s" % folder}
    out = params.get("out_dir")
    if out:
        os.makedirs(out, exist_ok=True)
    files = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(folder) for f in fs
                   if f.lower().endswith(".catpart"))
    progress = os.path.join(out, "_rescue_progress.jsonl") if out else None
    done = {}
    if progress and os.path.isfile(progress) and params.get("resume", True):
        for line in open(progress, encoding="utf-8", errors="ignore"):
            try:
                rec = json.loads(line)
                done[rec["part"]] = rec
            except Exception:
                pass
    limit = int(params.get("limit", 0) or 0)
    todo = [f for f in files if os.path.basename(f) not in done]
    batch = todo[:limit] if limit > 0 else todo
    rows = list(done.values())
    pf = open(progress, "a", encoding="utf-8") if progress else None
    for i, cat in enumerate(batch, 1):
        t0 = time.time()
        try:
            rec = rescue_one(cat, params)
        except Exception as exc:
            rec = {"part": os.path.basename(cat), "status": "error",
                   "error": str(exc)[:240], "tb": traceback.format_exc()[-600:]}
        rec["seconds"] = round(time.time() - t0, 1)
        rows.append(rec)
        if pf:
            pf.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            pf.flush()
        print("RESCUE %d/%d %5.1fs %-40s %-12s %s" % (
            i, len(batch), rec["seconds"], rec.get("part", ""), rec.get("status", ""),
            rec.get("note") or rec.get("error") or ""))
    if pf:
        pf.close()
    by = {}
    for r in rows:
        by[r.get("status")] = by.get(r.get("status"), 0) + 1
    if out:
        csv_path = os.path.join(out, "抢救清单.csv")
        cols = ["part", "code", "status", "shells", "T_median", "T_p2", "T_p98",
                "spread", "in_faces", "result_type", "seconds", "note", "error"]
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["零件", "编号", "状态", "壳单元", "T中位数", "T_p2", "T_p98",
                        "离散度", "输入面数", "结果类型", "耗时s", "说明", "错误"])
            for r in rows:
                w.writerow([r.get(k, "") if not isinstance(r.get(k), list)
                            else "; ".join(r.get(k)) for k in cols])
    else:
        csv_path = None
    return {"ok": by.get("error", 0) == 0, "n_total": len(rows), "by_status": by,
            "n_todo": len(todo), "n_left": len(todo) - len(batch),
            "rows": rows, "summary_csv": csv_path}


RESULT = run(globals().get("PARAMS", {}) or {})
RESULT

"""按处理阶段把 output 目录整理成可读的分类结构。

问题的来源：批处理把 540 个零件的 .ansa / .nas 全倒在同一个目录里，
926 个文件混在一起，一周后没人分得清哪件成功了、哪件失败了、哪件是兜底救回来的。

这个脚本做三件事：
  1. 按 汇总.csv 的「状态」列把产物分进 6 个分类目录
  2. 每个分类目录写一份 _清单.csv
  3. 在 out 根目录写 README.md，讲清每个目录里是什么

用法：
    python organize_output.py --out <output目录>              # 演练，只打印计划
    python organize_output.py --out <output目录> --apply       # 真移动

分类口径（对应 汇总.csv 的「状态」列）：

    hardware                                    -> 01_紧固件
    done                                        -> 02_Skin成功
    rescued / rescued_var                       -> 04_Casting成功
    not_sheet_metal                             -> 06_玻璃_未纳入
    timeout / casting_partial / casting_failed   -> 05_最终失败

    03_Skin失败 只放过程记录（所有 Skin 没出中面的件 + 去向），不放产物。

幂等：已经在分类目录里的文件不会被再移动；重复运行安全。
"""
import argparse
import csv
import glob
import json
import os
import shutil
import sys
from collections import Counter, defaultdict

CLASS = {
    "hardware": "01_紧固件",
    "done": "02_Skin成功",
    "rescued": "04_Casting成功",
    "rescued_var": "04_Casting成功",
    "not_sheet_metal": "06_玻璃_未纳入",
    "timeout": "05_最终失败",
    "casting_partial": "05_最终失败",
    "casting_failed": "05_最终失败",
}
SKIN_FAILED = ("rescued", "rescued_var", "not_sheet_metal",
               "timeout", "casting_partial", "casting_failed")
DEST = {
    "rescued": "04_Casting成功",
    "rescued_var": "04_Casting成功",
    "not_sheet_metal": "06_玻璃_未纳入",
    "timeout": "05_最终失败",
    "casting_partial": "05_最终失败",
    "casting_failed": "05_最终失败",
}

DIRS = ["00_总表", "01_紧固件", "02_Skin成功", "03_Skin失败",
        "04_Casting成功", "05_最终失败", "06_玻璃_未纳入", "_工作目录"]

WORK_DIRS = ["_ansa_tmp", "_audit", "_driver", "_driver_islands",
             "_driver_run1", "_driver_run2", "_repair", "_repair2",
             "_stale", "casting_probe", "probe_force", "casting", "glass"]

KEEP_AT_TOP = {"汇总.csv", "交付清单.csv", "未解决清单.csv", "校验不通过.csv",
               "_deck_stats.csv", "_progress.jsonl", "_casting_progress.jsonl"}

# 各目录的用途说明，写进 README
DESC = {
    "00_总表": ("540", "四张总清单 + 运行报告，先看这里"),
    "01_紧固件": ("129", "螺栓螺母类标准件，只有 .ansa（原始几何），不抽中面不划网格"),
    "02_Skin成功": ("382", "Skin 抽中面一次成功，每件 .ansa + .nas"),
    "03_Skin失败": ("29", "只是过程记录：Skin 没出中面的件清单，产物在 04/05/06"),
    "04_Casting成功": ("20", "Skin 失败后由 Casting 救回，每件 .ansa + .nas"),
    "05_最终失败": ("4", "两条路都没通，只有 .ansa（排查用）和失败原因"),
    "06_玻璃_未纳入": ("5", "固定玻璃，Casting 出了网格但未计入交付，等定"),
    "_工作目录": ("—", "驱动脚本、日志、旧版本备份。日常不用看"),
}

COLS = ["零件", "中文名称", "编号", "状态", "判定厚度mm", "壳单元",
        "连通块", "校验", "原因", "备注"]
HEAD = ["零件", "中文名称", "编号", "状态", "厚度mm", "壳单元",
        "连通块", "校验", "原因", "备注"]


def find_file(out, name, subdirs=("casting", "glass")):
    """在 out 根、然后再在 casting/glass 里找一个文件名。"""
    p = os.path.join(out, name)
    if os.path.isfile(p):
        return p
    for d in subdirs:
        p = os.path.join(out, d, name)
        if os.path.isfile(p):
            return p
    return None


def find_summary(out):
    """汇总.csv 可能在 out 根，也可能已被上一次整理移进 00_总表。"""
    for p in (os.path.join(out, "汇总.csv"),
              os.path.join(out, "00_总表", "汇总.csv")):
        if os.path.isfile(p):
            return p
    return None


def plan_moves(out):
    rows = list(csv.DictReader(open(find_summary(out), encoding="utf-8-sig")))
    plan = defaultdict(list)
    unknown = []

    for r in rows:
        st = r["状态"]
        cls = CLASS.get(st)
        if not cls:
            unknown.append((r["零件"], st))
            continue
        a = os.path.basename(r["ansa"]) if r.get("ansa") else ""
        n = os.path.basename(r["nas"]) if r.get("nas") else ""

        # 玻璃的 .ansa 优先用 glass/ 的重跑版本（顶层的可能是 Skin 失败的空库）
        if a:
            if cls == "06_玻璃_未纳入":
                cand = find_file(out, a, ("glass",))
            else:
                cand = find_file(out, a, ("casting",))
            if cand:
                plan[cls].append((cand, a))

        if n:
            cand = find_file(out, n, ("casting", "glass"))
            if cand:
                plan[cls].append((cand, n))

    # 没有清单记录的散落 .ansa/.nas：失败的排查产物，归 05
    known = set()
    for r in rows:
        for k in ("ansa", "nas"):
            if r.get(k):
                known.add(os.path.basename(r[k]))
    for f in os.listdir(out):
        p = os.path.join(out, f)
        if os.path.isfile(p) and (f.endswith(".ansa") or f.endswith(".nas")) and f not in known:
            plan["05_最终失败"].append((p, f))

    # 总表
    for f in os.listdir(out):
        p = os.path.join(out, f)
        if os.path.isfile(p) and (f in KEEP_AT_TOP or f.endswith(".md")):
            plan["00_总表"].append((p, f))

    # 玻璃的 .nas 清单里没记，从 glass/ 补
    if os.path.isdir(os.path.join(out, "glass")):
        have = {t for s, t in plan.get("06_玻璃_未纳入", [])}
        for src in sorted(glob.glob(os.path.join(out, "glass", "*.nas"))):
            name = os.path.basename(src)
            if name not in have:
                plan["06_玻璃_未纳入"].append((src, name))

    return plan, rows, unknown


def write_lists(out, rows):
    os.makedirs(os.path.join(out, "03_Skin失败"), exist_ok=True)
    groups = defaultdict(list)
    for r in rows:
        d = CLASS.get(r["状态"])
        if d:
            groups[d].append(r)

    for d, rs in groups.items():
        p = os.path.join(out, d, "_清单.csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(HEAD)
            for r in sorted(rs, key=lambda x: x["零件"]):
                w.writerow([(r.get(c) or "").strip() for c in COLS])

    skin_failed = [r for r in rows if r["状态"] in SKIN_FAILED]
    p = os.path.join(out, "03_Skin失败", "_清单.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["零件", "中文名称", "编号", "状态", "厚度mm", "壳单元",
                    "校验", "原因", "去向"])
        for r in sorted(skin_failed, key=lambda x: x["零件"]):
            w.writerow([r["零件"], r["中文名称"], r["编号"], r["状态"],
                        (r.get("判定厚度mm") or "").strip(),
                        (r.get("壳单元") or "").strip(),
                        (r.get("校验") or "").strip(),
                        (r.get("原因") or r.get("备注") or "").strip(),
                        DEST.get(r["状态"], "")])
    return groups, skin_failed


def write_readme(out, rows, groups):
    n_skin = len(groups.get("02_Skin成功", []))
    n_cast = len(groups.get("04_Casting成功", []))
    total = n_skin + n_cast
    lines = [
        "# 交付目录说明",
        "",
        "> 本目录按**处理阶段**分类，每个文件夹里有 `_清单.csv` 列出该类的全部零件。",
        "> 由 `organize_output.py` 自动生成，重跑批处理后重新执行该脚本即可更新。",
        "",
        "## 一眼看懂",
        "",
        "| 文件夹 | 件数 | 里面有什么 |",
        "|---|---|---|",
    ]
    for d in DIRS:
        n, txt = DESC[d]
        # 用实际件数覆盖静态描述里的数字
        if d in groups:
            n = str(len(groups[d]))
        lines.append("| `%s/` | %s | %s |" % (d, n, txt))
    lines += [
        "",
        "**交付总数 = %d 件**（Skin %d + Casting %d）。" % (total, n_skin, n_cast),
        "",
        "## 处理流程与去向",
        "",
        "```",
        "540 件目标件",
        "   │",
        "   ├─ 第一步：分离紧固件 ──────────────► 01_紧固件",
        "   │",
        "   ├─ 第二步：Skin 抽中面 + 划网格",
        "   │     ├─ 成功 ────────────────────► 02_Skin成功",
        "   │     └─ 失败 ──┐（记录在 03_Skin失败）",
        "   │               │",
        "   ├─ 第三步：Casting 兜底 ◄───────────┘",
        "   │     ├─ 成功 ────────────────────► 04_Casting成功",
        "   │     └─ 失败 ────────────────────► 05_最终失败",
        "   │",
        "   └─ 特例：玻璃 ─────────────────────► 06_玻璃_未纳入",
        "```",
        "",
        "`03_Skin失败/_清单.csv` 有一列「**去向**」，直接说明这件最后落在哪个文件夹。",
        "同一件可能既出现在 03 的清单里、产物又实体存在于 04——03 是过程账，不是结果账。",
        "",
        "## 怎么查某一件",
        "",
        "1. **已知件号**：在各文件夹搜文件名，或打开对应 `_清单.csv` 筛「编号」列。",
        "2. **已知零件名**：打开 `00_总表/汇总.csv` 筛「中文名称」，看「状态」列。",
        "3. **想知道为什么失败**：`00_总表/汇总.csv` 的「原因」列。",
        "",
        "`00_总表/汇总.csv` 是**唯一权威表**，一件一行，其他清单都是它的子集。",
        "",
        "## 文件命名",
        "",
        "| 后缀 | 含义 |",
        "|---|---|",
        "| `.ansa` | ANSA 数据库，含中面 + 网格 + PSHELL 属性 |",
        "| `.nas` | Nastran 求解文件，含壳网格 + MAT1 + PSHELL（T = 实测壁厚） |",
        "",
        "同名多实例（同一件号装在多个位置）的产物名带双下划线后缀，",
        "例如 `A0072600028__O032.ansa` = 件号 A0072600028 在 O032 装配位置的实例。",
        "",
        "## 已知问题",
        "",
        "1. **紧固件没有网格**——标准件按设计不抽中面，整车装配时需单独处理。",
        "2. **零件之间没有连接**——各件独立网格，节点不共享。整车求解前必须补焊点/胶接。",
        "3. **玻璃材质是钢的默认值**——用之前必须换成玻璃材料。",
        "",
        "## 可追溯性",
        "",
        "| 想确认 | 去哪看 |",
        "|---|---|",
        "| 每件的耗时、面数、四边形占比 | `00_总表/汇总.csv` 全字段 |",
        "| 每件的校验结果 | `00_总表/校验不通过.csv` |",
        "| 单元质量统计 | `00_总表/_deck_stats.csv` |",
        "| 某个文件是不是被重跑覆盖过 | `_工作目录/_旧版本_Skin阶段/` |",
    ]
    open(os.path.join(out, "README.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output 目录")
    ap.add_argument("--apply", action="store_true", help="真移动；不加只演练")
    ap.add_argument("--no-readme", action="store_true", help="不生成 README")
    args = ap.parse_args()
    out = os.path.abspath(args.out)

    if find_summary(out) is None:
        print("找不到 %s/汇总.csv（或 00_总表/汇总.csv），先跑 make_summary.py" % out)
        return 2

    plan, rows, unknown = plan_moves(out)

    print("=" * 64)
    print("整理计划" + ("（真执行）" if args.apply else "（演练，加 --apply 真移动）"))
    print("=" * 64)
    for d in DIRS:
        n = len(plan.get(d, []))
        if n:
            na = sum(1 for s, t in plan[d] if t.endswith(".ansa"))
            nn = sum(1 for s, t in plan[d] if t.endswith(".nas"))
            print("  %-16s %4d 个  (ansa %d / nas %d)" % (d, n, na, nn))
    if unknown:
        print("  !! 未知状态 %d 条: %s" % (len(unknown), unknown[:5]))

    # 对账：计划移动数 + 已在目标目录里的数 = 实际应有多少
    want = defaultdict(int)
    for r in rows:
        d = CLASS.get(r["状态"])
        if d:
            want[d] += 1
    print()
    print("%-16s %6s %8s %8s" % ("目录", "应有件", "待移动", "已在位"))
    bad = 0
    for d in DIRS:
        if d in ("00_总表", "03_Skin失败", "_工作目录"):
            continue
        pending = len(plan.get(d, []))
        onplace = 0
        p = os.path.join(out, d)
        if os.path.isdir(p):
            onplace = sum(1 for f in os.listdir(p)
                          if f.endswith(".ansa") or f.endswith(".nas"))
        w = want.get(d, 0)
        # 01 只有 ansa；02/04/06 应 ansa+nas 各一份；05 只有 ansa
        expect = w if d in ("01_紧固件", "05_最终失败") else w * 2
        got = pending + onplace
        mark = "" if got == expect else "  <== 差 %d" % (expect - got)
        if got != expect:
            bad += 1
        print("%-16s %6d %8d %8d%s" % (d, w, pending, onplace, mark))

    if not args.apply:
        print()
        print("演练结束。确认无误后加 --apply。")
        return 0 if bad == 0 else 1

    for d in DIRS:
        os.makedirs(os.path.join(out, d), exist_ok=True)

    moved, errors = Counter(), []
    for d, items in plan.items():
        for src, name in items:
            dst = os.path.join(out, d, name)
            if os.path.abspath(src) == os.path.abspath(dst):
                continue
            if os.path.exists(dst):
                continue          # 幂等：目标已存在就跳过
            try:
                shutil.move(src, dst)
                moved[d] += 1
            except Exception as exc:
                errors.append("%s -> %s: %s" % (src, d, exc))

    for w in WORK_DIRS:
        src = os.path.join(out, w)
        dst = os.path.join(out, "_工作目录", w)
        if os.path.isdir(src) and not os.path.exists(dst):
            shutil.move(src, dst)
            moved["_工作目录"] += 1

    groups, skin_failed = write_lists(out, rows)
    if not args.no_readme:
        write_readme(out, rows, groups)

    print()
    for d in DIRS:
        if moved.get(d):
            print("  移动 %-16s %d" % (d, moved[d]))
    print("  清单已更新，README.md 已生成")
    if errors:
        print("  错误 %d 条：" % len(errors))
        for e in errors[:10]:
            print("   ", e)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

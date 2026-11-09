---
name: ansa-midsurface-mesh
description: Extract mid-surfaces from thin-walled CAD geometry in ANSA and generate a shell mesh at a given target element size, with the measured wall thickness written into PSHELL properties. Covers both meshing engines - `mesh.Mesh` (Shell Mesh > Mesh) and a Batch Mesh session - driven by the user's `.ansa_mpar` / `.ansa_qual` files, plus the geometry repair that makes `base.Skin` succeed and the Casting second pass for parts Skin refuses. This skill should be used for requests such as "抽中面并划 5mm 网格", "钣金件抽中面并赋予正确厚度", "用批处理/批处理网格划网格", "midsurface + shell mesh for the sheet metal parts", "把 CAD 面模型变成壳网格并导出求解文件", or whenever an ANSA database full of closed thin-walled surfaces must become a solver-ready shell mesh. Also use it when a mesh built by this skill is judged worse than one built by hand in ANSA, or when a user questions whether their mpar/qual files were honoured.
agent_created: true
---

# ANSA mid-surface + shell mesh

Turn a closed thin-walled CAD description into a solver-ready shell mesh: pull a
mid-surface out of every sheet metal part, mesh it at a target element size, and
put the measured wall thickness into the PSHELL.

## What the result must contain

- one shell property per gauge, `T` equal to the **measured** thickness
- element count consistent with the mid-surface area and the target length
- no solid faces left behind for the parts that were shelled
- solver decks whose section thickness matches the model

## When to use

A closed thin-walled solid description (`FACE`/`CONS` present, `SOLID` and `SHELL`
at zero) that has to become a shell mesh.

Not for tetrahedral/volume meshing, not for re-meshing an existing shell model, not
for connections and welds - parts are meshed independently and therefore share no
nodes; assembling them into one deck is a separate problem with its own inputs and
its own acceptance test.

**Only two engines may produce elements: `mesh.Mesh` or a Batch Mesh session.**
Anything else - including `MidSurfAuto`'s built-in mesher - is a third algorithm
that the project's `.ansa_mpar` / `.ansa_qual` do not control. Surfaces may come
from `base.Skin` or from Casting; elements come from those two engines.

**The one sanctioned exception is `autosurf` in 0e.** When Casting cannot produce a
*continuous* surface (perimeter pairing below ~40%), the choice is between a
parameter-exact mesh in 5 disconnected blocks and a stitched mesh from a third
mesher. Take the stitched mesh, and say in the report which engine made it.

**A part `base.Skin` refuses is not automatically a solid.** Skin failing and a part
being thick-walled return the same `0`. Treat the failure as *unknown*, repair the
geometry, and if it still refuses, run the Casting second pass (`casting_midsurf.py`,
below). Never declare a part un-shellable on Skin's verdict alone.

## Prerequisites

- `ansa_status` reports `connected: true`, or drive the scripts from `ansa64.bat
  -nogui -nolauncher -exec "load_script: '<file>'"` (see *Batch mode*). `-execscript
  "file.py|main()"` fails: `cmd.exe` eats the `|`. Keep paths space-free, start the
  bat from PowerShell.
- Target element length, and the project's `.ansa_mpar` / `.ansa_qual`.
- `max_thickness` is a **hard filter, not a clamp** - a wall thicker than it makes
  Skin return 0 and the part then looks exactly like a solid. Automotive gauges go
  to 3.5 mm, so 3.0 is too tight for a body-in-white model: use 4.0-6.0.
- Verified on ANSA 24.1.2 with a NASTRAN deck. On another release, re-verify before
  trusting the rest.

## The two engines

Both are ready in `scripts/mesh_engine.py`: set `PARAMS["engine"]` to `"mesh"` or
`"batchmesh"`. Both read the handed mpar/qual; `mesh.Mesh` additionally needs
`change_option` set every run (it is an ANSA default, not part of the `.ansa`).

| | `mesh.Mesh` | Batch Mesh session |
|---|---|---|
| call | `mesh.Mesh(faces)` | `GetNewSession` + `ReadSession*` + `RunMeshingScenario` |
| feature treatment | full, incl. geometry-altering | as configured, no change option |
| scale | one database at a time | many parts, parallel, repeatable |

`change_option` values: `macro perimeters` (perimeters may be joined/released, all
feature treatments incl. fillet sharpening), `perimeter grids` (only perimeter node
distribution, no geometry-altering treatments), `mesh` (perimeters untouched, no
feature treatment).

**Do not read a quality difference into the engine choice.** The first pass of any
of them rewrites the perimeter node distribution, so a second pass measures the
second pass. Compare only first-mesh-of-a-fresh-Skin numbers, and say which pass a
number came from (`references/field-notes.md`).

## Workflow

`scripts/process_part.py` runs the whole chain for one part - classify, repair,
Skin, name properties, mesh, save - and returns one `RESULT`:

```python
PARAMS = {"catpart": r"D:/proj/B####_A##########.CATPart", "engine": "mesh",
          "mpar": r"D:/proj/5MM.ansa_mpar", "qual": r"D:/proj/5MM.ansa_qual",
          "max_thickness": 6.0, "hardware_area": 4000.0,
          "save": r"D:/out/A##########.ansa", "nas": r"D:/out/A##########.nas"}
exec(open(r"<skill>/scripts/process_part.py", encoding="utf-8").read(), globals())
RESULT
```

`scripts/process_folder.py` walks a folder, chunks the run with `limit`, resumes
from `_progress.jsonl`, and returns `RESULT["summary"]` with `by_status` and
`issues`. Same params plus `folder` / `out_dir`.

For anything over a few hundred parts, drive it with `scripts/parallel_batch.py`
instead of repeated `process_folder` calls - it runs outside the MCP server, so a
crash costs one part instead of a reconnect (`references/field-notes.md` explains
why the connector cannot survive this):

    python scripts/parallel_batch.py --src <parts> --out <out> \
        --mpar 5MM.ansa_mpar --qual 5MM.ansa_qual --workers 4

Workers scale roughly linearly until RAM or license slots run out: 6 workers on
16 cores / 64 GB took 540 parts in ~30 min (≈0.4-0.9 GB per worker). Re-run the
same command to resume - finished parts are read from the progress files.

It writes `_progress.jsonl`; finish with one `process_folder` call at `limit=0` to
produce the summary.

It also writes `_driver/stems.json` - the output-stem map decided on the whole
`--src` - and hands it to every worker. That is not optional: a worker only sees
its own slice, so a part number that appears at several assembly positions gets
the plain code in each slice and the files overwrite each other. On the 540-part
body-in-white model 44 part numbers sat at 2-4 positions: the first run delivered 452 `.ansa`
for 540 parts and 361 `.nas` for 382 shells, **with no error anywhere**.

`ansa64.bat` is resolved at import time: `$ANSA_BAT`, then `$ANSA_HOME`, then the
usual BETA install roots, newest folder first. Override with `--ana-bat` when the
machine holds several releases - the run is only as good as the version printed
by `base.Version`, so check it once per batch.

When the batch has finished there is **no ANSA process left**, and
`process_folder` cannot run outside ANSA (`base` is imported at module level).
Write the deliverable tables with `scripts/make_summary.py` - plain Python, reads
`_progress.jsonl`, applies the 0f gates and writes `汇总.csv` / `交付清单.csv` /
`未解决清单.csv` / `校验不通过.csv`:

    python scripts/make_summary.py --out <out> --target 5.0 [--connectivity]

### 0. Classify before touching anything

`base.DetectSolidDescription(estimate_thickness=True)` is the sheet-metal test and
the gauge estimate in one call: a list whose first element is
`{thickness: area_fraction}`; a dominant entry means a thin-walled body.

- Do **not** combine `estimate_thickness=True` with `return_percentage=True` - it
  raises "can not be both True".
- Keep `separate_connectivity` on; with `return_percentage=False` and connectivity
  off it returns a bare `1`/`0`, which is not a usable verdict.
- A result `<= 0` is a **negative sentinel** ("no consistent offset pair"), not
  "thick" - attempt Skin anyway.
- A dominant wall is not enough: keep `max_sheet_thickness` (default 4.0 mm) in the
  verdict, or a 7.45 mm weld-gun body reads as 100% thin.

### 0b. Standard parts are named, not measured

Run this **before** classification. Two signals, in order:

1. **Part-number block** - `standard_code_pattern` (default `S\d{6}$`). On a
   553-part body-in-white model the `A007S` block held 118 parts and every one was a standard
   part.
2. **CATIA Nomenclature** - a CATPart carries its Chinese name as
   `_Nomenclature` + one length byte + UTF-8 (`read_nomenclature`; ANSA only
   exposes the file stem). Keywords: 螺母/螺栓/螺柱/垫圈/螺钉/铆钉/卡扣,
   衬套/导套/螺套, 堵塞/堵盖.

3. **Surface area** only as the fallback for unnamed parts: `hardware_area`
   (default 4 000 mm2). Standard parts measured 360-3 412 mm2, the smallest real
   panel 4 739 mm2.

The name is **not** a verdict for structure - 托架 and 板 cover both 1 mm stampings
and thick castings, so sheet metal stays a geometry decision. A bolt must not cost
a `DetectSolidDescription` call or three Skin attempts.

### 0c. Property naming

```
B####_A##########.CATPart   -> code A##########
  original solid PSHELL     -> A##########_origin
  middle-surface PSHELL     -> A##########_T171      (1.709 mm)
```

Gauge suffix = measured wall in 0.01 mm, **rounded half up**: `floor(T*100 + 0.5)`,
not `round()`, so 1.705 does not fall to the even value. Rename after Skin (the
property does not exist before) and before meshing, so exported ELSET/section names
carry the code. No spaces or semicolons in property names - they break the Abaqus
`ELSET`.

### 0d. Every batch ends with an exception list

Reporting only the parts that succeeded is not acceptable: a skipped part is
invisible in a success count.

| status | meaning | what the user must be told |
|---|---|---|
| `done` | shell mesh written | element count, gauge, edge p50 |
| `hardware` | standard part (0b) | **nothing to do**; report the count (`n_hardware`) separately, it is **not** an issue |
| `not_sheet_metal` | wall below the area threshold, or thicker than `max_sheet_thickness` | the measured wall and why it was rejected |
| `skin_failed` | Skin produced no faces, Casting not yet tried | run `casting_midsurf.py` before calling it a failure |
| `geometry_only` | mesh requested, nothing produced | same as above |
| `crash` | ANSA died on the part `max_attempts` times | open it by hand; it was skipped, not processed |
| `error` | exception | the exception text |

A failing part must still be saved as geometry so the user can open it and see what
is missing.

Four defects a 289-part run exposed (details in `references/field-notes.md`):

- `Skin` returns three buckets - `skinned`, `check`, `failed`. A `check` group
  already has its middle surface; counting `skinned` alone writes off good parts.
- Every retry leaves a part behind (`new_part=True`), so keep only the last
  attempt's parts or the mesh lands on stacked surfaces.
- `DetectSolidDescription` returns `-1.0` for "cannot estimate" (see 0).
- Output names must not depend on processing order: compute the stem map once from
  the **name-sorted** full list and hand the same map to every worker.

The file name is not the number: `R###_A##########_WELD` -> `A##########`, i.e. the
second underscore token. A digit regex like `[A-Za-z]\d{7,10}` also throws away the
whole `A007S######` family. Duplicates get a `__<letter+digits>` suffix, and that
suffix regex must accept any letter prefix, not just `B`.

### 0e. Second pass for the parts Skin refuses

`scripts/casting_midsurf.py` has two paths. **The default is `autosurf`**, and it
changed on 2026-11-09 for a reason worth reading before switching back.

```python
PARAMS = {"folder": ..., "out_dir": ..., "nas": True}          # autosurf is the default
exec(open(r"<skill>/scripts/casting_midsurf.py", encoding="utf-8").read(), globals())
```

**`autosurf` - `base.MidSurfAuto`, one continuous patch.** It extracts the surface,
meshes it and stitches it in a single call. On a rear crash absorber (203 faces,
3.9 mm walls) it returned **one connected patch of 24 117 shells in 65 s**, where
the old path returned **5 disconnected blocks**.

The one parameter that is easy to misread is `thick` - the GUI's **Minimum
Thickness**. It is a *search lower bound*, so it is a **speed knob, not a quality
knob**. Swept on the test part (walls 2.5-5.4 mm):

| thick | 1.0 | 2.0 | 3.0 | 3.5 | 4.0 | 4.5 |
|---|---|---|---|---|---|---|
| seconds | 133 | 91 | 74 | 68 | 65 | 59 |
| shells | 23 923 | 24 178 | 23 859 | 24 281 | 24 516 | 24 195 |

Time halves from 1.0 to 4.5; the element count moves ±3% (noise). A small `thick`
is not wrong, just slow - it widens the range of face pairs the algorithm will
try. Take it from `DetectSolidDescription(estimate_thickness=True)`; the dominant
bin (3.5 at 19% area on the test part) is within a few tenths of the fastest
useful value.

`connect_weldings` did not change the block count on the test part (1 either way).
Block count is *not* perfectly deterministic: `thick=3.5` gave 2 blocks on one run
and 1 on another, at identical settings - re-run before treating a 2-block result
as a defect.

**`middle` - `MiddleGeometry` + your own engine**, opt in with `"casting": "middle"`.
Faster (8 s) and every element comes from `mesh.Mesh` / Batch Mesh driven by the
handed `.ansa_mpar` / `.ansa_qual`. But the surface is **discrete**: on the same
part `perimeters_success_rate` was 24.8%, so faces never really joined and the
mesh broke into 5 blocks. Stitching afterwards made it worse - `RebuildTopology`
gave 10 blocks, and rebuilding topology *before* Casting dropped the perimeter
rate to 19.5%. `thickness_tolerance` 1.0/3.0/5.0/8.0 gave 20/33/37/22%, so no
setting of it fixes this.

So the trade is **connectivity against control**: MidSurfAuto emits `SHELL` only
(no `FACE`), meshes with its own algorithm, and exposes only `length` - your
`.ansa_mpar` / `.ansa_qual` do not drive it. A single-block mesh is worth more
than a parameter-exact mesh that cannot be assembled, so `autosurf` is the default.
Pick `middle` only when the mpar/qual must be provably honoured.

Casting measures the wall while it builds, and `GetShellThickness` is valid right
after meshing - no `mesh.CalculateThickness` call (it also wants a third positional
`maximum_thickness`). Write the median into the PSHELL by hand, and rename to
`*_suspect` when the p2-p98 spread exceeds 15% of the median. MidSurfAuto leaves
the source solids in the deck: delete them before saving or the saved database
shows grey geometry over the mesh and roughly doubles in size.

**Run the second pass one part per process** when the set is a rescue list, not a
handful of brackets: `scripts/casting_batch.py` launches one `ansa64.bat` per part
with a hard timeout. The parts that reach this pass are exactly the ones that
**hang** rather than fail (`O067`/`O090` foam covers, `O139`/`O144`/`O145` NVH
strips), and in a single session the first hang eats the rest of the queue.

    python scripts/casting_batch.py --src <hard_parts> --out <out>/casting \
        --mpar 5MM.ansa_mpar --qual 5MM.ansa_qual --tol 2.0 --timeout 240

Budget **70 s per part** for `autosurf` (vs 8 s for `middle`). The per-part timeout
only needs to cover a slow `thick`, not a hang: even `thick=1.0` finished in 133 s
on a 203-face part, so 300 s is generous.

### 0f. Verify before reporting

All of these must hold, or the result is not done:

| gate | expectation |
|---|---|
| shell count | non-zero and close to `area / target^2` |
| `edge_p50` | within ~6% of target; 20%+ means the target length never applied |
| per-part `T` | equals the measured value, and is uniform |
| `elements_without_T` | **0 for a deliverable deck**, read from the PSHELL state |
| `placeholder_gauge` | any PSHELL still at `T = 1.0` means the gauge never arrived |
| **no shared output path** | two rows writing the same `.ansa` = one file silently lost |
| free-floating islands | a component of <=20 elements or <1% of the area is an unrestrained body; it makes the stiffness matrix singular |
| quad ratio | 0.8-0.95 on decks of 1000+ elements; ignore it below that, where the perimeter/count ratio dominates |
| faces left | only for the parts that were never shelled |

`scripts/deck_stats.py` measures the last three off a `.nas` without ANSA:
quad ratio, free edges per 1k elements, and `--blocks` for per-component area and
bbox. Use `--blocks` before calling a multi-component mid-surface "fragmented": on
a 4-block crash box the blocks were 60 000-341 000 mm2 walls (inner wall, outer
wall, bulkheads), which is a normal automotive body part, while the same scan found **three
2-16 element islands** elsewhere that really were debris.

### 0g. Exporting the deck

`base.OutputNastran(filename=...)` writes `MAT1` **only when the material's
`DEFINED` flag is `YES`**, and a CATPart import creates a placeholder with
`DEFINED = NO` - the deck then carries `PSHELL` pointing at `MID 1` with no `MAT1`
and nothing warns.

```python
for m in base.CollectEntities(deck, None, "__MATERIALS__"):
    base.SetEntityCardValues(deck, m, {"DEFINED": "YES"})
base.SaveAs(ansa)
base.OutputNastran(filename=nas)
```

Check `grep '^MAT1' file.nas`, not the return code. Creating a fresh MAT1 and
pointing `PSHELL.MID1` at it does not help: a new entity has the same
`DEFINED = NO`.

### 0h. Sort the output before handing it over

A finished batch leaves 900+ files in one flat directory - 540 `.ansa`, 400 `.nas`,
plus driver logs. Nobody can tell a success from a rescue a week later. Run:

    python scripts/organize_output.py --out <out>            # dry run, prints the plan
    python scripts/organize_output.py --out <out> --apply    # move + write lists

It sorts by the `状态` column of `汇总.csv` into six stage folders, writes a
`_清单.csv` into each, and generates `README.md` at the root:

| folder | comes from | contents |
|---|---|---|
| `01_紧固件` | `hardware` | standard parts, `.ansa` only, no mesh |
| `02_Skin成功` | `done` | `.ansa` + `.nas` |
| `03_Skin失败` | all non-`done` | **process record only** - a list with a `去向` column, no products |
| `04_Casting成功` | `rescued`, `rescued_var` | `.ansa` + `.nas` |
| `05_最终失败` | `timeout`, `casting_partial`, `casting_failed` | `.ansa` (for debugging) + reason |
| `06_玻璃_未纳入` | `not_sheet_metal` | `.ansa` + `.nas`, not counted in the delivery |

Two traps this script handles, both hit for real:

- **The same part number exists twice.** The Skin pass writes `<id>.ansa` at the
  output root; the Casting pass writes the *good* one into `out/casting/`. The root
  copy is the failed Skin database and is consistently smaller (8.2 MB vs 13.6 MB
  on one part). The script prefers `casting/` and parks the stale copy in
  `_工作目录/_旧版本_Skin阶段/`. Verify by size before trusting either.
- **Parts without a `汇总.csv` row** are leftovers from failed runs. They go to
  `05_最终失败`, not silently dropped.

The script is idempotent: already-sorted files stay put, so re-running after a
partial batch is safe. Its reconciliation table prints `应有件 / 待移动 / 已在位`
and flags any folder whose file count disagrees with the summary - treat a
non-empty `差 N` column as a bug, not a rounding error.

## Decision rules that matter

**`max_thickness` filters, it does not clamp.** A part above the cap returns 0 and
becomes indistinguishable from a solid; a `T` that equals the cap is not evidence of
clipping (four parts really were 3.000 mm at both a 3.0 and a 6.0 cap).

**Skin says nothing reliable about "is this sheet metal".** On one model it returned
0 for 13 parts up to a 40 mm cap. Repair, then Casting - in that order.

**Repair before giving up.** `base.checks.general.Geometry()` then `try_fix()` in a
loop, watching the FACE count (`try_fix` returns nothing). Do **not** follow it with
`RebuildTopology` - on B0025 that dropped Skin straight back to 0, and it also
failed where the check fixed it. `base.CheckAndFixGeometry` is deprecated in v24 and
silently skips two of the GUI's default checks.

**Owner of the middle surface.** `base.Skin(apply_thickness=True, new_pid=True,
new_part=True)` puts it in its own part with a PSHELL whose `T` is the measured
wall. Select it through the newly created property, **never** by face-id diff -
Skin renumbers faces.

**One T per part, from the median.** `CreatePropertiesFromShellsThickness` splits
fillet regions into a spurious second gauge on stampings; use it only for genuinely
variable-thickness geometry.

**Thickness is a measurement, not a hint.** Skin writes it; `MidSurfAuto`'s `thick`
argument only seeds detection; Casting's gauge lands on the elements. All agree.

## Pitfalls that cost real time

- `base.GetShellThickness(deck, entity)` takes the **entity**, not the id.
- After reopening a saved `.ansa`, `CollectEntities(deck, prop, "SHELL")` returns 0
  even though the elements exist. Link shells through the element card's `PID`.
- NASTRAN nodes are `GRID`, not `NODE`; a zero count is not evidence of absence.
  Coordinates are `X1/X2/X3`, connectivity `G1..G4`; read connectivity with
  `CollectEntities(deck, shell, "GRID")`.
- `base.ElementQuality` resolves `ASPECT`, `SKEW`, `WARP`, `TAPER`, `CRASH`,
  `SQUISH`; `JACOBIAN` and min/max length return the string `'error'` - use
  `base.CalculateOffElements(details=True)` for those.
- `base.checks` has only `general`, `geometry`, `mesh`, `penetration`.
- `base.SaveAs` and `Output*` return `0`/`1`; check the written file size.
- `base.IsPartForSkin` is wrong in both directions and not monotonic in its
  tolerance (bolts returned False, 6-face solid blocks returned True). Never decide
  from it alone.
- A naming convention is not a classification: `_P_` vs `_WP_` held on one bumper
  model by coincidence.
- `mesh.BatchGenerator()` ignores the target length, and no `SaveMeshParams` edit
  fixes that (`references/field-notes.md`).
- One part per file: `session.New("discard")` + `base.Open(cat)`, then `SaveAs`.
  Opening a second CATPart **replaces** the database instead of merging.

## Reporting

State the element count, the actual measured element size, and the per-part
thickness table. Keep three claims apart: the mesh exists, the thickness is
measured, the model is engineering-valid (no automatic step asserts the third).
Say that parts are meshed independently and therefore share no nodes.

## Bundled resources

- `scripts/process_part.py` - one-part pipeline: name/part-number gate, classify,
  repair, `Skin`, naming, mesh, save + export.
- `scripts/process_folder.py` - folder driver: `_progress.jsonl`, resume, chunking,
  worker subsets, mandatory exception list.
- `scripts/mesh_engine.py` - meshing only (`mesh.Mesh` or Batch Mesh).
- `scripts/casting_midsurf.py` - **second pass**: `MiddleGeometry` (faces only) then
  the chosen engine. Use for every part Skin refuses. Accepts a `folder` or a
  single `catpart`.
- `scripts/casting_batch.py` - the same second pass as **one ANSA process per
  part**, with a per-part timeout: a part that hangs costs one part, not the queue.
- `scripts/parallel_batch.py` - N ANSA batch processes over one folder, outside the
  MCP path: hardlinked slices, per-worker progress, stall watchdog, auto-restart.
  The only sane way to run hundreds of parts.
- `scripts/make_summary.py` - plain-Python reporting for a finished `parallel_batch`
  run: reads `_progress.jsonl`, applies the 0f gates (including "does the `.nas`
  really carry `MAT1`" and "two rows sharing one output path"), writes the four
  deliverable CSVs, and can scan every deck for disjoint components.
- `scripts/organize_output.py` - sorts a flat output directory into the six stage
  folders of 0h, writes a `_清单.csv` per folder and a `README.md` at the root.
  Idempotent, dry-run by default. Run it before every handover.
- `scripts/deck_stats.py` - per-deck statistics off the `.nas`: quad ratio, free
  edges per 1k, and `--blocks` for per-component area and bbox. This is how you
  tell a genuinely fragmented mid-surface from an inner-wall/outer-wall part.
- `scripts/casting_midsurf.py` - **second pass**, two paths: `autosurf` (default,
  `MidSurfAuto`, one connected patch, ~70 s/part) and `middle` (`MiddleGeometry` +
  your own engine, ~8 s/part, discrete faces). See 0e.
- `scripts/rescue_midsurf.py` - older standalone `MidSurfAuto` wrapper; superseded
  by `casting_midsurf.py --casting autosurf`, kept for the ad-hoc single part.
- `scripts/inspect_model.py` - read-only survey: entity counts, per-part bbox and
  area, property thickness, deck verdict.
- `references/ansa-api-verified.md` - exact signatures, versions, negative results.
- `references/field-notes.md` - the measurements behind the rules above.

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
for connections and welds.

**Only two engines may produce elements: `mesh.Mesh` or a Batch Mesh session.**
Anything else - including `MidSurfAuto`'s built-in mesher - is a third algorithm
that the project's `.ansa_mpar` / `.ansa_qual` do not control. Surfaces may come
from `base.Skin` or from Casting; elements come from those two engines.

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
  to 3.5 mm, so 3.0 is too tight for a BIW model: use 4.0-6.0.
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
PARAMS = {"catpart": r"D:/proj/B0029_A0072600089.CATPart", "engine": "mesh",
          "mpar": r"D:/proj/5MM.ansa_mpar", "qual": r"D:/proj/5MM.ansa_qual",
          "max_thickness": 6.0, "hardware_area": 4000.0,
          "save": r"D:/out/A0072600089.ansa", "nas": r"D:/out/A0072600089.nas"}
exec(open(r"<skill>/scripts/process_part.py", encoding="utf-8").read(), globals())
RESULT
```

`scripts/process_folder.py` walks a folder, chunks the run with `limit`, resumes
from `_progress.jsonl`, and returns `RESULT["summary"]` with `by_status` and
`issues`. Same params plus `folder` / `out_dir`.

Run 3-4 workers on disjoint file lists for a big folder: one process needed ~80 min
for the last 130 parts, three brought it under 35 (≈0.4-0.9 GB each; 16 cores / 64 GB
is comfortable).

For anything over a few hundred parts, drive it with `scripts/parallel_batch.py`
instead of repeated `process_folder` calls - it runs outside the MCP server, so a
crash costs one part instead of a reconnect (`references/field-notes.md` explains
why the connector cannot survive this):

    python scripts/parallel_batch.py --src <parts> --out <out> \
        --mpar 5MM.ansa_mpar --qual 5MM.ansa_qual --workers 4

It writes `_progress.jsonl`; finish with one `process_folder` call at `limit=0` to
produce the summary.

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
   553-part BIW body the `A007S` block held 118 parts and every one was a standard
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
B0029_A0072600089.CATPart   -> code A0072600089
  original solid PSHELL     -> A0072600089_origin
  middle-surface PSHELL     -> A0072600089_T171      (1.709 mm)
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

The file name is not the number: `R007_A0072600643_WELD` -> `A0072600643`, i.e. the
second underscore token. A digit regex like `[A-Za-z]\d{7,10}` also throws away the
whole `A007S030430` family. Duplicates get a `__<letter+digits>` suffix, and that
suffix regex must accept any letter prefix, not just `B`.

### 0e. Second pass for the parts Skin refuses

`scripts/casting_midsurf.py` calls `base.MiddleGeometry(faces,
thickness_tolerance)` - the Casting algorithm with the meshing stripped out - and
hands the resulting **faces** to the engine chosen above:

```python
PARAMS = {"folder": ..., "out_dir": ..., "engine": "mesh",
          "mpar": ..., "qual": ..., "limit": 0, "nas": False}
exec(open(r"<skill>/scripts/casting_midsurf.py", encoding="utf-8").read(), globals())
```

Seven parts Skin had rejected: 100% face success on every one, 50 s total, gauges
1.02-3.42 mm (`references/field-notes.md` has the table). Casting measures the wall
while it builds the faces and the mesher carries it onto the elements, so
`GetShellThickness` is valid right after meshing - no `mesh.CalculateThickness`
call (it also wants a third positional `maximum_thickness`). The PSHELL keeps its
1.0 default: write the median by hand, and rename to `*_suspect` when the p2-p98
spread exceeds 15% of the median.

`scripts/rescue_midsurf.py` does the same with `MidSurfAuto`. It works, but
MidSurfAuto emits a mesh-only part (**1306 SHELL / 0 FACE**, verified) with its own
mesher, so use it only when a free mesh is explicitly acceptable.

### 0f. Verify before reporting

All of these must hold, or the result is not done:

| gate | expectation |
|---|---|
| shell count | non-zero and close to `area / target^2` |
| `edge_p50` | within ~6% of target; 20%+ means the target length never applied |
| per-part `T` | equals the measured value, and is uniform |
| `elements_without_T` | **0 for a deliverable deck**, read from the PSHELL state |
| `placeholder_gauge` | any PSHELL still at `T = 1.0` means the gauge never arrived |
| faces left | only for the parts that were never shelled |

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
  the chosen engine. Use for every part Skin refuses.
- `scripts/parallel_batch.py` - N ANSA batch processes over one folder, outside the
  MCP path: hardlinked slices, per-worker progress, stall watchdog, auto-restart.
  The only sane way to run hundreds of parts.
- `scripts/rescue_midsurf.py` - same second pass on `MidSurfAuto`; mesh-only output,
  keep only if a free mesh is acceptable.
- `scripts/inspect_model.py` - read-only survey: entity counts, per-part bbox and
  area, property thickness, deck verdict.
- `references/ansa-api-verified.md` - exact signatures, versions, negative results.
- `references/field-notes.md` - the measurements behind the rules above.

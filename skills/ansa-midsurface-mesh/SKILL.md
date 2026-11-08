---
name: ansa-midsurface-mesh
description: Extract mid-surfaces from thin-walled CAD geometry in ANSA and generate a shell mesh at a given target element size, with the measured wall thickness written into PSHELL properties. Covers both meshing engines - `mesh.Mesh` (Shell Mesh > Mesh) and a Batch Mesh session - driven by the user's `.ansa_mpar` / `.ansa_qual` files, plus the geometry repair that makes `base.Skin` succeed. This skill should be used for requests such as "抽中面并划 5mm 网格", "钣金件抽中面并赋予正确厚度", "用批处理/批处理网格划网格", "midsurface + shell mesh for the sheet metal parts", "把 CAD 面模型变成壳网格并导出求解文件", or whenever an ANSA database full of closed thin-walled surfaces must become a solver-ready shell mesh. Also use it when a mesh built by this skill is judged worse than one built by hand in ANSA, or when a user questions whether their mpar/qual files were honoured.
agent_created: true
---

# ANSA mid-surface + shell mesh

Turn a closed thin-walled CAD description into a solver-ready shell mesh: pull a
mid-surface out of every sheet metal part, mesh it at a target element size, and
put the measured wall thickness into the PSHELL.

## What the result must contain

- one shell property per gauge, `T` equal to the measured thickness (not a default)
- element count consistent with the mid-surface area and the target length
- no solid faces left behind for the parts that were shelled
- solver decks whose section thickness matches the model

## When to use

Use when a part is a closed thin-walled solid description (`FACE`/`CONS` present,
`SOLID` and `SHELL` counts at zero) and the goal is a shell mesh.

Do not use for tetrahedral/volume meshing, for re-meshing an existing shell model,
or for defining connections and welds. Those are separate tasks.

**A part that `base.Skin` refuses is not automatically a solid.** Skin failing and
a part being thick-walled look identical from the return value, and on a real BIW
assembly Skin rejected 13 of 68 parts while `MidSurfAuto` went on to build a clean
uniform mesh on 10 of them. Treat a Skin failure as *unknown* and retry with
MidSurfAuto; only call a part non-shellable when both fail.

**Repair the geometry before giving up on Skin.** Verified on ANSA 24.1.2: two
CATParts at 417 and 442 faces returned 0 for every `max_thickness` up to 40 mm,
then produced 135 and 147 middle-surface faces with the correct gauges (1.709 mm)
after a geometry check-and-fix.

Use the v24 API, not the deprecated one:

```python
# defaults == the ANSA Geometry check panel (Checks > Geometry)
obj = base.checks.general.Geometry()      # problematic_surfs, unchecked_faces,
                                          # collapsed_cons, triple_cons, needle_faces,
                                          # overlap_faces, needle_areas, cracks = 1
                                          # single_cons, sharp_edges = 0
for _ in range(3):                        # fixes can expose new errors
    reports = obj.execute()
    if not reports:
        break
    before = len(base.CollectEntities(deck, None, "FACE"))
    for r in reports:
        r.try_fix()
    if len(base.CollectEntities(deck, None, "FACE")) == before:
        break
```

- `base.CheckAndFixGeometry` was **deprecated in v24.0** and only accepts nine
  options; it has no `Problematic Surfaces` and no `Needle Areas`. A pipeline
  built on it silently skips two of the checks the GUI runs by default.
- `try_fix()` returns nothing even when it repairs something - watch the FACE
  count, not the return value.
- Do **not** follow the repair with `base.RebuildTopology`; on B0025 that dropped
  Skin straight back to 0. `RebuildTopology` alone also failed where the check
  fixed it, so they are not interchangeable.
- Repair may split faces (`RebuildTopology` took 417 faces to 551), and on a
  264-part run the extra checks turned one un-shellable part into 5 338 elements
  (`B0854`), but only 2 of 101 failures recovered overall - the rest are genuinely
  not thin-walled solids.
- `base.Skin(apply_thickness=True, new_pid=True, new_part=True)` puts the middle
  surface in its own part and gives it a PSHELL whose `T` is the measured wall.
  A Batch Mesh run over that part inherits the same PSHELL, so the mesh carries
  the measured gauge with no extra write step - verify it anyway, a fresh
  property with the placeholder 1.0 mm is the failure mode to look for.


## Prerequisites

- The `ansa` MCP server must be connected: `ansa_status` reports `connected: true`.
- Confirm the target element length.
- Confirm the maximum plausible sheet thickness. Be generous: `max_thickness` is a
  **hard filter, not a clamp** - a part whose wall exceeds it makes `Skin` return 0
  and the part then looks exactly like a solid. Real automotive gauges go to 3.5 mm
  (verified), so 3.0 is too tight for a BIW model; use 4.0-6.0.
- Everything below is verified on **ANSA 23.0.0 with a NASTRAN deck**. On another
  release, re-verify at gate 1 before trusting the rest.

## Running without the MCP (batch mode)

When the `ansa` MCP server is not loaded, drive the same scripts from the ANSA
batch launcher. Verified on **ANSA 24.1.2**:

```bat
ansa64.bat -nogui -nolauncher -exec "load_script: 'D:/work/probe.py'"
```

- The script runs at top level and ANSA **quits by itself** afterwards - no
  `session.Exit`, and `session.Exit` does not exist.
- Do **not** use `-execscript "file.py|main()"`: the `|` is eaten by `cmd.exe`
  and the tail of the argument is executed as a separate command.
- The `ansa64.bat` launcher breaks on arguments containing spaces, so keep every
  path space-free and start it from PowerShell (Git Bash mis-quotes the bat path).
- Importing several CAD files needs `base.Open()` + `base.SaveAs()` per file, then
  `utils.Merge(filename=..., model_action="merge_model")`. `base.Open()` a second
  time **replaces** the database instead of merging - measured: three CATParts
  opened in a row left 69 faces, not 928.

## Two meshing engines - `mesh.Mesh` vs Batch Mesh

When the user supplies `.ansa_mpar` / `.ansa_qual`, meshing must run through one
of these two engines, not through `MidSurfAuto(length=...)`, which ignores both
files. **Neither engine extracts midsurfaces** - build the middle-surface FACEs
first (`base.Skin`, see above), then mesh them.

Both are ready to call in `scripts/mesh_engine.py`: set `PARAMS["engine"]` to
`"mesh"` or `"batchmesh"` and exec the file. Verified on ANSA 24.1.2.

| | `mesh.Mesh` (Shell Mesh > Mesh) | Batch Mesh session |
|---|---|---|
| call | `mesh.Mesh(faces)` | `GetNewSession` + `ReadSession*` + `RunMeshingScenario` |
| feature treatment | full, including geometry-altering ones | as configured, no interactive change option |
| scale | one database at a time | many parts, parallel, repeatable |

**Do not read a quality difference into the engine choice.** In one session on
one Skin result (B0027), `mesh.Mesh` gave 204 elements / aspect max 2.864 the
first time and 218 / 2.345 the second time after deleting the mesh, while a Batch
Mesh run straight after gave 208 / 2.861. The gap that looks like "engine A is
better" is the **run order**: with `change_option = macro perimeters` the first
pass rewrites the perimeter node distribution, so the second pass starts from a
different perimeter. Compare engines only on the first mesh of a fresh Skin, or
the conclusion is measuring the second pass.

Two consequences:

- A re-mesh of a saved database is always a *second* pass. A Batch Mesh that must
  reproduce first-pass numbers has to run in the session where `Skin` executed.
- When reporting a comparison, state which pass each number came from, otherwise
  the reader attributes it to the engine.

### `mesh.Mesh` - the interactive generator

```python
from ansa import base, mesh
mesh.ReadMeshParams(mpar)                  # must both be called: the mesh
mesh.ReadQualityCriteria(qual)             # generator reads the active set
base.SetANSAdefaultsValues({"mesh_change_option": "macro perimeters"})
mesh.Mesh(faces)                           # list of FACE entities
```

`mesh_change_option` is an **ANSA default, not part of the .ansa**:

| value | behaviour |
|---|---|
| `macro perimeters` | perimeters may be joined/released, every feature treatment applies including geometry-altering ones (fillet sharpening) |
| `perimeter grids` | only perimeter node distribution changes; feature treatments except the geometry-altering ones |
| `mesh` | perimeters and their grids untouched, only interior mesh, no feature treatment |

Set it explicitly every run - a session that reopens a saved database inherits
whatever the machine happens to have.

### Batch Mesh session

```python
from ansa import mesh, batchmesh
mesh.ReadMeshParams(mpar)
mesh.ReadQualityCriteria(qual)
ses  = batchmesh.GetNewSession("BM_5MM")
batchmesh.ReadSessionMeshParams(ses, mpar)
batchmesh.ReadSessionQualityCriteria(ses, qual)
scen = batchmesh.GetNewMeshingScenario("SC_5MM", "PARTS")
batchmesh.AddPartToMeshingScenario(parts_with_faces, scen)
batchmesh.AddSessionToMeshingScenario([ses], scen)
batchmesh.RunMeshingScenario(scen, time_limit=20)
```

- Give it **parts that own FACEs**. A part holding only shells produces 0
  elements, because the session erases existing mesh
  (`existing_mesh_treatment = Erase`) and has no macro to re-mesh.
- `GetSessionMeshParamsName([ses])` returns `""` even after a successful
  `ReadSessionMeshParams` - do not use it as proof; use the roundtrip below.
- `mesh.FEMToSurf` is only a bridge for `MidSurfAuto` shells and leaves faceted,
  non-fused geometry behind; it needs `mesh.AutoPaste(shells=..., distance=0.1,
  allow_element_collapse=True)` afterwards. Prefer `base.Skin` FACEs instead.

### Checks that apply to both

- **Prove the files were loaded**, do not trust the return code: call
  `mesh.SaveMeshParams(tmp)` / `mesh.SaveQualityCriteria(tmp)` and grep the
  roundtrip for `target_element_length` and the criteria lines. A decisive test is
  to edit a copy of the mpar (e.g. target 5 -> 8) and confirm the mesh edge length
  follows; on a 12-face panel that moved p50 from 4.78 to 6.71 mm.
- An mpar that differs from the ANSA factory default in only a couple of lines
  produces a mesh almost identical to "no file at all" - that is the file, not a
  failed load. Diff against `mesh.SaveMeshParams()` on a fresh session before
  concluding anything about whether the file was used.
- **After reopening a saved `.ansa`, `CollectEntities(deck, prop_or_part, "SHELL")`
  returns 0** even though the elements are there. Link shells to properties
  through the element card's `PID` field instead, or a verification script will
  falsely report "no properties / no thickness".
- Check `placeholder_gauge`: any meshed PSHELL still at `T = 1.0` means the gauge
  never arrived from `Skin`.
- One part per file: `session.New("discard")` + `base.Open(cat)` per part, then
  `base.SaveAs(<part>.ansa)`. Keep the original solid FACEs and the `_MIDSURF`
  part so geometry and mesh are both in the file.

## Workflow

`scripts/process_part.py` runs the whole chain for one part - classify, repair,
Skin, name the properties, mesh, save - and returns one `RESULT`. Use it unless a
step needs a decision in between; the individual steps are below.

### 0. Classify before doing anything

`base.DetectSolidDescription(estimate_thickness=True)` is the sheet-metal test and
the gauge estimate in one call. It returns a list whose first element is
`{thickness: area_fraction}`; a dominant entry means a thin-walled body.

| part | dominant wall | fraction | Skin afterwards |
|---|---|---|---|
| 417 faces | 1.709 mm | 98.2% | fails until the geometry is repaired |
| 442 faces | 1.709 mm | 98.1% | fails until the geometry is repaired |
| 69 faces | 2.657 mm | 100% | succeeds directly |

- Do **not** pass `estimate_thickness=True` together with
  `return_percentage=True` - it raises "can not be both True".
- `separate_connectivity=False` with `return_percentage=False` returns a bare
  `1` / `0`, which is not a usable verdict; keep connectivity separation on.
- The estimated gauge matched what Skin and MidSurfAuto measured to three
  decimals on all three parts, so it is a real measurement, not a guess.

### 0b. Property naming

Default rule, override with `code` in `PARAMS`:

```
B0029_A0072600089.CATPart      -> code A0072600089
  original solid PSHELL        -> A0072600089_origin
  middle-surface PSHELL        -> A0072600089_T171      (1.709 mm)
```

The gauge suffix is the measured wall in **0.01 mm, rounded half up**:
1.702 -> 170, 1.709 -> 171, 1.720 -> 172, 2.657 -> 266. Use
`floor(T*100 + 0.5)`, not `round()`, so 1.705 does not fall to the even value.
Rename **after** Skin (the middle-surface property does not exist before it) and
before meshing, so the exported ELSET/section names carry the code.

### 0c. Exporting the Nastran deck

`base.OutputNastran(filename=...)` writes MAT1 **only when the material's
`DEFINED` flag is `YES`**. A CATPart import creates a placeholder material with
`DEFINED = NO`, so the deck comes out with `PSHELL` pointing at `MID 1` and no
`MAT1` card - it will not run, and nothing in the export warns about it.

```python
for m in base.CollectEntities(deck, None, "__MATERIALS__"):
    base.SetEntityCardValues(deck, m, {"DEFINED": "YES"})
base.SaveAs(ansa)                    # so the .ansa agrees with the deck
base.OutputNastran(filename=nas)
```

Check the result, not the return code: `grep '^MAT1' file.nas` must find a line
like `MAT1           1 210000.             0.3  7.85-9`. Creating a brand-new MAT1
and pointing `PSHELL.MID1` at it is **not** enough - a fresh entity has the same
`DEFINED = NO` problem.

### 0d. Batch runs must end with an exception list

`scripts/process_folder.py` walks a folder of CATParts and returns
`RESULT["summary"]` with `by_status` and `issues`. **Reporting only the parts
that succeeded is not acceptable** - a skipped part is invisible in a success
count.

| status | meaning | what the user must be told |
|---|---|---|
| `done` | shell mesh written | element count, gauge, edge p50 |
| `hardware` | standard part - the part-number block (`standard_code_pattern`, default `S\d{6}$`) or the CATIA Nomenclature says so, otherwise surface area below `hardware_area` (default 4 000 mm2) | **nothing to do**. A bolt, nut or clip has no offsettable surface pair; Skin cannot work on it at any setting. Report the count (`n_hardware`) separately - it is **not** an issue |
| `not_sheet_metal` | dominant wall below the area threshold, or thicker than `max_sheet_thickness` | the measured wall and why it was rejected |
| `skin_failed` | Skin returned no faces even after `checkfix` and `rebuild` | the part needs manual middlesurfacing (or `MidSurfAuto` as a fallback) |
| `geometry_only` | mesh requested but nothing was produced | same as above |
| `crash` | ANSA died on the part `max_attempts` times | the file must be opened by hand; it was skipped, not processed |
| `error` | exception | the exception text |

Three classification traps found on a real folder:

- **A bolt is not a failed panel.** Standard parts answer the thickness estimate
  exactly like a panel, so Skin is attempted, fails, and dumps them into
  `skin_failed` where they bury the panels that really need a human. Two signals
  separate them, in this order:

  1. **Name and part number** - a CATPart carries its Chinese Nomenclature in the
     binary as `_Nomenclature` + one length byte + UTF-8 (`read_nomenclature`
     decodes it; ANSA only exposes the file stem). On 553 real parts the `S` block
     of the part-number range held 118 parts and every one was a standard part;
     the names agree (`六角法兰焊接螺母`, `焊接螺柱`, `车身堵塞`). This runs before
     classification, so a bolt costs no DetectSolidDescription and no Skin. It is
     also the only signal that catches large standard parts: a threaded bushing
     measures 11 636 mm2 and reads as a panel by area alone.
  2. **Surface area** - the fallback for unnamed parts. On the same body every
     standard part measured 360-3 412 mm2 while the smallest real panel was
     4 739 mm2, which is where `hardware_area` (4 000 mm2) comes from.

  The name is **not** a verdict for structure: 托架 and 板 appear 174 and 196 times
  and cover both 1 mm stampings and thick castings, so sheet metal stays a geometry
  decision. `是否钣金` in `汇总.csv` keeps the classifier's raw verdict and may read
  是 for a `hardware` row - the status wins. Anything between 3 000 and 5 000 mm2
  whose name is not a standard part gets "临界：名称非标准件，建议复核" appended to
  `原因` instead of being quietly filed.

- **A thick part can look 100% thin.** `R007_A0072600643_WELD.CATPart` reported a
  single thickness covering 100% of its faces - 7.453 mm. Area fraction alone
  calls that sheet metal. Keep `max_sheet_thickness` (default 4.0 mm) in the
  verdict.
- **The file-name suffix is not part of the number.**
  `R007_A0072600643_WELD` must become `A0072600643`, not `A0072600643_WELD`.
  Take the **second underscore token** (`stem.split("_")[1]`), not everything after
  the first underscore: a digit-pattern regex such as `[A-Za-z]\d{7,10}` also
  throws away the whole `A007S030430` family (118 of 264 parts in one folder).
  When several files share a number, the extra ones get a `__<B/O/R index>`
  suffix; that index regex must allow any letter prefix, not just `B`.

A file that fails must still be saved as geometry, so the user can open it and
see what is missing - do not leave an empty directory entry.

### 0e. Four defects a 289-part run exposed

These cost real parts on a full BIW folder. Check them before trusting any batch.

1. **`Skin` has three buckets, not one.** The return dict carries `skinned`,
   `check` and `failed`. A group in `check` already has its middle surface, ANSA
   only wants it reviewed - counting `skinned` alone writes off good parts.
   Measured: 4 of 9 "skin_failed" parts had 58~5 000 `check` faces and meshed fine
   once counted (`B0318` -> 10 712 elements).
   ```python
   n = sum(len(g.get("skin") or []) for k in ("skinned", "check")
           for g in (ret.get(k) or []))
   ```
2. **Every retry leaves a part behind.** `Skin(new_part=True)` creates a new part
   per group even when it reports `failed`/`check`. Retry twice and the database
   holds three stacked middle surfaces; the mesh then lands on all three. Snapshot
   the part ids before the call, keep only the last attempt's parts, delete the
   rest.
3. **`DetectSolidDescription` returns a negative sentinel when it cannot
   estimate.** `-1.0` means "no consistent offset pair", not "thick". Treating it
   as a thickness classifies a real panel as not-sheet-metal and silently drops
   it. `T <= 0` -> estimate unknown, attempt Skin anyway and let the measured
   gauge decide.
4. **Output names must not depend on processing order.** A folder can hold one
   part number at several assembly positions (four `B0343..B0349_A0072600170`
   here). If the "first file gets the plain name" rule is applied to a list that
   was sorted by *size*, two runs disagree and one part overwrites the other's
   `.ansa`. Compute the stem map once from the **name-sorted** full file list, and
   hand the same map to every worker:
   `A0072600170`, `A0072600170__B0345`, `A0072600170__B0347` ...

For a folder this size also: write one JSON line per part to `_progress.jsonl`,
resume by re-reading it, and run 3 ANSA workers on disjoint file lists (40 parts
each) - a single process needed ~80 min for the last 130 parts, three workers
brought that under 35. Three instances used ~0.4 GB each; 16 cores / 64 GB is
comfortable.

### 1. Inspect before touching anything

Exec `scripts/inspect_model.py` inside ANSA and read `RESULT`:

```python
SKILL = r"C:\Users\Dongmiao\.workbuddy\skills\ansa-midsurface-mesh"
exec(open(SKILL + r"\scripts\inspect_model.py", encoding="utf-8").read(), globals())
RESULT
```

Adjust `SKILL` to wherever the skill is installed.

Read `verdict.closed_cad_description`. If it is false the model is not a thin-wall
description and this skill does not apply.

Beware deck naming: in a NASTRAN deck nodes are `GRID`, not `NODE`, so
`CollectEntities(deck, None, "NODE")` returns 0 without error. A zero count is not
evidence of absence - check the active deck's own type names first.

### 2. Measure, classify and mesh

Exec `scripts/midsurface_mesh.py` with `PARAMS`:

```python
PARAMS = {
    "source": r"D:/proj/model.ansa",
    "target_length": 5.0,
    "max_thickness": 6.0,
    "output_dir": r"D:/proj/mesh_out",
    "variable_thickness_policy": "drop",   # block | write_median | drop
}
SKILL = r"C:\Users\Dongmiao\.workbuddy\skills\ansa-midsurface-mesh"
exec(open(SKILL + r"\scripts\midsurface_mesh.py", encoding="utf-8").read(), globals())
RESULT
```

The script runs two passes and is safe to re-run: it reloads `source` before each
pass, so a failed attempt leaves nothing behind in the session.

- **Pass A** calls `base.Skin(apply_thickness=True, new_pid=True, offset_type=2,
  ok_to_offset=True, max_thickness=<cap>, delete=False)` per part.
  `ret == 1` → `status: "thin"`, and the new PSHELL's `T` is the measured wall.
  `ret == 0` → `status: "unknown"`, **not** "not sheet metal": the wall may simply
  be thicker than the cap, or the topology may defeat the offset.
- **Pass B** reloads and calls `base.MidSurfAuto(faces=..., thick=<hint>,
  length=<target>, elem_type=3, part="auto_create", property="auto_create",
  ret_ents=True, get_result_type=True)` for **every** part that is thin *or*
  unknown. For unknown parts the hint is a guess (1.0 / 0.8 / 1.5 / 2.0 / 0.6 mm,
  all below the target length) and MidSurfAuto corrects it. **One call can create
  several output parts, and `ret.type` does not list them all** - the created parts
  are found by diffing the `ANSAPART` set before and after the call, plus a final
  sweep for anything still unclaimed. Every output part is measured and written
  separately. Thickness is read with `base.GetShellThickness(deck, shell)` over up
  to 400 evenly spread elements and uniformity is decided on the **p2-p98** range,
  not on min/max - a single mis-paired element at 20 mm on a 3 mm panel must not
  condemn the part.

Statuses in `pass_b` are the only thing you should report from:

| status | meaning |
|---|---|
| `meshed` | shells created, thickness uniform, `T` written |
| `meshed_median` | `write_median` policy: spread over tolerance, median written on purpose |
| `deferred_manual` | `drop` policy: shells deleted, solid faces kept for manual modelling |
| `meshed_variable` | `block` policy: shells kept, spread over tolerance, **`T` not written** |
| `not_shellable` | both Skin and MidSurfAuto produced nothing - a real solid |
| `midsurf_failed` | MidSurfAuto raised on every hint |
| `no_thickness_read` | mesh exists but no element thickness could be read |

`variable_thickness_policy` decides what happens to a part whose gauge genuinely
varies. **`drop` is the only one that produces a deck you can analyse as-is**: the
part leaves the shell mesh, its solid faces stay in the `.ansa` for manual
modelling, and it is listed in `deferred_manual`. `block` keeps the shells but
leaves the default 1.0 mm gauge in every export - `verify.elements_without_T` then
reports exactly how many elements are affected. Never ship a `block` deck without
telling the user that number.

Set `"uniform_tolerance": 0.15` to loosen the 10% default (it applies to the p2-p98
range; in practice 10% is tight for extruded or hydroformed profiles).

Set `"dry_run": True` to stop after pass A. That returns `n_thin_parts` and
`n_unknown_parts` but cannot resolve the unknowns - measuring them requires
MidSurfAuto, which a dry run deliberately skips.
Set `"fallback_to_midsurf": False` to restore Skin-only gating (it will then
silently drop every part Skin refuses).

### 3. Verify before reporting

The script returns `verify`. All of these must hold, or the result is not done:

| Gate | Expectation |
|---|---|
| `n_shells` | non-zero, and close to `total_area / target^2` |
| `edge_p50` | within ~6% of the target length |
| `edge_p10` / `edge_p90` | roughly target x (0.8 ... 1.05) |
| `quad_ratio_sampled` | typically 0.8-0.95 for `elem_type=3` |
| per part `T_written` | equals `T_measured`; `uniform` is true |
| `faces_left` | only the parts that were never shelled |
| `n_parts_needing_review` | **must be 0 before delivery**, or every entry in `review` must be explained |
| `elements_without_T` | **must be 0 for a deliverable deck** - measured from the actual PSHELL state, not from the status counts |
| `props_without_T` | the offenders behind that number (pid, shell count, T); their properties are renamed `*_UNVERIFIED_T` in the saved model so the hazard is visible in the GUI |
| `strays_found` / `strays_resolved` | parts MidSurfAuto created without reporting them; non-zero means `ret.type` under-reported and the sweep had to clean up |
| `n_created_parts` per part | should equal `n_reported_outputs`; when it is larger, the call created something it did not list |
| `n_parts_deferred_manual` | with `drop`, the parts needing hand modelling; check the geometry is still in the `.ansa` |
| `from_midsurf_fallback` | how many parts Skin could not measure; a high count means the cap or the topology needs attention, not that the run is wrong |

`pass_b` status is the gate that matters, not `pass_a`. A part that ends as
`meshed` is done. `deferred_manual` means the part is out of the shell mesh but its
solid faces are still in the `.ansa` - that is the hand-off for manual modelling, not
a failure. `not_shellable` is a genuine solid/casting.

Beware the quiet consequence of `block`: because `T` was not written, the PSHELL
keeps the mesher's default `T=1.0`, and that value **is** what the exported
Nastran/Abaqus deck will contain. The shells are real and welded into the model, so
a `review` entry is not a harmless warning - it is a part with the wrong gauge
sitting in the delivered file. In class5.stp a `block` run put 18,298 of 77,034
elements (23.8%) on a placeholder 1.0 mm gauge, unseen by any other metric. Use
`drop` when the parts are going to be modelled by hand anyway.

`edge_p50` sitting a few percent under the target is normal: the mesher projects
onto curved geometry and the `sqrt(area)` proxy is not an edge length. A gap of 20%
or more means the target length was not applied at all - stop and investigate.

If a part is not uniform, its thickness varies beyond `uniform_tolerance`. The
verdict is computed on the p2-p98 range because **min/max is not reproducible**: two
instances of `JA10-34-801` in class5.stp have identical geometry and identical median
(3.25 mm) yet one sampled [3.0, 3.25] and passed while the other sampled
[2.83, 3.25] and was blocked - the same part, opposite verdicts, from a 40-element
min/max window. Report `spread_pct` and `T_range_raw` together, and re-check rather
than trusting a single number.

### 4. Report honestly

State the element count, the actual measured element size, and the per-part
thickness table. Distinguish three separate claims:

- the mesh exists (element count and area)
- the thickness is measured (per-element read-back)
- the model is engineering-valid (never asserted by any automatic step)

Also state that parts are meshed independently and therefore do not share nodes -
connections must be defined separately.

## Decision rules that matter

**`max_thickness` is a filter, not a clamp.** When the true wall is thicker than the
cap, `Skin` returns 0 and the part becomes indistinguishable from a solid. It does
not truncate `T` to the cap. Measured on class5.stp: at a 3.0 cap, `JA10-34-881` and
`JA10-34-8N1` were reported "not thin-walled"; at a 6.0 cap both came out `thin` with
`T = 3.5`. Conversely, a `T` that happens to equal the cap is not evidence of
clipping - four parts reported `T = 3.000` at both caps because they really are
3.0 mm.

**Skin says nothing reliable about "is this sheet metal".** On the same model Skin
returned 0 for 13 parts even at a 40 mm cap, so the cap was not the cause. MidSurfAuto
then built uniform meshes (T 2.0-2.5 mm, spread < 1%) on 10 of those 13. Always give
MidSurfAuto the last word.

**Mesh size: use `MidSurfAuto(length=...)`, not `BatchGenerator`.**
`mesh.BatchGenerator()` ignores the target length entirely. Editing
`target_element_length` in `SaveMeshParams`/`ReadMeshParams` has no effect
(verified: a 10 mm setting still produced 4.85 mm elements), and
`mesh.SetMeshParamTargetLength` rejects every label tried. `MidSurfAuto(length=)`
does work: 10.0 gave 9.76 mm, 5.0 gave 4.91 mm on the same part.

**Thickness: `Skin` gives it directly, `MidSurfAuto` gives it per element.**
Both are measurements from the solid description and agree exactly. The `thick`
argument of `MidSurfAuto` is only a hint - passing a wrong value still returns the
true thickness.

**One T per part, from the median - for constant-thickness parts only.**
`base.CreatePropertiesFromShellsThickness(shells, step)` exists and groups elements
by gauge, but on a stamped part it splits fillet regions (where the measured
distance is shorter) into a spurious second property. Use it only for genuinely
variable-thickness geometry.

**Never select the mid-surface by face-id diff.** `base.Skin` renumbers faces. The
reliable selector is the newly created `@SKIN` property: collect its faces.

## Pitfalls that cost real time

- `base.GetShellThickness(deck, entity)` takes the **entity**, not the id. Passing an
  id raises "argument 2 must be an ansa.base.Entity".
- GRID coordinates are card fields `X1/X2/X3`, not `X/Y/Z` (Nastran deck); shell
  connectivity is `G1..G4`. `GetEntityCardValues` takes a single entity, so a node
  coordinate map needs one call per grid. Read connectivity with
  `CollectEntities(deck, shell, "GRID")` instead of guessing field names.
- `base.ElementQuality` resolves `ASPECT`, `SKEW`, `WARP`, `TAPER`, `CRASH`,
  `SQUISH`; `JACOBIAN`, `MIN_LENGTH`/`MAX_LENGTH`, `MINLEN`/`MAXLEN`, `ANGLE_*`
  all come back as the string `'error'`. For those, use
  `base.CalculateOffElements(details=True)`, which returns ANSA's own off-element
  counts per criterion (`{'TOTAL OFF': ..., 'MIN LENGTH': ..., 'JACOBIAN': ...}`).
- `base.checks.mesh.MeshQuality().execute(code)` takes an int
  (`EXEC_ON_ALL` / `EXEC_ON_MODEL` / ...) and returns a list of `CheckReport`;
  the report exposes `status` and `issues`, not per-criterion counts.
- `base.checks` has only `general`, `geometry`, `mesh`, `penetration`. There is no
  `base.checks.shell`.
- Property names containing spaces or semicolons make the exported Abaqus `ELSET`
  unparsable. Rename before exporting; the exporter rewrites `.` to `X`.
- `base.SaveAs` and the `Output*` functions return `0`/`1`, not booleans - check the
  written file size instead of the return code.
- A part naming convention is not a classification. In one bumper model every `_P_`
  part was sheet metal and every `_WP_` part was not, but that was a coincidence of
  that dataset; classify with `Skin`'s return value.
- `base.IsPartForSkin` is wrong in both directions and is not monotonic in its
  tolerance. In class5.stp: `HXM113-0800` (bolts) returned False while the 6-face
  solid blocks `JA10-34-8P8/8P9` returned **True**; `JA10-34-8H1` returned True at
  tol 0.1 and False at 0.3. Never decide from it alone.
- `Skin` failing is not a verdict. It fails for at least three different reasons -
  wall over the cap, topology the offset cannot follow, and genuinely thick geometry -
  and returns the same `0` for all of them. Only MidSurfAuto separates them.

## Bundled resources

- `scripts/process_part.py` - the one-part pipeline: classify, repair, `Skin`,
  property naming, mesh, save `.ansa` + export `.nas`.
- `scripts/process_folder.py` - folder driver around `process_part.py`:
  `_progress.jsonl`, resume, chunking, worker subsets, and the mandatory
  exception list.
- `scripts/mesh_engine.py` - meshing only (`mesh.Mesh` or a Batch Mesh session),
  switched by `PARAMS["engine"]`.
- `scripts/inspect_model.py` - read-only model survey: entity counts, per-part
  bounding box and area, property thickness, deck verdict.
- `scripts/midsurface_mesh.py` - legacy two-pass build (`MidSurfAuto` + measured
  thickness) for runs that have no `.ansa_mpar` / `.ansa_qual`.
- `references/ansa-api-verified.md` - exact signatures, what was verified on which
  release, and the negative results worth not repeating.

---
name: ansa-midsurface-mesh
description: Extract mid-surfaces from thin-walled CAD geometry in ANSA and generate a shell mesh at a given target element size, with the measured wall thickness written into PSHELL properties. This skill should be used for requests such as "抽中面并划 5mm 网格", "钣金件抽中面并赋予正确厚度", "midsurface + shell mesh for the sheet metal parts", "把 CAD 面模型变成壳网格并导出求解文件", or whenever an ANSA database full of closed thin-walled surfaces must become a solver-ready shell mesh. Covers part classification, thickness measurement, element-size control, verification gates, and Nastran/Abaqus export.
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

## Prerequisites

- The `ansa` MCP server must be connected: `ansa_status` reports `connected: true`.
- Confirm the target element length.
- Confirm the maximum plausible sheet thickness. Be generous: `max_thickness` is a
  **hard filter, not a clamp** - a part whose wall exceeds it makes `Skin` return 0
  and the part then looks exactly like a solid. Real automotive gauges go to 3.5 mm
  (verified), so 3.0 is too tight for a BIW model; use 4.0-6.0.
- Everything below is verified on **ANSA 23.0.0 with a NASTRAN deck**. On another
  release, re-verify at gate 1 before trusting the rest.

## Workflow

### 1. Inspect before touching anything

Exec `scripts/inspect_model.py` inside ANSA and read `RESULT`:

```python
SKILL = r"C:\Users\<you>\.workbuddy\skills\ansa-midsurface-mesh"
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
SKILL = r"C:\Users\<you>\.workbuddy\skills\ansa-midsurface-mesh"
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

- `scripts/inspect_model.py` - read-only model survey: entity counts, per-part
  bounding box and area, property thickness, deck verdict.
- `scripts/midsurface_mesh.py` - the two-pass build with verification and export.
- `references/ansa-api-verified.md` - exact signatures, what was verified on which
  release, and the negative results worth not repeating.

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

## Prerequisites

- The `ansa` MCP server must be connected: `ansa_status` reports `connected: true`.
- Confirm the target element length, and a maximum plausible sheet thickness
  (a stamping is typically 0.5-3.0 mm; anything above the cap is not sheet metal).
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
    "max_thickness": 3.0,
    "output_dir": r"D:/proj/mesh_out",
}
SKILL = r"C:\Users\<you>\.workbuddy\skills\ansa-midsurface-mesh"
exec(open(SKILL + r"\scripts\midsurface_mesh.py", encoding="utf-8").read(), globals())
RESULT
```

The script runs two passes and is safe to re-run: it reloads `source` before each
pass, so a failed attempt leaves nothing behind in the session.

- **Pass A** calls `base.Skin(apply_thickness=True, new_pid=True, offset_type=2,
  ok_to_offset=True, max_thickness=<cap>, delete=False)` per part. `ret == 1` means
  the part is thin-walled and the new PSHELL's `T` is the measured wall thickness.
  `ret == 0` means it is not - leave that part as geometry, do not force it.
- **Pass B** reloads and calls `base.MidSurfAuto(faces=..., thick=<measured T>,
  length=<target>, elem_type=3, part="auto_create", property="auto_create",
  ret_ents=True, get_result_type=True)` per thin part. It then reads the thickness
  MidSurfAuto stored on each element with `base.GetShellThickness(deck, shell)`,
  takes the median, and writes it back into the PSHELL `T`.

Set `"dry_run": True` to stop after pass A and get only the thickness table.

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

`edge_p50` sitting a few percent under the target is normal: the mesher projects
onto curved geometry and the `sqrt(area)` proxy is not an edge length. A gap of 20%
or more means the target length was not applied at all - stop and investigate.

If a part reports `uniform: false`, its thickness varies beyond 10%. Do not silently
average it; report the range and ask whether that part is a tailored blank.

### 4. Report honestly

State the element count, the actual measured element size, and the per-part
thickness table. Distinguish three separate claims:

- the mesh exists (element count and area)
- the thickness is measured (per-element read-back)
- the model is engineering-valid (never asserted by any automatic step)

Also state that parts are meshed independently and therefore do not share nodes -
connections must be defined separately.

## Decision rules that matter

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

## Bundled resources

- `scripts/inspect_model.py` - read-only model survey: entity counts, per-part
  bounding box and area, property thickness, deck verdict.
- `scripts/midsurface_mesh.py` - the two-pass build with verification and export.
- `references/ansa-api-verified.md` - exact signatures, what was verified on which
  release, and the negative results worth not repeating.

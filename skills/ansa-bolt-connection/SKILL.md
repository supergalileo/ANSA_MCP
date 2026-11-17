---
name: ansa-bolt-connection
description: Build bolted joints in ANSA from the real bolt solids — one CBAR (PBARL ROD, diameter = bolt shank) with two RBE2 spiders, one per plate stack, centres on the hole axis and slave nodes taken from the whole hole ring. Use for requests such as "用螺栓建立连接", "建螺栓连接", "螺栓用 RBE2 加 beam", "把紧固件连到副车架/车身", "bolt connection from CATIA bolts", "bolt joints for a stiffness model", or whenever a shell model of separately meshed parts must be joined at its bolt holes and the analyst cares that each spider really closes on the hole instead of grabbing half of it. Also use it when an existing bolt connection is judged wrong because a spider sits off-centre, covers only part of the ring, or a multi-layer stack got split into several spiders.
agent_created: true
---

# ANSA bolt connections from bolt solids

Turn a set of bolt CATParts plus a shell model into solver-ready bolted joints:
one beam per bolt, one spider per plate stack, centres on the hole axis.

## What the result must contain

- **one CBAR per bolt**, `PBARL` `ROD` section, `DIM1` = the bolt shank diameter
- **exactly two RBE2 per bolt** for a through-bolt, **one** for a single-plate bolt
- **each RBE2 centre on the hole axis**, not on a hole-edge node
- **the whole hole ring inside the spider** — angular gap ≤ 45° on a 5 mm mesh
- **one RBE2 per plate stack**, even when the stack has 3 or 4 layers
- no free (element-less) node in any spider's slave set

If any of those fail, the joint is wrong even when it "looks connected".

## When to use

A shell model where every part was meshed **independently**, so no two parts
share nodes at the bolt holes, and the bolt solids are available as CATPart.
That is the only input this skill accepts — it does not weld, does not bond, does
not touch the mesh.

Not for: seam welds, adhesive, clinch, solid-bolt meshing (hexa bolt body), or
models where the parts are already joined.

## Prerequisites

- `ansa_status` reports `connected: true`; otherwise `ansa_launch` first.
  **Use `mode="gui"`** — `base.Open()` on an `.ansa` file returns 2 and loads
  nothing in batch mode, so a saved result cannot be reopened or verified there.
- The structure model already meshed and loaded (`.nas` via `InputNastran`,
  `.ansa` via GUI-mode `Open`).
- 18 bolt CATParts, or whatever the BOM lists — paths or a directory.
- Know which part numbers are **single-plate** bolts (weld studs / blind tapped
  holes). They get one spider and no beam; list them in `single_side`.

## Connection topology

```
[stack A ring nodes] ──RBE2_A(GN = hole centre A)
                            │
                      CBAR  PBARL ROD  DIM1 = shank ∅
                            │
[stack B ring nodes] ──RBE2_B(GN = hole centre B)
```

A through-bolt therefore owns **2 RBE2 + 1 CBAR**. A bolt with only one plate in
the model (weld stud into a blind hole) owns **1 RBE2** and no beam — do not
invent a second plate.

## Workflow

Two stages, because `base.Open()` on a CATPart **replaces** the model.

### Stage 1 — extract the axes

```python
exec(open(r"<skill>/scripts/bolt_connection.py", encoding="utf-8").read(), globals())
RESULT = run({"stage": "extract", "bolts": [r"D:/bolts", ...], "work_dir": r"D:/work"})
```

Walks every CATPart, takes `base.CreateCurvesAtTubesCenterLine` → the longest
curve is the shank axis, its `diameter` is the bolt diameter. Writes
`bolt_geo.json`.

### Stage 2 — build on the structure

```python
RESULT = run({
    "stage": "build",
    "work_dir": r"D:/work",
    "single_side": ["A007S010401", "A007S010026"],
    "grab_grow": 5.0,
    "on_axis_tol": 3.0,
    "export_nas": r"D:/out/model_with_bolts.nas",
    "save_ansa":  r"D:/out/model_with_bolts.ansa",
})
```

`stage: "all"` does both and reloads `PARAMS["structure"]` in between — use it
when starting from scratch in one call, otherwise prefer the two explicit stages
so a failure in stage 2 does not cost you the extraction.

Long models: call it through `ansa_run_async`, not `ansa_run_python` — the plain
call holds the IAP socket for the whole run.

## Parameters

| key | default | meaning |
|---|---|---|
| `stage` | `"all"` | `extract` / `build` / `all` |
| `bolts` | — | list of CATPart paths or a directory |
| `structure` | `None` | `.nas` to load between stages (required by `all`) |
| `work_dir` | — | scratch dir for `bolt_geo.json`, `plan.json`, exchange `.nas` |
| `grab_grow` | `5.0` | slave-node radius = hole radius + this |
| `on_axis_tol` | `3.0` | a hole belongs to a bolt if its centre is within this of the axis |
| `layer_gap` | `3.0` | axial gap that starts a new plate layer |
| `merge_gap` | `4.0` | adjacent layers closer than this share one RBE2 |
| `single_side` | `()` | part-number prefixes treated as single-plate |
| `export_nas` / `save_ansa` / `report` | `None` | outputs |

## Rules that make or break the joint

1. **`on_axis_tol` must stay small (≈3 mm).** Real coaxial holes sit 0.4–1.0 mm
   off the axis. Widen it to 25 mm and a neighbouring hole 21 mm away joins the
   same side — the centre then drifts 2–4 mm and every spider looks "off". This
   is the single most common defect.
2. **Centre = mean of that side's hole centres, then snapped onto the bolt axis.**
   Never use a hole-edge node: it is one radius off by construction.
3. **Slave set = everything within `hole_radius + grab_grow`, inside the layer
   band ±2.5 mm.** Do not filter by "minimum radius + 3" — that is how half a
   ring gets dropped.
4. **Judge completeness by angular gap**, not by node count. Project the ring onto
   the plane normal to the axis; ≤ 45° is complete on a 5 mm mesh, > 70° means
   nodes were missed.
5. **Multi-layer stacks share one RBE2.** The centre then sits between layers and
   is *supposed* to be 1–4 mm from the nearest hole centre. If the analyst wants
   the centre exactly on a hole, the only honest answer is one RBE2 per layer.
6. **Remove the centre node from its own slave set** —
   `base.RemoveFromSet(set_ent, [gn])`. Nastran forbids GN inside GM and ANSA does
   not do it for you.
7. **Single-plate bolts get no beam.** A 2 mm beam between two touching plates is
   not a bolt, it is a numerical accident.

## Traps that cost hours (all verified)

| trap | fact |
|---|---|
| `CreateEntity` cannot build the beam | `CBAR`, `CROD`, `PBAR`, `PBARL`, `PBARL_ROD` all return `None`; an empty `PBARL_ROD` entity can be created but `SECTION`/`DIM1` refuse to be written. **Only nas import works.** |
| `GRID` cannot be created either | `CreateEntity(DECK,'GRID',{...})` → `None`; `CreateEntity(...,{})` → invalid mbcontainer. Write a tiny nas and `InputNastran` it. |
| `PBARL` loses `DIM1` both ways | ANSA reads `SECTION='ROD'` but `DIM1=None`, and writes `PBARL 1001 1 MSCBML0 ROD` with no diameter. Fix on import with `set_entity_values({'DIM1': d})`, and after export with `patch_pbarl_dims()`. |
| ANSA internal type names | `PBARL_ROD` is the entity type, but only for **querying/patching** existing entities. `CollectEntities('PBAR')` and `'PBARL'` return 0. |
| `.ansa` unreadable in batch | `base.Open` returns 2 and loads nothing. GUI mode only. |
| `DeleteEntity` on a half-built property | once wiped the whole database (GRID 61241 → 0). `ansa_save` first. |
| `DefineConnectionHoles` with proximity 0 | every connection ends up single-part and `RealizeConnections` returns 0. Irrelevant here (holes are only surveyed, then deleted) but fatal if you try the built-in Bolt route. |
| `AssignConnectionSettings` | resets every field you do not pass; always read `GetAllConnectionSettings`, patch, write back. |

## Verification

```python
# centres vs the hole they belong to
d = min(math.dist(gn.position, h["pos"]) for h in holes)

# ring completeness  -> already reported per side as gap_deg
# free nodes         -> must be 0
used = {n._id for sh in base.CollectEntities(DECK, None, "SHELL")
        for n in sh.get_entity_values(DECK, ["G1","G2","G3","G4"]).values() if n}
```

Accept when: every side `gap_deg ≤ 45`, `free nodes = 0`, and the `.nas` has
`PBARL` lines whose columns 41–48 carry the diameter.

## Reporting

State per bolt: sides, slave-node count, angular gap, beam length, and whether a
centre was **fitted** (hole finder missed that layer). A fitted centre is an
estimate — say so rather than presenting it as a hole centre.

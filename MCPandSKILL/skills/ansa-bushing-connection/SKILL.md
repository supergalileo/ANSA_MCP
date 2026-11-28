---
name: ansa-bushing-connection
description: Build rubber-bushing joints in ANSA as RBE2 spiders plus CBUSH/PBUSH, in either of the two accepted idealisations - a zero-length point bushing at the plate gap (default, industry standard) or a real-length bushing spanning the two plates. Use for requests such as "衬套怎么处理", "建衬套连接", "橡胶衬套用 CBUSH", "衬套刚度", "bushing connection", "rubber mount joint", "bushing for a stiffness model", "把橡胶衬套连到两块板", or whenever a shell model of separately meshed parts must be joined at a bushing bore and the analyst must choose between an equivalent zero-length element and a full-length element without double counting the rocking stiffness. Also use it when an existing CBUSH is judged wrong because K1..K6 are in the wrong order, the element has no G0 orientation, the exported PBUSH lost K5/K6 to field gluing, the joint was scaled "to compensate a lever" that a CBUSH does not have, or the bushing was meshed as rubber instead of being condensed into a spring.
agent_created: true
---

# ANSA rubber bushings: RBE2 + CBUSH

Turn a rubber bushing (2-6 rubber solids through a pair of plate bores) into a
solver-ready spring joint: one CBUSH, one PBUSH, two RBE2 spiders, centres on the
bore axis, slave nodes on the hole edge of each plate.

## What the result must contain

- **one CBUSH + one PBUSH per bushing**
- **exactly two RBE2**, one per plate, centre **on the bore axis**
- **the whole hole ring** inside each spider - angular gap <= 45 deg on a 5 mm mesh
- **PBUSH order is [axial, radial, radial, torsion, rocking, rocking]** -
  that is K1, K2, K3, K4, K5, K6 in Nastran order. Written in any other order the
  model is silently 4x wrong in the radial direction.
- **a G0 grid** on the CBUSH card (a grid, not a coordinate system) whenever
  K2...K6 are non-zero, otherwise OptiStruct refuses the deck
  (`ERROR #99 ... must be zero for CBUSH with no G0, CID, and blank X1, X2, X3`)
- **a reported face stiffness**, not just the element stiffness - see
  `references/stiffness-modeling.md`

## When to use

Separately meshed plates share no nodes at the bushing bore, the rubber is
available as CATPart/STEP, and the analyst wants a spring joint instead of a
meshed rubber body. That is the only input this skill accepts: it does not mesh,
weld, bond, or split the plates.

Not for: solid rubber meshing, adhesive joints, bolt joints (use
`ansa-bolt-connection`), or bushings whose stiffness matrix was measured as a
full 6x6 with coupling terms - this skill builds a diagonal PBUSH only.

## Prerequisites

- `ansa_status` reports `connected: true`; otherwise `ansa_launch` first.
- The structure deck already meshed and loaded (`.nas` via `InputNastran`,
  `.ansa` via GUI-mode `Open`; batch `base.Open` on `.ansa` returns 2).
- The rubber parts as CATPart/STEP, or their axes from an earlier extract.
- The radial stiffness (or permission to use the placeholder). Class5 used
  1000 N/mm for a 63 mm bore; without a test report this is a placeholder, not a
  measurement, and must be labelled as such.

## Topology

```
[plate A ring] --RBE2_A(GN = A side grid)
                        |
                  CBUSH  PBUSH (K1..K6)
                        |
[plate B ring] --RBE2_B(GN = B side grid)
```

The two grids sit **on the bore axis**, 2 mm apart (point mode) or at the two
plate mid-planes (spanning mode). Ring nodes are the hole edge of each plate,
picked within `ring_band` and `+-z_tol` of the plate position.

## Two idealisations, one stiffness

| | `mode: "point"` (default) | `mode: "spanning"` |
|---|---|---|
| element length | 2 mm at the gap centre | the real plate-to-plate gap |
| grids | on the bore axis, mid-gap | on the bore axis, at each plate plane |
| what spans the gap | the RBE2 spokes | the CBUSH itself |
| PBUSH | K2 = `k_rad`, K5/K6 = `k_rad*L^2/12` | identical |
| solved stiffness | reference | identical to point (verified) |
| when | default; fewer grids, one band per plate | when the analyst wants a visible full-length element, or the gap must be filled by the element |

Why they agree: an OptiStruct CBUSH is a **two-node spring**. It springs the six
DOFs of GA against the six DOFs of GB and carries no length coupling, so K2 is
already force/relative-displacement at the plates and K5/K6 already
moment/relative-rotation. Give the 44 mm gap to the spokes or to the element and
the solved answer is the same - class5, +Z 1000 N and +Y 1000 N at the loaded
point: point 165.6 / 9913.9 N/mm, spanning 165.6 / 9913.9 N/mm, equal to five
digits.

Consequences:

- **Never scale K2 by 4 "to compensate a lever".** There is no lever term; the
  scale only makes the joint stiff. (`spanning_fix: "scale_radial"` is rejected
  with a warning and the physical K is written.)
- `spanning_fix: "rigid_rocking"` (K5=K6=1e5*k_rad) is for a deliberately rigid
  joint only: on class5 it raised the +Z stiffness from 165.6 to 200.2 N/mm, +21%.
- Both modes rigidify the hole ring through the RBE2 spider. For bore stresses,
  use RBE3 or a meshed rubber body instead - this skill is for global stiffness.

## Workflow

Two stages, because `base.Open()` on a part **replaces** the model.

### Stage 1 - extract the axes

```python
exec(open(r"<skill>/scripts/bushing_connection.py", encoding="utf-8").read(), globals())
RESULT = run({"stage": "extract", "rubber": [r"D:/parts/06_nonmetal"],
              "work_dir": r"D:/work"})
```

Walks each rubber solid, takes `CreateCurvesAtTubesCenterLine` -> longest curve
as the axis, curve `diameter` as that surface's diameter, then groups coaxial
parts into one bushing (`axis_tol` 5 mm, `angle_tol` 5 deg). Writes
`bushing_geo.json`: per bushing origin, dir, span, L, `r_in` (smallest
diameter -> the bore), `r_out_est` (largest diameter -> rubber OD).

### Stage 2 - build on the structure

```python
RESULT = run({"stage": "build", "geo": r"D:/work/bushing_geo.json",
              "mode": "point", "k_rad": 1000.0,
              "export_nas": r"D:/out/model_with_bushings.nas"})
```

`stage: "all"` extracts, reloads `PARAMS["structure"]`, then builds - one call
when starting from scratch. Long models: drive it through `ansa_run_async`, not
`ansa_run_python`, which holds the IAP socket for the whole run.

## Parameters

| key | default | meaning |
|---|---|---|
| `stage` | `"all"` | `extract` / `build` / `all` |
| `rubber` | - | rubber part paths or a directory |
| `structure` | `None` | structure `.nas` (required by `all`) |
| `work_dir` | - | scratch dir for json / card exchange |
| `geo` | `None` | `bushing_geo.json` from an earlier extract |
| `bushings` | `[]` | explicit specs, merged with `geo` |
| `mode` | `"point"` | `point` / `spanning` |
| `spanning_fix` | `"physical"` | `physical` / `rigid_rocking` (deliberately rigid joint) |
| `k_rad` | `1000.0` | target radial stiffness at the plate face, N/mm |
| `E`, `G` | `4.0`, `0.6` | rubber moduli, MPa, for axial and torsional K |
| `ring_band` | auto | `[r_lo, r_hi]` override for the hole-edge band |
| `z_tol` | `8.0` | axial half-band that selects one plate's ring |
| `min_ring` / `max_gap` | `6` / `90` | refusal thresholds per side |
| `export_nas` / `save_ansa` / `report` | `None` | outputs |

Stiffness it writes, with `r_out` defaulting to the measured hole-ring radius:

```
K1 = E*A/L,  A = pi(ro^2 - ri^2)        axial
K2 = K3 = k_rad                         radial
K4 = G*J/L,  J = pi/2(ro^4 - ri^4)      torsion
K5 = K6 = k_rad*L^2/12                  rocking

Same numbers in both modes: the idealisation moves the gap, not the stiffness.
```

## Rules that make or break the joint

1. **Never leave G0 empty.** K2...K6 need an orientation: G0 is a **grid id**
   (field 6 of the CBUSH card), and the local x axis becomes GA->G0. A CID or
   X1/X2/X3 in the wrong field gives
   `*** ERROR # 1000 *** ... Field 'CID' of CBUSH bulk data`.
2. **Keep the 2 mm offset in point mode.** OptiStruct rejects a zero-length
   CBUSH with `ERROR #40`; +-1 mm along the axis is enough and adds negligible
   stiffness.
3. **Put the two grids on the bore axis**, not on ring nodes. A ring node is one
   radius off by construction.
4. **Judge the ring by angular gap**, not by node count: <= 45 deg is complete on
   a 5 mm mesh.
5. **Remove the centre node from its own slave set** - Nastran forbids GN inside
   GM and ANSA does not do it for you (the script filters it).
6. **Do not read the deck back with `CollectEntities("CQUAD4")`** - use
   `"SHELL"` for shells and `"GRID"` for nodes; CBUSH/PBUSH read back fine.
7. **CBUSH cannot be created by `CreateEntity`** (returns None) and neither can
   GRID - write the cards to a small `.nas` and `InputNastran` it, then add the
   RBE2s through the API. PBUSH *can* be created, but the text route keeps the
   six K values in one place.
8. **State the placeholder.** If `k_rad` came from this table rather than a test
   report, say so; the absolute stiffness of the whole model is then indicative.

## Verification

```python
# per bushing: ring node counts and gaps come back in RESULT["build"]["sides"]
# free nodes must be 0
used = {n._id for sh in base.CollectEntities(DECK, None, "SHELL")
        for n in sh.get_entity_values(DECK, ["G1","G2","G3","G4"]).values() if n}
```

Accept when every side has `gap <= 45`, `len(ids) >= 6`, the exported deck has
one `PBUSH` and one `CBUSH` per bushing, and `PBUSH` read back from the deck has
all six K values (see the export-gluing trap below).

Then solve one load case per direction and, if both modes are in doubt, solve the
same load case on the point build and on the spanning build: the loaded-point
displacement must agree within a few percent. On class5 it agrees to five digits
(165.6 and 9913.9 N/mm), so a 4x difference means the PBUSH values, not the
element length, are wrong.

## Traps that cost hours (all verified)

| trap | fact |
|---|---|
| `ERROR #40` zero-length CBUSH | the element needs a non-zero length; 2 mm is the accepted workaround |
| `ERROR #99` K2..K6 | without G0/CID/vector only K1 may be non-zero |
| `ERROR #14` Missing GRID | G0 references a grid that does not exist, not a missing coordinate system |
| PBUSH written without the `K` flag | `PBUSH,pid,K,k1..k6`; without the flag ANSA reads K1..K6 as 0 |
| ANSA writes `PBUSH 7001K ...` | the `K` flag sits against the pid; valid, leave it |
| ANSA glues long PBUSH values | the fixed 8-column export writes `27429.8161333.3` (= 27429.8 + 161333.3), so K5/K6 are silently lost; the builder rewrites every PBUSH as free-field `PBUSH,pid,K,...` after export, which reads back exact and solves the same |
| `.ansa` unreadable in batch | `base.Open` returns 2; GUI mode only |
| `session.New("discard")` | the only reliable way to unload between parts in batch |

## Reporting

Per bushing: mode, element length, plate span, ring node counts and angular gaps,
the six K values, and the solved loaded-point stiffness if a load case was run.
Say which stiffness is a placeholder and which came from data.

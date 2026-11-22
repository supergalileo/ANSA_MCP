# Failure taxonomy — 143 beads, 22-part GL8 tailgate-class subframe

Measured 2026-11-21 on ANSA 24.1.2. The model: `class5.stp` (CATIA V5R21 AP203,
51 MB, 265 PRODUCT, 7 800 faces) split into 176 parts — 22 sheet panels, 5
fasteners, 143 weld beads, 6 rubber parts. All 22 panels meshed at 5 mm
(63 188 shells, gauges 1.5-3.5 mm).

## What the bead geometry looks like

| property | value |
|---|---|
| beads | 143 files, 4-36 faces each, Ø4.0 mm for 140 of them |
| centerlines extracted | **145** (two beads produced two candidates each) |
| chord | min 0, p50 55 mm, max 240.6 mm |
| bulge off chord | p50 0.57 mm, p90 23.95 mm, max **76.64 mm** |
| straight (bulge < 0.5 mm) | 70 / 145 |
| distance to nearest shell node | min 0.70, p50 1.26, p90 2.14, max 7.6 mm |

The distance line is the important one: **every bead sits within 8 mm of the
mesh**. Any "search radius too small" theory is wrong for this model, and a
40 mm radius changed nothing but made `find_exactly=2` unsatisfiable.

## Parts within 3 mm of a bead

| distinct parts | seams |
|---|---|
| 2 | 118 |
| 3 | 6 |
| 1 | 16 |
| 0 | 5 |

Three-part seams are the ones that need `find_up_to=3`; single-part seams are
self-welds (a hem or a lapped edge inside one stamping).

## What actually failed, and why

First pass, `search_distance=3, find_exactly=2`:

```
145 connections created
120 positioned, 25 empty
   then (5,2)  -> +3, 22 empty
        (6,1)  -> +0
        (10,1) -> +0
realized 120/123   (3 had two parts but produced no FE)
RBE2 +2178
```

The 22 that stayed empty, mapped back to their bead files:

| symptom | count | examples | cause |
|---|---|---|---|
| chord = 0.0 | 4 | `821__s114`, `s115`, `s127`, `s128` | the "tube" is a nugget: `CreateCurvesAtTubesCenterLine` returns a point, not a line. These are `SpotweldPoint_Type` geometry mis-filed as seam. |
| two centerlines, same bead | 4 | `8P8`, `8P9` (2 each) | both candidates project to nothing; the pair also blocks a clean attach |
| strong curvature | 5 | `s14` (118 mm chord / 23 mm bulge), `s15`, `s20`, `s118`, `s121` (94/40) | automatic weld positioning refuses the whole line when one stretch cannot be projected |
| local projection failure | 9 | `s25` (15 mm), `s30`, `s34`, `s39`, `s52`... | the line runs where the two plates do not overlap |

## What the fallback bought

Cutting all 22 into ~20 mm pieces produced **97 pieces**; `(3,2)` attached 13,
`(5,2)` attached none of the rest. Those 13 realised as 74 RBE2.

The remaining 84 pieces belong to seams whose second plate is genuinely absent
from the 22-part model — a sub-assembly of a larger vehicle. No radius fixes
that; the honest output is a list.

## Spot fallback — tried, worse

Sampling the same lines into 151 `SpotweldPoint_Type` connections and asking the
manager to attach them gave **0 with two parts** at every radius; at
`find_exactly=1` 97 attached to a *single* part. A point finds the nearest plate
only, whereas a line gives the manager two surfaces to project onto. Do not use
spots to rescue a seam.

## Final state of the reference model

| metric | value |
|---|---|
| connections kept | 133 (all naming two parts) |
| FE coverage | 133 / 133 |
| RBE2 | 2 252, every one with ≥ 2 nodes |
| floating grids | 0 of 65 446 |
| shells | 63 338 (was 63 188 — weld nodes split 150 shells) |
| deck | 7.2 MB `.nas`, 22 PSHELL, 22 MAT1 |

## Solid weld pass (RBE3-HEXA-RBE3, W = 4 mm)

| metric | value |
|---|---|
| connections | 133 (same model, reopened from the RBE2 deck) |
| realised as solid | **118** |
| CHEXA created | **832** |
| RBE3 created | **3 722** (4-5 nodes each) |
| fell back to RBE2 | 15 connections → **174 RBE2** left in the deck |
| PSOLID | 1, MID 23 |
| MAT1 in deck | 22 until patched, **23** after `patch_materials()` |
| grids | 65 446 → **69 342** (the bricks add nodes) |
| floating grids | 0 |

Width sweep, same model:

| W | RBE3-HEXA-RBE3 | RBE3-PENTA-RBE3 |
|---|---|---|
| 2 mm | 118 realised, 832 CHEXA | 0 |
| 4 mm | 118 realised, 832 CHEXA | 0 |
| 6 mm | 118 realised, 832 CHEXA | 0 |

The 15 fallbacks are the same seams that fought the line representation earlier
(zero-length beads and short stubs). The solid pattern is *stricter* than RBE2:
it needs a volume, not just two nodes.

## Brick dimensions: what the three sizes come from

Measured on the reference model (W set to 4 mm, 988 bricks):

| dimension | value | where it comes from |
|---|---|---|
| width | **4.00 mm exactly** | the `W` card field |
| length along seam | p50 10.03 mm | `SeamLine_RBE3_HEXA_RBE3_StepLength`, auto when empty |
| thickness | **exactly the `Height` setting** | `SeamLine_RBE3_HEXA_RBE3_Height`, in mm |

The card has no thickness field (`D`, `H`, `T` all read `{}`), which is why an
earlier pass concluded the thickness was purely geometric. It is not: the values
live in the pattern defaults and are only reachable through
`connections.SetDefault`:

```
config/NVH.defaults (and every other profile):
  SeamLine_RBE3_HEXA_RBE3_Height          = 1.000000
  SeamLine_RBE3_HEXA_RBE3_StepLength      =
  SeamLine_RBE3_HEXA_RBE3_NumOfLayers     = 1
  SeamLine_RBE3_HEXA_RBE3_NumOfStripes    = 1
```

Controlled sweep, same model, thickness measured from the exported deck:

| `Height` | bricks | thickness (min / p50 / max) |
|---|---|---|
| cleared (`""`) | 832 | 0 / 0.49 / 2.64 — follows the plate gap |
| 1.0 | 988 | 1.00 / 1.00 / 1.00 |
| 2.0 | 988 | 1.98 / 2.00 / 2.00 |
| 3.0 | 988 | 2.97 / 3.00 / 3.00 |
| 3.0, `NumOfLayers=2` | 948 | 1.50 per layer |

`StepLength=20` gave 474 bricks with a median length of 21.03 mm — the station
spacing, as expected.

Two things this explains:

1. **Why one run showed varying thickness and another showed a constant.** With
   `Height` empty the brick is driven by the local plate gap and collapses where
   the two mid-surfaces coincide; with `Height` set every brick is exact.
2. **Why the realisation rate moved.** 118 of 133 seams realise with `Height`
   cleared, 132 of 133 with it set: a fixed thickness lets a brick exist where
   there is no gap to fill. With `Height = 2` the deck ends with 988 CHEXA,
   4 259 RBE3 and 221 RBE2 from the one stubborn seam.

The brick is still a **connector, not a weld bead**: it carries no throat or leg
dimension, so it cannot size a weld — but its thickness is a free parameter, not
a slave of the geometry.

## Quad weld pass (OVERLAP-SHELL, W = 4 mm)

| metric | value |
|---|---|
| connections | 133 |
| realised as node-shared quads | **108** |
| no FE at all | 25 (list by id) — the joint is not an overlap |
| shells added / removed | 4 079 / 6 |
| new quads bridging two different parts | 3 339 of 4 415 |
| quad short edge (p50) | 4.74 mm |
| quad long edge (p50) | 5.51 mm |
| RBE2 / RBE3 | 0 / 0 |
| plate shells re-propertied | **1 971** (thickness kept, material id changed) |

Row count and property restore, same model:

| `RootShells` | realised | shells added | re-propertied | restored | bridging quads |
|---|---|---|---|---|---|
| `"Double Row"` (default) | 108 | 4 073 | 1 971 | — | 3 339 |
| `"Single Row"` | 111 | 2 389 | 1 038 | 1 038 (0 left) | 1 875 |

With `restore_properties=True` the exported deck has **no pre-existing element with a
changed property**; the weld quads keep their own PIDs. Without it, 1 038 plate
shells stay reassigned even after `EraseConnectionFeRep`.

The alternative `RBE3-SHELL-RBE3` realises all 133 but produces a **floating**
strip: 2 090 quads, none sharing a node with the plates, tied by 3 107 RBE3. It is
a valid weld representation, just not a node-shared one.

Sweeping the other joint patterns in one session produced numbers that could not be
reproduced in clean runs (+818 vs +90 for `LASERWELD-SHELL`) because the erase does
not undo the re-mesh. Treat only single-pattern, single-session measurements as
evidence.

## Why 22 seams refuse a quad weld

All 22 named two parts, so nothing was missing; the pattern declined the joint.
Joint angle from the mesh: 11 near-coplanar, 6 sandwich, 5 near-perpendicular.

Tolerance made it worse, not better:

| lever | realised (of 22) |
|---|---|
| default | 1 |
| `SearchDist` 5 / 10 / 20 | 0 / 0 / 0 |
| `Parameters_Gap` 0.5–5 | 0 / 0 / 0 |

A second pass with `RBE3-SHELL-RBE3` covers all 22, giving 111 + 22 = 133 of 133
with no connection left without FE. `RBE3-HEXA-RBE3` reached 16/22, `SHELL-RBE3`
14/22 — so the fallback choice matters and was measured, not assumed.

## Reusable lessons

1. Classify by chord first. A zero-chord bead is a spot and no amount of seam
   tuning will place it.
2. A 2-part check at 3 mm predicts the outcome better than any ANSA trial —
   run `seam_audit.py` before opening ANSA at all.
3. `RealizeConnections` returns a count, not a list. Three connections with two
   parts silently produced nothing; only `GetFirstFeRep` per connection found
   them.
4. Element count moving is expected. Treat any *shrink* as a defect and any
   growth as weld-node splitting, not as duplication.

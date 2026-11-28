# Bushing stiffness: what the six K values mean and how they are derived

All values in N, mm, MPa (N/mm^2), tonne-mm-s consistent with the rest of the deck.

## 1. Element frame

OptiStruct CBUSH with `G0` (a **grid id**, field 6 of the card) builds its local
frame from the element axis:

- local x = GA -> GB (the element axis, i.e. the bushing bore axis)
- local y, z complete the frame; G0 fixes the roll of that frame

Therefore **K1 is axial, K2/K3 are the two radial directions, K4 is torsion about
the bore axis, K5/K6 are the two rocking (conical) directions**. This was
calibrated on class5 with a 100 N probe on each global axis
(`probe/cb_orient4.py`): PBUSH K=[1000,3000,7000,1e6,1e6,1e6], GB on +Z,
G0 on +X, gave dz = -0.1000 (K1), dx = -0.0334 (K2), dy = -0.0143 (K3).

## 2. Diagonal model

The script writes a diagonal PBUSH:

```
K1 = E*A/L            A = pi*(ro^2 - ri^2)
K2 = K3 = k_rad       target face radial stiffness
K4 = G*J/L            J = pi/2*(ro^4 - ri^4)
K5 = K6 = k_rad*L^2/12
```

`E = 4 MPa`, `G = 0.6 MPa` are typical filled-rubber values; `k_rad = 1000 N/mm`
is a placeholder for a 60-70 mm bore bushing, **not** a measurement. The rocking
formula is the cantilever one: a radial spring `k` distributed over length L
rotates by `M*L/(k*L^2/12)`.

`ro` defaults to the measured hole-ring radius (the bore the rubber fills), not
to the rubber outer diameter: the plate area that the spring reacts against is the
hole footprint, so `ro = r_ring` and `ri = bore radius` keeps `A` and `J`
consistent with the model.

## 3. Point vs spanning: the element length does not change the stiffness

An OptiStruct CBUSH is a **two-node spring**: it connects the six DOFs of GA to the
six DOFs of GB through the diagonal PBUSH matrix, with no beam-like length
coupling. Therefore

- a lateral force at one grid gives `u = F/K2` whatever the distance between the
  grids, and
- a relative rotation of the two grids gives `M = K5*theta`.

So K2 *is* the plate-to-plate radial stiffness and K5/K6 *are* the plate-to-plate
rocking stiffness. Whether the 44 mm gap is carried by the RBE2 spokes (point
mode, 2 mm element) or by the element itself (spanning mode) makes no difference
to the solved model.

Verified on class5 - same PBUSH 960001/960002, 1000 N at two loaded points:

| build | element length | +Z (node 61698) | +Y (node 58894) |
|---|---|---|---|
| point | 2 mm | 165.6 N/mm | 9913.9 N/mm |
| spanning | 44 mm / 24 mm | 165.6 N/mm | 9913.9 N/mm |

Equal to five digits (`out/117_solve_pair.py`). Two more variants on the same
deck show what the "fixes" actually do (`out/116_bush_variants.py`):

| variant | +Z | +Y |
|---|---|---|
| point / spanning, physical K | 165.6 | 9913.9 |
| spanning, `rigid_rocking` (K5=K6=1e5*k_rad) | 200.2 (+21 %) | 8795.9 |
| spanning, K2=4*k_rad, physical K5 | 178.8 (+8 %) | 8574.9 |

### Corrected claim

Earlier revisions of this skill argued that a long element adds lever compliance
`(L/2)^2/K_rock`, that the face stiffness therefore collapses to `k_rad/4`, and
that K2 had to be scaled by 4. That is **not** how a CBUSH behaves - the A/B above
shows identical results for 2 mm and 44 mm with the same K, so the lever term does
not exist and the 4x scale only stiffens the joint. Do not reintroduce it.

### Choosing

- **point** (default): 3 grids per bushing, element length 2 mm, the spokes carry
  the gap. Fewer nodes, no ring band needed at both plates.
- **spanning**: grids at the two plate planes, the element spans the real gap. Use
  it when the gap must be visible in the deck, when the plates are far apart, or
  when the analyst will measure the element length in the GUI.
- Either way the RBE2 spider rigidifies the hole ring; for bore stresses use RBE3
  or a meshed rubber body.

## 4. When the real matrix is known

A measured bushing gives a 6x6 (or at least K_rad, K_ax, K_tor, K_rock). Feed
`k_rad` with the measured radial value and use `mode: "point"`: then K2 is the
measured number and nothing is double counted. A full 6x6 with coupling needs
PBUSH's K1..K6 plus MATS/PBUSH coupled form - outside this skill; do not fake
coupling with diagonal terms.

## 5. What to check on the solved model

1. One load case per direction, 1000 N at a point far from the bushings.
2. Compare the loaded-point displacement with `F / k_total` where
   `k_total` is the series/parallel combination of the bushings in that load path.
3. A 4x error in one direction and not in the others means the PBUSH order is
   wrong (radial written into K1), or K2 was scaled by 4 under the lever theory a
   CBUSH does not have.
4. Fixing the bushing grids instead of the plates turns the model into a
   "bushing-only" compliance test - useful to isolate the spring, not the
   structure's stiffness.

## 6. Results from class5 (for reference)

Delivered deck (`out/08_class5/full/class5_bush.nas`): 2 bushings, CBUSH length
2 mm, PBUSH 7001 = [180.3, 1000, 1000, 18297.8, 161333.3, 161333.3], PBUSH 7002 =
[330.5, 1000, 1000, 33545.9, 48000, 48000]. Bushings fixed at the inner grids,
1000 N applied:

| case | elastic | bushings rigid | ratio |
|---|---|---|---|
| vertical, mid body | 2278 N/mm | 3337 | 0.68 |
| vertical, rear tip | 178 N/mm | 632 | 0.28 |
| lateral | 8331 N/mm | 9851 | 0.85 |
| torsion about X | 6.06e5 N*mm/deg | 1.70e6 | 0.36 |
| axial | 2443 N/mm | 3039 | 0.80 |

The skill, which measures the hole ring instead of assuming 31.5 mm, writes
K1 = 236.1 / 432.9 N/mm and gives 165.6 N/mm (+Z at node 61698) and 9913.9 N/mm
(+Y at node 58894) with either idealisation.

The bushings dominate the rear-tip vertical and the torsional stiffness: any
absolute claim on those numbers requires the real rubber data.

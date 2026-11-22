---
name: ansa-seam-weld
description: Build seam-weld connections in ANSA from weld-bead solids — read each bead's centerline, rebuild it as a 3D curve, and drive ANSA's own connection manager (CreateConnectionLine SeamLine_Type → AutoSetConnectivity → SetWeldPosition → RealizeConnections) so every weld position becomes a shared node. Output can be rigid RBE2 links, an elastic CBAR post-pass, or a solid weld brick (CHEXA) with an RBE3 spider on each plate for stress work. Use for requests such as "把焊缝几何转成 seam line 建连接", "焊缝自动生成连接", "焊道用 RBE2/CBAR 建出来", "焊缝用实体单元加 RBE3", "六面体焊缝", "把焊缝连到两块板", "RBE2 转 bar 单元", "seam weld from weld beads", "solid weld with RBE3", or whenever a shell model of independently meshed panels carries weld beads as tube solids and the joints must become solver-ready. Also use it when an existing seam weld is judged wrong because a connection names only one part, the weld positions sit off the bead, RealizeConnections produced nothing, or the weld has to carry stress rather than act as a rigid clamp.
agent_created: true
---

# ANSA seam welds from weld-bead solids

Turn a folder of weld-bead tubes plus a shell model into solver-ready seam welds:
one centerline per bead, one connection per centerline, one RBE2 per plate per
weld position.

## What the result must contain

- **one connection per bead centerline**, `SeamLine_Type`, naming **two parts**
  (or one, for a deliberate self-weld)
- **a FE representation on every connection kept** — a connection with no RBE2
  is bookkeeping, not a weld
- **one RBE2 per weld position per plate**: a shared node, `CM=123`, tied to one
  node on each side. Two RBE2 per position for a two-plate joint
- **or, in the elastic variant, one CBAR per RBE2** — `PBARL ROD`, `DIM1` equal
  to the bead diameter, and the diameter present in the exported card
- **or, for stress work, a solid weld** — CHEXA bricks through each joint with an
  RBE3 spider on each plate, a `PSOLID` whose material exists in the deck, and
  the connections that fell back to RBE2 listed explicitly
- **no grid used by nothing** — a node that belongs to no element and no RBE2 is
  a singular DOF
- shell count grows a little, because inserting a weld node splits a shell

## When to use

A shell model whose parts were meshed **independently** (no shared nodes), and
whose joints exist as weld-bead geometry (tubes, `welding` bodies, laser-weld
strips). The beads must be solids or faces — this skill projects lines, so a bead
that is only a line in the CAD is not an input.

Not for: spot welds that are spheres (use `SpotweldPoint_Type`), adhesive
(`AdhesiveLine_Type`), hemming, or structural bolts (`ansa-bolt-connection`).
Not for a model where the parts already share nodes.

## Prerequisites

- `ansa_status` reports `connected: true`. **Use `mode="gui"`**: `SetWeldPosition`
  is documented as GUI-only for the manual method, and screenshots are the
  cheapest way to confirm the weld positions landed.
- The meshed parts as one `.nas` per part, plus the bead STEP files, one per bead.
- ANSA 24.1.2 verified. On another release re-check the traps below: the FE
  parameter names are neither documented nor validated.

## Topology

```
plate A node ──RBE2_A(GN = weld node, CM = 123)
                      │
                 weld node (new GRID)
                      │
plate B node ──RBE2_B(GN = weld node, CM = 123)
```

A two-plate seam therefore owns **2 RBE2 per weld position**. The weld node is
new; both plates keep their own mesh, which is the whole point.

## Workflow

Four stages. Each writes JSON, so a failure never costs the previous stage.

```
scripts/seam_audit.py    offline geometry check (no ANSA, no license)
scripts/seam_extract.py  bead .stp -> centerline points, INSIDE ANSA
scripts/seam_build.py    assemble + connect + realise,   INSIDE ANSA
scripts/seam_chunks.py   fallback for the seams the manager refuses
scripts/seam_final.py    cleanup, acceptance numbers, deck export
```

### 0. Audit the geometry first

```bash
python scripts/seam_audit.py --nas-dir D:/out/parts --seams _seam_lines.json --radius 3
```

Reports, per bead, the distance to the nearest shell node and how many **distinct
parts** sit within the radius. A bead with one part in range is a self-weld or a
weld to a part that was never meshed; a bead hundreds of mm from any node cannot
be welded by any radius. Do this before spending license time — on the reference
model 118 of 145 seams had two parts at 3 mm, 16 had one, 5 had none.

### 1. Extract the centerlines (ANSA)

```python
exec(open(r"<skill>/scripts/seam_extract.py", encoding="utf-8").read(), globals())
```

`base.Open` on a CAD file **replaces the database**, so each bead is opened,
measured and discarded; only the JSON survives. The axis comes from
`base.CreateCurvesAtTubesCenterLine(faces)`, which returns
`{curve: {"diameter": d}}`, and is then sampled at 15 stations with
`base.EvaluateCurvePoint(curve, u)`.

### 2. Build the connections (ANSA)

```python
exec(open(r"<skill>/scripts/seam_build.py", encoding="utf-8").read(), globals())
```

Assembles the parts, rebuilds one spline per centerline, creates one
`SeamLine_Type` connection per curve with **empty connectivity**, attaches the
plates in a distance sweep, places the weld positions, and realises once at the
end.

### 3. Sweep the radius, do not just enlarge it

Default `steps = [(3.0, 2), (5.0, 2), (6.0, 1), (10.0, 1)]` as
`(search_distance, find_exactly)`. The second value is not a detail: `(40.0, 2)`
finds a dozen parts and satisfies nothing, which is exactly how 21 seams stayed
empty while a wider radius was being tried.

### 4. Chunk what the manager still refuses

```python
exec(open(r"<skill>/scripts/seam_chunks.py", encoding="utf-8").read(), globals())
```

Cuts each failing line into ~20 mm pieces and re-runs the same chain on the
pieces. On the reference model 22 seams produced 97 pieces, 13 of which welded.
Keep the original connection until its pieces exist.

### 5. Accept and export

```python
exec(open(r"<skill>/scripts/seam_final.py", encoding="utf-8").read(), globals())
```

Deletes the connections that never reached two parts, counts what is left, checks
for floating grids, and writes the deck.

### 6. Optional: make the weld elastic (RBE2 → CBAR)

```python
exec(open(r"<skill>/scripts/rbe2_to_bar.py", encoding="utf-8").read(), globals())
```

Replaces every two-node RBE2 with one CBAR of `PBARL ROD`, `DIM1` = bead
diameter. Run it as a post-pass on the realised model: the connection manager
cannot produce bars (see traps), so this is the only route to an elastic weld.

Each weld position becomes `plate A -- bar -- weld node -- bar -- plate B`. That
is mechanically the same as one bar of the full length: two collinear bars in
series give `EA/L1 + EA/L2 = EA/L`, and the intermediate node adds no stiffness.

Spiders with three or more nodes are skipped on purpose — that is the bolt
pattern, and collapsing a spider to a single bar would connect only one slave.

Switching representation changes the answer: RBE2 is a rigid clamp (stiff, never
for weld strength or fatigue), CBAR is an elastic rod. Say which one the deck
contains, and re-run the modal check after switching — the first elastic modes
move.

### 7. Optional: weld as a solid — CHEXA brick + RBE3

```python
exec(open(r"<skill>/scripts/solid_rep.py", encoding="utf-8").read(), globals())
```

The connection manager can build the weld itself as a **solid**: FE
representation `RBE3-HEXA-RBE3` puts a CHEXA brick through the joint with an RBE3
spider on each plate. This is the representation to hand to a stress or fatigue
job; RBE2 and CBAR are idealisations that carry no weld stress.

Two conditions decide whether it works, and both fail silently:

| condition | symptom when wrong |
|---|---|
| `W` (weld width) must be > 0 | the card defaults to `W = 0`, and `RealizeConnections` then returns **0** with no elements — not the RBE2 fallback, nothing |
| the weld material must exist in the deck | ANSA creates a MAT1 while realising and **leaves it out of the export**, so `PSOLID` points at a missing material |

`solid_rep.py` sets `W`, applies the pattern defaults, realises, and patches the
missing `MAT1` after export.

**The weld thickness is a parameter, not a consequence of the gap.** The pattern
owns its own settings, named `<Type>_<Pattern>_<Field>` and stored in the ANSA
profile defaults (`config/<Profile>.defaults`, e.g. `NVH.defaults`):

| key | meaning | default |
|---|---|---|
| `SeamLine_RBE3_HEXA_RBE3_Height` | weld thickness, mm | empty → follow the plate gap |
| `SeamLine_RBE3_HEXA_RBE3_StepLength` | weld position spacing, mm | empty → auto (~10 mm at 5 mm mesh) |
| `SeamLine_RBE3_HEXA_RBE3_NumOfLayers` | elements through the thickness | 1 |
| `SeamLine_RBE3_HEXA_RBE3_NumOfStripes` | elements across the width | 1 |

Reach them only through `connections.SetDefault(("SeamLine_RBE3_HEXA_RBE3_Height", "2.0"))`.
Measured on the reference model:

| `Height` | bricks | measured thickness |
|---|---|---|
| cleared | 832 | 0 – 2.64, median 0.49 (follows the gap, collapses where coplanar) |
| 1.0 | 988 | exactly 1.00 |
| 2.0 | 988 | exactly 2.00 |
| 3.0 | 988 | exactly 3.00 |
| 3.0 with `NumOfLayers=2` | 948 | 1.50 per layer |

Setting the thickness also **rescues degenerate positions**: 118 of 133 seams
realise with `Height` cleared, 132 of 133 with it set, because a fixed thickness
lets a brick exist where the two mid-surfaces coincide.

Penta variant: `RBE3-PENTA-RBE3` produced **zero elements at every width tried
(2 / 4 / 6 mm)**, so this release offers the hexa brick only. A wedge weld has to
be built by hand.

Expect a partial result, not a clean sweep: on the reference model 118 of 133
seams built as solids and the remaining 15 fell back to RBE2 in the same call
(174 RBE2 in the deck). Report the split per seam, and never call the model
"solid welds" without it.

### 8. Optional: weld as quad shells — node-shared

```python
exec(open(r"<skill>/scripts/shell_rep.py", encoding="utf-8").read(), globals())
```

`OVERLAP-SHELL` builds the weld out of **CQUAD4 sitting on the existing plate
nodes**, so the joint is bridged by a shared mesh instead of spiders. This is the
representation to use when the weld has to look and behave like a welded joint in a
crash or durability deck, without the extra DOF of RBE3 or the thickness parameter
of a brick.

Measured on the reference model (W = 4 mm, 5 mm plate mesh):

| metric | value |
|---|---|
| realised | **108 / 133** |
| new shells | 4 079 (6 removed by the local re-mesh) |
| quads spanning two different parts | 3 339 of 4 415 new quads |
| quad short edge | p50 4.74 mm (≈ W) |
| quad long edge | p50 5.51 mm (follows the plate mesh, not `StepLength`) |
| RBE2 / RBE3 created | **0 / 0** |

**The pattern must match the joint, and tolerance is not the lever.** On the 22
seams `OVERLAP-SHELL` refused, every one already named **two parts**, so the
connection manager could see the partner plate — the pattern simply declined the
joint. Measured joint geometry across those 22: 11 near-coplanar (0–2°), 6
sandwich (175–180°), 5 near-perpendicular (67–112°).

Widening the search does not help; it makes things worse, because more candidates
have to satisfy a stricter condition:

| setting | seams realised (of 22) |
|---|---|
| default | **1** |
| `SearchDist` 5 / 10 / 20 mm | 0 / 0 / 0 |
| `Parameters_Gap` 0.5 / 1 / 2 / 3 / 5 mm | 0 / 0 / 0 |

So stop tuning tolerance the moment a pattern refuses — change the representation
instead. `fallback_rep` (default `"RBE3-SHELL-RBE3"`) runs a second pass over
whatever the first pass left without FE. That pattern has no root-geometry
requirement and realises **22 of 22** here, giving full coverage:

| pass | representation | connections |
|---|---|---|
| 1 | `OVERLAP-SHELL`, one row, node-shared | 111 |
| 2 | `RBE3-SHELL-RBE3`, spider-tied strip | 22 |
| left without FE | — | **0** |

The two kinds are not equivalent — pass 1 shares nodes with the plates, pass 2 ties
the strip with RBE3 — so report which connections came from which pass.

The rest of the family (`T-JOINT-SHELL`, `EDGEWELD-SHELL`, `LASERWELD-SHELL`,
`CRIMPWELD-SHELL`, `Y-JOINT-SHELL`) builds only a toe strip — about **90 shells
for the whole model**, not a weld bridge. `RBE3-SHELL-RBE3` on its own does realise
all 133 but its 2 090 quads are **floating**: not one shares a node with the
plates, they are tied by 3 107 RBE3. Use it as the fallback, not as the default.

Two side effects are counted by the script, never left to be discovered later:

| side effect | reference model |
|---|---|
| local re-mesh around the seam | 4 079 shells added, 6 removed |
| plate shells re-assigned to per-weld properties | **1 971** — thickness kept, material id changed |

Report both. A weld pass that quietly re-properties 3 % of the plate is a modelling
change, not a display detail.

#### One row or two?

`SeamLine_OVERLAP-SHELL_RootShells` ships as **`"Double Row"`**, which builds two
quad rows meeting at a ridge — one row per plate, correct for a lap joint but easy
to read as "duplicated connection elements". **This skill defaults to
`"Single Row"`**: one web from the plate edge to the other plate, roughly half the
elements. Pass `root_shells="Double Row"` to get the shipped behaviour back.

| `RootShells` | realised | shells added | re-propertied | bridging quads |
|---|---|---|---|---|
| `"Single Row"` (skill default) | 111 | **2 389** | 1 038 | 1 875 |
| `"Double Row"` (ANSA default) | 108 | 4 073 | 1 971 | 3 339 |

Set it explicitly with
`connections.SetDefault(("SeamLine_OVERLAP-SHELL_RootShells", "Single Row"))`, or
leave `root_shells` alone and the script does it.

#### Putting the properties back

**Nothing restores them on its own.** `EraseConnectionFeRep` removes the FE entities
the pattern owns and leaves the re-propertied plate shells carrying the weld
property — so a pass that is later "undone" still leaves the model changed.

`restore_properties=True` writes the original PID back on every shell the pattern
reassigned, from the snapshot taken before realising. Verified: 1 038 changed,
1 038 restored, 0 left changed, and the exported deck shows zero pre-existing
elements with a different property.

## Parameters

| key | default | meaning |
|---|---|---|
| `folder` / `out_json` | — | bead folder, centerline JSON output |
| `n_pts` | 15 | stations sampled along each bead axis |
| `nas_dir` | — | one `.nas` per meshed part |
| `steps` | `[(3,2),(5,2),(6,1),(10,1)]` | `(search_distance, find_exactly)` sweep |
| `fe_type` | `"RBE2"` | the only value this release honours (see traps) |
| `chunk` / `samples` | 20.0 / 40 | fallback piece length, curve sampling |
| `delete_unused` | True | drop connections with fewer than two parts |
| `diameter` | 4.0 | rod diameter for the CBAR post-pass |
| `fe_rep` | `"RBE3-HEXA-RBE3"` | solid-weld representation; `"RBE2"` / `"RBE3"` also valid |
| `width` | 4.0 | weld width `W` — **must be non-zero** for any solid rep |
| `height` | None | weld thickness in mm; None leaves the pattern following the plate gap |
| `step_length` | None | weld position spacing in mm |
| `num_layers` | None | elements through the thickness |
| `fe_rep` (quad) | `"OVERLAP-SHELL"` | quad-weld pattern; must match the joint type |
| `root_shells` (quad) | `"Single Row"` | one web per seam; `"Double Row"` restores the ANSA default |
| `fallback_rep` (quad) | `"RBE3-SHELL-RBE3"` | second pass for seams the joint pattern refuses; `""` disables |
| `restore_properties` (quad) | False | put the original PID back on re-propertied plate shells |
| `export_nas` / `save_ansa` | — | post-pass output paths |

## Rules that make or break the weld

1. **Sample the centerline densely.** 75 of 145 reference beads bulge off their
   chord, the worst by **76.6 mm on a 224 mm bead**. A 3-point spline cuts that
   corner and the weld then follows the wrong path; 15 stations track it.
2. **`EvaluateCurvePoint` takes a normalised arc length, 0..1**, not millimetres.
   Feeding millimetres silently clamps every sample into the first millimetre.
3. **Connectivity must start empty.** `CreateConnectionLine(type, curves, id, [])`
   then `AutoSetConnectivityInConnections` — `EntsToConnectivityString` returns
   `"-1"` for a part with no module id, and a `-1` connectivity welds nothing.
4. **`find_exactly` must match the physics.** 2 for a two-plate seam, 1 for a hem
   inside one stamping. Demanding 2 from a self-weld can never succeed.
5. **Realise once, at the end.** `RealizeConnections` on a connection that
   already owns a FE representation adds a second one.
6. **A weld position needs its own node.** Do not use a plate node as `GN`: the
   position sits between the plates by construction, and reusing a plate node
   makes the joint eccentric.

## Traps that cost hours (all verified on 24.1.2)

| trap | fact |
|---|---|
| `RealizeConnections` does not validate the FE name | an unknown string returns the same success count and the same RBE2 output — `RBAR`, `CWELD` and `ZZZ_NONSENSE` all "work" as RBE2. A **known** pattern that cannot be built returns **0** instead. So 0 means "recognised but failed", never "unknown name". |
| `W` defaults to 0 on a SeamLine | with a zero width every solid pattern silently builds nothing (`realized = 0`). Read `W` and set it to the bead diameter before blaming the FE name. |
| the weld material is created but not exported | the model holds `MAT1 n`, the deck does not: `PSOLID` ends up pointing at a material that is not in the file. Patch after export. |
| `RBE3-PENTA-RBE3` | returns 0 at every width — the penta/wedge variant is not available for seams in this release, only the hexa brick. |
| `connections.SetDefault` is session-global | the value survives `session.New()` and every later realisation. A run that "leaves it alone" inherits whatever the previous run set — clear a key with `""` to restore the pattern's own rule. This is what makes two apparently identical runs produce different brick counts. |
| the pattern settings are not in the API docs | they live in `config/<Profile>.defaults`, hundreds of commented keys per pattern. Grep that file before concluding a dimension is not settable. |
| `EraseConnectionFeRep` does not undo the re-mesh | shells the pattern rebuilt stay in the model, so **a sweep over several patterns in one session accumulates** — `LASERWELD-SHELL` showed "+818 shells" in a sweep and "+90" in a clean session. One pattern per fresh session, or the numbers lie. |
| quad patterns only build for their own joint | `OVERLAP-SHELL` leaves 25 of 133 seams with no FE at all; it does not fall back. Report the ids instead of assuming a defect. |
| nothing undoes the re-propertying | `EraseConnectionFeRep` clears the FE entities, not the plate shells that were reassigned to the weld property. Snapshot element→PID first if the original assignment has to be recoverable. |
| `RootShells` defaults to `"Double Row"` | a lap-joint weld then shows two quad rows meeting at a ridge. It is not a duplicate — it is the pattern's default; `"Single Row"` gives the single web. |
| a refused seam is not a tolerance problem | all 22 refused seams already named two parts. `SearchDist` 5/10/20 mm gave 0 and `Parameters_Gap` 0.5–5 mm gave 0, against 1 at the default. Enlarging a search radius never converts a wrong joint type into a right one. |
| `search_distance` is not monotonic in usefulness | one number for the whole model; too small misses the far plate, too large makes `find_exactly=2` unsatisfiable. Sweep in pairs and carry the failures forward. |
| `GetAllConnectionSettings(conn)` returns `None` | documented as "no parameters" for `SeamLine_Type`; there is no way to enumerate the legal FE values at runtime. |
| connectivity is not readable from the entity | `get_entity_values(deck, ["Connectivity"])`, `"CNCTN"`, `"Parts"`, `"PID"` all answer `{}`. Read it back with `OutputConnections(conns, "XML", path)` and parse that. |
| XML tag depends on the connection type | `connection_1d` for lines, `connection_0d` for points. A parser that only knows `_1d` reports every spot as unattached. |
| `get_entity_values(deck, None)` | raises `invalid parameters`; the card labels must be listed. And `G1..G4` come back as **Entity handles with negative ids**, not ints. |
| `SetWeldPosition` needs GUI for the manual method | `"Automatic"` works headless; `"Manual"` returns an empty failure list and does nothing. |
| deleting before the fallback succeeds | the curve goes with the connection. Build the pieces first, delete second. |
| shell count grows after realising | inserting a weld node splits a shell: 63 188 → 63 338 on the reference model. Not a defect. |
| `base.BestView` takes one Entity | a list raises `argument 1 must be ansa.base.Entity`; use `base.ZoomAll()` for the whole model. |
| `CollectEntities(deck, None, "PBARL")` | **0**. A rod property is stored as `PBARL_ROD`; querying `PBARL` or `PBAR` reports no property while the card exists. |
| `PBARL` loses `DIM1` on **export** | the entity answers `DIM1 = 4.0`, the deck still writes `PBARL 5001 1 MSCBML0 ROD` with no diameter, and nothing warns. Patch columns 41-48 after `OutputNastran` — `patch_pbarl_dims()` in `rbe2_to_bar.py`. |
| CBAR / PBARL cannot be created by API | `CreateEntity` returns `None` for both; write a small `.nas` and `InputNastran` it, then set `DIM1` on the imported entity. |
| reading an RBE2's nodes | `"GM NSET"` returns `{}`; use `base.CollectEntities(deck, rbe2, "GRID")`, which answers exactly `[GN, GM]` for a two-node link. |
| per-file numbering in the `.nas` decks | node **and** property ids restart at 1 in every part file, so offline analysis must key on `(file, id)`, or 22 parts collapse into one and every seam looks single-part. |

## Failure taxonomy

Every spontaneous failure on the reference model belonged to one of four kinds —
classify before inventing a fix:

| symptom | count | what it is | what to do |
|---|---|---|---|
| chord = 0 | 4 | the "bead" is a nugget, not a seam | build `SpotweldPoint_Type` instead |
| two centerlines for one bead | 4 | `CreateCurvesAtTubesCenterLine` returned two candidates | dedupe by midpoint distance, keep the longer |
| strong curvature | 5 | 66-118 mm bead, 23-40 mm bulge | chunk into 20 mm pieces |
| local projection failure | 9 | the line cannot be projected onto both plates | chunk, then report what is left |

Chunking recovered 13 of 97 pieces; the rest are seams whose second plate is not
in the model. Say so rather than widening the radius again.

## Verification

All of these must hold, read from the model and the exported deck:

| gate | expectation |
|---|---|
| connections kept | every one names ≥ 2 parts (read from the XML, not the entity) |
| FE coverage | `GetFirstFeRep` returns something for every kept connection |
| RBE2 count | non-zero, and every RBE2 has `No.of.Nodes ≥ 2` |
| floating grids | **0**, counting grids used by shells, bars and RBE2 `GN` |
| deck | `.nas` carries the `RBE2` cards, plus `PSHELL` and `MAT1` per part |
| element delta | shell count grew only by the weld-node splits |
| bar variant | every CBAR has two nodes, `PBARL_ROD` exists, and the exported `PBARL` line carries `DIM1` in columns 41-48 |
| solid variant | `CHEXA` cards present, every `PSOLID` MID has a `MAT1` in the deck, and the RBE2 left in the deck is attributed to named fallback seams |
| quad variant | `CQUAD4` count grew, `RBE2`/`RBE3` are zero (a node-shared weld needs neither), the failed connection ids are listed, and the re-propertied shell count is reported |

Reference numbers on a 22-part, 63 188-shell subframe: 145 centerlines, 120 full
seams + 13 chunk pieces, 133 connections all two-part, 2 252 RBE2, 0 free grids,
63 338 shells after realisation.

## Reporting

State per seam group: how many were welded as one line, how many needed chunking,
and how many remain unbuildable with the reason. Keep three claims apart: the
connection exists, the weld produced elements, the joint is engineering-valid
(nothing here asserts the third — an RBE2 seam is a rigid clamp, not a weld, and
must not be used for weld strength or fatigue).

Never report success from the return value of `RealizeConnections` alone: it
counts connections, not elements, and it accepts nonsense parameters.

## Bundled resources

- `scripts/seam_audit.py` — offline: distance to nearest node and distinct parts
  per bead. Run before touching ANSA.
- `scripts/seam_extract.py` — bead STEP → centerline points, diameter, chord and
  bulge, one database per bead.
- `scripts/seam_build.py` — assemble, create lines, sweep the radius, place weld
  positions, realise once.
- `scripts/seam_chunks.py` — cut refused seams into pieces and retry.
- `scripts/seam_final.py` — delete unused connections, acceptance numbers, deck.
- `scripts/rbe2_to_bar.py` — post-pass: every two-node RBE2 becomes one CBAR of
  `PBARL ROD`; patches the diameter back into the exported card.
- `scripts/solid_rep.py` — solid weld: `RBE3-HEXA-RBE3` with `W` set, plus the
  missing `MAT1` appended to the exported deck.
- `scripts/shell_rep.py` — node-shared quad weld: `OVERLAP-SHELL`, with the re-mesh
  and re-property side effects counted and the failed seams listed.
- `references/api-verified.md` — exact signatures and the negative results behind
  every trap above.
- `references/failure-taxonomy.md` — the measurements from the 143-bead model.

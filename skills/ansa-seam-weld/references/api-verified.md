# Verified API — ANSA 24.1.2, seam welds

Signatures below were read from the 24.1.2 Sphinx reference
(`<ANSA_HOME>/docs/extending/python_api/html/reference/api_ref_ansa/generated/`)
and then exercised on a live GUI session. Anything marked **negative** was tried
and does not work — it is written down so the next run does not repeat it.

## Geometry

```python
base.CreateCurvesAtTubesCenterLine(input_ents) -> dict
#   input_ents: list of faces and shells
#   returns {curve_entity: {"diameter": float}}
#   one entry per tube found; a bead can yield two entries (see taxonomy)
```

```python
base.EvaluateCurvePoint(CURVE, U) -> list[float]     # [x, y, z]
#   U is the NORMALISED arc length, 0.0 .. 1.0 - not millimetres.
#   U = 0.6 means 60% along the curve.
```

```python
base.CreateCurve(total, x, y, z) -> object
#   total points, three parallel coordinate lists. Produces a spline through
#   the points; use >= 15 stations for a curved bead.
```

## Connections

```python
connections.CreateConnectionLine(type, curves, id, connectivity) -> Entity
#   type: "SpotweldLine_Type" | "AdhesiveLine_Type" | "SeamLine_Type" | "Hemming_Type"
#   id: 0 lets ANSA assign; connectivity: [] on purpose, fill it with AutoSet
```

```python
connections.AutoSetConnectivityInConnections(
    connections, filter_visible=False, output_mode='pid',
    search_faces=True, search_shells=True, search_solids=False, search_tubes=False,
    search_projection=True, search_holes=False,
    use_hole_shape=True, use_hole_proximity=True,
    hole_proximity_distance=100.0, hole_proximity_angle=10.0,
    match_hole_params='any', search_distance=5.0, use_settings=False,
    search_on_direction=False, search_from=-1.0, search_to=1.0,
    find_nearest=2, find_up_to=2, find_exactly=2,
    between_parts=False, clear_connectivity=True,
    tubes_feature_line_angle_limit=30.0, consider_part_hierarchy=False,
) -> None
#   Clears each connection's connectivity, then re-detects it.
#   search_distance is ONE number for the whole model - sweep it.
#   find_exactly=2 is unsatisfiable when more parts than that are in range.
```

```python
connections.SetWeldPosition(connections, set_method='Manual') -> list[Entity]
#   "Automatic" works headless. "Manual" needs the GUI and returns an empty
#   failure list immediately - it is not a no-op you can script around.
```

```python
connections.RealizeConnections(connections, parameters) -> int
#   parameters: {"<Type>": "<FE rep>", "<Type>_<FE rep>_<field>": "<value>"}
#   Returns the number of connections realised; 0 when all failed.
```

```python
connections.EraseConnectionFeRep(connections) -> int
connections.CollectConnectionCurves(connection) -> list[Entity]
connections.GetFirstFeRep(connection) / GetNextFeRep(connection)
connections.OutputConnections(connections, filetype, filename) -> int   # "XML" | "VIP"
connections.GetAllConnectionSettings(connection) -> object | None
connections.GetConnectionSettings(connection, keys) -> object | None
```

## Model

```python
base.InputNastran(filename, ..., merge_parts='on', model_action='merge_model', ...)
#   repeated calls append to the current database; this is how 22 part decks
#   become one model. Property ids are offset automatically.
base.OutputNastran(filename=...) -> int
base.ZoomAll()
base.BestView(entity)        # ONE entity, not a list
base.SaveAs(path) -> int
```

## Negative results

| tried | result |
|---|---|
| `base.CollectEntities(deck, None, "PART")` | **0**. The part entity type is `"ANSAPART"`; use that or the count reads as "no parts" while 23 exist. |
| `RealizeConnections(conns, {"SeamLine_Type": "RBAR"})` | returns success and emits **RBE2**. Same for `"CWELD"` and for `"ZZZ_NONSENSE"` — unrecognised names fall back to the type default without a warning. |
| `GetAllConnectionSettings(seam_connection)` | `None`. No runtime enumeration of legal FE representations for `SeamLine_Type`. |
| `conn.get_entity_values(deck, ["Connectivity"])` (also `CNCTN`, `ConnectivityString`, `Parts`, `PID`, `Defined`, `FE Rep`) | `{}` for every label. Connectivity is only visible through `OutputConnections`. |
| `element.get_entity_values(deck, None)` | raises `Entity.get_entity_values: invalid parameters`. Labels must be listed. |
| `get_entity_values(deck, ["G1", ...])` type check | returns **Entity** objects (negative internal ids), so `isinstance(x, int)` filters everything out and every node looks unused. |
| `base.BestView([shell, ...])` | `argument 1 must be ansa.base.Entity, not list`. |
| `constants.ANSA` | `module 'constants' has no attribute 'ANSA'` — use `constants.NASTRAN` for these decks. |

## Connection list XML

`OutputConnections(conns, "XML", path)` is the only read-back path. Shape:

```xml
<connection_group>
  <id> 1 </id>
  <connected_to> <pid> 1 </pid> <pid> 16 </pid> </connected_to>
  <connection_list>
    <connection_1d>                 <!-- lines -->
      <id> 100004 </id> <type>seamweld</type>
      <loc_list> <loc> x y z </loc> ... </loc_list>
    </connection_1d>
    <connection_0d>                 <!-- points -->
      <id> 100242 </id> <type>spotweld</type> <loc> x y z </loc>
    </connection_0d>
  </connection_list>
</connection_group>
```

`connected_to` empty means the connection found no plates. Connections sharing a
PID pair are grouped together, so a group can hold several `connection_1d`.

## RBE2 → CBAR post-pass

Reading a realised two-node RBE2:

```python
vals  = e.get_entity_values(deck, ["GN", "CM", "No.of.Nodes"])
#   GN: Entity (grid), CM: 123, No.of.Nodes: 2
grids = base.CollectEntities(deck, e, "GRID")     # -> [<GN grid>, <GM grid>]
# "GM NSET" returns {} - the node list is only reachable this way
```

Creating the bars — **`CreateEntity` cannot do it**, the card has to arrive as a
deck:

```python
PBARL,%d,1,,ROD,%.3f                       # PID, MID, GROUP(empty), TYPE, DIM1
CBAR,%d,%d,%d,%d,%.4f,%.4f,%.4f            # EID, PID, GA, GB, X1, X2, X3
base.InputNastran(temp_nas)                # appends to the current model
```

Then write the diameter back on the imported property, because the import keeps
`SECTION='ROD'` and drops `DIM1`:

```python
pid.set_entity_values(deck, {"DIM1": 4.0})
```

Export does it again in the other direction: the entity answers `DIM1 = 4.0`,
yet `OutputNastran` writes `PBARL       5001       1MSCBML0 ROD` and stops. Patch
columns 41-48 of every `PBARL` line after export (`patch_pbarl_dims`).

| query | answer |
|---|---|
| `CollectEntities(deck, None, "PBARL")` | **0** |
| `CollectEntities(deck, None, "PBAR")` | **0** |
| `CollectEntities(deck, None, "PBARL_ROD")` | 1 |
| `__PROPERTIES__` | 23 (22 PSHELL + 1 rod) |

## FE representations for SeamLine_Type — measured

The name is not validated, so the only way to learn the truth is to realise and
count what came out. Result on the 133-seam reference model (W = 4 mm):

| name passed | returned | SOLID | RBE2 | RBE3 | verdict |
|---|---|---|---|---|---|
| `RBE2` | 133 | 0 | 2 252 | 0 | default, two-node rigid links |
| `RBE3` | 132 | 0 | 0 | 2 205 | real, multi-node interpolation spiders |
| `RBE3-HEXA-RBE3` | 118 | **832 CHEXA** | 174 | 3 722 | real: solid brick + spider each side |
| `RBE3-PENTA-RBE3` | **0** | 0 | 0 | 0 | recognised but builds nothing, at W = 2 / 4 / 6 |
| `RBAR`, `CWELD`, `RBE3-HEXA`, `HEXA-RBE3`, `RBE3-WEDGE-RBE3`, `ZZZ_NONSENSE` | 133 | 0 | 2 252–2 284 | 0 | unknown names silently fall back to RBE2 |

Reading the table: **0 is a diagnosis, not a failure**. An unknown name cannot
return 0 — it returns the RBE2 default. So `RBE3-HEXA-RBE3` returning 0 means the
pattern exists and could not be built, and `RBE3-PENTA-RBE3` returning 0 at every
width means the wedge variant is not implemented for seams.

## Solid weld specifics

`W` — the weld width on the connection card:

```python
c.get_entity_values(deck, ["W"])          # -> {'W': 0.0}   default!
c.set_entity_values(deck, {"W": 4.0})     # bead diameter
```

With `W = 0` the hexa pattern returns 0 elements and nothing warns. With `W` set
it builds; the welded width also feeds the RBE3 spider size (spider node counts
moved between 3 640 and 3 722 across W = 2/4/6 mm).

### Pattern settings — the ones that own the brick geometry

Every connection pattern has its own parameter block, named
`<Type>_<Pattern>_<Field>`, stored in the profile defaults
(`config/<Profile>.defaults` — `NVH.defaults`, `CrashSafety.defaults`, …; grep
that file, it lists hundreds of keys per pattern). Reach them only through:

```python
connections.SetDefault(("SeamLine_RBE3_HEXA_RBE3_Height", "2.0"))   # -> 1 on success
```

| key | effect | verified |
|---|---|---|
| `SeamLine_RBE3_HEXA_RBE3_Height` | weld thickness in **mm** | 1.0 / 2.0 / 3.0 gave bricks of 1.00 / 2.00 / 3.00 mm |
| `SeamLine_RBE3_HEXA_RBE3_StepLength` | weld position spacing in mm | 20 → 474 bricks, median length 21.03 mm |
| `SeamLine_RBE3_HEXA_RBE3_NumOfLayers` | elements through the thickness | 2 layers at Height 3 → 1.50 mm each |
| `SeamLine_RBE3_HEXA_RBE3_NumOfStripes` | elements across the width | not exercised |

**`SetDefault` is session-global and nothing clears it.** It survives
`session.New()` and every later realisation; a run that never touches it inherits
the previous run's value. Clear a key with `""` to go back to the pattern's own
rule. Two runs that differ only in what was set before them will otherwise
disagree, which is exactly how the "thickness is geometric" conclusion was
reached by mistake.

Other keys in the same block, not yet exercised:
`SpecifyGap`, `SearchDist`, `RBE3Diam`, `PSOLID_ID`, `InsertRBE2`,
`OrientOnP1/P2`, `FailIfAspect`, `FeatureAngle`, `DistributionIndex`,
`DistributionOnLen/OffLen`, `KeepAllBranches`, `SeparateRefCPinflags`.

Material hole in the export:

```
model:  MAT1 23 exists (created during realisation)
deck:   PSOLID        23      23      SMECH
        MAT1 1..22 only            <- MID 23 missing, property dangles
```

`patch_materials()` appends the missing card; the small-field layout matters —
six 8-column fields starting at column 9 (`MAT1    ` is the tag padded to 8), and
the density is written `7.85-9`, not `7.85e-09`:

```
MAT1          23  210000           0.300  7.85-9
```

Querying the result: solids answer to `CollectEntities(deck, None, "SOLID")`
(ANSA internal type 529); `CHEXA` / `CPENTA` as entity types return 0, so entity
counts tell you nothing about the card type — grep the deck for that.

## Shell / quad representations

`config/<Profile>.defaults` lists the whole menu for `SeamLine_Type`; the entries
that build shells rather than bars, bricks or spiders are:

| pattern | intent | measured (W = 4, 5 mm mesh) |
|---|---|---|
| `OVERLAP-SHELL` | overlap joint, quads on existing nodes | **108 / 133**, +4 073 shells, 3 339 quads bridge two parts, 0 RBE2/RBE3 |
| `OVERLAP-SHELL-CLOSED` | same, closed root | 95 / 133 |
| `T-JOINT-SHELL` | T-joint | 133 / 133 but only ~500 shells |
| `T-JOINT-SHELL-CLOSED` / `-DOUBLE-CLOSED` | T-joint, closed | 62 / 71 |
| `EDGEWELD-SHELL` | edge weld | 133 / 133 but only **+90** shells |
| `LASERWELD-SHELL` / `-CLOSED` | laser weld | 133 / 132, +90 each |
| `CRIMPWELD-SHELL` | crimp weld | 132, +90 |
| `Y-JOINT-SHELL` | Y-joint | 75, +1 960 |
| `RBE3-SHELL-RBE3` | strip + spider each side | **133 / 133**, +2 090 quads, **all floating**, +3 107 RBE3 |
| `SHELL_RBE3` | strip + one spider | 86, +1 109 |

The "+90 shells" patterns only add a toe strip; they are not weld bridges. The
node-shared ones are the `-SHELL` joint patterns, and each only builds where the
joint really is that type — `OVERLAP-SHELL` reports 25 seams with no FE rather
than silently producing something wrong.

Quad size on the reference model: short edge p50 4.74 mm (tracks `W`), long edge
p50 5.51 mm (tracks the plate mesh, `StepLength` has no effect on this pattern).

### Side effects of a node-shared quad pass

`OVERLAP-SHELL` re-meshes and re-properties, measured in-model with an
element→property snapshot before and after:

| effect | value |
|---|---|
| shells added | 4 079 |
| shells removed by re-mesh | 6 |
| plate shells re-assigned to the per-weld property | **1 971** |
| properties | 22 → 28 |
| thickness of the re-propertied shells | unchanged |
| material id of the re-propertied shells | **changed** (e.g. PID 1/MID 2 → PID 52/MID 4) |

### Row count and restoring the properties

`SeamLine_OVERLAP-SHELL_RootShells` defaults to `"Double Row"`. Two rows meet at a
ridge over the joint, which is easy to mistake for duplicate elements.

| `RootShells` | realised | shells added | re-propertied | bridging quads |
|---|---|---|---|---|
| `"Double Row"` | 108 / 133 | 4 073 | 1 971 | 3 339 |
| `"Single Row"` | 111 / 133 | 2 389 | 1 038 | 1 875 |

```python
connections.SetDefault(("SeamLine_OVERLAP-SHELL_RootShells", "Single Row"))   # rc=1
```

Restoring the plate properties — nothing in the API does it:

```python
before = snapshot(deck)                 # {eid: (pid, pid_entity)}
# ... realise ...
fixed = restore(before, deck)           # writes the old PID back, returns the count
```

Verified on the reference model: 1 038 shells re-assigned, 1 038 restored, 0 left
changed; the exported deck then contains **zero** pre-existing elements with a
different property. The weld quads keep their own properties — only the plate
assignment is rolled back.

### Why a joint pattern refuses a seam — and why tolerance cannot fix it

On the reference model `OVERLAP-SHELL` refused 22 seams. Every one of them already
named **two parts** (`P1`, `P2` populated, `Error Class = "No FE Rep."`), so the
failure is geometric, not connective. Joint angle across those 22, measured from
the mesh (Newell normals, two closest distinct parts):

| class | seams |
|---|---|
| near-coplanar (0–2°) | 11 |
| sandwich (175–180°) | 6 |
| near-perpendicular (67–112°) | 5 |

Tolerance sweeps, each on a clean load of the isolated 22-seam model:

| lever | value | realised |
|---|---|---|
| — | default | 1 |
| `SeamLine_OVERLAP-SHELL_SearchDist` | 5 / 10 / 20 mm | 0 / 0 / 0 |
| `SeamLine_OVERLAP-SHELL_Parameters_Gap` | 0.5 / 1 / 2 / 3 / 5 mm | 0 / 0 / 0 |

Widening the search is counter-productive: more candidate parts must satisfy a
condition that the local geometry cannot meet.

What does cover them is a pattern without a root-geometry requirement, run as a
second pass on the leftovers:

| representation | realised (of 22) | result |
|---|---|---|
| `RBE3-SHELL-RBE3` | **22 / 22** | +174 shells, spider-tied strip |
| `RBE3-HEXA-RBE3` | 16 / 22 | 43 solid bricks |
| `SHELL-RBE3` | 14 / 22 | +122 shells |

Two-pass result on the full model: 111 node-shared quad welds + 22 spider-tied
strips, **0 connections without FE**.

### Sweeping patterns is a trap

`EraseConnectionFeRep` removes the FE entities it owns but **not** the plate shells
the pattern rebuilt, so running several patterns in one session accumulates and
inflates every delta after the first. `LASERWELD-SHELL` measured "+818 shells" in a
sweep and "+90" in a clean session; the only trustworthy number is one pattern per
fresh session, measured inside that run.

## Verified conversion on the reference model

| step | result |
|---|---|
| RBE2 before | 2 252, all `No.of.Nodes = 2` |
| spiders skipped | 0 |
| CBAR created | 2 252 (`PBARL ROD`, `DIM1` 4.00) |
| RBE2 after | 0 |
| floating grids | 0 of 65 446 |
| bars with fewer than two nodes | 0 |
| exported `PBARL` | `PBARL  5001  1 MSCBML0 ROD   4.00` |
| shells | 63 338 unchanged |

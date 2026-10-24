# ANSA API facts - verified 2026-10, ANSA 23.0.0 / NASTRAN deck

Everything here was executed against a real ANSA 23.0.0 session. Items marked
**unverified** were never exercised on another release and must be re-checked
before being relied on.

## Environment

| Item | Value |
|---|---|
| ANSA version used | 23.0.0 (compiled Jul 2022) |
| Deck | NASTRAN (`base.CurrentDeck()` returns `1`) |
| Embedded python | 3.9+ |
| Binding style | C extension, **no docstrings**; `__text_signature__` is always `None` |

Because signatures are not discoverable at runtime, call a function and read the
`TypeError` text: it names the missing positional argument. Iterating that way is
faster than guessing from documentation.

## Mid-surface

### `base.Skin(...)`

```
base.Skin(apply_thickness, new_pid, offset_type, ok_to_offset, max_thickness,
          delete, entities, similarity, treat_chamfers, new_part, part, property,
          deviation, larger_side, fail_on_all_errors) -> int
```

Verified call that works:

```python
base.Skin(apply_thickness=True, new_pid=True, offset_type=2, ok_to_offset=True,
          max_thickness=3.0, delete=False, entities=<part FACE list>)
```

| Observed | Detail |
|---|---|
| Return | `1` = the part was recognised as a thin solid and converted; `0` = **unknown**, not "not thin" |
| `apply_thickness=True` | writes the **measured** wall thickness into a new PSHELL `T` |
| `new_pid=True` | creates one PSHELL named `<old name>@SKIN` |
| `delete=False` | keeps the original solid faces - essential while iterating |
| Side effect | **renumbers faces**. Do not diff face-id sets to find the result |

#### `0` is ambiguous - three different causes, one return value

`max_thickness` is a **hard filter, not a clamp**. A wall thicker than the cap makes
Skin return `0`; it never truncates `T` to the cap. Measured on class5.stp:

| Part | true wall | `max_thickness=3.0` | `max_thickness=6.0` |
|---|---|---|---|
| `JA10-34-881` | 3.5 mm | `0` | `1`, T=3.5 |
| `JA10-34-8N1` | 3.5 mm | `0` | `1`, T=3.5 |

But the cap is not the only reason. On the same model 13 large panels returned `0` at
caps of 6, 15 **and 40 mm** - so for those the cause is topology, not thickness. Two
of them (`JA10-34-821`, 1514 faces; `JA10-34-8Z1`, 702 faces) also produced no
MidSurfAuto output and really are solids. The other 10 produced uniform meshes.

Practical consequence: never branch "is this sheet metal?" on Skin's return value.
Feed every `0` to `MidSurfAuto` and let the mesh and its element thickness decide.

Conversely, a `T` equal to the cap is **not** evidence of clipping: four parts on
class5.stp returned exactly `T=3.000` at both a 3.0 and a 6.0 cap because they are
genuinely 3.0 mm.

Correct way to select the mid-surface afterwards: take the property created by this
call and collect its faces.

```python
before = {p._id for p in base.CollectEntities(deck, None, "__PROPERTIES__")}
base.Skin(...)
new_props = [p for p in base.CollectEntities(deck, None, "__PROPERTIES__")
             if p._id not in before]
mid_faces = base.CollectEntities(deck, new_props[0], "FACE")
```

### `base.MidSurfAuto(faces=..., thick=..., length=..., ...)`

The only routine found that obeys a target element length. Documented parameters
used: `faces`, `thick`, `length`, `elem_type`, `part`, `property`, `ret_ents`,
`get_result_type`.

```python
r = base.MidSurfAuto(faces=faces, thick=1.8, length=5.0, elem_type=3,
                     part="auto_create", property="auto_create",
                     ret_ents=True, get_result_type=True)
r.type        # [[input_part_id, output_part_id, result_type, solid_pct], ...]
r.ents        # created elements when ret_ents=True
```

| `length` | Resulting equivalent edge (`sqrt(area/n)`) |
|---|---|
| 10.0 | 9.76 mm |
| 5.0 | 4.91 mm |

`result_type` observed: `"Steady thickness"`. Documented alternatives are
`"Ribs"`, `"Variable thickness"`, `"Solid"`.

**One call can create several output parts, and `ret.type` does not list them all.**
`ret.type` holds one row per *reported* part, but the part set it reports is
incomplete. Measured on class5.stp:

| Run | Parts reported | Shells on reported parts | Shells actually in the model |
|---|---|---|---|
| full model, `ret.type[0]` only | 30 inputs | - | 39 output parts |
| drop policy, every `ret.type` row | 71 | 66,084 | 69,352 |

The 3,268-shell gap and the three properties that never got a measured `T` (7,494 /
1,696 / 1,672 shells) were parts the call created and never reported. Neither
`ret.type[0]` nor iterating all rows is enough.

The reliable handle is a **part-set diff**:

```python
before = {p._id for p in base.CollectEntities(deck, None, "ANSAPART")}
ret = base.MidSurfAuto(...)
created = [p for p in base.CollectEntities(deck, None, "ANSAPART")
           if p._id not in before]
```

Measure every created part that has shells, and keep a final sweep for anything
still unclaimed before writing the deck. Verify the result by reading the exported
properties back - a status counter says nothing about what actually landed in the
file. `base.GetEntityCardValues(part, ["PID"])` *is* trustworthy: on 33 parts the
part's PID matched the PID on every one of its shells, 0 mismatches.

**`thick` is a detection hint, not the result.** Proof: on a part whose true wall is
2.8 mm, `thick=1.0` still yielded `GetShellThickness` values of 2.797-2.800. Use a
value in the right order of magnitude and let the algorithm measure.

Corollary, measured on class5.stp: MidSurfAuto succeeded with `thick=1.0` on parts
whose true walls were 2.0, 2.5 and 3.25 mm *and* which Skin had rejected outright. A
guessed hint is therefore enough to recover a part Skin refuses - it does not have to
be close to the truth.

`property="auto_create"` builds a PSHELL but leaves `T` at the 1.0 default. The
measured thickness lives on the elements, not in the property.

**Reading them back: min/max is not a usable verdict.** Measured on class5.stp, two
instances of the same part number `JA10-34-801` (identical face count, bounding box
and area) gave opposite results from a 40-element min/max window:

| instance | median | sampled range | verdict |
|---|---|---|---|
| part 8 | 3.25 | [2.83, 3.25] | spread 12.9% → blocked |
| part 46 | 3.25 | [3.0, 3.25] | spread 7.7% → written |

Same geometry, same median, opposite verdict - because `shells[::step]` lands on
different elements and min/max is decided by one element. MidSurfAuto also mis-pairs
faces occasionally: `JA10-34-803` had a median of 3.23 mm with a single element at
**20.137 mm**, and min/max let that one element condemn the whole part. Use p2/p98
over several hundred elements, and report the raw extremes separately.

### `base.GetShellThickness(deck, shell)`

Signature trap: the second argument must be an **Entity**, not an id.

```python
base.GetShellThickness(deck, shell_entity)   # correct, returns float
base.GetShellThickness(deck, shell._id)      # TypeError: argument 2 must be ansa.base.Entity
base.GetShellThickness(shell._id)            # TypeError: missing required argument 'shell'
```

This is the per-element thickness produced by `MidSurfAuto`, and the value to write
into the PSHELL.

### `mesh.CalculateThickness(middle_entities, skin_entities, ...)`

The Batch-Mesh "Calculate Thickness" step, available as its own call. Requires the
mid-surface entities **and** the original skin faces. Full parameter list
**unverified** - probing stopped after the two required positional arguments were
identified.

### `base.CreatePropertiesFromShellsThickness(shells, thickness_step)`

Groups shells into PSHELLs by gauge. Verified: `thickness_step=0.01` on a 1232
element part produced two properties - `T=2.8` for 1230 elements and `T=2.73` for 2.
Those 2 are fillet elements where the measured wall distance is shorter, not a real
second gauge. On constant-thickness stampings this splits spuriously; use it only
for genuinely variable-thickness parts.

Related: `mesh.GetGridsThickness(grids)` / `mesh.SetGridsThickness`,
`base.GetPlanarGMatrixAndThickness`, `base.ApplyThicknessToLaminateElements`.

## Element size - negative results

These were all tried on `mesh.BatchGenerator()` and did **not** work. Do not spend
time on them again.

| Attempt | Outcome |
|---|---|
| `mesh.SaveMeshParams(f)` -> edit `target_element_length = 10.` -> `mesh.ReadMeshParams(f)` | `ReadMeshParams` returns `1`, mesh unchanged (4.85 mm, 230 elements on a 5420 mm2 face) |
| `mesh.SetMeshParamTargetLength(5.0)` | `TypeError: argument 1 must be str, not float` |
| `mesh.SetMeshParamTargetLength('target_element_length', 5.0)` | `Invalid label given` |
| labels `''`, `'default'`, `'Target length'`, `'target length'`, `'Target Length'`, `'shell'`, `'surface'`, `'batch'`, `'general'`, `'General'` | all `Invalid label given` |
| `mesh.BatchGenerator(faces)` | works, but always at the session's own default target length |

Conclusion: `BatchGenerator` is fine for "mesh at whatever the session default is",
useless for a specified size. Use `MidSurfAuto(length=...)`.

## Part classification

### `base.IsPartForSkin(part, thickness_tolerance) -> bool`

Present but **wrong in both directions**, and not monotonic in the tolerance.

| Part | 0.1 | 0.3 | 1.0 | 3.0 | reality |
|---|---|---|---|---|---|
| `JA10-34-8H1` | True | False | False | False | MidSurfAuto: 5696 shells, T=2.5 uniform |
| `JA10-34-8G1` | False | False | True | True | MidSurfAuto: 5870 shells, T=2.5 uniform |
| `JA10-34-8P8` (6-face block) | True | True | True | True | solid block, no midsurface possible |

Use `MidSurfAuto`'s output as the authority and this only as a weak hint.

### `base.GetPartDepth(part)`

Returns `{"depth": int, "parent_part": Entity}` - assembly hierarchy depth, not
thickness. Easy to misread from the name.

### Geometry measurement

```python
base.BoundBox(entities) -> [xmin, ymin, zmin, xmax, ymax, zmax]
base.GetFaceArea(face) -> float          # mm2
base.CalcShellArea(shell) -> float       # mm2
```

For a flat plate modelled as a closed thin solid the smallest bounding-box
dimension is a good first estimate of the wall thickness; for curved parts it is
meaningless because the thickness is not axis-aligned.

## Checks module

`base.checks` exposes exactly four submodules: `general`, `geometry`, `mesh`,
`penetration`. There is **no** `base.checks.shell`.

```python
chk = base.checks.mesh.MeshQuality()
chk.execute()
```

`execute()` returning normally only means the check ran. Reading numeric results
back out is **unverified**; the object exposes `parameters`, `quality_criteria`,
`read_f11_values`.

## View and screenshot

```python
base.ZoomAll()                 # fit everything
base.ZoomInEnt(entities)       # zoom onto a selection
base.SetViewAngles(...)        # exists, argument shape unverified
utils.SnapShot(path, "png")    # returns 0 on success
```

`utils.SnapShot` writes the current graphics window. Rotating the view afterwards
did not change a subsequent snapshot in one test - re-verify if a second angle is
needed.

## Solvers and files

```python
base.SaveAs(path)                       # returns 0 on success
base.OutputNastran(filename=path)       # returns 1 on success
base.OutputAbaqus(filename=path)        # returns 1 on success
```

Return codes are ints, not booleans. Confirm success from the file size on disk.

`base.Open(path)` **does not raise when the file is missing.** It writes
`File does not exist !` to the ANSA log and returns, leaving the session with a
single empty `Untitled` part. Everything downstream then behaves normally and the
run reports "no thin-walled part found", so a renamed folder looks exactly like a
model where `max_thickness` is too low. Always `os.path.isfile()` the path first and
verify `len(CollectEntities(deck, None, "FACE"))` after loading.

The Abaqus writer derives `ELSET` names from property names. A property called
`Default PSHELL Property` becomes `ELSET=P74072;Default PSHELL Property, ...`,
which does not parse. Rename properties to compact identifiers first; a `.` in the
name is rewritten to `X` by the exporter. Note the exporter still emits the part id
and the property name joined by `;` (`P161;SHELL_T1`), so a semicolon in the ELSET
name survives the rename - it comes from ANSA, not from your property name.

## Entity type naming per deck

Type strings are card names of the active deck, and a wrong name returns an empty
list instead of an error.

| Concept | NASTRAN | note |
|---|---|---|
| node | `GRID` | `NODE` returns 0 silently |
| shell | `SHELL` | |
| property | `PSHELL` | |
| material | `MAT1` | |
| pseudo-types | `__PROPERTIES__`, `__MATERIALS__` | work across decks |

## Housekeeping

```python
session.New("discard")     # clear the database without saving
base.DeleteEntity([ents])  # accepts a list
base.GetEntity(deck, "ANSAPART", part_id)
base.CollectEntities(deck, container_entity, "FACE")   # container filters scope
```

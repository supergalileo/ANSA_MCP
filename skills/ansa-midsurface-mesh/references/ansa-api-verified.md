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
| Return | `1` = the part was recognised as a thin solid and converted; `0` = not thin |
| `apply_thickness=True` | writes the **measured** wall thickness into a new PSHELL `T` |
| `new_pid=True` | creates one PSHELL named `<old name>@SKIN` |
| `delete=False` | keeps the original solid faces - essential while iterating |
| Side effect | **renumbers faces**. Do not diff face-id sets to find the result |

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

**`thick` is a detection hint, not the result.** Proof: on a part whose true wall is
2.8 mm, `thick=1.0` still yielded `GetShellThickness` values of 2.797-2.800. Use a
value in the right order of magnitude and let the algorithm measure.

`property="auto_create"` builds a PSHELL but leaves `T` at the 1.0 default. The
measured thickness lives on the elements, not in the property.

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

Present but **not reliable on its own**: the result is not monotonic in the
tolerance. On the same model, one part returned `False` at 0.1, `True` at 0.3 and
`False` at 1.0. Use `Skin()`'s return value as the authority and this only as a
hint.

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

The Abaqus writer derives `ELSET` names from property names. A property called
`Default PSHELL Property` becomes `ELSET=P74072;Default PSHELL Property, ...`,
which does not parse. Rename properties to compact identifiers first; a `.` in the
name is rewritten to `X` by the exporter.

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

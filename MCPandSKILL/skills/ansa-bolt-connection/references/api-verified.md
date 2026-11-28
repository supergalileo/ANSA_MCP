# Verified API — ANSA 25.0.0 / NASTRAN deck

Everything below was executed against a real model (61211 nodes, 60430 shells,
39 PSHELL) on 2026-11-17. Signatures are as the bindings accept them, not as the
docs phrase them.

## Bolt solids → axis

```python
base.Open(catpart)                                  # REPLACES the model
faces = base.CollectEntities(constants.NASTRAN, None, "FACE")
crv   = base.CreateCurvesAtTubesCenterLine(faces)   # {Entity: {"diameter": float}}
base.GetCurveLength(curve)                          # float
base.EvaluateCurvePoint(curve, 0.0)                 # [x,y,z]  (single float U, 0..1)
```

The longest returned curve is the shank. Shorter ones are fillets/heads
(`diameter` 16, 28.5 …). Take `diameter` of the longest as the bolt diameter.

## Holes

```python
connections.DefineConnectionHoles(
    parts, create="Bolt_Type", output_mode="properties",
    parts_proximity_for_connection_merging=0.0, maximum_length=0.0,
    exclude_single_part_connections=False, match_hole_params="any",
    search_holes=True, use_hole_shape=True, use_hole_proximity=True,
    hole_proximity_distance=100.0, hole_proximity_angle=10.0,
    hole_minimum_diameter=3.0, hole_maximum_diameter=25.0,
    tube_minimum_diameter=0.0, tube_maximum_diameter=0.0,
    search_tubes=False, tubes_feature_line_angle_limit=30.0)
# -> list[Entity];  b.position -> (x,y,z);  b.get_entity_values(DECK,["D"])["D"]
```

Survey then delete — the entity is only used as a hole probe.

`output_mode` does **not** change what `P1` holds: `#28` is a PSHELL/pid, not a
part id. Part id = `base.GetEntityPart(shell)._id`; the two differ by 2 in this
model. Do not mix them.

## Nodes, sets, rigid bodies

```python
s = base.CreateEntity(DECK, "SET", {"Name": "BOLT_x"})   # -> Entity (type 3105)
base.AddToSet(s, [node, ...])                            # -> 1
base.RemoveFromSet(s, [gn])                              # -> 1
base.CreateEntity(DECK, "RBE2", {"GN": node, "GM NSET": s, "CM": "123456"})
base.CollectEntities(DECK, set_entity, "GRID", recursive=True)   # set members
```

`RBE2` card fields: `EID, GN, CM, GM NSET, ALPHA, No.of.Nodes`. There is **no**
direct node list — the slaves live in the SET.

## Beam + section

```python
# no CreateEntity path exists — write nas and import
base.InputNastran(path_with_PBARL_and_CBAR)
pbar = cbar.get_entity_values(DECK, ["PID"])["PID"]      # type N_PBAR, ansa_type "PBARL_ROD"
pbar.set_entity_values(DECK, {"DIM1": 14.0})             # the only way DIM1 sticks
```

`PBARL_ROD` card fields: `TYPE, PID, MID, SECTION, DIM1..DIM6, NSM`.
`SECTION` reads `"ROD"`, `DIM1` is the diameter — but only after the manual set.

Export writes `PBARL       1001       1MSCBML0 ROD` with no DIM1; patch columns
41-48 afterwards.

## Model I/O

| call | behaviour |
|---|---|
| `base.InputNastran(path)` | merges; returns 1 |
| `base.Open(path.nas)` | does NOT import nas (clears the model) |
| `base.Open(path.ansa)` | works **only in GUI mode**; returns 2 in batch |
| `base.OutputNastran(path)` | export current deck |
| `base.SaveAs(path)` | save .ansa |

## Reference result (this project)

18 bolts → 30 RBE2 + 12 CBAR + 12 PBARL, 932 slave nodes, 0 free, gap ≤ 45.1°.

| bolt | sides | nodes per side | gap | beam |
|---|---|---|---|---|
| 023 ×2 | 2 | 44 / 31–32 | 17–26 | 65.7 |
| 024 ×2 | 2 | 24 / 20–24 | 27–41 | 61.2 |
| 025 ×2 | 2 | 23–28 / 21–25 | 26–37 | 61.2 |
| 080 ×4 | 2 | 39–45 / 39–42 | 21–27 | 46.3–46.6 |
| 402 ×2 | 2 | 12 / 24–46 | 23–45 | 38.3–38.5 |
| 401 ×4 | 1 | 26 | 36 | none |
| 026 ×2 | 1 | 12 | 35–44 | none |

023's A-side and 402's second layer are **fitted** — the hole finder did not see
them, so the centre comes from the node ring.

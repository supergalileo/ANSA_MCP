# Verified ANSA / OptiStruct API facts (bushings)

Everything below was executed, not read from documentation. ANSA 2020, NASTRAN
deck, OptiStruct 2020.

## Creating the entities

| entity | how it works | notes |
|---|---|---|
| GRID | `base.InputNastran` only | `CreateEntity(DECK,"GRID",{...})` returns None |
| PBUSH | `CreateEntity(DECK,"PBUSH")` works, fields K1..K6 readable/writable via `GetEntityCardValues` / `SetEntityCardValues` | text import is simpler and keeps K order explicit |
| CBUSH | **text import only** | `CreateEntity(DECK,"CBUSH")` returns None |
| RBE2 | `base.CreateEntity(DECK,"RBE2",{"GN":gn,"GM NSET":set,"CM":"123456"})` | build the SET first |
| SET | `base.CreateEntity(DECK,"SET",{"Name":...})` + `base.AddToSet(set,[...])` | one SET per spider |

Minimal card block that imports cleanly (free field, comma separated):

```
BEGIN BULK
GRID,970001,,0.0,0.0,0.0
GRID,970002,,0.0,0.0,2.0
GRID,970101,,2.0,0.0,0.0
PBUSH,7001,K,180.3,1000.0,1000.0,18297.8,161333.3,161333.3
CBUSH,1,7001,970001,970002,970101
ENDDATA
```

The `K` flag after the pid is mandatory. Without it ANSA reads K1..K6 as 0
(verified with two variants of the same card).

## Reading a model back

- nodes: `base.CollectEntities(DECK, None, "GRID")`, position via `n.position`,
  id via `n._id`
- shells: the type string is `SHELL`, **not** `CQUAD4`/`CTRIA3` (those
  return 0 and make you think the import failed)
- CBAR / CBUSH / PBUSH / RBE2 / PSHELL: plain type strings work
- PBUSH values: `get_entity_values(DECK, ["K1", ..., "K6"])` returns the six K as written -
  use it to prove the export did not lose a field

## OptiStruct card reading

- CBUSH field 6 is **G0 = grid id**. Putting a coordinate-system id there gives
  `*** ERROR # 1000 *** ... Incorrect data in field # 9. Field 'CID' of CBUSH bulk data.`
  (the reader shifts the remaining fields left).
- G0 pointing at a non-existent grid: `*** ERROR #14 *** ... Missing GRID GO # 101 referenced by CBUSH # 1`.
- No G0, no CID, blank X1/X2/X3: only K1 may be non-zero,
  `*** ERROR #99 *** K2/M2/B2, K3/M3/B3, K5/M5/B5, and K6/M6/B6 on PBUSH must be zero ...`
- Zero-length element: `*** ERROR #40 ***`. 2 mm is enough.
- Fixed-field 8-column cards must be built column by column
  (`"%-8s"` per field); a shifted field gives
  `*** ERROR #1484 *** ... Field 2 contains embedded blank(s).`
- `PARAM,CHECKEL,NO` is needed when the mesh contains slivers
  (`*** ERROR #2203 *** Acceptable range violation: Vertex Angle ...`).
- One `SPC` card per subcase: a second one gives
  `*** ERROR #1009 *** Multiple "SPC" cards in SUBCASE # 1`. Merge the DOFs into
  the same SID instead.
- ANSA writes its own `SET` cards into the deck; strip them (and their numeric
  continuation lines) before handing the deck to OptiStruct.

## Reading results

- Displacements: `.pch` grid-point displacement records, 16 columns per value,
  the gid field is 10 wide and a negative number overflows one column to the
  left, so parse with a regex:
  `re.compile(r"[-+]?\d+\.\d+E[-+]\d+")` rather than by column offsets.
- SPC forces: sum over **all** SPC grids present in the .pch, not over the few
  ids the script remembers - otherwise the equilibrium check is meaningless.

## Batch-mode facts

- `base.Open` on a `.ansa` file returns 2 and loads nothing; use GUI mode.
- `base.Open` on a CATPart/STEP replaces the model - hence the two-stage
  extract/build split, and `session.New("discard")` before reloading.
- `OutputNastran` re-emits fixed-field cards, so the exported deck no longer
  looks like the free-field input - that is expected and it is the format
  OptiStruct and ANSA both accept.
- **But it glues 9-character values.** A PBUSH written free-field comes back as
  `PBUSH     960001K          236.1   1000.   1000. 27429.8161333.3161333.3` -
  K4 and K5 fused into one 9-column token, so read-back and the solver see
  `27429.8` and a damaged K5/K6. The builder rewrites every PBUSH as
  `PBUSH,pid,K,k1,...,k6` after `OutputNastran`; read-back then gives
  236.137 / 1000 / 1000 / 27429.8 / 161333 / 161333 exactly.

## CBUSH formulation (measured, not assumed)

- A CBUSH is a **two-node spring** between the six DOFs of GA and GB; it has no
  length coupling. Same PBUSH, 2 mm versus 44 mm element, class5 LC2/LC3:
  165.6 / 9913.9 N/mm both ways. The lever compliance `(L/2)^2/K5` that
  earlier notes claimed does not exist; do not scale K2 to compensate it.

# Field notes - measurements, war stories, and what not to repeat

Companion to `SKILL.md`. Everything here was measured; the rules derived from it
live in `SKILL.md`, the raw numbers and the story of how each was found live here.

## Casting leaves the surface in pieces - two separate causes

Found on the delivered 540-part folder: 7 of 24 Casting results were fragmented
(one crash box had **12 disjoint patches**). Two causes, both measurable:

**1. `thickness_tolerance` too tight silently drops walls.** The rate that
`MiddleGeometry` returns (`faces_success_rate`) is the tell:

| part | tol 1.0 | tol 2.0 |
|---|---|---|
| O044 crash box | **16.6%** of faces, 2 075 shells, 12 patches | 99.9%, 8 218 shells, 9 patches |
| B0736 sill insert | 100%, 28 068, 6 | 97.7%, 28 822, 5 |
| O025 crash box | 7 patches | **13.2%** - still not enough, needs 5.0+ |

The part still came out `rescued` because nothing gated on the rate. Fixed:
default is now 2.0 and anything under `min_face_rate` (90%) ends as
`casting_partial` instead of being silently shipped.

**2. `MiddleGeometry` does not connect the patches; `MidSurfAuto` does.**
Same crash box: geometry+mesh gives 9 components, full Casting gives 3, at 4x the
element count and 206 s vs 12 s. So the connection step lives inside MidSurfAuto,
which is also the third mesher we do not want and can hang for 10+ minutes on a
sill insert.

Stitching APIs were tried and do not fit: `FacesFuseProximities` requires two
*different* face lists, `JoinFaces` takes one argument and rejects mid-surface
faces, `mesh.AutoPaste` returned 0 and changed nothing.

Practical split: use Casting at tol 2.0 and accept the remaining patches as
separate walls joined by welds/RBE3 (normal BIW practice), or run `MidSurfAuto`
on those specific parts as an explicit exception.

## Other mid-surface entry points in ANSA

Beyond `Skin` and Casting, the API also exposes `base.SkinSingleTRB`,
`base.MidSurfExtrusion` (extruded profiles), `base.FacesMiddleSingle` and
`base.CurvesMiddle` (single face / single curve). QA helpers:
`mesh.CheckMiddleSurface`, `mesh.CheckMiddleMesh`, `mesh.GetMiddleMeshQualityScore`.

`MidSurfExtrusion` is a dead end as an automatic generator: it takes **no
arguments**, returns a count (3 on a crash box, 1 on a sill insert), and creates
**zero** faces - tested on two parts, with and without a prior selection
(`base.SelectEntities` does not exist in this build). It also burned 35 s on the
sill insert. Do not spend time on it again; `MiddleGeometry` at tol 2.0 is the
usable geometry path.

## Why long batches must not go through the MCP server

Measured on a 540-part BIW folder, four disconnects in one afternoon. The chain:

1. ANSA's kernel dies outright on some geometry - the log ends with
   `running... finished.` and no Python traceback at all, so the script never sees
   an exception.
2. The `ansa` MCP server is the **parent** of that ANSA process. When ANSA goes,
   the server goes with it: the state file's `pid` and `ansa_pid` both point at
   processes that no longer exist.
3. The host does not respawn a stdio server by itself, so every crash costs a
   manual reconnect.

Nothing inside the server can fix (3). `scripts/parallel_batch.py` sidesteps the
whole chain by launching `ansa64.bat -nogui -exec` directly: no connector in the
path, no 600 s tool limit, and a crash costs one part.

### Three traps in that driver, all hit for real

- **A dead ANSA leaves the cmd.exe wrapper alive.** Watching the process for exit
  reports "still running" forever: four workers sat idle for 11 minutes while
  everything looked healthy. The only reliable signal is the progress file going
  stale - 200 s without a new line means the worker is dead or hung, kill the
  process tree and restart.
- **`shutil.rmtree` on a slice folder trips the bulk-delete guard.** Clearing 57
  stale hardlinks before a restart exceeded the guard's threshold and killed the
  driver itself. Every run and every restart now gets a fresh directory and
  nothing is ever deleted.
- **The merged progress file must be appended to, never rewritten.** `_progress.jsonl`
  also holds every record from an earlier MCP-driven phase; rewriting it with just
  the current run's worker records dropped 490 rows and cost the summary. It was
  rebuilt from `汇总.csv` plus the worker files.

Also: `json.dumps` on a params dict emits `false`, which is not Python - generating
a runner script with it produces an instant `NameError`. Use `pprint.pformat`.

### Parallelism that actually helps

16 logical cores, 4 workers, ~0.9 GB RSS each: 2.2 parts/min on a folder whose
tail parts take 30-60 s each. More interesting than the speed:

**four workers ran 25 parts with zero crashes, while the single-process runs died
every 30-40 minutes.** Each worker exits after ~8 parts, so far less accumulated
kernel state. Shorter process lifetimes are more stable than one long one.

### Parts that kill a batch

Two classes showed up on 540 parts, both non-structural trim:

| part | behaviour | handling |
|---|---|---|
| `B0595_A0072600531` | crashes ANSA when processed **after other parts**, fine alone (99 509 shells, T=1.062) | run in its own process |
| `O067`/`O090` 泡沫塑料盖, `O139`/`O144`/`O145` NVH 贴条 | Casting or meshing hangs indefinitely, no crash | mass-equivalent these; they are G11 non-structural anyway |

The first one is the useful lesson: "this part crashes ANSA" can mean "this part
crashes ANSA **in this context**". Retry it alone before writing it off.

## Casting second pass - the numbers

Seven parts a 553-part BIW folder had rejected via `Skin`, run through
`scripts/casting_midsurf.py` with `engine="mesh"` and the project's 5 mm
`.ansa_mpar` / `.ansa_qual`:

| part | faces in | mid faces | face rate | shells | gauge mm | time |
|---|---|---|---|---|---|---|
| B0360 托架 | 322 | 125 | 100% | 1 486 | 1.52 | 7.1 s |
| B0367 横梁 | - | 20 | 100% | 21 298 | 1.52 | 20.0 s |
| B0504 | - | 34 | 100% | 765 | 2.91 | 4.9 s |
| B0571 端部托架 | - | 33 | 100% | 779 | 2.91 | 4.7 s |
| B0870 加强托架 | 354 | 141 | 100% | 3 002 | 1.02 | 7.7 s |
| B1028 托架 | 102 | 10 | 100% | 96 | 3.42 | 2.9 s |
| O081 后安装托架 | 145 | 21 | 100% | 1 256 | 1.37 | 2.8 s |

50 s for the seven, 6 `rescued` + 1 `rescued_var` (B0367, p2-p98 spread 34% - the
rear rail genuinely varies, the median is written and the property renamed
`*_suspect`).

For comparison, `MidSurfAuto` on four of the same parts: 33 s, 324 s, 14 s, 14 s -
370 s total, and the process then died inside the mesher.

## Why MidSurfAuto cannot be the second pass here

`base.MidSurfAuto` creates a part called `Middle Skin Shells` holding 1306 SHELL
and **0 FACE** (B0360, verified by reopening the saved `.ansa`). It is a mesher.
Its elements cannot be re-meshed by another engine, cannot be handed to a Batch
Mesh session, and carry a mesh from a third algorithm that no `.ansa_mpar`
controls.

ANSA logs `Middle-Casting time : N s` during every MidSurfAuto call - that is the
Casting routine doing the surface work. `base.MiddleGeometry(faces, tol)` is the
same routine with the meshing removed, which is why the split works: Casting finds
the surface, `mesh.Mesh` or Batch Mesh builds the elements.

## Engine comparison - and why most comparisons are wrong

In one session on one `Skin` result (B0027): `mesh.Mesh` gave 204 elements /
worst aspect 2.864; a second `mesh.Mesh` after deleting the mesh gave 218 / 2.345;
a Batch Mesh run straight after gave 208 / 2.861.

The apparent "engine A is better" gap is the **run order**. With
`change_option = macro perimeters` the first pass rewrites the perimeter node
distribution, so every later pass starts from different perimeters. Compare
engines only on the first mesh of a fresh Skin, and say which pass a number came
from.

## The four defects a 289-part run exposed

1. **`Skin` has three buckets.** The return dict carries `skinned`, `check` and
   `failed`. A `check` group already has its middle surface - ANSA only wants it
   reviewed. Counting `skinned` alone writes off good parts: 4 of 9 parts reported
   as `skin_failed` held 58-5 000 `check` faces and meshed fine (`B0318` ->
   10 712 elements).
   ```python
   n = sum(len(g.get("skin") or []) for k in ("skinned", "check")
           for g in (ret.get(k) or []))
   ```
2. **Every retry leaves a part behind.** `Skin(new_part=True)` creates a part per
   group even when it reports `failed`/`check`. Retry twice and the database holds
   three stacked middle surfaces, and the mesh lands on all three. Snapshot the
   part ids first, keep the last attempt, delete the rest.
3. **`DetectSolidDescription` returns a negative sentinel** (`-1.0`) when it cannot
   estimate: "no consistent offset pair", not "thick". `T <= 0` means the estimate
   is unknown - try Skin anyway.
4. **Output names must not depend on processing order.** One part number can sit at
   several assembly positions (four `B0343..B0349_A0072600170`). Computing the
   stem map from a size-sorted list makes two runs disagree and one part overwrite
   another's `.ansa`. Compute it once from the **name-sorted** full list and hand
   the same map to every worker.

## Verification gates (apply to any of the pipelines)

| gate | expectation |
|---|---|
| shell count | non-zero and close to `total_area / target^2` |
| `edge_p50` | within ~6% of target; 20%+ means the target length never applied |
| `edge_p10`/`p90` | roughly target x (0.8 ... 1.05) |
| quad ratio | typically 0.8-0.95 |
| per-part `T` | equals the measured value, and is uniform |
| `elements_without_T` | **0 for a deliverable deck**, read from the PSHELL state |
| `placeholder_gauge` | any PSHELL still at `T = 1.0` means the gauge never arrived |
| faces left | only for parts that were never shelled |

`edge_p50` a few percent under target is normal: the mesher projects onto curved
geometry and `sqrt(area)` is a proxy, not an edge length.

## min/max is not reproducible - use p2/p98

Two instances of `JA10-34-801` in class5.stp have identical geometry and identical
median (3.25 mm), yet one sampled `[3.0, 3.25]` and passed while the other sampled
`[2.83, 3.25]` and was blocked. Same part, opposite verdicts, from a 40-element
min/max window. `JA10-34-803` had a median of 3.23 mm with one mis-paired element
at 20.137 mm; min/max lets that single element condemn the part.

## The `block` policy hazard

`block` keeps shells whose gauge spread exceeded tolerance and leaves `T` at the
mesher default 1.0 - and that value **is** what ships. In class5.stp a `block` run
put 18 298 of 77 034 elements (23.8%) on a placeholder 1.0 mm gauge, invisible to
every other metric. `drop` is the only policy that yields a deck analysable as-is.

## Element size - negative results worth not repeating

`mesh.BatchGenerator()` ignores the target length. Editing `target_element_length`
through `SaveMeshParams`/`ReadMeshParams` has no effect (a 10 mm setting still
produced 4.85 mm elements on a 5 420 mm2 face). `mesh.SetMeshParamTargetLength`
rejects every label tried (`''`, `'default'`, `'Target Length'`, `'shell'`, ...).
Only `MidSurfAuto(length=...)` and the mpar-driven engines honour a size.

## 553-part BIW folder - classification counts

After the name/part-number gate (see `SKILL.md`): 553 parts = 383 `done`,
129 `hardware`, 29 `not_sheet_metal`, 12 `skin_failed`. The `A007S` part-number
block held 118 parts and every one was a standard part; the Chinese Nomenclature
agreed on 115 of them and covered the 3 with unread names. Eight large standard
parts (threaded bushings 11 636 mm2, guide sleeves 37 387 mm2) were missed by the
area rule alone and recovered by the name rule.

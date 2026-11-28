# Splitting an assembly STEP - measurements

Everything here comes from one real file: a 51 MB CATIA V5R21 STEP of a
tailgate, `CONFIG_CONTROL_DESIGN` (AP203), written 2016.

## What the file contains

| count | entity |
| ----- | ------ |
| 588 772 | entities in total, **one per line**, no continuation lines |
| 45 | `PRODUCT` |
| 45 | `SHAPE_DEFINITION_REPRESENTATION`, `SHAPE_REPRESENTATION` |
| 33 | `ADVANCED_BREP_SHAPE_REPRESENTATION` (the parts that carry geometry) |
| 177 | `MANIFOLD_SOLID_BREP` |
| 7 800 | `ADVANCED_FACE` |
| 79 | `NEXT_ASSEMBLY_USAGE_OCCURRENCE` / `CONTEXT_DEPENDENT_SHAPE_REPRESENTATION` / `ITEM_DEFINED_TRANSFORMATION` |
| 12 | `PRODUCT_DEFINITION` (assembly nodes, no geometry) |
| 33 | `PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS` (parts) |

Two different definition types for the same idea is the first thing that trips a
naive reader: a part is `PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS('','',#formation,#context,(#doc))`
and an assembly node is `PRODUCT_DEFINITION('','',#formation,#context)`. Filter
on `PRODUCT_DEFINITION` alone and you get the 12 assembly nodes and none of the
33 parts.

The product chain is:

```
PRODUCT                                          name lives here
 <- PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE
    <- PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS
       <- PRODUCT_DEFINITION_SHAPE
          <- SHAPE_DEFINITION_REPRESENTATION
             -> SHAPE_REPRESENTATION               placement only, no solids
                <- SHAPE_REPRESENTATION_RELATIONSHIP   (in-degree 0 !)
                   -> ADVANCED_BREP_SHAPE_REPRESENTATION   the real geometry
```

`PRODUCT_DEFINITION_SHAPE` is 124 against 45 `PRODUCT`: the instance-level CDSR
creates one extra per assembly instance.

## Verification against ANSA

Split then re-import each part into ANSA 24.1.2, compared with the source:

| metric | source STEP | sum of split parts |
| ------ | ----------- | ------------------ |
| `ADVANCED_FACE` | 7 800 | **7 800** |
| parts imported | 68 (ANSA splits multi-solid products) | 33 files, 1 part each |
| bbox | -817…583 × -594…594 × -161…47 | every part inside it |

Face count matching exactly is the gate that matters: it proves the closure
walk did not drop a face and did not duplicate one.

Runtime: **24 s** for 51 MB, including writing 33 files.

Three sample parts carried through `process_part.py` unchanged:

| part | solids / faces | verdict | shells | `edge_p50` | `T` |
| ---- | -------------- | ------- | ------ | ---------- | --- |
| 73-face bracket | 1 / 73 | `done` | 433 | 4.831 | 2.5 |
| 174-face panel | 1 / 174 | `done` | 1 036 | 5.006 | 3.0 |
| 22-face weld nut | 1 / 22 | `hardware` | – | – | area 805 mm², wall 7.5 mm |

The third row is the important one: the splitter classified it `fastener` by
size, and the pipeline independently reached `hardware` from a measured area of
805 mm² and a 7.5 mm wall. Two different methods, same answer.

## The three entities that break the file

### `VECTOR` - crash

Arguments are `(name, #direction, magnitude)`. A coordinate rewriter that treats
every `CARTESIAN_POINT`/`DIRECTION`/`VECTOR` alike reads `#3825` as a number,
gets nothing, and emits `VECTOR(' ',(0,0,0))`. ANSA then dies with
`0xC0000005` (`-1073741819`) partway through the import - the progress bar
reached 42% on the first attempt, so it looks like a geometry problem, not a
syntax one.

### `SHAPE_REPRESENTATION_RELATIONSHIP` - silent empty import

It has in-degree 0: the shell rep does not reference it, the BREP rep does not
reference it, only it references both. A closure walk from the product therefore
never reaches it, and the exported file looks complete - product name, placement,
context, units all present. ANSA imports it without a single warning and reports
**1 part, 0 faces**. The only symptom is the number zero.

### `APPLICATION_PROTOCOL_DEFINITION` - refused file

Also in-degree 0, also missed by any closure-based subsetter. It is mandatory in
`CONFIG_CONTROL_DESIGN`.

The general lesson: **in-degree 0 is not the same as unreachable.** Anything that
only points *at* the geometry (relationships, protocol declarations, approvals)
has to be added to the keep-set by type, not by reachability.

## Bounding boxes: three corrections, all of them load-bearing

### Placement origins are not geometry

`AXIS2_PLACEMENT_3D` carries an origin point, and it sits wherever the datum was
defined - not on the part. Measured on a 15 mm weld nut, the raw point cloud
reached `(0, 0, 0)` and `(280, 532, 69)`:

| part | raw point cloud | placement removed |
| ---- | --------------- | ----------------- |
| weld nut A | 783 × 82 × 138 | 14.9 × 12.4 × 15.6 |
| weld nut B | 8 × 789 × 122 | 14.9 × 15.1 × 8.3 |
| clip | 501 × 12 × 28 | 19.0 × 12.0 × 19.0 |

The classifier's fastener gate is 60 mm, so a 783 mm box turns a nut into a
panel and the pipeline pays a full `Skin` attempt on it.

**It is not b-spline control points**, which an earlier version of this note
claimed. Excluding placement origins alone brings every part to its real size;
control points stay in the closure and never showed up as an outlier.

### A cylinder's diameter is on no vertex

`VERTEX_POINT` covers the seam plane and nothing else. A round tube measuring
23.95 × 23.0 × 55.3 mm in ANSA came out as 1.0 × 23.0 × 54.9 from vertices
alone. Any axis shorter than `2 x modal cylinder radius` is widened to it; the
same part then reads 23.0 × 23.0 × 54.9.

### Vertex boxes under-report big panels

A closed b-spline face with no boundary vertex contributes no points, so a
1 212 mm door outer measures 1 039 mm. Harmless for classification - 1 039 mm is
still obviously not a bolt - but do not quote it as the part size.

## The solid name is the best classifier in the file

CATIA writes non-Latin names as `\X2\` + four hex digits per character +
`\X0\` (UTF-16BE). Decoding them turns the design tree into a labelled data
set - no guessing:

Part numbers are masked as `P-A` … `P-F`; they carry no meaning here.

| raw | decoded | part |
| --- | ------- | ---- |
| `welding` | welding | 127 bodies in one cross-member |
| `6A6180F68F74627F` | 橡胶轴承 | `P-A` |
| `6A6180F6886C5957` | 橡胶衬套 | `P-B` |
| `710A63A587BA6BCD` | 焊接螺母 | `S-A` … |
| `677F` | 板 | every stamping |
| `570667F1` | 圆柱 | `P-E` |

**This outranks every geometric test**, and the reason is concrete: `P-A` is a
rubber bushing with a hex flange, and the cylinder-ratio test scored it 61%
cylindrical and called it a weld run. Rendering it settled the argument in one
look - a 66 mm hex body with a bore. `P-B` is the same family (橡胶衬套). Both
had been classified `weld_seam` and would have been dropped from the mesh.

Order of authority: **decoded solid name > product name > body of revolution >
bounding box**.

## Weld runs are tubes, and there are far more than meet the eye

A tailgate cross-member is one PRODUCT holding **128 solids**: one 646-face
panel plus 127 bodies that CATIA itself names `welding`. Handed to `Skin` as one
part the whole thing fails, and the failure is reported against the part, not
against the 127 tubes.

CATIA expresses the same tube in two ways, and only the first is visible to a
cylinder counter:

| expression | signature | example |
| ---------- | --------- | ------- |
| analytic | `CYLINDRICAL_SURFACE` ≥ 40% of the faces, no b-spline, modal radius 1.5-50 mm | 6 faces = 4 cylinders + 2 planes, radii 7.5 / 12.5 mm |
| b-spline | knot range closes on itself: `12.5663706144 = 4π` or `6.283185307 = 2π` | 4 faces = 2 closed b-splines + 2 planes, `(0, 25)` × `(0, 12.566)` |

The b-spline case is the one that hides. In this model it accounted for
**127 of the 128 solids** and every one of them passed the cylinder test with a
score of zero.

The 2π tolerance has to be tight. The *linear* direction of the same surface is
`(0, 25)`, and `25 / 2π = 3.979` - within 0.5% of 4. A loose "is this a round
number of turns" test classifies every panel as a weld. `|k - round(k)| < 0.005`
separates them: `12.566 / 2π = 2.000` passes, `25 / 2π = 3.979` does not.

Radius bounds: below 1.5 mm it is a sheet-metal fillet, not a tube; above 50 mm
it is a hinge eye.

### What the split produces

| run | files | sheet | fastener | nonmetal | weld |
| --- | ----- | ----- | -------- | -------- | ---- |
| per product, mixed split (default) | 176 | 22 | 5 | 6 | 143 |
| per product, `--no-split-mixed` | 33 | 22 | 5 | 4 | 2 |
| per solid, `--split-solids` | 177 | 22 | 5 | 7 | 143 |

**The 143 is the number that matters**: 127 bodies named `welding` inside
`P-C`, 14 inside `P-D`, plus `P-E` and `P-F`. A geometry-only classifier found
3 of them; the name found 143.

Verified end to end on three of them:

| part | class | `Skin` result |
| ---- | ----- | ------------- |
| 185 × 8 × 19 flat tube | `weld_seam` | `hardware` after Skin found nothing (area 2 232 mm²) |
| Ø23 × 55 round tube | `weld_seam` | `skin_failed` (area 6 984 mm², 4 mm wall) |
| 1 039 mm door outer | `sheet` | `done`, 6 127 shells, `edge_p50` 4.935 |

Every weld body fails `Skin` - which is the whole point of pulling them out.

## Assembly transforms

79 `ITEM_DEFINED_TRANSFORMATION`, of which **45 are identity and 34 carry a real
translation**. So the "CATIA already flattened it" assumption is wrong about half
the time, and skipping the transform leaves parts piled at the origin while
looking perfectly valid.

Direction of the transform, verified against part positions:
`p_parent = M(item_2) · M(item_1)⁻¹ · p_child`, with item 1 being the placement
inside the child rep and item 2 the placement inside the parent rep.

One product at several instances (`nauo` up to 12 on this model) is exported
once, from the first instance's transform: the geometry is identical, and
re-meshing a mirrored copy produces the same shell mesh.

## Classification outcome on this model

33 parts with geometry: **5 fastener, 27 sheet, 1 unknown** (an assembly node
with no vertices).

| part family | bbox (vertex) | class | plausible? |
| ----------- | ------------- | ----- | ---------- |
| 5 × small blocky bodies | 15-29 mm | `fastener` | yes - all measured 800-1 700 mm², wall 7-8 mm |
| 1 × 1 × 23 × 55 shim | 1.0 × 23.0 × 54.9 | `sheet` | yes - 6 faces, 6 817 mm² |
| 27 × panels | 40-1 170 mm | `sheet` | yes |

No `weld_spot` and no `weld_seam` came out of this file: an AP203 STEP carries
solid geometry only, and spot welds are an FE concept, not a CAD one. Expect
weld classes from data that actually models them (seam adhesive as a thin
extrusion, weld nuggets as small cylinders) and from named parts.

## What the splitter deliberately does not do

- **No surface area.** A kernel is required; guessing it from face count is
  wrong by 3× on curved panels. Area-based classification stays with
  `process_part.py`, which has ANSA.
- **No solid-level splitting.** A product with 128 solids stays one file. The
  largest part here (`n_solids = 128`, 1 514 faces) is a single door inner.
- **No de-duplication of instances.** Only the first instance is exported; the
  CSV records the count in `装配实例`.

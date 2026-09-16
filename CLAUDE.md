# Schraubenschublade 1077 — project handoff

Everything a fresh session needs to continue this work. Written 2026-09-15.
Drop this file in the repo root (or rename it `CLAUDE.md`) so it is read automatically.

---

## 1. What this project is

A 3D-printed Gridfinity storage system for a **single drawer of screws**, plus the tooling
that produces it:

- **The drawer** is a real cabinet drawer, inner dimensions **1077 × 602 × 75 mm**.
  At the 42 mm Gridfinity pitch that is a **25 × 14 grid = 350 cells**.
- **The contents** are 116 screw positions from an invoice (ISO 10642 countersunk,
  DIN 912 socket cap, DIN 7985 pan head PH, DIN 933 hex), 10–100 pieces each.
- **The output** is one printable bin per position: a Gridfinity box whose top-front
  shelf carries a two-colour label (body on filament 1, label inlay on filament 2),
  sliced in **Bambu Studio** with an AMS.
- The label artwork comes from **Alexandre Chappel's ModuBOX label generator**
  (`https://label.alch.shop/`), scraped once per screw size and cached.

The user is a CS student in Austria. Screw type names in the data are German:
`SK` Senkkopf, `LK` Linsenkopf PH, `ZK` Zylinderkopf, `6kt` Sechskant.

**Tone that works with this user:** direct, no flattery, state disagreement plainly,
show the arithmetic, name what has not been verified. They catch hand-waving.

---

## 2. Current state — the numbers that matter

| | value |
|---|---|
| positions | 116 (metal screws). Chipboard screws, order positions 117–136, are **excluded on request** |
| bin footprints | 87 × 2×1, 24 × 2×2, 5 × 2×3 (width × depth in cells) |
| bin height | **8U = 56 mm, uniform — this is a hard, non-negotiable requirement** |
| cells used | 300 of 350; free 50 (SK 0, LK 2, ZK 8, 6kt 26, plus a 1-column reserve strip = 14) |
| fill target | bulk volume ≤ 80 % of the usable cavity; worst bin 80.9 % (6kt M10×80, already at the largest allowed footprint) |
| drawer blocks | SK 4 cols │ LK 4 │ ZK 8 │ 6kt 8 │ reserve 1 = 25 |
| column depths | SK 14/14, LK 14/13, ZK 13/13/13/13, 6kt 11/11/11/10 |
| script version | `2026-09-13.7-volume-checked-uniform-8U` |

---

## 3. The rules the design settles on

Derived over many iterations; do not silently re-litigate them.

1. **Every bin is exactly 2 cells wide.** Allowed footprints are `2x1`, `2x2`, `2x3` only.
   Uniform width means one bin per drawer column, which is what makes strict size ordering
   possible, and every label shelf is the same wide 84 mm.
2. **Portrait rule.** Anything deeper than one row stands upright so its label sits on the
   short side. `2x1` is the deliberate landscape exception (`LANDSCAPE_EXCEPTION`).
3. **Uniform height.** `UNIFORM_HEIGHT_U = 8`. 7U cannot hold this order at any sane fill
   level without 1-cell-wide bins; mixed heights were explicitly rejected.
4. **Volume rule.** Screw volume comes from real DIN/ISO head tables plus a mean thread
   cross-section (validated against mass tables to ~5 %: M8×20 hex = 12.6 g computed vs
   12.2 g listed). Bulk volume = steel / `PACKING_FRACTION` (0.45). Cavity deducts the
   label ramp. Target `FILL_TARGET = 0.80`, hard stop `FILL_HARD_LIMIT = 0.85`.
   *The old rule capped **solid** volume at 46 %, which at 45 % packing means a bin filled
   to the brim — that bug is why 24 bins were undersized.*
5. **Layout.** Four full-depth type blocks in invoice order. Within a block the size-sorted
   sequence (descending diameter, then length) is **dealt across all columns at once**,
   always into the shallowest column, so diameters form bands across the drawer depth:
   M10/M8 at the back, M3 at the front. Every column is **front-anchored** — leftover cells
   sit at the very back.
6. **Labels are produced by `generate_gridfinity_chappel_bins.py`, never re-invented.**
   A canvas-rasterised text label was tried and rejected by the user. The pipeline path is:
   scrape the Chappel STL → `_extract_chappel_graphics()` → `make_label_inlay()` →
   flush 0.6 mm inlay in a pocket cut by `make_finished_body()`.

---

## 4. The code

```
generator/generate_gridfinity_chappel_bins.py   the pipeline: inventory, sizing, geometry, labels, 3MF
generator/assemble_final_drawer_v9.py           drawer assembly + layout algorithm + preview manifest
api/app.py                                      FastAPI wrapper around the pipeline (docker profile "cad")
web/public/index.html                           the browser editor (single file, no build step)
docker-compose.yml                              web always; api behind `--profile cad`
```

### generate_gridfinity_chappel_bins.py

Hardcoded `SPECS` (116 dicts: pos, kind, thread, diameter, length, qty, norm, material,
artnr). **The spreadsheet the user supplied matches these exactly** — same positions, same
quantities — so no re-import is needed. Key entry points:

| function | purpose |
|---|---|
| `_apply_footprints()` | runs at import, assigns `grid_w/grid_d/height_u/fill_ratio_est` |
| `screw_volume`, `bin_cavity`, `bin_fill` | the physical model described above |
| `fetch_chappel_labels(out, positions, headed, overwrite)` | Playwright scrape into `<out>/chappel_raw_labels/` |
| `make_finished_body(spec, out)` | body with the label pocket cut |
| `make_label_inlay(spec, out)` | the label solid |
| `export_assembled_3mf(body, label, path, name)` | two-part 3MF |
| `validate_specs()` | all invariants (cells == 300, uniform height, fit, fill, block capacity) |
| `filename_stem(spec)` | `P006_SK_M6x80_Q5_2x3_8U` |

CLI: `--build`, `--fetch-labels`, `--positions 1,2,6`, `--out DIR`.

Label cache names are keyed on **screw type + size only**, never on footprint or height —
changing a box size does not invalidate the cache.

### assemble_final_drawer_v9.py

`ZONE_WIDTH`, `pack_zone()` (the dealing packer), `build_layout()`, `occupancy()`,
`free_rectangles()`, plus the drawer shell, screw stand-ins and GLB/STL/CSV export.
`check_bins()` refuses a box set that is not 2 cells wide or not 8U.

### The editor (web/public/index.html)

Self-contained: no framework, no CDN. Contains an embedded copy of the 116 bins
(`BINS`), a Gridfinity mesh builder, a ZIP reader/writer built on `CompressionStream`,
a 3MF writer, and the drag-and-drop UI. Published as a claude.ai artifact as well.

Capabilities: drag/resize bins, per-bin modal, global height slider, auto-arrange
(same dealing algorithm), CSV export, local-storage persistence, STL body export,
3MF export, and — when the build service is up — routing 3MF builds to the real pipeline.

---

## 5. Hard-won details that will bite a fresh session

- **3MF written with ElementTree gets `ns0:` prefixes** on `_rels/.rels`, and BambuStudio's
  parser (`XML_ParserCreate(nullptr)`, raw `strcmp` on tag names) cannot read them: the load
  aborts with "does not contain any geometry data" before the mesh is touched. Write the
  rels and `[Content_Types].xml` as literal strings. `register_namespace('', ...)` can only
  hold the empty prefix for one URI at a time.
- **Two-colour parts** need `Metadata/model_settings.config` with `<part id=… subtype="normal_part">`
  and `<metadata key="extruder" value="2"/>`; `part id` must equal the 3MF object id of the
  component. BambuStudio preserves per-volume `extruder` across its config reset.
- **Multi-body vs booleaned.** Browser-built bodies are overlapping closed volumes (walls,
  floor, feet, shelf, ramp) — slicers union them, mesh checkers call them multi-body.
  Pipeline bodies are one solid. Keep sub-solid vertices separate when indexing, or shared
  edges appear 4× and look non-manifold.
- **CSS class collisions are real bugs.** A `.ghost` rule for the drag preview also hit
  `button.ghost`, made those buttons `position:absolute`, collapsed their row to zero height
  and let a sibling `div` swallow every click. That is what "the download button does
  nothing" turned out to be. Check `document.elementFromPoint` on a control before
  theorising about handlers.
- **OCP/CadQuery is not thread safe** — the service serialises builds behind a lock.
- **The label ramp eats cavity volume**: usable volume is
  `ix*iy*(rampBottom - FLOOR_Z) + ix*(12*iy - 72)`, not `ix*iy*h`.

---

## 6. What is verified, and what is not

**Verified in this environment**

- Sizing and layout reproduce identically in the generator and the assembly script; all 116
  bins placed, no overlaps, every column front-anchored and strictly descending.
- Mesh topology of browser-built bodies and labels: every half-edge paired, dimensions exact
  (2×1 at 8U = 83.5 × 41.5 × 56 mm, base at z = 0).
- 3MF structure: unprefixed rels, BODY + LABEL_INLAY + assembly object, extruders 1 and 2,
  label flush with the bin top and centred on the shelf band.
- Editor behaviour in headless Chromium: drag, modal, export buttons, real download events.
- API contract with a stubbed pipeline: single file, batch zip, auto-scrape of missing
  labels, 404 on unknown position, files persisted to `./out/`.

**Not verified — do this first**

1. **CadQuery has never actually run here.** No cadquery/trimesh/Playwright in this sandbox.
   The first `docker compose --profile cad up --build` on the user's machine is the real
   test. Try P006 (M6×80, the only 2×3) first — widest label, longest screw.
2. **The label scrape** against label.alch.shop has not been exercised; the site's option
   names may have drifted (`CHAPPEL_OPTION_HINTS` carries fallbacks).
3. **Print validation.** No bin from the current 8U set has been printed. Check a 2×1 and
   the 2×3, especially the 45° ramp printing without supports and the label colour split.
4. **Downloads inside the claude.ai artifact still fail for the user** and I could not
   reproduce it — the platform save resolves but nothing arrives. The local package sidesteps
   it entirely. If you pick this up, instrument `claude.use("downloads")` and the returned
   error code rather than guessing.

---

## 7. Open questions / possible next steps

- **Background batch builds.** `/api/generate` is synchronous; 116 boxes through CadQuery
  single-threaded is a long blocking request. Offered a job + progress endpoint writing into
  `./out/`; the user has not answered.
- **6kt M10×80 sits at 81 % fill.** Options: allow a `2x4` for that one position, or store 20
  of the 25 pieces. Needs a decision.
- **SK and LK blocks have 0 and 2 free cells.** Adding a screw size later forces a re-size,
  not just a drop-in.
- **Bulk packing factor 0.45 is an estimate.** Fill a real bin, compare against the model,
  adjust `PACKING_FRACTION`; everything downstream is calibrated off it.
- The drawer assembly script still exports a GLB/STL of the whole drawer; nobody has looked
  at that render since the layout changed to 8U.

---

## 8. File inventory

Delivered over the conversation (latest wins):

| file | what |
|---|---|
| `schublade-planner.tar.gz` | the docker compose package — this is the live project |
| `generate_gridfinity_chappel_bins.py` | pipeline, current version inside the package |
| `assemble_final_drawer_v9.py` | layout + assembly, current version |
| `drawer_layout_v9.png`, `drawer_layout_v9_preview.csv` | the approved layout and its pick list |
| `bin_fit_audit.csv` | per-bin steel/bulk/cavity/fill audit that exposed the sizing bug |
| earlier `v3`–`v8` scripts and previews | the iteration history, superseded |

Artifact (claude.ai, private): the editor, version 7.

## 9. How to run

```bash
tar xzf schublade-planner.tar.gz && cd schublade-planner
docker compose up -d --build                  # editor on http://localhost:8080
docker compose --profile cad up -d --build    # + the CadQuery build service

# or the pipeline directly
docker compose --profile cad run --rm api \
  python /app/generator/generate_gridfinity_chappel_bins.py --build --out /data/cache
```

`./cache` holds scraped label artwork (keep it), `./out` receives built files,
`./generator` is bind-mounted so host edits apply after `docker compose restart api`.

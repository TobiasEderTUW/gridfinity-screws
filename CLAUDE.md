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
| script version | `2026-09-17.1-label-side` |

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
api/app.py                                      FastAPI wrapper around the pipeline (jobs, caches)
web/public/index.html                           the browser editor (single file, no build step)
docker-compose.yml                              web + api, both on the default bridge
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
(`BINS`), a ZIP reader, an STL/3MF WebGL viewer, and the drag-and-drop UI.

Capabilities: drag/resize bins, per-bin modal, global height slider, auto-arrange
(same dealing algorithm), CSV export, local-storage persistence, 3MF export and viewing.

**Every 3MF comes from the build service — no exceptions.** The browser mesh builder,
browser 3MF writer, STL body export and the "Load labels" STL picker were removed
(2026-09-16) because they could only produce label-less boxes. If the service is down the
editor errors out instead of downloading. Labels are fetched on demand by the build job
(`/api/jobs`) for exactly the positions requested (one box → one scrape; "All as 3MF" → all missing).
There is no manual fetch button and no drawer zone lines or header verdict chips.

Since 2026-09-16 the page title is **"Fami Screw Drawer"** and the embedded `BINS` default
layout is the user's hand-arranged layout (308 of 350 cells). It deliberately differs from
the generator's sizing in 7 bins: P005 and P006 are **1×3** (breaking rule 1 at the user's
choice), P049/P109/P110 are 2×2, P112/P114 are 2×3. The generator's `SPECS` and
`validate_specs()` still describe the old 300-cell set. Local storage key is
`schublade1077.v2` (bumped so the new default shows).

Layout persistence is local storage plus **CSV export/import** (same columns both ways;
import validates bounds, overlaps, a single height, and rejects the whole file on any
error). The claude.ai `db` "saved layouts" list was removed.

**Label edge (2026-09-17).** Each box has `label: "bot" | "top"` (editor vocabulary: the
grid is drawn with the drawer front at the bottom). `bot` = shelf on the front edge (the
original design), `top` = on the back edge. Set in the box menu, with **L** on a selection,
or for all boxes in "Generate boxes"; stored in local storage and in the CSV `label` column
(optional on import, default `bot`); a dark stripe on each tile marks the edge. The api maps
it to the generator's `label_side` (`front` / `back`). In the generator, `make_bin_body(...,
side)` mirrors shelf + ramp through XZ; `_chappel_label_geometry` only moves the pocket and
inlay to the back shelf, **it does not rotate the graphic**, so the text still reads from the
drawer front (verified: identical centre-of-mass offset for both sides). Back files are
named `…_8U_LBACK`; front names are unchanged. CLI: `--label-side front|back`.
`assemble_final_drawer_v9.py` parses the suffix and keeps its screw stand-in away from the
label edge.

**Label style (2026-09-17).** Global switch `state.style` = `integrated` | `removable`
(local storage; not in the CSV). Removable, after MakerWorld model 431547 (reference files
the user uploaded are in `./tmp/`, untracked): `make_snap_bin()` = `make_bin_shell()` + a
snap rim (`_snap_ring`: rounded slab minus an hourglass of two 45° tapered extrusions;
OCC's `chamfer` fails on that loop, and a cutter face coincident with the wall face made the
union eat all RAM and reboot the Codespace, hence the 0.3 mm overshoot).
`make_label_plate()` = plate with a V-groove (same ring, grown by the clearance) + flush
inlay; `build_label_plate_3mf()` flips it face down. Verified in CadQuery for a 2×1, front
and back: rim profile 0.6 deep / 45° / 0.1 land, zero plate-bin overlap, plate material
above and below the rim tip. Not printed. Since 2026-09-17 (user request) the snap bin also
has `_support_ramp()` under the label: 45° prism whose flat top sits PLATE_REST_GAP = 0.1 mm
below the plate and reaches the full plate depth (checked: reach falls 1:1 with height, no
material in the gap, no plate clash, both edges, 10/19 mm). The clip is unchanged. So the
bin depends on label edge and depth: `bins/BIN_<w>x<d>_<u>U_D<depth>[_LBACK]_x<count>.3mf`
(deduplicated in `removable_file_plan()` by (w, d, u, side, depth)) +
`labels/LABEL_<type>_<thread>x<len>_W<w>_D<depth>[_LBACK].3mf`; a single box is a zip of both.
Plate depth ("Label width" in the GUI) is `spec['label_depth']`, default 10 mm (was a fixed
14), GUI 6–19 mm, API 6–30 mm but `plate_depth()` also refuses more than half the bin's inner
depth (19.55 mm for a 1-deep bin). Artwork height = depth − 3.75 mm: 6.3 mm at 10, 2.3 mm
at 6. Checked in CadQuery at 6/10/19 mm, both edges, 2×1 / 2×3 / 1×3: no plate-bin overlap.

**MakerWorld style (2026-09-17)**, third `label_style` value `makerworld`: replica of model
431547 measured from its STL. Label = `make_mw_label()`: face plate `MW_PLATE` 1.8, skirt
`MW_SKIRT` 3.16 on both sides + wall side, down to `MW_DEPTH` 5.05, outer profile
(inset, depth) (0,0)→(1.31,1.31)→(1.31,3.10)→(0.61,3.80)→(0.61,5.05), built as a stack of
45° tapered rounded-rect bands (`_mw_offset_stack`), band-cut at the free edge, 0.15 mm
clearance. Bin = `make_mw_bin()`: shell + the matching lip all round (no ramp), so bins
dedupe per size only: `BIN_<w>x<d>_<u>U_MW_x<n>`; labels `LABEL_…_D<depth>_MW[_LBACK]`.
Verified in CadQuery (2×1, both edges): lip reach 0→1.31→1.31→0.61→0 at depths
0/1.31/3.1/3.8/4.41, skirt insets 0.64/1.46/1.10/0.76 at 0.5/2.2/3.45/4.5 mm, no clash.
Not printed. The reference's 0.5 mm sticker rim is intentionally omitted (face-down print).

**No position or quantity anywhere visible** (2026-09-17): tiles, box menu (quantity
stepper removed), tooltips, file names (`filename_stem` = `SK_M6x80_2x3_8U[_LBACK]`), the
generator manifest, the assembly script's CSV and GLB node names, and the editor CSV (boxes
identified by type + thread + length, unique in the inventory). `pos` survives only as an
internal key (API items, `SPECS`, the Chappel label cache names, `--positions`).

**3MF caching, two layers:** the browser keeps built files in memory keyed by
`{height, [pos, w, d]…}` (256 MB cap, oldest evicted), so repeating a download or view is
instant; the api keeps `cache/built_3mf/<stem>.3mf`, reused while it is newer than the
generator script, `app.py` and that position's label STL. The stem encodes footprint and
height, so any size change rebuilds.

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
- **Codespaces drops forwarded traffic on compose networks** (legacy iptables `FORWARD`
  policy DROP, only `docker0` allowed). Symptom: nginx 504 on `/api/health`, the editor
  thinks there is no service, and the api cannot resolve `label.alch.shop`. Fix in place:
  both services use `network_mode: bridge`; nginx proxies to
  `host.docker.internal:${API_PORT}` (`extra_hosts: host-gateway`; `web/nginx.conf` is an
  nginx *template*, rendered at start). Don't move them back onto a project network, and
  don't go back to `network_mode: service:api`: it made web die with every api restart.
- **Codespaces disk is tight** (32 GB loop device shared by the dev container, `/workspaces`
  and Docker; about 3 GB free with the stack up). The api image is 4.4 GB unpacked, and a
  rebuild without build cache fills the disk ("no space left on device", and once it left
  corrupted image layers). So `app.py`, `generator/` and `web/public/` are bind-mounted:
  edits need `docker compose restart api` at most, never a rebuild. Deleting files from the
  base image (e.g. `/opt/conda/pkgs`) frees nothing; they live in a lower overlay layer.
  If a rebuild is unavoidable: `docker compose down`, `docker rmi schublade-api`,
  `docker builder prune -af`, then build, then prune again.
- **Editor boot order matters:** `render()` saves to local storage, so `loadLocal()` must run
  before the first render. Until 2026-09-17 it ran after, and every reload silently reset the
  stored layout to the defaults.
- **Docker disk space leaks into containerd leases.** Failed/killed builds leave leases in
  namespace `moby` that pin GBs of snapshots `docker system df` does not show. Fix used on
  2026-09-17 (freed 11 GB): with no containers or builds running,
  `for id in $(sudo ctr -n moby leases ls -q); do sudo ctr -n moby leases rm --sync $id; done`.
- **The api container is capped at 4 GB** (`mem_limit`), so a runaway OCC boolean kills the
  container instead of rebooting the Codespace.
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

1. **CadQuery and the scrape ran for all 116 positions** (2026-09-16, Codespace, "All as
   3MF" as a background job: 26 min from cold, 116 files, every one with LABEL_INLAY).
   Only P006 was inspected in detail (extruder 2, inlay top at z = 56, centred on the
   shelf); the other label graphics have not been looked at.
2. **The job flow through GitHub's port forwarding** was tested only inside the container
   (headless Chromium), not through the `*.app.github.dev` URL itself.
3. **Print validation.** No bin from the current 8U set has been printed. Check a 2×1 and
   the 2×3, especially the 45° ramp printing without supports and the label colour split.
4. **Downloads inside the claude.ai artifact still fail for the user** and I could not
   reproduce it — the platform save resolves but nothing arrives. The local package sidesteps
   it entirely. If you pick this up, instrument `claude.use("downloads")` and the returned
   error code rather than guessing.

---

## 7. Open questions / possible next steps

- **Background batch builds — done 2026-09-16.** The editor uses `POST /api/jobs` + polling
  (`api/app.py`, one worker thread, identical requests deduplicated, results in
  `cache/jobs/`). Reason: GitHub's port forwarding kills requests after about a minute, so the
  synchronous "All as 3MF" failed with nginx `499` while the scrape continued.
  `/api/generate` remains for scripts only. Jobs live in memory: an api restart loses them
  (the poller then gets a 404 and says to start again), but labels and built boxes stay
  cached on disk.
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
docker compose up -d --build                  # editor on :8080 + CadQuery build service on :8000

# or the pipeline directly
docker compose run --rm api \
  python /app/generator/generate_gridfinity_chappel_bins.py --build --out /data/cache
```

`./cache` holds scraped label artwork (keep it), `./out` receives built files,
`./generator` is bind-mounted so host edits apply after `docker compose restart api`.

# Schraubenschublade 1077 — project handoff

Everything a fresh session needs to continue this work. Written 2026-09-15, last updated
2026-09-30 (offline labels, all part types, fill model removed).
Drop this file in the repo root (or rename it `CLAUDE.md`) so it is read automatically.

---

## 1. What this project is

A 3D-printed Gridfinity storage system for a **single drawer of screws**, plus the tooling
that produces it:

- **The drawer** is a real cabinet drawer, inner dimensions **1077 × 602 × 75 mm**.
  At the 42 mm Gridfinity pitch that is a **25 × 14 grid = 350 cells**.
- **The contents**: since 2026-09-29 any part type of the ModuBOX label generator
  (`https://label.alch.shop/`, ALCH AS / Alexandre Chappel): 53 drive × head screw types and
  14 nuts/washers. The default set is the 116 screw positions of the original invoice
  (ISO 10642 countersunk, DIN 912 socket cap, DIN 7985 pan head PH, DIN 933 hex).
- **The output** is one printable bin per box: a Gridfinity box whose label edge carries a
  two-colour label (body on filament 1, label inlay on filament 2), sliced in
  **Bambu Studio** with an AMS.
- The label artwork is **composed offline** (`generator/label_art.py`) from stored
  icons-only exports of that site plus its font; **nothing scrapes the site at runtime** (§3.6).

The user is a CS student in Austria. The original invoice used German type names:
`SK` Senkkopf = `allen__countersunk`, `LK` Linsenkopf PH = `phillips__pan`,
`ZK` Zylinderkopf = `allen__socket`, `6kt` Sechskant = `hex__hex` (`LEGACY_PART`).

**Tone that works with this user:** direct, no flattery, state disagreement plainly,
show the arithmetic, name what has not been verified. They catch hand-waving.

---

## 2. Current state — the numbers that matter

| | value |
|---|---|
| part types | 67 (53 bolt drive×head combinations + 14 nuts/washers), site version v1.2.1, assets downloaded 2026-09-29 |
| default set | 116 positions (metal screws). Chipboard screws, order positions 117–136, are **excluded on request** |
| generator `SPECS` footprints | 87 × 2×1, 24 × 2×2, 5 × 2×3 = 300 cells (frozen from the old fill model; plain data now) |
| editor default layout | the user's hand layout, 308 of 350 cells (differs in 7 bins, see §4) |
| bin height | **8U = 56 mm, uniform — this is a hard, non-negotiable requirement** |
| fill estimation | **removed on request (2026-09-29)** — no volume model anywhere; footprints are the user's choice |
| script version | `2026-09-29.1-offline-labels` |

---

## 3. The rules the design settles on

Derived over many iterations; do not silently re-litigate them.

1. **Every bin is exactly 2 cells wide.** Allowed footprints are `2x1`, `2x2`, `2x3` only.
   Uniform width means one bin per drawer column, which is what makes strict size ordering
   possible, and every label shelf is the same wide 84 mm.
2. **Portrait rule.** Anything deeper than one row stands upright so its label sits on the
   short side. `2x1` is the deliberate landscape exception (`LANDSCAPE_EXCEPTION`).
   (Rules 1 and 2 describe the original sizing; the user's own layout breaks rule 1 for
   P005/P006, and the editor allows any footprint 1–4 × 1–4.)
3. **Uniform height.** `UNIFORM_HEIGHT_U = 8`. 7U cannot hold this order at any sane fill
   level without 1-cell-wide bins; mixed heights were explicitly rejected.
4. **No fill or volume model** (removed 2026-09-29 at the user's request: "remove all fill
   estimation!"). The old DIN head tables, `screw_volume`, `bin_cavity`, `bin_fill`,
   `_apply_footprints` and the 80 % target are gone; `SPECS` footprints are frozen data.
   The editor's screw-length "does not fit" check was removed too (2026-09-19). Do not add
   either back without being asked.
5. **Layout.** One full-depth block per head family (countersunk, pan, socket, hex, flange
   pan, shoulder, flange hex, nuts, washers — the first four are the old SK/LK/ZK/6kt order).
   Within a block the size-sorted sequence (descending thread diameter, then length) is
   **dealt across all columns at once**, always into the shallowest column, so diameters
   form bands across the drawer depth. Column width = the block's widest box. Every column is
   **front-anchored** — leftover cells sit at the very back.
6. **Labels: offline, from stored exports, never from the page source.** Decided with the user
   2026-09-29 ("all web scraping should be removed for the production version, so everything
   needed should be downloaded and included in the repo"; text: "local text, same font").
   - The site's LICENSE (ALCH AS, proprietary, github.com/ALCH-Modubox/Label) allows using
     the label files it exports and **forbids extracting its source or iconography**. The
     page embeds the icons as vector data (`ICON_DATA`); **do not copy that**. The icons come
     from 67 icons-only Raised exports (`generator/assets/label_icons/`), sectioned like the
     old pipeline sectioned whole labels.
   - The text is set by us in HarmonyOS Sans SC Regular (the site's font, per its NOTICE),
     Huawei's unmodified TTF in `generator/assets/fonts/` with its LICENSE. That license
     allows bundling unmodified copies with software, requires a visible notice (the editor
     sidebar has it) and **forbids modification — no subsetting**.
   - Layout reproduces the site's exports (measured, not copied): icons as exported, text
     ink 2.0 mm after the icons' layout box, cap height 5.7 mm, group centred on the 52.9 mm
     plate, text shrinks only if the group gets within 1.2 mm of the edge; GPOS kerning.
     Verified against the 116 scraped labels of the old pipeline: symmetric difference
     0.00006 % of the area (negative controls: wrong digit 15.8 %, wrong drive icon 3.4 %).
   - A canvas-rasterised text label was tried long ago and rejected; this is vector text in
     the site's font, which the user chose explicitly.
   - Path: `label_art.compose(part, text)` → `_label_geometry()` → `make_label_inlay()` /
     label plates → flush 0.6 mm inlay in a pocket cut by `make_finished_body()`.

---

## 4. The code

```
generator/generate_gridfinity_chappel_bins.py   the pipeline: default set, geometry, labels, 3MF
generator/label_art.py                          label composition (icons + text), editor catalogue
generator/assets/                               icon exports, font + license, label_catalogue.json
tools/download_label_assets.py                  one-off Playwright downloader for generator/assets
web/public/parts.json                           editor catalogue, derived (python generator/label_art.py)
generator/assemble_final_drawer_v9.py           drawer assembly + layout algorithm + preview manifest
api/app.py                                      FastAPI wrapper around the pipeline (jobs, caches)
web/public/index.html                           the browser editor (single file, no build step)
docker-compose.yml                              web + api, both on the default bridge
```

### generate_gridfinity_chappel_bins.py

A spec (one box) is `{pos, part, thread, length (None for nuts/washers), text (optional
override), grid_w, grid_d, height_u, norm}` plus label options (`label_style`, `label_side`,
`label_depth`, `label_fit`, `label_detent`). `SPECS` = the 116 default positions (qty,
material, artnr and fill data were dropped with the fill model). Key entry points:

| function | purpose |
|---|---|
| `label_text(spec)` | the spec's text, else `M6×20` (bolt) / `M6` (nut, washer) |
| `_label_geometry(spec, base_z, thickness, area)` | composed artwork fitted into the label area |
| `make_finished_body(spec)` / `make_label_inlay(spec)` | body with pocket / the inlay (no `out` arg any more) |
| `make_label_plate(spec)`, `make_mw_label(spec)` | removable / MakerWorld labels |
| `export_assembled_3mf(body, label, path, name)` | two-part 3MF |
| `validate_spec(spec)` | known part, drawable text, footprint inside the drawer (422 in the api) |
| `validate_specs()` | the default set: unique pos, each spec valid, uniform 8U |
| `screw_name(spec)` / `filename_stem(spec)` | `allen-countersunk_M6x80` / `…_2x3_8U[_LBACK]` |

`label_art.py`: `catalogue()`, `parts()`, `part_name(key)`, `icon_polygons(key)`,
`text_polygons(text)`, `compose(key, text)`, `web_catalogue()`; `python label_art.py [path]`
writes `web/public/parts.json`.

CLI: `-o DIR`, `--positions 1,2,6`, `--style`, `--label-side`, `--label-depth`,
`--label-fit`, `--detents` (`--build` is accepted and ignored; `--fetch-labels` is gone).

### assemble_final_drawer_v9.py

`ZONE_WIDTH`, `pack_zone()` (the dealing packer), `build_layout()`, `occupancy()`,
`free_rectangles()`, plus the drawer shell, screw stand-ins and GLB/STL/CSV export.
`check_bins()` refuses a box set that is not 2 cells wide or not 8U. It only knows the four
original kinds: `PART_KIND` maps `allen-countersunk`/`allen-socket`/`phillips-pan`/`hex-hex`
file names to SK/ZK/LK/6kt and refuses any other part type (zones and screw stand-ins are
per kind). Nobody has run it since the 8U change. **The user said to ignore this script**
(2026-09-30): don't spend time on it.

### The editor (web/public/index.html)

Self-contained: no framework, no CDN (except Google Fonts for the UI font). Contains the
default layout (`BINS`, 116 boxes as `{pos, part, thread, len, w, d, x, y}`), a ZIP reader,
an STL/3MF WebGL viewer, and the drag-and-drop UI. Loads `parts.json` (icons as SVG paths,
names, drive→head rules, layout constants) and the label font from `fonts/` (nginx serves
`generator/assets/fonts` there: a compose bind mount, and a COPY in the web image, whose
build context is now the repo root with a whitelist `.dockerignore`).

Capabilities: drag/resize bins, per-box menu (part type via icon pickers, thread, length,
label text override, footprint, label edge, live label preview, Duplicate, Delete), **Add
box**, Delete key, global height slider, auto-arrange, CSV export/import, local-storage
persistence, 3MF export and viewing.

**Tiles are a top view** ("realistic + type tint", user's choice 2026-09-29): body tinted by
head family (`FAMILY`, CSS vars `--sk --lk --zk --kt --fp --sh --fh --nut --wsh`, wood heads
×0.88, darker for bigger threads), floor, the label strip at its real place and size for the
current style/depth, and the artwork drawn like the generator: `labelArt()` mirrors
`label_art.compose` (canvas `measureText` for the ink box, SVG `<text>` in the same font),
`labelStrip()` mirrors the integrated shelf / `_plate_area` / `_mw_label_area`, scale
capped at 1.35. A caption repeats the label text in the open floor (the printed text is a
few pixels at normal zoom). Tiles redraw only when their key changes.

**Every 3MF comes from the build service — no exceptions.** The browser mesh builder,
browser 3MF writer, STL body export and the "Load labels" STL picker were removed
(2026-09-16) because they could only produce label-less boxes. If the service is down the
editor errors out instead of downloading. There are no drawer zone lines or header verdict
chips.

Since 2026-09-16 the page title is **"Fami Screw Drawer"** and the embedded `BINS` default
layout is the user's hand-arranged layout (308 of 350 cells). It deliberately differs from
the generator's sizing in 7 bins: P005 and P006 are **1×3** (breaking rule 1 at the user's
choice), P049/P109/P110 are 2×2, P112/P114 are 2×3. The generator's `SPECS` still
describe the old 300-cell set. Local storage key is `schublade1077.v3` (full boxes); a
`schublade1077.v2` layout (positions onto the fixed 116) is read once when v3 is missing.

Layout persistence is local storage plus **CSV export/import** (same columns both ways:
`part, thread, length_mm, text, box_w, box_d, height_u, grid_x, row_from_front, label`;
RFC 4180 quoting for the text). The file is the whole drawer: import **replaces** every box.
Old files with a `type` column (SK/LK/ZK/6kt) still import via `CAT.legacy`. Import validates
parts, bounds, overlaps and a single height, and rejects the whole file on any error. The
claude.ai `db` "saved layouts" list was removed.

**Label edge (2026-09-17).** Each box has `label: "bot" | "top"` (editor vocabulary: the
grid is drawn with the drawer front at the bottom). `bot` = shelf on the front edge (the
original design), `top` = on the back edge. Set in the box menu, with **L** on a selection,
or for all boxes in "Generate boxes"; stored in local storage and in the CSV `label` column
(optional on import, default `bot`); the drawn label strip on each tile marks the edge. The api maps
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
45° tapered rounded-rect bands (`_mw_offset_stack`), band-cut at the free edge. Clearance
between clip and lip = `spec['label_fit']` ("Label fit" slider, only shown for this style),
default 0.05 mm since 2026-09-18 (0.15 let the printed label slide), range −0.30…0.30,
negative = press fit; label files carry it (`…_MW_FIT0.05`), bins do not depend on it.
Checked in CadQuery at +0.05/+0.30/−0.10: recess gap to the lip equals the fit exactly. Bin = `make_mw_bin()`: shell + the matching lip all round (no ramp), so bins
dedupe per size only: `BIN_<w>x<d>_<u>U_MW_x<n>`; labels `LABEL_…_D<depth>_MW_FIT<fit>[_LBACK]`.
Verified in CadQuery (2×1, both edges): lip reach 0→1.31→1.31→0.61→0 at depths
0/1.31/3.1/3.8/4.41, skirt insets 0.64/1.46/1.10/0.76 at 0.5/2.2/3.45/4.5 mm, no clash.
Not printed. The reference's 0.5 mm sticker rim is intentionally omitted (face-down print).
**Detents** (`spec['label_detent']`, checkbox shown with this style, `--detents`), third
version 2026-09-18, confirmed with the user by sketch: at 8 places (2 per wall, piece centre
`DETENT_OFFSET` 5 mm from each corner) the lip turns down: a vertical piece with exactly the
lip's cross-section stood upright (`_mw_lip_section`, 1.31 proud, 4.41 wide) from the lip to
`DETENT_DROP` 4 mm below it, bottom end chamfered 45° (`_detent_pieces`). The label's skirt
gets the matching slot (section +0.1) below the lip's straight face; face plate untouched.
Pieces do not depend on the label and run round all four walls **on purpose**: a printed bin
can later take a new label on any wall. The user does *not* want left/right labels generated
(added and removed again 2026-09-18); only front/back labels exist. Detent bins dedupe per
size: `BIN_…_MW_DET_x<n>`; labels get `_DET`. Earlier versions (sphere bumps at the label
middle, then small 0.4 mm ribs on the lip face) were rejected.
Traps: union disjoint detail solids into the bin one at a time; default-oriented spheres broke
OCC on the −x lip; and **OCC `Volume()` is unreliable on these bins** (≈2000 mm³ off the
exported mesh for the plain 2×1), so judge booleans by local volume inside a box around the
detail, or by the exported mesh. Verified 2×1/2×3/1×3: reach 1.31 from the lip to ~7 mm,
tapering to 0 at 8.41; each piece adds ~30 mm³ locally; mesh watertight, one body; front and
back labels valid with slots, no clash at fit 0.05.

**Shared default drawer (2026-09-30).** The app runs on a lab-internal Docker host, same
compose setup, **no login or users on purpose** (user: "semi public, lab internal use…
no login or users required! Just the shared backend"; access is restricted by the network).
`PUT /api/default` stores the editor snapshot (validated: known parts, no overlaps, inside
the drawer, ≥ 1 box) in `STATE_DIR` = `./state/default_layout.json` (atomic replace),
copying the previous one to `state/default_history/default_<time>-<ns>.json`; `GET` returns
`{saved_at, layout}` or 404. Editor: **Set as default** (confirm dialog), **Load default**
(was Reset), note `#defaultNote`. Boot: a browser with a local layout loads it; one without
restores the shared default; no default / service down → built-in `BINS`.

**Drag without text selection (2026-09-30).** `onPointerDown` calls `preventDefault()` (the
press would otherwise start a text selection that grows with the drag), focuses the grid by
hand (arrow keys), clears any selection and sets `body.drag-active` (`user-select:none`)
until `pointerup`/`pointercancel`; `.drawer-shell` and `.tray` are always unselectable.
`onPointerCancel` drops a drag without moving anything.

**No position or quantity anywhere visible** (2026-09-17): tiles, box menu, tooltips, file
names (`filename_stem` = `allen-countersunk_M6x80_2x3_8U[_LBACK]`: part slug + label-text
slug; the editor's `screwName()` must produce the same), the generator manifest, the
assembly script's CSV and GLB node names, and the editor CSV. `pos` survives only as an
internal id (editor boxes, `SPECS`, `--positions`); API items carry no id at all.

**API items (2026-09-29):** `{part, thread, length?, text?, w, d, label}`; `_spec_for()`
builds a spec and `validate_spec()` turns an unknown part or a glyph the font lacks into a
422. Integrated boxes and label plates with the same stem are built once per request.
`/api/labels`, `/api/labels/fetch`, `fetch_missing` and the job's `labels` phase are gone.

**3MF caching, two layers:** the browser keeps built files in memory keyed by
`{height, style, …, items}` (256 MB cap, oldest evicted), so repeating a download or view
is instant; the api keeps `cache/built_3mf/<stem>.3mf`, reused while it is newer than the
generator script, `app.py` and, for anything with a label, `label_art.py`, the catalogue, the
font and that part's icon export. The stem encodes part, text, footprint and height, so any
of those changing rebuilds.

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
  thinks there is no service. Fix in place:
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
- **Tapered rounded-rect segments must not run their corner radius through zero.** The MW
  label cavity (inset 3.2 > inner radius 2.55) had a 45° foot built from its bottom outline;
  shrinking upward, the corner radius hit 0 part-way up. OCC called the solid valid, Bambu
  Studio reported "2 non-manifold edges" on every label (2026-09-19). `_mw_offset_stack` now
  clamps the radius at a segment's small end and grows it towards the big end. Check meshes,
  not just `isValid()`: count edges not shared by exactly two triangles.
- **The api container is capped at 4 GB** (`mem_limit`), so a runaway OCC boolean kills the
  container instead of rebooting the Codespace.
- **OCP/CadQuery is not thread safe** — the service serialises builds behind a lock.
- **Overlapping strokes in label exports.** The wood-screw icons (threads) are separate,
  overlapping solids. Nesting all section loops together by even/odd turned overlaps into
  holes (invalid polygons), and extruding overlapping polygons made one 2×1 pocket cut run
  >5 min at 2.2 GB. `label_art.export_polygons` sections each connected solid on its own and
  `_disjoint()` unions everything before it reaches OCC. Now 67/67 types build in 2–37 s
  (lock washers slowest), zero bad mesh edges.
- **Icon layout box ≠ icon ink.** The site centres the icons' *layout* box; the External-hex
  drive ink is narrower than its slot (0.27 mm shift) and the Phillips ink overshoots by
  0.02 mm. For bolts the box's right edge is the head ink (flush), symmetric about 0 in the
  export; nuts are one centred icon (`label_art._icon_box`, `compose`).
- **A nested bind mount needs its mount point in the outer (read-only) mount**:
  `web/public/fonts/` exists (with a README) only so Docker can mount the fonts there.
- **nginx:1.27-alpine has no `font/ttf` MIME type**; `web/nginx.conf` declares it for
  `/fonts/` and caches that path for a week (the editor itself stays `no-store`).
- **No browser in the api image any more**, so headless UI tests need another Chromium
  (`tools/uitest.Dockerfile`, a throwaway image on the default bridge, page at
  `http://172.17.0.1:8080/`; delete the 2 GB image afterwards).

---

## 6. What is verified, and what is not

**Verified in this environment**

- Offline labels (2026-09-29): composed artwork equals the site's own exports for all 116
  old labels (sym. diff 0.00006 %, Hausdorff < 0.001 mm; negative controls 15.8 % / 3.4 %).
  All 67 part types build as integrated 2×1 bins: BODY + LABEL_INLAY, extruders 1/2, zero
  edges not shared by exactly two triangles. A nut label builds in all three styles.
- Shared default (2026-09-30), two browser contexts: A saves (confirm, cancel works), a fresh
  B starts from it, B's own edits survive a reload, Load default fetches A's newer default,
  aborted `/api/default` falls back to the built-in drawer with a warning. API rejects unknown
  parts, overlaps, out-of-drawer boxes and empty drawers with 422. Set-as-default round trip
  0.11 s. Drag test with real mouse input: no selection even over the sidebar, drop lands,
  click still opens the menu, sidebar text still selectable.
- **Tiles are to scale** (2026-09-30): the tile SVG shows exactly the millimetres the tile
  covers (`topSVG`, viewBox inset by 2 px of border/gap), so both axes share cell/42 px per mm.
  Measured by rendered ink at 240 px/cell against `_label_geometry` bounds for 6 cases (all
  three styles, both edges, 1×3/2×1/2×2/2×3, a nut, a wood screw with its own text, a lock
  washer at 6 mm depth): every edge within 0.23 mm (1 px = 0.175 mm), sizes within 0.2 mm.
- Editor in headless Chromium (before the browser left the api image): 116 tiles with drawn
  labels, pickers obey the drive→head rules, Add/Duplicate/Delete, CSV round trip, reload
  keeps the layout, a custom `pozidriv__countersunk_wood` "4.5×40 Spax" box downloads as a
  3MF through the job flow, a MakerWorld batch dedupes two identical washer boxes.
- Mesh topology of browser-built bodies and labels: every half-edge paired, dimensions exact
  (2×1 at 8U = 83.5 × 41.5 × 56 mm, base at z = 0).
- 3MF structure: unprefixed rels, BODY + LABEL_INLAY + assembly object, extruders 1 and 2,
  label flush with the bin top and centred on the shelf band.
- Editor behaviour in headless Chromium: drag, modal, export buttons, real download events.
- API contract: single file, batch zip, 422 on an unknown part or a missing glyph, files
  persisted to `./out/`.

**Not verified — do this first**

1. **Label graphics were compared numerically, not looked at in Bambu Studio** for the new
   part types; the lock-washer and heat-set icons are the most detailed, check one printed.
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
  synchronous "All as 3MF" failed with nginx `499` while the build continued.
  `/api/generate` remains for scripts only. Jobs live in memory: an api restart loses them
  (the poller then gets a 404 and says to start again), but built boxes stay cached on disk.
- **Thinner base (analysed 2026-09-19, no decision yet).** The base is solid to z = 7.2.
  Hollow feet ("Lite"/efficient floor, 1.2 mm bottom) measured −14 % filament on a 2×1
  (31.0 → 26.8 g), −21 % on a 2×3, ≈ −0.65 kg for the drawer, at the cost of a dipped floor;
  a thinner flat floor saves only ~2 %. Walls are ⅔ of the filament. Not implemented.
- **New site part types** need `tools/download_label_assets.py` + `python generator/label_art.py`.
  The old scraped labels (`cache/chappel_raw_labels/`) were deleted on request 2026-09-30,
  after the verification in §6; so were built files under the old `SK_…` names.
- **The icon set itself:** the user said (2026-09-30) this is a personal, never-public
  project and the page's icon data may be used if that makes things easier. It did not:
  the exports already reproduce the site exactly, so nothing was switched.
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
| `bin_fit_audit.csv` | per-bin steel/bulk/cavity/fill audit that exposed the sizing bug (fill model since removed) |
| `tools/uitest.Dockerfile` | throwaway headless-Chromium image for editor tests |
| earlier `v3`–`v8` scripts and previews | the iteration history, superseded |

Artifact (claude.ai, private): the editor, version 7.

## 9. How to run

```bash
tar xzf schublade-planner.tar.gz && cd schublade-planner
docker compose up -d --build                  # editor on :8080 + CadQuery build service on :8000

# or the pipeline directly
docker compose run --rm api \
  python /app/generator/generate_gridfinity_chappel_bins.py -o /data/out/cli
```

`./cache` holds built 3MFs and job results, `./out` receives built files, `./generator`
is bind-mounted so host edits apply after `docker compose restart api`. Neither service
needs internet access.

# Fami Screw Drawer — local planner

Drawer layout editor for a Gridfinity screw drawer, plus the CadQuery pipeline that builds
the real boxes. Every part type of the ModuBOX label generator (label.alch.shop) can be put in
a box: 53 screw/bolt types (drive × head) and 14 nuts and washers. The default drawer is the
116 metal-screw positions of the original order. Runs entirely on your machine and fully
offline; downloads are plain browser downloads, so nothing sits between you and the file.

```
docker compose up -d --build          # editor on http://localhost:8080
                                      # + CadQuery build service on :8000
```

Both services start by default. The editor arranges bins; every 3MF it hands out — from
the box menu, **Selected 3MF** or **All as 3MF** — is built by the build service with
`generate_gridfinity_chappel_bins.py`: a bin (`make_mw_bin()`, one part) and a clip-on
label (`make_mw_label()`: `LABEL_PLATE` on filament 1, the flush 0.6 mm `LABEL_INLAY` on
filament 2, so Bambu Studio opens it as a two-colour object). A box is always these two
files, so every download is a zip.

**No download is ever label-less.** There is no browser-built fallback and no STL body
export. If the build service is unreachable, the editor reports an error and hands out
nothing. The same goes for **View selected**.

## Shared default drawer

The lab shares one default drawer, stored by the build service in `./state/`:

- **Set as default** (sidebar, *Layout*) stores the current drawer, boxes, positions, height
  and label settings, as the default for everyone, after a confirmation. An empty drawer is
  refused. Each replaced default is kept in `./state/default_history/`; to go back, copy one
  over `./state/default_layout.json`.
- A browser that has no layout of its own starts from the shared default. Browsers with a
  layout keep it (local storage) until they click **Load default**.
- Without a saved default, or with the build service down, the built-in 116-box drawer
  stands in; the note under the buttons says which one you got.

There is no login: the installation is meant for a network whose access is already
restricted (lab internal). Anyone who can open the page can set the default and queue
builds.

## Boxes and part types

Click a box to select it; click the selected box again (or double-click, or press Enter)
to open its menu (dragging a box of a multi-selection moves the whole selection; it only
lands if every box fits): part type (drive and head icons, or nut/washer), thread, length,
label text (defaults to `M6×20` for screws, `M6` for nuts and washers; type anything to
override, e.g. `M6×20 A2`), footprint and label edge. **Add box** (sidebar), **Duplicate** and
**Delete** (box menu, or the Delete key on a selection) change the set. The drive/head
combinations are the website's: External hex takes only the hex heads, Flange bolt only the
flange-hex heads, every other drive the other seven heads.

Each tile is a top view of the printed box: body tinted by head type (darker = larger
thread), the floor, and the label strip on its edge with the artwork drawn to scale, in the
same font and layout the generator prints. The caption repeats the label text, because at
normal zoom the printed lettering is only a few pixels high. **Auto-arrange** makes one block
of columns per head type (countersunk, pan, socket, hex, the other heads, nuts, washers),
biggest thread at the back.

There is no fill or volume estimate: you choose the footprint.

## Bins and labels

One design since 2026-09-30 (the integrated shelf and the removable snap plate were
removed on request):

- **Bin:** plain walls with a lip round the top inside, the counterpart of the label's clip
  profile (45° faces only). It does not depend on the label, so every box of one size shares
  one file: `bins/BIN_2x1_8U_MW_x83.3mf` (the count in the name).
- **Label:** replica of MakerWorld model 431547: a 1.8 mm face plate with a 3.16 mm skirt along
  both sides and the wall-side edge, reaching 5 mm down; the skirt's recess clips round the
  bin lip and a 0.6 mm bump hooks under it. Label inlay flush in the face; exported
  face-down. `labels/LABEL_allen-countersunk_M6x80_W2_D10_MW_FIT0.05.3mf` per distinct label.
- **Label width** (slider, 6–19 mm, default 10) is the label's depth from the wall.
  **All labels of one box width are exactly the same size**, and text keeps its
  proportions: the size is the largest at which every label of that width in the drawer fits
  (icons at most depth − fit − 3 mm, 6.95 mm at 10, capped at 10.96 mm; text 0.7 × that).
  The note under the slider shows the result per width; in the default drawer the two 1×3
  boxes (M6×40, M6×80) make the 1-wide labels 6.35 mm. Adding a box with a long text can
  make all labels of its width smaller. Label files carry the size (`_H6.95`).
- **Layout:** the icons sit at the left edge of the label, in the same place on every label of
  a box width; the text is centred in the space right of them.
- **Bambu Studio 02.08.02** says "The 3mf file has invalid config, load geometry data only"
  for these files. That is a bug in that version (fixed in 02.08.03): the files carry no
  printer settings on purpose. Geometry and the filament of each part load correctly.
- **Label fit** (default 0.05 mm all round the clip profile, −0.30 to 0.30, negative is a
  press fit): only the label changes, so a looser or tighter label fits bins you already
  printed. Checked in CAD only.
- **Detents** ticked: the bin's lip turns down at two places per wall (near each corner): a
  short vertical piece with the lip's own cross-section runs from the lip 4 mm down the wall
  (bottom end 45°, prints without support), and the label's skirt has matching slots, so it
  cannot slide (`BIN_2x1_8U_MW_DET`). Lip and pieces run round all four walls, so a printed
  bin can later take a new label on any wall; the tools generate front (bot) and back (top)
  labels.

CLI: `--label-depth 10`, `--label-fit 0.05`, `--detents`, `--label-side front|back`. Print
one bin and one label before the batch.

The MakerWorld profile was measured from the model's STL (in `./tmp/`, untracked, licence
CC BY-NC-SA). Its 0.5 mm raised sticker rim is left out: the label prints face down for the
two-colour inlay, and the rim would leave the whole face as an unsupported bridge.

Files carry no position number and no quantity: boxes are named by part type and label
text (`allen-countersunk_M6x80`, `nyloc-nut_M6`). Boxes with the same label share one
label file.

## Labels

A label is the part type's icon(s) followed by a line of text, laid out like the ModuBOX
label generator lays it out, then fitted into the box's label area and extruded as a flush
two-colour inlay. It is composed **offline** by `generator/label_art.py` from
`generator/assets/`:

- `label_icons/*.stl` — one icons-only *Raised* export per part type (67), downloaded once
  from label.alch.shop with `tools/download_label_assets.py`. The site's license allows
  using the label files it exports and forbids extracting its source or icon set, so the
  icons are recovered by sectioning those exports, never copied out of the page.
- `fonts/HarmonyOS_Sans_SC_Regular.ttf` — the font the site uses, Huawei's unmodified file,
  with its license next to it (redistribution with software is allowed if unmodified and
  credited; the editor credits it). The text is set with the font's own kerning.
- `label_catalogue.json` — part keys, names, groups and the drive → head rules.

Checked against 116 labels the site produced for the original order: the composed
artwork matches every one (symmetric difference 0.00006 % of the area; one wrong digit is
15 %). `web/public/parts.json` is derived from the same exports for the editor
(`python generator/label_art.py`).

Neither the service nor the generator contacts the website. To pick up new part types,
run the downloader once in any Python with Playwright
(`pip install playwright && python -m playwright install --with-deps chromium`, then
`python tools/download_label_assets.py`), then regenerate `parts.json`. It was last run
2026-09-29 against site version v1.2.1, inside the api image before the browser was removed
from it.

## First run

1. `docker compose up -d --build` — the api image pulls CadQuery and trimesh. Expect 5–15
   minutes the first time; after that it is layer-cached and starting up is immediate.
2. Use **3MF** in a box's menu, **Selected 3MF** or **All as 3MF**. No network access is
   needed.

Every server-built file is also written to `./out/` on the host, so a batch is on disk even
if you close the tab.

## Layout

```
docker-compose.yml     web + api, both in the default set
web/                   nginx + the editor; /api/ is proxied to the api's published port
                       (host.docker.internal:${API_PORT})
generator/             pipeline, bind-mounted read-only into the service
  generate_gridfinity_chappel_bins.py    default set, box + label geometry, 3MF
  label_art.py                           label composition (icons + text), parts.json
  assets/                                icon exports, font + license, catalogue
  assemble_final_drawer_v9.py            drawer assembly (knows the four original kinds)
tools/download_label_assets.py           one-off: refresh generator/assets from the website
cache/                 built 3MF files and job results
out/                   server-built 3MF files
state/                 the shared default drawer and its backups — back this up
```

`generator/`, `api/app.py`, `web/public/` and the label font (as `web/public/fonts/`) are
bind-mounted. Editor changes are live on
reload; for the other two run `docker compose restart api`. No image rebuild is needed for
any of them, which matters on a small Codespaces disk.

## The original CLI still works

```
docker compose run --rm api \
  python /app/generator/generate_gridfinity_chappel_bins.py -o /data/out/cli
```

It builds the default set (`--positions 1,2,6` for a subset) with the same
`--label-side`, `--label-depth`, `--label-fit` and `--detents` options as the editor.
```

## Service endpoints

| method | path | purpose |
|---|---|---|
| GET | `/api/health` | is CadQuery loadable, how many part types are known |
| POST | `/api/jobs` | same body as `/api/generate` (items may add `"text"` and `"label": "top"`, the request `"label_depth"`, `"label_fit"`, `"label_detent"` and `"drawer"`: every box of the drawer as `{part, thread, length, text, w}`, which sets the label size per box width; an old `"style"` field is ignored); queues a background build, returns `{id, phase, done, total, current}`. An identical running or finished request is reattached, not duplicated |
| GET | `/api/jobs/{id}` | status: `queued` → `build` (done/total = files) → `done` or `error` |
| GET | `/api/jobs/{id}/file` | the 3MF (one box) or zip, once `done`; the last 8 finished jobs are kept in `cache/jobs/` |
| GET | `/api/default` | the shared default drawer `{saved_at, layout}`, 404 if none |
| PUT | `/api/default` | store a drawer (editor snapshot, `v: 3`) as the default; 422 on unknown parts, overlaps, boxes outside the drawer or an empty drawer |
| POST | `/api/generate` | `{"items": [{"part": "allen__countersunk", "thread": "M6", "length": 80, "w": 2, "d": 3}], "height_u": 8}` → 3MF or zip, synchronously (for scripts). An unknown part or a character the font lacks is a 422 |

## Things worth knowing

- **OCP is not thread safe**, so one worker thread runs jobs in order. Labels are cut as
  meshes (manifold3d, 0.1–0.5 s each) on a tray built once per size and setting; bins take
  1–2 s and are built once per size, so a whole drawer takes about a minute from cold. The editor runs every build as a job and
  polls it once a second, because a single long request gets cut off by proxies: GitHub
  Codespaces port forwarding dropped the old synchronous "All as 3MF" after about a minute
  (nginx logged `499`) while the build carried on server-side. If the page is closed, the
  job still finishes; clicking the same button again reattaches to it.
- **Networking.** Both containers run on Docker's default bridge (`network_mode: bridge`),
  and nginx reaches the api through the host's published port (`host.docker.internal`,
  mapped to the docker0 gateway). On a compose project network, hosts that only forward
  `docker0` traffic (GitHub Codespaces: legacy iptables `FORWARD DROP`) gave nginx a 504 on
  `/api/`. An earlier fix had web
  share api's network namespace; that took the editor down whenever api restarted.
- **Built boxes are cached.** The page keeps every file it built in memory, so downloading
  or viewing the same boxes again is instant. The service also keeps them in
  `./cache/built_3mf/` and reuses them until the generator, `label_art.py` or a label asset
  changes.
  Changing a box's size or the height is always a fresh build.
- **Label edge.** Every box has its label on the front edge (**Bot**, the drawer-front
  side, default) or the back edge (**Top**, as the drawer is drawn). Choose per box in its
  menu, with **L** on a selection, or for all boxes under *Generate boxes*. The label strip on
  each tile shows the edge. A top label is the same label mirrored to the back; the
  artwork is moved, not rotated, so it still reads from the drawer front. Those label files
  end in `_LBACK`; the bin is the same for both edges. From the CLI: `--label-side front|back`.
- **Layout file.** **Export CSV** / **Import CSV…** use the same columns
  (`part, thread, length_mm, text, box_w, box_d, height_u, grid_x, row_from_front, label`).
  The file is the whole drawer: import replaces every box. `text` and `label` are optional
  (default: generated text, `bot`), `length_mm` is empty for nuts and washers. Files from
  before 2026-09-29 (a `type` column with SK/LK/ZK/6kt) still import. An import with an
  unknown part, a box out of bounds, overlaps or mixed heights is rejected as a whole.
- **Dragging never selects text**: the press's default action is cancelled, the drawer and
  tray are `user-select: none`, and the whole page is while a drag runs.
- The editor keeps your working layout in the browser's local storage (`schublade1077.v3`; a
  v2 layout from before is carried over once).
- Ports come from `.env` (`WEB_PORT`, `API_PORT`); copy `.env.example` to start.

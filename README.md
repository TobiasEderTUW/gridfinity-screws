# Fami Screw Drawer — local planner

Drawer layout editor for the 116-position Gridfinity screw drawer, plus the CadQuery
pipeline that builds the real boxes. Runs entirely on your machine; downloads are plain
browser downloads, so nothing sits between you and the file.

```
docker compose up -d --build          # editor on http://localhost:8080
                                      # + CadQuery build service on :8000
```

Both services start by default. The editor arranges bins; every 3MF it hands out — from
the box menu, **Selected 3MF** or **All as 3MF** — is built by the build service with
`generate_gridfinity_chappel_bins.py`: `make_finished_body()` (one booleaned solid with the
label pocket cut) plus `make_label_inlay()` (the real Chappel artwork, flush 0.6 mm), as two
parts (`BODY` on filament 1, `LABEL_INLAY` on filament 2) so Bambu Studio opens it as a
two-colour object.

**No download is ever label-less.** There is no browser-built fallback and no STL body
export. If the build service is unreachable, or a label cannot be fetched, the editor
reports an error and hands out nothing. The same goes for **View selected**.

The sidebar shows the service state and how many labels are cached.

## Label styles

A switch under *Generate boxes* chooses one of three designs for every box:

| | **Integrated** (default) | **Removable** | **MakerWorld** |
|---|---|---|---|
| bin | shelf + 45° ramp at the label edge, Chappel inlay cut into it | plain walls with a 0.6 mm snap rim round the inside, 1.6 mm below the top (45° above and below, prints without support), and under the label a 45° ramp whose flat top the plate rests on (0.1 mm gap) | plain walls with a lip round the top inside, the counterpart of the label's clip profile (45° faces only) |
| label | part of the bin, filament 2 | separate 2.4 mm plate that fills the inner width, **Label width** deep (slider, 6–19 mm, default 10); a V-groove on its wall-side edges clicks over the rim; Chappel inlay flush on filament 2; exported face-down, ready to print | replica of MakerWorld model 431547: a 1.8 mm face plate with a 3.16 mm skirt along both sides and the wall-side edge, reaching 5 mm down; the skirt's recess clips round the bin lip and a 0.6 mm bump hooks under it. Chappel inlay flush in the face; **Label width** slider; exported face-down |
| files | one `SK_M6x80_2x3_8U.3mf` per box | `bins/BIN_2x1_8U_D10_x83.3mf`, one per distinct bin (size, label width, `_LBACK` for top-edge boxes), the count in the name, plus `labels/LABEL_SK_M6x80_W2_D10.3mf` per box | `bins/BIN_2x1_8U_MW_x83.3mf`, one per size (the bin does not depend on the label), plus `labels/LABEL_SK_M6x80_W2_D10_MW_FIT0.05.3mf` per box |

The removable design follows the MakerWorld *Removable Label – Gridfinity AddOn*
(model 431547) in spirit; the dimensions are ours. A removable bin does not depend on the
label artwork, so every box with the same size, label edge and label width shares one file
(the ramp sits under the label, so edge and width shape the bin). A single removable box
downloads as a zip holding its bin and its label. CLI: `--style integrated|removable|makerworld`, `--label-fit 0.05`,
`--label-depth 10`. The Chappel artwork keeps 1.8 mm to the plate's long edges, so a
shallow plate means small lettering: at 10 mm the artwork is 6.3 mm tall, at 6 mm only
2.3 mm, which is below what prints legibly.
The fit is checked in CAD only (removable: 0.15 mm clearance, 0.45 mm snap engagement;
MakerWorld: **Label fit** slider, default 0.05 mm all round the clip profile, −0.30 to
0.30, negative is a press fit; only the label changes, so a looser or tighter label fits
bins you already printed). With **Detents** ticked, the bin's lip turns down at two places per wall (near each corner):
a short vertical piece with the lip's own cross-section runs from the lip 4 mm down the wall
(bottom end 45°, prints without support), and the label's skirt has matching slots, so it
cannot slide. The bin still fits any label width on any wall (`BIN_2x1_8U_MW_DET`), CLI
`--detents`. Lip and ribs run round all four walls, so a printed bin can later take a new
label on any wall; the tools generate front (bot) and back (top) labels:
print one bin and one label before the batch.

The MakerWorld profile was measured from the model's STL (in `./tmp/`, untracked, licence
CC BY-NC-SA). Its 0.5 mm raised sticker rim is left out: the label prints face down for the
two-colour inlay, and the rim would leave the whole face as an unsupported bridge.

Files carry no position number and no quantity: boxes are named by type and size
(`SK_M6x80`), which is unique in the inventory.

## Labels

The label artwork comes from Alexandre Chappel's generator, exactly as the script has
always done it: the raw label is downloaded once per screw size, its icon and text
outlines are extracted, and they are re-extruded as a flush inlay sized to that box's
shelf. Nothing about that changes here — the service just calls the script.

- Artwork is fetched on demand, in the background of a build: building one box scrapes
  that one label; **All as 3MF** scrapes every label still missing. Nothing is fetched
  before you ask for a box.
- The cache lives in `./cache/chappel_raw_labels/` and is keyed by screw type and size
  only, so changing a box's footprint or height never re-downloads anything.

## First run

1. `docker compose up -d --build` — the api image pulls CadQuery, trimesh and a headless
   Chromium. Expect 5–15 minutes and ~2.5 GB the first time; after that it is layer-cached
   and starting up is immediate.
2. Use **3MF** in a box's menu, **Selected 3MF** or **All as 3MF**. The first build of a
   position drives Chromium against `label.alch.shop`, downloads that raw label and stores
   it in `./cache/chappel_raw_labels/` (needs outbound network). Later builds and container
   rebuilds reuse the cache.

Every server-built file is also written to `./out/` on the host, so a batch is on disk even
if you close the tab.

## Layout

```
docker-compose.yml     web + api, both in the default set
web/                   nginx + the editor; /api/ is proxied to the api's published port
                       (host.docker.internal:${API_PORT})
generator/             your two pipeline scripts, bind-mounted read-only into the service
  generate_gridfinity_chappel_bins.py    inventory, sizing rules, box + label geometry
  assemble_final_drawer_v9.py            drawer assembly and layout proof
cache/                 scraped label artwork — keep this, it saves the scrape
out/                   server-built 3MF files
```

`generator/`, `api/app.py` and `web/public/` are bind-mounted. Editor changes are live on
reload; for the other two run `docker compose restart api`. No image rebuild is needed for
any of them, which matters on a small Codespaces disk.

## The original CLI still works

```
docker compose run --rm api \
  python /app/generator/generate_gridfinity_chappel_bins.py --build --out /data/cache
```

## Service endpoints

| method | path | purpose |
|---|---|---|
| GET | `/api/health` | is CadQuery loadable, how many labels are cached |
| GET | `/api/labels` | cached positions |
| POST | `/api/labels/fetch` | scrape missing labels (`{"positions": [6, 7]}` or `{}` for all) |
| POST | `/api/jobs` | same body as `/api/generate` (items may add `"label": "top"`, the request `"style": "removable"` and `"label_depth": 10`); queues a background build, returns `{id, phase, done, total, current}`. An identical running or finished request is reattached, not duplicated |
| GET | `/api/jobs/{id}` | status: `queued` → `labels` (done/total = labels fetched) → `build` (done/total = boxes) → `done` or `error` |
| GET | `/api/jobs/{id}/file` | the 3MF (one box) or zip, once `done`; the last 8 finished jobs are kept in `cache/jobs/` |
| POST | `/api/generate` | `{"items": [{"pos": 6, "w": 2, "d": 3}], "height_u": 8}` → 3MF or zip, synchronously (for scripts) |

## Things worth knowing

- **OCP is not thread safe**, so one worker thread runs jobs in order. A 116-box batch from
  cold (labels + builds) takes tens of minutes. The editor runs every build as a job and
  polls it once a second, because a single long request gets cut off by proxies: GitHub
  Codespaces port forwarding dropped the old synchronous "All as 3MF" after about a minute
  (nginx logged `499`) while the scrape carried on server-side. If the page is closed, the
  job still finishes; clicking the same button again reattaches to it.
- **Networking.** Both containers run on Docker's default bridge (`network_mode: bridge`),
  and nginx reaches the api through the host's published port (`host.docker.internal`,
  mapped to the docker0 gateway). On a compose project network, hosts that only forward
  `docker0` traffic (GitHub Codespaces: legacy iptables `FORWARD DROP`) gave nginx a 504 on
  `/api/` and left the scrape unable to resolve `label.alch.shop`. An earlier fix had web
  share api's network namespace; that took the editor down whenever api restarted.
- **Built boxes are cached.** The page keeps every file it built in memory, so downloading
  or viewing the same boxes again is instant. The service also keeps them in
  `./cache/built_3mf/` and reuses them until the generator script or that label changes.
  Changing a box's size or the height is always a fresh build.
- **Label edge.** Every box has its label shelf on the front edge (**Bot**, the drawer-front
  side, default) or the back edge (**Top**, as the drawer is drawn). Choose per box in its
  menu, with **L** on a selection, or for all boxes under *Generate boxes*. A dark stripe on
  each tile shows the edge. A top label is the same shelf and ramp mirrored to the back; the
  label artwork is moved, not rotated, so it still reads from the drawer front. Those files
  end in `_LBACK`. From the CLI: `--label-side front|back`.
- **Layout file.** **Export CSV** / **Import CSV…** use the same columns
  (`type, thread, length_mm, box_w, box_d, height_u, grid_x, row_from_front, label`); a box is
  identified by type + thread + length, and `label` is optional on import (default `bot`).
  An import that is out of bounds, overlapping, lists a box twice or mixes heights is
  rejected as a whole. Boxes missing from the file are moved out of the drawer.
- **Label artwork is cached per position internally** (`cache/chappel_raw_labels/P006_…`);
  that number never appears in the editor or in built files.
- The editor keeps your working layout in the browser's local storage.
- Ports come from `.env` (`WEB_PORT`, `API_PORT`); copy `.env.example` to start.

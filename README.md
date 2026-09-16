# Schraubenschublade 1077 — local planner

Drawer layout editor for the 116-position Gridfinity screw drawer, plus the CadQuery
pipeline that builds the real boxes. Runs entirely on your machine; downloads are plain
browser downloads, so nothing sits between you and the file.

```
docker compose up -d --build          # editor on http://localhost:8080
                                      # + CadQuery build service on :8000
```

Both services start by default. The editor arranges bins and exports browser-built boxes;
the build service produces the exact CadQuery geometry, and the editor routes the 3MF
buttons through it automatically — which is what you want for anything you intend to print.

## The two ways to get a box

| | browser build | build service (default) |
|---|---|---|
| speed | instant | seconds per box |
| body | correct envelope, feet, shelf, 45° ramp, multi-body | `make_finished_body()`, one booleaned solid with the label pocket cut |
| label | none, unless you load label STLs the script already produced | `make_label_inlay()` — the real Chappel artwork, flush in its pocket |
| use it for | test-fitting a size, checking a layout | anything you intend to print |

**Labels are made by `generate_gridfinity_chappel_bins.py`.** The browser cannot run
CadQuery, so a box built in the page is a bare body. The build service is the real thing:
it calls the script's own `make_finished_body()` and `make_label_inlay()`,
so what you get is what the pipeline has always produced — body with the pocket cut,
Chappel icon and text as a flush 0.6 mm inlay, exported as two parts (`BODY` on filament 1,
`LABEL_INLAY` on filament 2) so Bambu Studio opens it as a two-colour object.

The editor picks the server automatically when it is running: a **Local build service**
card appears in the sidebar with a checkbox that routes the 3MF buttons through it.

## Labels

The label artwork comes from Alexandre Chappel's generator, exactly as the script has
always done it: the raw label is downloaded once per screw size, its icon and text
outlines are extracted, and they are re-extruded as a flush inlay sized to that box's
shelf. Nothing about that changes here — the service just calls the script.

- Missing artwork is scraped automatically the first time you build a box that needs it.
  **Fetch & cache labels** does the whole inventory up front instead.
- The cache lives in `./cache/chappel_raw_labels/` and is keyed by screw type and size
  only, so changing a box's footprint or height never re-downloads anything.
- **Load labels…** in the page is an offline shortcut: pick `labels_inlay_stl` files the
  script produced earlier and the browser build embeds them, re-seated on the shelf. It is
  a convenience, not the label pipeline.

## First run

1. `docker compose up -d --build` — the api image pulls CadQuery, trimesh and a headless
   Chromium. Expect 5–15 minutes and ~2.5 GB the first time; after that it is layer-cached
   and starting up is immediate.
2. In the editor, press **Fetch & cache labels**. The service drives Chromium against
   `label.alch.shop`, downloads the raw label for every position and stores it in
   `./cache/chappel_raw_labels/`. Needs outbound network; it runs once — later builds and
   container rebuilds reuse the cache.
3. Use **Selected 3MF** / **All as 3MF**. The service is already ticked by default; untick
   it only if you deliberately want the faster, label-less browser build.

Every server-built file is also written to `./out/` on the host, so a batch is on disk even
if you close the tab.

## Layout

```
docker-compose.yml     web + api, both in the default set
web/                   nginx + the editor; /api/ is proxied to the build service
generator/             your two pipeline scripts, bind-mounted read-only into the service
  generate_gridfinity_chappel_bins.py    inventory, sizing rules, box + label geometry
  assemble_final_drawer_v9.py            drawer assembly and layout proof
cache/                 scraped label artwork — keep this, it saves the scrape
out/                   server-built 3MF files
```

Edit anything in `generator/` on the host and restart the api service to pick it up:
`docker compose restart api`.

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
| POST | `/api/generate` | `{"items": [{"pos": 6, "w": 2, "d": 3}], "height_u": 8}` → 3MF or zip |

## Things worth knowing

- **OCP is not thread safe**, so the service serialises builds behind a lock. A 116-box
  batch is a long single-threaded job — start it and leave it.
- **Browser-built bodies are multi-body**: walls, floor, feet, shelf and ramp are separate
  closed volumes in one file. Slicers union them at slice time; a mesh checker will call
  it multi-body. Server-built bodies are a single booleaned solid.
- **Labels are matched by position number** (`P006` in the filename), not by screw size.
- **Browser-generated labels are raised, not inlaid.** Cutting a text-shaped pocket needs a
  CAD boolean, which is what the build service does; the raised version prints the same way
  in two colours, it just stands 0.6 mm proud.
- The editor keeps your working layout in the browser's local storage. Saved named layouts
  need the artifact platform and are unavailable locally; use **Export CSV** instead.
- Ports come from `.env` (`WEB_PORT`, `API_PORT`); copy `.env.example` to start.

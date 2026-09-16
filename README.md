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
web/                   nginx + the editor; /api/ is proxied to 127.0.0.1:8000
                       (web shares the api container's network namespace)
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
| POST | `/api/jobs` | same body as `/api/generate`; queues a background build, returns `{id, phase, done, total, current}`. An identical running or finished request is reattached, not duplicated |
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
- **Networking.** `api` runs on Docker's default bridge (`network_mode: bridge`) and `web`
  runs inside api's network namespace. On a compose project network, hosts that only
  forward `docker0` traffic (GitHub Codespaces: legacy iptables `FORWARD DROP`) gave nginx a
  504 on `/api/` and left the scrape unable to resolve `label.alch.shop`. Both ports are
  therefore published on the `api` service.
- **Built boxes are cached.** The page keeps every file it built in memory, so downloading
  or viewing the same boxes again is instant. The service also keeps them in
  `./cache/built_3mf/` and reuses them until the generator script or that label changes.
  Changing a box's size or the height is always a fresh build.
- **Layout file.** **Export CSV** / **Import CSV…** use the same columns
  (`position, …, box_w, box_d, height_u, grid_x, row_from_front`). An import that is out of
  bounds, overlapping or mixes heights is rejected as a whole. Positions missing from the
  file are moved out of the drawer.
- **Labels are matched by position number** (`P006` in the filename), not by screw size.
- The editor keeps your working layout in the browser's local storage.
- Ports come from `.env` (`WEB_PORT`, `API_PORT`); copy `.env.example` to start.

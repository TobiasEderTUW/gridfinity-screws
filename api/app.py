"""Build service for the drawer planner.

Wraps the CadQuery pipeline so the editor can ask for the real thing: a body with the
label pocket cut out plus the label inlay as a two-part 3MF, or bins and label plates for
Built files are cached under CACHE_DIR and reused.

Fully offline: the label artwork is composed from generator/assets (icon exports + font).
A box is described by its part type, thread, length and optional label text, so any part
type of the catalogue can be built, not only the 116 positions of the default set.
The editor's 3MF export and viewer depend on this service: there is no browser-built
fallback, because that could only produce a body without its label.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import queue
import shutil
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

CACHE = Path(os.environ.get("CACHE_DIR", "/data/cache"))
OUT = Path(os.environ.get("OUT_DIR", "/data/out"))
STATE = Path(os.environ.get("STATE_DIR", "/data/state"))   # shared editor state (the default drawer)
GENERATOR = Path("/app/generator/generate_gridfinity_chappel_bins.py")
LABEL_ART = GENERATOR.with_name("label_art.py")
ASSETS = GENERATOR.parent / "assets"
BUILT = CACHE / "built_3mf"            # finished 3MFs, reused while their inputs are unchanged
BUILD_LOCK = threading.Lock()          # CadQuery/OCP is not thread safe

app = FastAPI(title="Schublade build service")
_gen = None
_gen_error: str | None = None


def generator():
    """Import the pipeline script once, lazily (it is slow and optional)."""
    global _gen, _gen_error
    if _gen is not None or _gen_error is not None:
        if _gen_error:
            raise HTTPException(503, _gen_error)
        return _gen
    if not GENERATOR.exists():
        _gen_error = f"{GENERATOR} not found — mount ./generator into the container"
        raise HTTPException(503, _gen_error)
    try:
        spec = importlib.util.spec_from_file_location("chappel_generator", GENERATOR)
        module = importlib.util.module_from_spec(spec)
        sys.modules["chappel_generator"] = module
        spec.loader.exec_module(module)
        _gen = module
    except Exception as exc:                      # noqa: BLE001 - reported to the caller
        _gen_error = f"could not load the generator: {exc}"
        raise HTTPException(503, _gen_error) from exc
    return _gen


# Editor vocabulary, as the drawer is drawn (front at the bottom): "bot" = label shelf on
# the front edge (the original design), "top" = on the back edge.
LABEL_SIDE = {"bot": "front", "top": "back"}


class Item(BaseModel):
    part: str = Field(min_length=1, max_length=64)            # catalogue key, e.g. allen__countersunk
    thread: str = Field(min_length=1, max_length=16)          # M6, #8, 1/4" ...
    length: float | None = Field(default=None, gt=0, le=1000)  # None for nuts and washers
    text: str | None = Field(default=None, max_length=40)     # label text; default thread×length
    w: int = Field(ge=1, le=6)
    d: int = Field(ge=1, le=6)
    label: Literal["bot", "top"] = "bot"


class BuildRequest(BaseModel):
    items: list[Item]
    height_u: int = Field(default=8, ge=3, le=12)
    keep: bool = True                 # also write the files into OUT_DIR
    # "makerworld": lipped bins deduplicated per size + one clip-on label tray per screw.
    # One label style since 2026-09-30: lipped bins, deduplicated per size (and detents), plus
    # one clip-on label per distinct part/text/width/edge. A "style" field is ignored.
    label_depth: float = Field(default=10.0, ge=6.0, le=30.0)   # label depth from the wall, mm
    label_fit: float = Field(default=0.05, ge=-0.30, le=0.30)     # makerworld clip gap, mm
    label_detent: bool = False        # makerworld: detent bumps on the side lips + dimples


@app.get("/api/health")
def health():
    cad = True
    detail = None
    try:
        gen = generator()
    except HTTPException as exc:
        cad, detail = False, exc.detail
    parts = len(gen.label_art.parts()) if cad else 0
    return {"cad": cad, "detail": detail, "parts": parts, "cache": str(CACHE), "out": str(OUT)}


def _spec_for(gen, item: Item, height_u: int) -> dict:
    length = item.length
    if length is not None and float(length).is_integer():
        length = int(length)
    spec = {"pos": 0, "part": item.part, "thread": item.thread.strip(), "length": length,
            "text": (item.text or "").strip() or None, "norm": "",
            "grid_w": item.w, "grid_d": item.d, "height_u": height_u,
            "label_side": LABEL_SIDE[item.label]}
    try:
        gen.validate_spec(spec)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return spec


def _label_inputs(gen, spec: dict) -> list[Path]:
    """Files a label's artwork is made from; a newer one invalidates the cached build."""
    icon = ASSETS / gen.label_art.part(spec["part"])["icon_file"]
    return [LABEL_ART, ASSETS / "label_catalogue.json", ASSETS / "fonts" / "HarmonyOS_Sans_SC_Regular.ttf", icon]


def _cached(path: Path, extra: list[Path]) -> bool:
    """Is this cached build newer than everything it was made from?

    The name encodes part, text, footprint, height, side and label options, so any of those changing
    is a miss by construction; a newer generator, app.py or label asset invalidates it.
    """
    if not path.is_file():
        return False
    inputs = [GENERATOR, Path(__file__)] + extra
    newest = max(p.stat().st_mtime for p in inputs if p.exists())
    return path.stat().st_mtime >= newest


def _cached_build(stem: str, extra: list[Path], build) -> bytes:
    """Return BUILT/<stem>.3mf, building it with build(path) first if it is stale."""
    path = BUILT / f"{stem}.3mf"
    if not _cached(path, extra):
        work = BUILT / ".partial"             # same file system, so replace() is atomic
        work.mkdir(parents=True, exist_ok=True)
        tmp = work / f"{stem}.3mf"            # the builders name the 3MF object after the file
        build(tmp)
        tmp.replace(path)                     # never a half-written cache entry
    return path.read_bytes()


def _build_mw_bin(gen, key: tuple, count: int) -> tuple[str, bytes]:
    w, d, u = key[:3]
    det = len(key) == 4                                      # (w, d, u, 'det'): with detent ribs
    data = _cached_build(gen.mw_bin_stem(w, d, u, detent=det), [],
                         lambda p: gen.build_mw_bin_3mf(w, d, u, p, det))
    return f"bins/{gen.mw_bin_stem(w, d, u, count, det)}.3mf", data


def _build_label_plate(gen, spec: dict) -> tuple[str, bytes]:
    stem = gen.label_plate_stem(spec)
    data = _cached_build(stem, _label_inputs(gen, spec), lambda p: gen.build_label_plate_3mf(spec, p))
    return f"labels/{stem}.3mf", data


def _check_request(gen, req: BuildRequest) -> None:
    """Reject bad input up front, before anything is queued."""
    if not req.items:
        raise HTTPException(400, "no items requested")
    for item in req.items:
        spec = _spec_for(gen, item, req.height_u)
        try:
            gen.plate_depth({**spec, "label_depth": req.label_depth})
        except ValueError as exc:
            raise HTTPException(422, f"{gen.screw_name(spec)}: {exc}") from exc


def _build_all(gen, items: list[Item], height_u: int, keep: bool,
               label_depth: float = 10.0, on_box=lambda done, name, total: None,
               label_fit: float = 0.05, label_detent: bool = False) -> tuple[str, str, bytes]:
    """Build every file the request needs -> (filename, media type, bytes). Caller holds BUILD_LOCK.
    Boxes of one size share a bin file; boxes with the same label share a label file."""
    specs = [_spec_for(gen, item, height_u) for item in items]
    for spec in specs:
        spec["label_depth"] = label_depth
        spec["label_fit"] = label_fit
        spec["label_detent"] = label_detent
    bins, labels = gen.file_plan(specs)
    jobs = ([(gen.mw_bin_stem(*key[:3], count, len(key) == 4), lambda k=key, c=count: _build_mw_bin(gen, k, c))
             for key, count in bins]
            + [(stem, lambda sp=sp: _build_label_plate(gen, sp))
               for stem, sp in {gen.label_plate_stem(sp): sp for sp in labels}.items()])

    built: list[tuple[str, bytes]] = []
    for n, (name, job) in enumerate(jobs):
        on_box(n, name, len(jobs))
        try:
            built.append(job())
        except Exception as exc:                  # noqa: BLE001
            raise RuntimeError(f"{name}: {exc}") from exc
    on_box(len(jobs), "", len(jobs))

    if keep:
        for name, data in built:
            target = OUT / f"makerworld_{height_u}U" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    # A box is always two files (bin + label), so the result is always a zip.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # 3MFs are already deflated
        for name, data in built:
            zf.writestr(name, data)
    return f"drawer_3mf_{height_u}U.zip", "application/zip", buf.getvalue()


@app.post("/api/generate")
def generate(req: BuildRequest):
    """Synchronous build, for scripts. The editor uses /api/jobs: a long batch outlives
    HTTP proxies (GitHub's port forwarding drops the request after about a minute)."""
    gen = generator()
    _check_request(gen, req)
    with BUILD_LOCK:
        try:
            name, media, data = _build_all(gen, req.items, req.height_u, req.keep, req.label_depth,
                                           label_fit=req.label_fit, label_detent=req.label_detent)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------------------
# Background jobs. One worker thread works the queue in order (CadQuery is not thread
# safe anyway); the editor polls short status requests and fetches the file at the end.
# ---------------------------------------------------------------------------------------
JOBS_DIR = CACHE / "jobs"
JOBS_KEEP = 8                           # finished jobs whose files are kept on disk
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()


def _job_key(req: BuildRequest) -> str:
    return json.dumps([req.height_u, req.keep, req.label_depth, req.label_fit, req.label_detent,
                       [[i.part, i.thread, i.length, i.text, i.w, i.d, i.label] for i in req.items]])


def _job_public(job: dict) -> dict:
    job = job.copy()                    # the worker thread adds keys while we read
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _run_job(job: dict) -> None:
    gen = generator()
    req: BuildRequest = job["_req"]
    with BUILD_LOCK:
        def on_box(done: int, name: str, total: int) -> None:
            job.update(done=done, current=name, total=total)

        job.update(phase="build", done=0, total=len(req.items), current="")
        name, media, data = _build_all(gen, req.items, req.height_u, req.keep, req.label_depth, on_box,
                                       label_fit=req.label_fit, label_detent=req.label_detent)

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = JOBS_DIR / f"{job['id']}.bin"
    path.write_bytes(data)
    job.update(phase="done", filename=name, media=media, size=len(data), _path=path,
               finished=time.time())
    _prune_jobs()


def _prune_jobs() -> None:
    with JOBS_LOCK:
        finished = sorted((j for j in JOBS.values() if j["phase"] in ("done", "error")),
                          key=lambda j: j.get("finished", 0))
        for job in finished[:-JOBS_KEEP]:
            path = job.get("_path")
            if path is not None:
                path.unlink(missing_ok=True)
            JOBS.pop(job["id"], None)


def _worker() -> None:
    while True:
        job = JOBS[JOB_QUEUE.get()]
        job["started"] = time.time()
        try:
            _run_job(job)
        except Exception as exc:                  # noqa: BLE001 - reported to the poller
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            job.update(phase="error", error=str(detail), finished=time.time())
            _prune_jobs()


threading.Thread(target=_worker, name="build-worker", daemon=True).start()


@app.post("/api/jobs")
def create_job(req: BuildRequest):
    gen = generator()
    _check_request(gen, req)
    key = _job_key(req)
    with JOBS_LOCK:
        # Same request again (a second click, a reload) -> attach to that job rather than
        # queueing a duplicate. A finished one is reused while its file is still on disk.
        for job in JOBS.values():
            if job["_key"] == key and job["phase"] != "error":
                if job["phase"] != "done" or job["_path"].exists():
                    return _job_public(job)
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "phase": "queued", "done": 0, "total": len(req.items),
               "current": "", "items": len(req.items), "created": time.time(),
               "queued_ahead": JOB_QUEUE.qsize(),
               "_key": key, "_req": req}
        JOBS[job_id] = job
    JOB_QUEUE.put(job_id)
    return _job_public(job)


def _job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job — the service may have restarted; start it again")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    return _job_public(_job(job_id))


@app.get("/api/jobs/{job_id}/file")
def job_file(job_id: str):
    job = _job(job_id)
    if job["phase"] != "done":
        raise HTTPException(409, f"job is {job['phase']}, not done")
    path: Path = job["_path"]
    if not path.exists():
        raise HTTPException(410, "the file of this job was cleaned up; start it again")
    return FileResponse(path, media_type=job["media"], filename=job["filename"])


@app.get("/api/space")
def space():
    usage = shutil.disk_usage(str(CACHE if CACHE.exists() else Path("/")))
    return {"free_mb": usage.free // 1048576}


# ---------------------------------------------------------------------------------------
# The shared default drawer. One per installation: "Set as default" in the editor stores
# the current design here, and every browser without its own layout (and "Load default")
# starts from it. Each replaced default is kept in STATE/default_history/.
# ---------------------------------------------------------------------------------------
DEFAULT_FILE = STATE / "default_layout.json"
DEFAULT_HISTORY = STATE / "default_history"
DEFAULT_LOCK = threading.Lock()
GRID_W, GRID_D = 25, 14


def _known_parts() -> set[str]:
    catalogue = json.loads((ASSETS / "label_catalogue.json").read_text(encoding="utf-8"))
    return {p["key"] for p in catalogue["parts"]}


class LayoutBin(BaseModel):
    pos: int = Field(ge=1)
    part: str = Field(min_length=1, max_length=64)
    thread: str = Field(max_length=16)
    len: float | None = Field(default=None, gt=0, le=1000)
    text: str | None = Field(default=None, max_length=40)
    w: int = Field(ge=1, le=6)
    d: int = Field(ge=1, le=6)
    x: int | None = Field(default=None, ge=0, lt=GRID_W)
    y: int | None = Field(default=None, ge=0, lt=GRID_D)
    label: Literal["bot", "top"] = "bot"


class Layout(BaseModel):
    """The editor's snapshot (local storage format v3)."""
    v: Literal[3] = 3
    u: int = Field(default=8, ge=3, le=10)
    # the only label style since 2026-09-30; older snapshots say "integrated" etc., which
    # is accepted and stored as "makerworld"
    style: str = "makerworld"
    labelDepth: float = Field(default=10.0, ge=6.0, le=30.0)
    labelFit: float = Field(default=0.05, ge=-0.30, le=0.30)
    detent: bool = False
    bins: list[LayoutBin] = Field(min_length=1, max_length=GRID_W * GRID_D)   # an empty default is a mistake


def _check_layout(layout: Layout) -> None:
    unknown = sorted({b.part for b in layout.bins} - _known_parts())
    if unknown:
        raise HTTPException(422, f"unknown part type(s): {', '.join(unknown[:5])}")
    cells: dict[tuple[int, int], int] = {}
    for b in layout.bins:
        if (b.x is None) != (b.y is None):
            raise HTTPException(422, f"box {b.pos}: x and y must both be set or both be empty")
        if b.x is None:
            continue
        if b.x + b.w > GRID_W or b.y + b.d > GRID_D:
            raise HTTPException(422, f"box {b.pos} sticks out of the drawer")
        for i in range(b.w):
            for j in range(b.d):
                other = cells.setdefault((b.x + i, b.y + j), b.pos)
                if other != b.pos:
                    raise HTTPException(422, f"boxes {other} and {b.pos} overlap")


@app.get("/api/default")
def get_default():
    if not DEFAULT_FILE.is_file():
        raise HTTPException(404, "no default drawer saved yet")
    return json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))


@app.put("/api/default")
def set_default(layout: Layout):
    _check_layout(layout)
    layout.style = "makerworld"
    record = {"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "layout": layout.model_dump()}
    with DEFAULT_LOCK:
        STATE.mkdir(parents=True, exist_ok=True)
        if DEFAULT_FILE.is_file():               # keep what it replaces
            DEFAULT_HISTORY.mkdir(exist_ok=True)
            stamp = time.strftime('%Y%m%d-%H%M%S') + f"-{time.time_ns() % 1_000_000_000:09d}"
            shutil.copy2(DEFAULT_FILE, DEFAULT_HISTORY / f"default_{stamp}.json")
        tmp = DEFAULT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(DEFAULT_FILE)                # readers never see a half-written file
    return {"saved_at": record["saved_at"], "boxes": len(layout.bins)}


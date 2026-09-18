"""Build service for the Schraubenschublade planner.

Wraps the existing CadQuery pipeline so the editor can ask for the real thing:
a body with the label pocket cut out, plus the Chappel label inlay, exported as a
two-part 3MF. Scraped label artwork is cached under CACHE_DIR and reused.

The editor's 3MF export and viewer depend on this service: there is no browser-built
fallback, because that could only produce a body without its label. Label artwork is
fetched on demand, for exactly the positions a build asks for.
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
GENERATOR = Path("/app/generator/generate_gridfinity_chappel_bins.py")
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


def cached_labels() -> set[int]:
    folder = CACHE / "chappel_raw_labels"
    if not folder.exists():
        return set()
    out = set()
    for f in folder.glob("P*_CHAPPEL_ORIGINAL.stl"):
        try:
            out.add(int(f.name[1:4]))
        except ValueError:
            continue
    return out


# Editor vocabulary, as the drawer is drawn (front at the bottom): "bot" = label shelf on
# the front edge (the original design), "top" = on the back edge.
LABEL_SIDE = {"bot": "front", "top": "back"}


class Item(BaseModel):
    pos: int
    w: int = Field(ge=1, le=6)
    d: int = Field(ge=1, le=6)
    label: Literal["bot", "top"] = "bot"


class BuildRequest(BaseModel):
    items: list[Item]
    height_u: int = Field(default=8, ge=3, le=12)
    keep: bool = True                 # also write the files into OUT_DIR
    fetch_missing: bool = True        # scrape label artwork we do not have yet
    # "integrated": label inlaid in each bin, one 3MF per screw.
    # "removable": snap-rim bins deduplicated per size/edge/depth + one label plate per screw.
    # "makerworld": lipped bins deduplicated per size + one clip-on label tray per screw.
    style: Literal["integrated", "removable", "makerworld"] = "integrated"
    label_depth: float = Field(default=10.0, ge=6.0, le=30.0)   # removable plate depth, mm
    label_fit: float = Field(default=0.05, ge=-0.30, le=0.30)     # makerworld clip gap, mm
    label_detent: bool = False        # makerworld: detent bumps on the side lips + dimples


class FetchRequest(BaseModel):
    positions: list[int] | None = None
    headed: bool = False


@app.get("/api/health")
def health():
    cad = True
    detail = None
    try:
        generator()
    except HTTPException as exc:
        cad, detail = False, exc.detail
    specs = len(_gen.SPECS) if cad else 0
    return {"cad": cad, "detail": detail, "labels": len(cached_labels()),
            "positions": specs, "cache": str(CACHE), "out": str(OUT)}


@app.get("/api/labels")
def labels():
    return {"cached": sorted(cached_labels())}


@app.post("/api/labels/fetch")
def fetch_labels(req: FetchRequest):
    gen = generator()
    CACHE.mkdir(parents=True, exist_ok=True)
    wanted = set(req.positions) if req.positions else {s["pos"] for s in gen.SPECS}
    missing = wanted - cached_labels()
    if missing:
        with BUILD_LOCK:
            try:
                gen.fetch_chappel_labels(CACHE, positions=missing, headed=req.headed)
            except Exception as exc:              # noqa: BLE001
                raise HTTPException(502, f"label fetch failed: {exc}") from exc
    have = cached_labels()
    return {"cached": len(have), "total": len(wanted), "fetched": sorted(missing & have)}


def _spec_for(gen, item: Item, height_u: int) -> dict:
    base = next((s for s in gen.SPECS if s["pos"] == item.pos), None)
    if base is None:
        raise HTTPException(404, f"position {item.pos} is not in the inventory")
    spec = dict(base)
    spec["grid_w"], spec["grid_d"], spec["height_u"] = item.w, item.d, height_u
    spec["label_side"] = LABEL_SIDE[item.label]
    return spec


def _cached(path: Path, label: Path | None, needs_label: bool) -> bool:
    """Is this cached build newer than everything it was made from?

    The name encodes screw, footprint, height, side and style, so any of those changing is a
    miss by construction; a newer generator script, app.py or label artwork invalidates it.
    """
    if not path.is_file() or (needs_label and label is None):
        return False
    inputs = [GENERATOR, Path(__file__)] + ([label] if label is not None else [])
    newest = max(p.stat().st_mtime for p in inputs if p.exists())
    return path.stat().st_mtime >= newest


def _cached_build(stem: str, label: Path | None, needs_label: bool, build) -> bytes:
    """Return BUILT/<stem>.3mf, building it with build(path) first if it is stale."""
    path = BUILT / f"{stem}.3mf"
    if not _cached(path, label, needs_label):
        work = BUILT / ".partial"             # same file system, so replace() is atomic
        work.mkdir(parents=True, exist_ok=True)
        tmp = work / f"{stem}.3mf"            # the builders name the 3MF object after the file
        build(tmp)
        tmp.replace(path)                     # never a half-written cache entry
    return path.read_bytes()


def _build_integrated(gen, spec: dict) -> tuple[str, bytes]:
    stem = gen.filename_stem(spec)
    label = gen.find_chappel_cache_path(CACHE, spec)
    data = _cached_build(stem, label, True, lambda p: gen.build_integrated_3mf(spec, CACHE, p))
    return f"{stem}.3mf", data


def _build_snap_bin(gen, key: tuple, count: int) -> tuple[str, bytes]:
    w, d, u, side, depth = key
    data = _cached_build(gen.snap_bin_stem(w, d, u, side, depth), None, False,
                         lambda p: gen.build_snap_bin_3mf(w, d, u, p, side, depth))
    return f"bins/{gen.snap_bin_stem(w, d, u, side, depth, count)}.3mf", data


def _build_mw_bin(gen, key: tuple, count: int) -> tuple[str, bytes]:
    w, d, u = key[:3]
    det = len(key) == 4                                      # (w, d, u, 'det'): with detent ribs
    data = _cached_build(gen.mw_bin_stem(w, d, u, detent=det), None, False,
                         lambda p: gen.build_mw_bin_3mf(w, d, u, p, det))
    return f"bins/{gen.mw_bin_stem(w, d, u, count, det)}.3mf", data


def _build_label_plate(gen, spec: dict) -> tuple[str, bytes]:
    stem = gen.label_plate_stem(spec)
    label = gen.find_chappel_cache_path(CACHE, spec)
    data = _cached_build(stem, label, True, lambda p: gen.build_label_plate_3mf(spec, CACHE, p))
    return f"labels/{stem}.3mf", data


def _check_request(gen, req: BuildRequest) -> None:
    """Reject bad input up front, before anything is queued or scraped."""
    if not req.items:
        raise HTTPException(400, "no items requested")
    for item in req.items:
        spec = _spec_for(gen, item, req.height_u)
        if req.style in ("removable", "makerworld"):
            try:
                gen.plate_depth({**spec, "label_depth": req.label_depth})
            except ValueError as exc:
                raise HTTPException(422, f"{gen.screw_name(spec)}: {exc}") from exc


def _fetch_missing(gen, items: list[Item]) -> None:
    """Scrape label artwork for exactly the requested positions that lack it. Caller holds BUILD_LOCK."""
    wanted = {i.pos for i in items}
    missing = sorted(wanted - cached_labels())
    if missing:
        try:
            gen.fetch_chappel_labels(CACHE, positions=set(missing))
        except Exception as exc:                  # noqa: BLE001
            got = len(set(missing) & cached_labels())
            raise RuntimeError(
                f"label scrape stopped after {got} of {len(missing)} labels: {exc} "
                "— fetched labels are kept, so retrying continues from there"
            ) from exc
    missing = sorted(wanted - cached_labels())
    if missing:
        raise RuntimeError(
            f"no label artwork for position(s) {missing[:10]}"
            f"{' …' if len(missing) > 10 else ''} — the scrape finished but produced no file; "
            "no box is built without its label."
        )


def _build_all(gen, items: list[Item], height_u: int, keep: bool, style: str = "integrated",
               label_depth: float = 10.0, on_box=lambda done, name, total: None,
               label_fit: float = 0.05, label_detent: bool = False) -> tuple[str, str, bytes]:
    """Build every file the request needs -> (filename, media type, bytes). Caller holds BUILD_LOCK."""
    specs = [_spec_for(gen, item, height_u) for item in items]
    for spec in specs:
        spec["label_style"] = style
        spec["label_depth"] = label_depth
        spec["label_fit"] = label_fit
        spec["label_detent"] = label_detent
    if style in ("removable", "makerworld"):
        bins, labels = gen.removable_file_plan(specs)
        if style == "makerworld":
            bin_jobs = [(gen.mw_bin_stem(*key[:3], count, len(key) == 4),
                         lambda k=key, c=count: _build_mw_bin(gen, k, c))
                        for key, count in bins]
        else:
            bin_jobs = [(gen.snap_bin_stem(*key, count=count), lambda k=key, c=count: _build_snap_bin(gen, k, c))
                        for key, count in bins]
        jobs = (bin_jobs
                + [(gen.label_plate_stem(sp), lambda sp=sp: _build_label_plate(gen, sp)) for sp in labels])
    else:
        jobs = [(gen.filename_stem(sp), lambda sp=sp: _build_integrated(gen, sp)) for sp in specs]

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
            target = OUT / (f"{style}_{height_u}U" if style != "integrated" else "") / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    # A removable box is always two files (bin + label), so it always comes as a zip.
    if len(built) == 1:
        name, data = built[0]
        return name, "model/3mf", data
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # 3MFs are already deflated
        for name, data in built:
            zf.writestr(name, data)
    suffix = "" if style == "integrated" else f"_{style}"
    return f"drawer_3mf_{height_u}U{suffix}.zip", "application/zip", buf.getvalue()


@app.post("/api/generate")
def generate(req: BuildRequest):
    """Synchronous build, for scripts. The editor uses /api/jobs: a long batch outlives
    HTTP proxies (GitHub's port forwarding drops the request after about a minute)."""
    gen = generator()
    _check_request(gen, req)
    with BUILD_LOCK:
        try:
            if req.fetch_missing:
                _fetch_missing(gen, req.items)
            elif not {i.pos for i in req.items} <= cached_labels():
                raise RuntimeError("label artwork missing and fetch_missing is false")
            name, media, data = _build_all(gen, req.items, req.height_u, req.keep, req.style, req.label_depth,
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
    return json.dumps([req.height_u, req.keep, req.style, req.label_depth, req.label_fit, req.label_detent,
                       [[i.pos, i.w, i.d, i.label] for i in req.items]])


def _job_public(job: dict) -> dict:
    job = job.copy()                    # the worker thread adds keys while we read
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    wanted = job["_wanted"]
    if job["phase"] == "labels":        # the scrape reports nothing; count its files instead
        out["done"] = len(wanted & cached_labels()) - job["_had"]
    return out


def _run_job(job: dict) -> None:
    gen = generator()
    req: BuildRequest = job["_req"]
    with BUILD_LOCK:
        have = cached_labels()
        missing = job["_wanted"] - have
        if missing:
            job.update(phase="labels", done=0, total=len(missing), current="label.alch.shop",
                       _had=len(job["_wanted"] & have))
            _fetch_missing(gen, req.items)

        def on_box(done: int, name: str, total: int) -> None:
            job.update(done=done, current=name, total=total)

        job.update(phase="build", done=0, total=len(req.items), current="")
        name, media, data = _build_all(gen, req.items, req.height_u, req.keep, req.style, req.label_depth, on_box,
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
    req.fetch_missing = True
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
               "_key": key, "_req": req, "_wanted": {i.pos for i in req.items}, "_had": 0}
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

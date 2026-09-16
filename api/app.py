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
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

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


class Item(BaseModel):
    pos: int
    w: int = Field(ge=1, le=6)
    d: int = Field(ge=1, le=6)


class BuildRequest(BaseModel):
    items: list[Item]
    height_u: int = Field(default=8, ge=3, le=12)
    keep: bool = True                 # also write the files into OUT_DIR
    fetch_missing: bool = True        # scrape label artwork we do not have yet


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
    return spec


def _cached_3mf(gen, spec: dict) -> Path | None:
    """A previously built 3MF for this exact spec, if nothing it depends on changed since.

    The file name encodes position, footprint and height, so a size change is a cache miss
    by construction. A newer generator script, app.py or label artwork invalidates it too.
    """
    path = BUILT / f"{gen.filename_stem(spec)}.3mf"
    if not path.is_file():
        return None
    label = gen.find_chappel_cache_path(CACHE, spec)
    if label is None:
        return None
    inputs = (GENERATOR, Path(__file__), label)
    newest = max(p.stat().st_mtime for p in inputs if p.exists())
    return path if path.stat().st_mtime >= newest else None


def _build_one(gen, spec: dict, work: Path) -> tuple[str, bytes]:
    """Body with the label pocket cut + label inlay -> one two-part 3MF."""
    import trimesh

    stem = gen.filename_stem(spec)
    cached = _cached_3mf(gen, spec)
    if cached is not None:
        return cached.name, cached.read_bytes()
    body_path, label_path = work / f"{stem}_BODY.stl", work / f"{stem}_LABEL.stl"
    body = gen.make_finished_body(spec, CACHE)
    gen.cq.exporters.export(body, str(body_path), tolerance=0.09, angularTolerance=0.22)
    inlay = gen.make_label_inlay(spec, CACHE)
    gen.cq.exporters.export(inlay, str(label_path), tolerance=0.06, angularTolerance=0.18)

    body_mesh = trimesh.load_mesh(body_path, force="mesh")
    label_mesh = trimesh.load_mesh(label_path, force="mesh")
    body_mesh.visual.face_colors = [70, 70, 70, 255]
    label_mesh.visual.face_colors = gen.KIND_COLOR[spec["kind"]]
    out_path = work / f"{stem}.3mf"
    gen.export_assembled_3mf(body_mesh, label_mesh, out_path, stem)
    data = out_path.read_bytes()
    BUILT.mkdir(parents=True, exist_ok=True)
    tmp = BUILT / f".{out_path.name}.tmp"
    tmp.write_bytes(data)
    tmp.replace(BUILT / out_path.name)          # atomic: never a half-written cache entry
    return out_path.name, data


def _check_request(gen, items: list[Item], height_u: int) -> None:
    """Reject bad input up front, before anything is queued or scraped."""
    if not items:
        raise HTTPException(400, "no items requested")
    for item in items:
        _spec_for(gen, item, height_u)


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


def _build_all(gen, items: list[Item], height_u: int, keep: bool,
               on_box=lambda done, name: None) -> tuple[str, str, bytes]:
    """Build every item -> (filename, media type, bytes). Caller holds BUILD_LOCK."""
    built: list[tuple[str, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="schublade_") as tmp:
        work = Path(tmp)
        for n, item in enumerate(items):
            spec = _spec_for(gen, item, height_u)
            on_box(n, gen.filename_stem(spec))
            try:
                built.append(_build_one(gen, spec, work))
            except Exception as exc:              # noqa: BLE001
                raise RuntimeError(f"P{item.pos:03d}: {exc}") from exc
        on_box(len(items), "")

    if keep:
        OUT.mkdir(parents=True, exist_ok=True)
        for name, data in built:
            (OUT / name).write_bytes(data)

    if len(built) == 1:
        name, data = built[0]
        return name, "model/3mf", data
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # 3MFs are already deflated
        for name, data in built:
            zf.writestr(name, data)
    return f"schublade_3mf_{height_u}U.zip", "application/zip", buf.getvalue()


@app.post("/api/generate")
def generate(req: BuildRequest):
    """Synchronous build, for scripts. The editor uses /api/jobs: a long batch outlives
    HTTP proxies (GitHub's port forwarding drops the request after about a minute)."""
    gen = generator()
    _check_request(gen, req.items, req.height_u)
    with BUILD_LOCK:
        try:
            if req.fetch_missing:
                _fetch_missing(gen, req.items)
            elif not {i.pos for i in req.items} <= cached_labels():
                raise RuntimeError("label artwork missing and fetch_missing is false")
            name, media, data = _build_all(gen, req.items, req.height_u, req.keep)
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
    return json.dumps([req.height_u, req.keep, [[i.pos, i.w, i.d] for i in req.items]])


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

        def on_box(done: int, name: str) -> None:
            job.update(done=done, current=name)

        job.update(phase="build", done=0, total=len(req.items), current="")
        name, media, data = _build_all(gen, req.items, req.height_u, req.keep, on_box)

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
    _check_request(gen, req.items, req.height_u)
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

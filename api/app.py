"""Build service for the Schraubenschublade planner.

Wraps the existing CadQuery pipeline so the editor can ask for the real thing:
a body with the label pocket cut out, plus the Chappel label inlay, exported as a
two-part 3MF. Scraped label artwork is cached under CACHE_DIR and reused.

The editor works without this service; it is the optional "cad" compose profile.
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

CACHE = Path(os.environ.get("CACHE_DIR", "/data/cache"))
OUT = Path(os.environ.get("OUT_DIR", "/data/out"))
GENERATOR = Path("/app/generator/generate_gridfinity_chappel_bins.py")
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


def _build_one(gen, spec: dict, work: Path) -> tuple[str, bytes]:
    """Body with the label pocket cut + label inlay -> one two-part 3MF."""
    import trimesh

    stem = gen.filename_stem(spec)
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
    return out_path.name, out_path.read_bytes()


@app.post("/api/generate")
def generate(req: BuildRequest):
    gen = generator()
    if not req.items:
        raise HTTPException(400, "no items requested")
    wanted = {i.pos for i in req.items}
    missing = sorted(wanted - cached_labels())
    if missing and req.fetch_missing:
        with BUILD_LOCK:
            try:
                gen.fetch_chappel_labels(CACHE, positions=set(missing))
            except Exception as exc:              # noqa: BLE001
                raise HTTPException(
                    502,
                    f"label artwork for {missing[:6]} is not cached and the scrape failed: {exc}",
                ) from exc
        missing = sorted(wanted - cached_labels())
    if missing:
        raise HTTPException(
            412,
            f"no label artwork for position(s) {missing[:10]}"
            f"{' …' if len(missing) > 10 else ''} — run “Fetch & cache labels”.",
        )

    built: list[tuple[str, bytes]] = []
    with BUILD_LOCK, tempfile.TemporaryDirectory(prefix="schublade_") as tmp:
        work = Path(tmp)
        for item in req.items:
            spec = _spec_for(gen, item, req.height_u)
            try:
                built.append(_build_one(gen, spec, work))
            except HTTPException:
                raise
            except Exception as exc:              # noqa: BLE001
                raise HTTPException(500, f"P{item.pos:03d}: {exc}") from exc

    if req.keep:
        OUT.mkdir(parents=True, exist_ok=True)
        for name, data in built:
            (OUT / name).write_bytes(data)

    if len(built) == 1:
        name, data = built[0]
        return Response(data, media_type="model/3mf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # 3MFs are already deflated
        for name, data in built:
            zf.writestr(name, data)
    zipname = f"schublade_3mf_{req.height_u}U.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{zipname}"'})


@app.get("/api/space")
def space():
    usage = shutil.disk_usage(str(CACHE if CACHE.exists() else Path("/")))
    return {"free_mb": usage.free // 1048576}

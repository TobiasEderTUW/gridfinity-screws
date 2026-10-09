#!/usr/bin/env python3
"""Check that the fast mesh label path builds the same solids as the CadQuery path.

For every case: build the clip-on label both ways (make_mw_label -> CadQuery booleans,
make_mw_label_mesh -> cached tray + manifold3d pocket cut) and compare
  * the symmetric difference volume of plate and inlay (what is in one solid but not the other),
  * volumes, bounding boxes, watertightness, edges not shared by exactly two triangles,
  * build time.
Run inside the api container:
    docker compose exec api python /app/tools/verify_label_mesh.py      (tools/ mounted or copied)
"""
from __future__ import annotations

import sys
import time

import numpy as np
import trimesh

sys.path.insert(0, "/app/generator")
import generate_gridfinity_chappel_bins as g   # noqa: E402
import label_art as la                          # noqa: E402


def bad_edges(m: trimesh.Trimesh) -> int:
    f = m.faces
    e = np.sort(np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, n = np.unique(e, axis=0, return_counts=True)
    return int((n != 2).sum())


def sym_diff(a: trimesh.Trimesh, b: trimesh.Trimesh) -> float:
    ab = trimesh.boolean.difference([a, b], engine="manifold")
    ba = trimesh.boolean.difference([b, a], engine="manifold")
    return abs(ab.volume) + abs(ba.volume)


def cases():
    base = dict(pos=0, text=None, grid_w=2, grid_d=1, height_u=8, norm='', label_depth=10.0,
                label_fit=0.05, label_side='front', label_detent=False)
    for key, p in la.parts().items():
        yield {**base, 'part': key, 'thread': 'M6', 'length': 30 if p['category'] == 'bolt' else None}
    extra = [
        dict(part='allen__countersunk', thread='M6', length=80, grid_w=1, grid_d=3, label_side='back'),
        dict(part='hex__hex', thread='M10', length=80, grid_d=3, label_detent=True),
        dict(part='torx__pan_wood', thread='4.0', length=30, text='4.0×30 Spax', label_depth=19.0),
        dict(part='internal_lock_washer', thread='M8', length=None, label_fit=-0.1, label_detent=True, label_side='back'),
        dict(part='phillips__pan', thread='M3', length=8, grid_w=3, grid_d=2, label_depth=6.0),
    ]
    for e in extra:
        yield {**base, **e}


def main() -> int:
    worst = 0.0
    t_cq = t_mesh = 0.0
    fails = []
    for sp in cases():
        name = f"{g.screw_name(sp)} {sp['grid_w']}x{sp['grid_d']} d{sp['label_depth']:g} fit{sp['label_fit']:g}" \
               f"{' det' if sp['label_detent'] else ''} {sp['label_side']}"
        t = time.time()
        plate_cq, inlay_cq = (g._to_mesh(x, 0.05, 0.18) for x in g.make_mw_label(sp))
        t_cq += time.time() - t
        t = time.time()
        plate_m, inlay_m = g.make_mw_label_mesh(sp)
        t_mesh += time.time() - t
        d_plate, d_inlay = sym_diff(plate_cq, plate_m), sym_diff(inlay_cq, inlay_m)
        bbox = float(np.abs(plate_cq.bounds - plate_m.bounds).max())
        ok = (d_plate < 0.05 and d_inlay < 0.05 and bbox < 0.01 and plate_m.is_watertight
              and inlay_m.is_watertight and bad_edges(plate_m) == 0 and bad_edges(inlay_m) == 0)
        worst = max(worst, d_plate, d_inlay)
        print(f"{'OK ' if ok else 'BAD'} {name:62s} plate {plate_m.volume:8.2f} mm3 (diff {d_plate:.4f}) "
              f"inlay {inlay_m.volume:6.2f} (diff {d_inlay:.4f}) bbox {bbox:.4f}", flush=True)
        if not ok:
            fails.append(name)
    print(f"\n{len(fails)} failed; worst symmetric difference {worst:.4f} mm3; "
          f"CadQuery {t_cq:.0f} s, mesh {t_mesh:.0f} s")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

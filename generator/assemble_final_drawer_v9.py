#!/usr/bin/env python3
"""Assemble the finished screw drawer - v9, uniform 8U, volume-checked.

* EVERY bin is 8U = 56 mm high - uniform height is a hard requirement.  At 7U the
  15.04.2026 order cannot be stored at a sane fill level without falling back to
  1-cell-wide bins; 56 mm clears the 75 mm drawer with 19 mm to spare.
* Bin volume comes from DIN/ISO head tables plus a 45 % loose packing density.  The
  ordered quantity fills at most 80 % of the usable cavity (label ramp deducted).  The
  only exception is 6kt M10x80 at 81 %, which is already on the largest allowed
  footprint.  300 of the 350 cells are in use.
* Every bin is two cells wide (2x1, 2x2, 2x3), so a column holds exactly one bin and
  every label shelf is a wide 84 mm.
* Inside a type block the size-sorted sequence is dealt across all columns at once, so
  the diameters form bands ACROSS the drawer: M10/M8 at the back, M3 at the front edge.
* Every column is anchored to the front; leftover cells sit at the very back.
* Four type blocks plus a spare column at the right edge:

        |   SK   |   LK   |     ZK     |    6kt    | R |
        |   4    |   4    |     8      |     8     | 1 |   = 25 columns

The 20 chipboard screws of the 2026 order (pos. 117-136) are not part of this set.
"""

from __future__ import annotations
import argparse
import csv
import json
import math
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import trimesh
import cadquery as cq

# -------- configuration --------
DEFAULT_SOURCE_ZIP = Path('/mnt/data/test_chappel(1).zip')
DEFAULT_OUT_DIR = Path('/mnt/data/final_drawer_model_v9')

CELL = 42.0
GRID_W = 25
GRID_D = 14
DRAWER_INNER_W = 1077.0
DRAWER_INNER_D = 602.0
DRAWER_INNER_H = 75.0
WALL = 2.0
BOTTOM = 2.0
GRID_OFFSET_X = (DRAWER_INNER_W - GRID_W * CELL) / 2.0
GRID_OFFSET_Y = (DRAWER_INNER_D - GRID_D * CELL) / 2.0
FLOOR_Z = BOTTOM
EXPECTED_BINS = 116
EXPECTED_CELLS = 300
BIN_HEIGHT_U = 8        # hard requirement: every bin the same height
GEN_VERSION = '2026-09-13-drawer-v9-uniform-8U'

TYPE_NAMES = {'SK': 'Senkkopf', 'LK': 'Linsenkopf', 'ZK': 'Zylinderkopf', '6kt': 'Sechskant'}
TYPE_COLORS = {
    'SK': [188, 217, 240, 255],
    'LK': [237, 225, 184, 255],
    'ZK': [191, 229, 184, 255],
    '6kt': [239, 195, 186, 255],
}
LABEL_COLOR = [250, 250, 250, 255]
SCREW_COLOR = [175, 180, 186, 255]
DRAWER_COLOR = [228, 231, 235, 255]
RESERVE_COLOR = [213, 221, 225, 255]

# Full-depth type blocks, in left-to-right order.  Widths must sum to at most GRID_W
# and must be even, because a column is one 2-cell wide bin.  Columns left over at the
# right edge are reserve.
ZONE_ORDER = ['SK', 'LK', 'ZK', '6kt']
ZONE_WIDTH = {'SK': 4, 'LK': 4, 'ZK': 8, '6kt': 8}
BIN_WIDTH = 2          # every bin is two cells wide in this revision


def zone_x0() -> dict[str, int]:
    x, out = 0, {}
    for kind in ZONE_ORDER:
        out[kind] = x
        x += ZONE_WIDTH[kind]
    if x > GRID_W:
        raise SystemExit(f'zone widths sum to {x} columns, the drawer has {GRID_W}')
    return out


# -------- generic helpers --------

def cq_to_trimesh(obj, tolerance=0.12, ang_tolerance=0.12):
    shape = obj.val() if hasattr(obj, 'val') else obj
    verts, faces = shape.tessellate(tolerance, ang_tolerance)
    vertices = np.array([(v.x, v.y, v.z) for v in verts], dtype=float)
    faces = np.array(faces, dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    mesh.fix_normals()
    return mesh


def mesh_from_file(path):
    mesh = trimesh.load_mesh(path, force='mesh')
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(tuple(mesh.geometry.values()))
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    mesh.fix_normals()
    return mesh


def make_drawer_shell():
    outer = cq.Workplane('XY').box(DRAWER_INNER_W + 2*WALL, DRAWER_INNER_D + 2*WALL,
                                   DRAWER_INNER_H + BOTTOM, centered=(False, False, False))
    cavity = (cq.Workplane('XY').transformed(offset=(WALL, WALL, BOTTOM))
              .box(DRAWER_INNER_W, DRAWER_INNER_D, DRAWER_INNER_H, centered=(False, False, False)))
    return cq_to_trimesh(outer.cut(cavity))


def regular_hex_polygon(af):
    r = af / math.sqrt(3.0)  # circumradius for flat-to-flat af
    return [(r * math.cos(math.pi/6.0 + i * math.pi/3.0),
             r * math.sin(math.pi/6.0 + i * math.pi/3.0)) for i in range(6)]


def common_head_dims(kind, d):
    if kind == 'SK':
        return {'head_d': 2.0*d, 'head_h': 0.55*d}
    if kind == 'ZK':
        return {
            'head_d': {3: 5.5, 4: 7.0, 5: 8.5, 6: 10.0, 8: 13.0, 10: 16.0}.get(d, 1.6*d),
            'head_h': {3: 3.0, 4: 4.0, 5: 5.0, 6: 6.0, 8: 8.0, 10: 10.0}.get(d, d),
        }
    if kind == 'LK':
        return {'head_d': 2.0*d, 'head_h': 0.65*d}
    if kind == '6kt':
        return {
            'head_af': {3: 5.5, 4: 7.0, 5: 8.0, 6: 10.0, 8: 13.0, 10: 17.0}.get(d, 1.7*d),
            'head_h': {3: 2.0, 4: 2.8, 5: 3.5, 6: 4.0, 8: 5.3, 10: 6.4}.get(d, 0.65*d),
        }
    raise ValueError(kind)


def build_screw(kind, d, L):
    # Clean reference models: one watertight solid, no detached tip pieces.
    if kind == 'SK':
        dims = common_head_dims(kind, d)
        hd, hh = dims['head_d'], min(dims['head_h'], L*0.45)
        shaft_L = max(L - hh, 0.8*d)
        shank = cq.Workplane('YZ').circle(d/2).extrude(shaft_L)
        head = (cq.Workplane('YZ').workplane(offset=shaft_L).circle(d/2)
                .workplane(offset=hh).circle(hd/2).loft(combine=True))
        solid = shank.union(head)
    elif kind == 'ZK':
        dims = common_head_dims(kind, d)
        hd, hh = dims['head_d'], dims['head_h']
        head = cq.Workplane('YZ').circle(hd/2).extrude(hh)
        shank = cq.Workplane('YZ').workplane(offset=hh).circle(d/2).extrude(L)
        solid = head.union(shank)
    elif kind == 'LK':
        dims = common_head_dims(kind, d)
        hd, hh = dims['head_d'], dims['head_h']
        cyl_h = hh * 0.45
        dome_h = hh - cyl_h
        head_cyl = cq.Workplane('YZ').circle(hd/2).extrude(cyl_h)
        head_dome = (cq.Workplane('YZ').workplane(offset=cyl_h).circle(hd/2)
                     .workplane(offset=dome_h).circle(hd*0.65/2).loft(combine=True))
        shank = cq.Workplane('YZ').workplane(offset=hh).circle(d/2).extrude(L)
        solid = head_cyl.union(head_dome).union(shank)
    elif kind == '6kt':
        dims = common_head_dims(kind, d)
        af, hh = dims['head_af'], dims['head_h']
        head = cq.Workplane('YZ').polyline(regular_hex_polygon(af)).close().extrude(hh)
        shank = cq.Workplane('YZ').workplane(offset=hh).circle(d/2).extrude(L)
        solid = head.union(shank)
    else:
        raise ValueError(kind)

    mesh = cq_to_trimesh(solid, tolerance=0.10, ang_tolerance=0.10)
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    mesh.fix_normals()

    # Normalize so min-X = 0 and the screw is centered in Y/Z.
    minb, maxb = mesh.bounds
    mesh.apply_translation([-minb[0], -(minb[1]+maxb[1])/2.0, -(minb[2]+maxb[2])/2.0])
    return mesh

# -------- parse generated box set --------
RX = re.compile(r'^P(\d+)_([^_]+)_M?(\d+)x(\d+)_Q(\d+)_([0-9]+)x([0-9]+)_([0-9]+)U_(BODY|LABEL_INLAY)\.stl$')


def load_items(source_zip: Path, work: Path) -> list[dict]:
    with zipfile.ZipFile(source_zip) as z:
        z.extractall(work)

    bodies, labels = {}, {}
    for folder, bucket in [('bodies_stl', bodies), ('labels_inlay_stl', labels)]:
        d = work / folder
        for fn in os.listdir(d):
            m = RX.match(fn)
            if not m:
                continue
            pos, kind, dia, length, qty, w, h, u, _suffix = m.groups()
            bucket[int(pos)] = {
                'pos': int(pos), 'kind': kind, 'dia': int(dia), 'length': int(length),
                'qty': int(qty), 'grid_w': int(w), 'grid_d': int(h), 'u': int(u),
                'path': str(d / fn), 'file': fn,
            }

    items = []
    for pos in sorted(bodies):
        rec = dict(bodies[pos])
        label = labels.get(pos)
        rec['label_path'] = label['path'] if label else None
        rec['label_file'] = label['file'] if label else ''
        items.append(rec)
    return items


def check_bins(items: list[dict]) -> None:
    """Reject a box set that does not match this revision, with a precise message."""
    bad_width = [it for it in items if it['grid_w'] != BIN_WIDTH]
    if bad_width:
        sample = ', '.join(f"P{it['pos']:03d} {it['grid_w']}x{it['grid_d']}" for it in bad_width[:8])
        raise SystemExit(
            f'{len(bad_width)} bin(s) are not {BIN_WIDTH} cells wide, e.g. {sample}.\n'
            'Re-run the generator: this layout expects 2x1, 2x2 and 2x3 bins only.')
    bad_depth = [it for it in items if it['grid_d'] not in (1, 2, 3)]
    if bad_depth:
        sample = ', '.join(f"P{it['pos']:03d} {it['grid_w']}x{it['grid_d']}" for it in bad_depth[:8])
        raise SystemExit(f'bin(s) deeper than 3 cells: {sample}')
    bad_height = [it for it in items if it['u'] != BIN_HEIGHT_U]
    if bad_height:
        sample = ', '.join(f"P{it['pos']:03d} {it['u']}U" for it in bad_height[:8])
        raise SystemExit(
            f'every bin must be {BIN_HEIGHT_U}U; found {sample}. Regenerate the box set.')


# -------- layout --------

def _sort_key(b: dict):
    """Reading order inside a block: biggest diameter first, then longest first."""
    return (-b['dia'], -b['length'], b['pos'])


def pack_zone(bins: list[dict], width: int, depth: int = GRID_D):
    """Deal the size-sorted bins across the columns of one block.

    Returns [(bin, x, row)] relative to the block, row 0 = back.

    Each bin goes into the column that is currently the shallowest, so all columns grow
    at the same rate and a diameter ends up as a band of rows across the block instead of
    being buried in one column.  Ties go to the left, which keeps the reading direction.
    Every column is then pushed forward, so its last bin ends on the front row and the
    unused depth stays at the back.
    """
    if width % 2:
        raise SystemExit(f'block width {width} is odd, but every bin is {BIN_WIDTH} cells wide')
    ncols = width // BIN_WIDTH
    columns: list[list[dict]] = [[] for _ in range(ncols)]
    used = [0] * ncols

    for b in sorted(bins, key=_sort_key):
        if b['grid_d'] > depth:
            raise RuntimeError(f'P{b["pos"]:03d} is {b["grid_d"]} cells deep, the drawer has {depth}')
        index = min(range(ncols), key=lambda i: (used[i], i))
        if used[index] + b['grid_d'] > depth:
            raise RuntimeError(
                f'block of {width} columns is full at P{b["pos"]:03d}; '
                f'column depths {used}')
        columns[index].append(b)
        used[index] += b['grid_d']

    placed = []
    for index, column in enumerate(columns):
        row = depth - used[index]          # front-anchor the stack
        for b in column:
            placed.append((b, BIN_WIDTH * index, row))
            row += b['grid_d']
    return placed


def build_layout(items: list[dict]) -> list[dict]:
    """Return the item dicts enriched with absolute grid_x / grid_y (y = 0 at the front)."""
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_kind[it['kind']].append(it)
    unknown = set(by_kind) - set(ZONE_WIDTH)
    if unknown:
        raise SystemExit(f'No zone defined for screw type(s): {sorted(unknown)}')

    x0 = zone_x0()
    out = []
    for kind in ZONE_ORDER:
        for b, x, row in pack_zone(by_kind[kind], ZONE_WIDTH[kind]):
            out.append({**b,
                        'zone': kind,
                        'grid_x': x0[kind] + x,
                        'grid_y': GRID_D - (row + b['grid_d'])})
    return out


def occupancy(placements: list[dict]) -> dict[tuple[int, int], int]:
    occ: dict[tuple[int, int], int] = {}
    for p in placements:
        for dx in range(p['grid_w']):
            for dy in range(p['grid_d']):
                cell = (p['grid_x'] + dx, p['grid_y'] + dy)
                if not (0 <= cell[0] < GRID_W and 0 <= cell[1] < GRID_D):
                    raise RuntimeError(f'P{p["pos"]:03d} leaves the drawer grid at {cell}')
                if cell in occ:
                    raise RuntimeError(f'cell {cell}: P{p["pos"]:03d} overlaps P{occ[cell]:03d}')
                occ[cell] = p['pos']
    return occ


def free_rectangles(occ: dict[tuple[int, int], int]) -> list[tuple[str, int, int, int, int]]:
    """Greedily merge the unused cells of every block into maximal rectangles."""
    x0 = zone_x0()
    spans = {k: range(x0[k], x0[k] + ZONE_WIDTH[k]) for k in ZONE_ORDER}
    used_w = sum(ZONE_WIDTH.values())
    if used_w < GRID_W:
        spans['reserve'] = range(used_w, GRID_W)
    rects = []
    for kind, span in spans.items():
        free = {(x, y) for x in span for y in range(GRID_D) if (x, y) not in occ}
        while free:
            sx, sy = min(free, key=lambda c: (c[1], c[0]))
            rw = 0
            while (sx + rw, sy) in free:
                rw += 1
            rh = 1
            while all((sx + i, sy + rh) in free for i in range(rw)):
                rh += 1
            for i in range(rw):
                for j in range(rh):
                    free.discard((sx + i, sy + j))
            rects.append((kind, sx, sy, rw, rh))
    return rects


# -------- scene assembly --------

def assemble(source_zip: Path, out_dir: Path) -> dict:
    work = Path(tempfile.mkdtemp(prefix='drawer_asm_v9_'))
    out_dir.mkdir(parents=True, exist_ok=True)

    items = load_items(source_zip, work)
    if len(items) != EXPECTED_BINS:
        raise SystemExit(f'Expected {EXPECTED_BINS} bins in {source_zip.name}, found {len(items)}')
    check_bins(items)

    placements = build_layout(items)
    occ = occupancy(placements)
    if len(occ) != EXPECTED_CELLS:
        print(f'note: {len(occ)} cells occupied, expected {EXPECTED_CELLS}')

    scene = trimesh.Scene()
    scene.metadata['units'] = 'mm'

    shell = make_drawer_shell()
    shell.visual.vertex_colors = DRAWER_COLOR
    scene.add_geometry(shell, node_name='drawer_shell', geom_name='drawer_shell')

    # per-zone reserve plates instead of one global reserve column
    for n, (kind, x, y, w, h) in enumerate(free_rectangles(occ), 1):
        plate = (cq.Workplane('XY')
                 .transformed(offset=(WALL + GRID_OFFSET_X + x*CELL,
                                      WALL + GRID_OFFSET_Y + y*CELL, FLOOR_Z))
                 .box(w*CELL, h*CELL, 1.0, centered=(False, False, False)))
        mesh = cq_to_trimesh(plate)
        mesh.visual.vertex_colors = RESERVE_COLOR
        name = f'reserve_{kind}_{n:02d}'
        scene.add_geometry(mesh, node_name=name, geom_name=name)

    manifest_path = out_dir / 'drawer_layout_manifest_v9.csv'
    with manifest_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['position', 'type', 'type_name', 'diameter_mm', 'length_mm', 'qty',
                         'box_w', 'box_d', 'orientation', 'height_u', 'zone',
                         'grid_x', 'grid_y', 'x_mm', 'y_mm', 'z_mm', 'body_file', 'label_file'])

        for p in sorted(placements, key=lambda d: (ZONE_ORDER.index(d['zone']),
                                                   -d['grid_y'], d['grid_x'])):
            body = mesh_from_file(p['path'])
            ext = body.extents
            expect = (p['grid_w']*CELL, p['grid_d']*CELL)
            if abs(ext[2] - p['u'] * 7.0) > 1.0:
                raise SystemExit(
                    f'P{p["pos"]:03d}: mesh is {ext[2]:.1f} mm tall but its name says '
                    f'{p["u"]}U ({p["u"] * 7.0:.0f} mm). Regenerate this bin.')
            if abs(ext[0] - expect[0]) > 1.5 or abs(ext[1] - expect[1]) > 1.5:
                raise SystemExit(
                    f'P{p["pos"]:03d}: mesh is {ext[0]:.1f} x {ext[1]:.1f} mm but its name says '
                    f'{p["grid_w"]}x{p["grid_d"]} cells. Regenerate this bin.')

            tx = WALL + GRID_OFFSET_X + p['grid_x'] * CELL + ext[0] / 2.0
            ty = WALL + GRID_OFFSET_Y + p['grid_y'] * CELL + ext[1] / 2.0
            tz = FLOOR_Z
            body.apply_translation([tx, ty, tz])
            body.visual.vertex_colors = TYPE_COLORS[p['kind']]
            scene.add_geometry(body, node_name=f'body_{p["pos"]:03d}',
                               geom_name=f'body_{p["pos"]:03d}')

            if p['label_path']:
                label = mesh_from_file(p['label_path'])
                label.apply_translation([tx, ty, tz])
                label.visual.vertex_colors = LABEL_COLOR
                scene.add_geometry(label, node_name=f'label_{p["pos"]:03d}',
                                   geom_name=f'label_{p["pos"]:03d}')

            screw = build_screw(p['kind'], p['dia'], p['length'])
            comps = screw.split(only_watertight=False)
            if len(comps) != 1:
                screw = max(comps, key=lambda m: m.volume if m.is_volume else m.area).copy()
                screw.remove_unreferenced_vertices()
                screw.merge_vertices()
                screw.fix_normals()
            # the screw model lies along +X; a portrait bin is long in Y, so turn it
            if p['grid_d'] > p['grid_w']:
                screw.apply_transform(trimesh.transformations.rotation_matrix(
                    math.pi/2, [0, 0, 1]))
                minb, maxb = screw.bounds
                screw.apply_translation([-(minb[0]+maxb[0])/2.0, -minb[1], 0.0])
            s_ext = screw.extents
            # hover it inside the open bin, away from the label edge at the front
            screw_tx = WALL + GRID_OFFSET_X + p['grid_x']*CELL + (ext[0] - s_ext[0]) / 2.0
            screw_ty = (WALL + GRID_OFFSET_Y + p['grid_y']*CELL
                        + (ext[1]*0.67 if p['grid_d'] == 1 else (ext[1] - s_ext[1])/2.0 + 6.0))
            screw_tz = FLOOR_Z + ext[2] + 2.0
            screw.apply_translation([screw_tx, screw_ty, screw_tz])
            screw.visual.vertex_colors = SCREW_COLOR
            scene.add_geometry(screw, node_name=f'screw_{p["pos"]:03d}',
                               geom_name=f'screw_{p["pos"]:03d}')

            writer.writerow([
                p['pos'], p['kind'], TYPE_NAMES[p['kind']], p['dia'], p['length'], p['qty'],
                p['grid_w'], p['grid_d'],
                'landscape' if p['grid_w'] > p['grid_d'] else 'portrait',
                p['u'], p['zone'], p['grid_x'], p['grid_y'],
                round(tx, 3), round(ty, 3), round(tz, 3), p['file'], p['label_file'],
            ])

    rects = free_rectangles(occ)
    zone_free = {k: sum(w*h for kind, _x, _y, w, h in rects if kind == k)
                 for k in list(ZONE_ORDER) + ['reserve']}
    summary = {
        'version': GEN_VERSION,
        'source_zip': source_zip.name,
        'drawer_inner_mm': [DRAWER_INNER_W, DRAWER_INNER_D, DRAWER_INNER_H],
        'grid': [GRID_W, GRID_D],
        'cell_mm': CELL,
        'grid_offset_mm': [GRID_OFFSET_X, GRID_OFFSET_Y],
        'bin_count': len(placements),
        'occupied_cells': len(occ),
        'free_cells': GRID_W*GRID_D - len(occ),
        'free_cells_per_zone': zone_free,
        'uniform_height_u': 7,
        'zones': {k: {'x0': zone_x0()[k], 'width': ZONE_WIDTH[k], 'depth': GRID_D}
                  for k in ZONE_ORDER},
        'footprints': {f'{w}x{d}': sum(1 for p in placements
                                       if (p['grid_w'], p['grid_d']) == (w, d))
                       for w, d in sorted({(p['grid_w'], p['grid_d']) for p in placements})},
        'front_anchored': True,
        'portrait_bins': sum(1 for p in placements if p['grid_d'] > p['grid_w']),
        'landscape_bins': sum(1 for p in placements if p['grid_w'] > p['grid_d']),
        'bin_heights_u': {str(u): sum(1 for p in placements if p['u'] == u)
                          for u in sorted({q['u'] for q in placements})},
        'square_bins': sum(1 for p in placements if p['grid_w'] == p['grid_d']),
    }
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')

    glb_path = out_dir / 'fertige_schublade_gridfinity_v9.glb'
    with glb_path.open('wb') as fh:
        fh.write(scene.export(file_type='glb'))

    merged = trimesh.util.concatenate([g.copy() for g in scene.geometry.values()])
    stl_path = out_dir / 'fertige_schublade_gridfinity_v9_merged.stl'
    merged.export(stl_path)

    bundle = out_dir.parent / 'fertige_schublade_gridfinity_v9_bundle.zip'
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as z:
        for file in [glb_path, stl_path, manifest_path, out_dir / 'summary.json']:
            z.write(file, arcname=file.name)
        this_file = Path(__file__).resolve()
        if this_file.exists():
            z.write(this_file, arcname=this_file.name)

    print('Created:')
    for p in (glb_path, stl_path, manifest_path, bundle):
        print(' ', p)
    print(json.dumps(summary, indent=2))
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--source-zip', type=Path, default=DEFAULT_SOURCE_ZIP,
                    help='zip with bodies_stl/ and labels_inlay_stl/')
    ap.add_argument('--out-dir', type=Path, default=DEFAULT_OUT_DIR)
    args = ap.parse_args()
    assemble(args.source_zip, args.out_dir)


if __name__ == '__main__':
    main()

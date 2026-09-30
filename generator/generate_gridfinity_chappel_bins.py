#!/usr/bin/env python3
"""Generate Gridfinity drawer bins with two-colour labels, for any part type of label.alch.shop.

Everything is offline. The label artwork is composed by label_art.py from files in
generator/assets/: the website's icons-only exports (one per part type) and the font it
uses (HarmonyOS Sans SC, Huawei's unmodified distribution). tools/download_label_assets.py
filled that folder once; neither this script nor the service ever contacts the website.

A spec (one bin) is: part (catalogue key, e.g. 'allen__countersunk'), thread, length (None
for nuts and washers), optional label text (default 'M6×20' / 'M6'), footprint, height, and
the label options (style, edge, depth, fit, detents). The 116 positions of the original
order are the default set below.

Dependencies: cadquery trimesh shapely networkx fonttools

Design choices:
  * Standard 42 mm Gridfinity XY pitch and a standard-compatible 41.5 mm multi-step foot.
  * No stacking lip: these are drawer bins, which gives more clearance.
  * 1.2 mm walls. EVERY bin is 8U = 56 mm high.
  * Footprints are chosen by the user; there is no fill or volume model.
  * One label style: the bin has a lip round its top inside (optionally with detents); a
    separately printed clip-on label (replica of MakerWorld model 431547) sits on the front
    or back edge. Its icon + text outlines are a 0.6 mm deep, flush two-colour inlay.
  * Bins do not depend on their label, so one bin file serves every box of that size.
"""

from __future__ import annotations
import argparse
import csv
import math
import zipfile
import xml.etree.ElementTree as ET
import re
import unicodedata
import tempfile
import numpy as np
from pathlib import Path
import sys
import cadquery as cq
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent))   # the service loads us by path
import label_art  # noqa: E402
try:
    import networkx  # required by trimesh.path when recovering closed label contours
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: networkx\n"
        "Install it in this virtual environment with:\n"
        "    pip install networkx\n"
        "Then run the script again."
    ) from exc

GRID = 42.0
XY_CLEARANCE = 0.5
WALL = 1.2
BODY_RADIUS = 3.75
FOOT_H = 4.75
FLOOR_Z = 7.2
# Which edge carries the label, in plan view: 'front' (the drawer-front side) or 'back'.
# A back label is the front one mirrored in Y; the artwork itself is only moved, never
# rotated, so it still reads from the drawer front.
LABEL_SIDES = ('front', 'back')
DEFAULT_LABEL_SIDE = 'front'

# The one label style since 2026-09-30 (the integrated shelf and the removable snap plate
# were removed on request; see git history before that date): a separately printed
# two-colour label that clips onto a lip round the bin's top inside.
DEFAULT_PLATE_DEPTH = 10.0   # label depth from the inner wall face ("Label width" in the GUI)
PLATE_DEPTH_RANGE = (6.0, 30.0)

# Replica of the snap profile of MakerWorld model 431547 (measured from its
# STL, and the designer's 1.8 / 3.8 mm sketch). The label is an inverted tray: a 1.8 mm face
# plate with a 3.16 mm skirt along both sides and the wall-side edge. Seen in section, the
# skirt's outer face runs (inset from the label edge, depth below the face):
#   (0, 0) -> (1.31, 1.31) -> (1.31, 3.10) -> (0.61, 3.80) -> (0.61, 5.05)
# The bin gets the matching lip all round its top inside; the recess clips round it and the
# 0.61 bump hooks under it. The reference's 0.5 mm sticker rim is left out: printed face down
# it would leave the whole face as a bridge.
MW_PLATE = 1.80
MW_SKIRT = 3.16              # skirt width, measured in from the label edge
MW_DEPTH = 5.05              # skirt reaches this far below the face
MW_P1, MW_P2, MW_P3, MW_P4 = 1.31, 3.10, 3.80, 0.61   # profile breakpoints, see above
DEFAULT_MW_FIT = 0.05        # label profile to the bin lip, per side, mm ("Label fit" in the GUI).
MW_FIT_RANGE = (-0.30, 0.30) # 0.15 let the label slide (user test 2026-09-18); < 0 = press fit
# Optional detents ("Detents" in the GUI): at a few places the lip turns down. A short
# vertical piece with exactly the lip's cross-section (the (inset, depth) profile above, stood
# upright) runs from the lip DETENT_DROP further down the wall, its bottom end chamfered 45
# degrees so it prints without support. The label's skirt gets a matching slot below the lip,
# so the piece sits in it and the label cannot slide. Pieces sit near every corner on all four
# walls; they do not depend on the label, so a printed bin can later take a new label of any
# width on any wall (only front/back labels are generated today).
DETENT_OFFSET = 5.0          # piece centre from the adjacent wall face (edges 2.8..7.2 mm,
                             # clear of the 2.55 mm inner corner radius)
DETENT_DROP = 4.0            # how far the piece runs below the bottom of the lip
DETENT_CLEARANCE = 0.10      # label slot = piece section grown by this
MW_SAFE = 1.5                # artwork margin to the face edges
LABEL_INLAY_DEPTH = 0.60        # 3 layers at 0.20 mm
LABEL_POCKET_BOTTOM_CLEARANCE = 0.02
LABEL_POCKET_OVERTRAVEL = 0.08  # cutter continues above the top face for robust booleans
UNIFORM_HEIGHT_U = 8              # ALL bins are 56 mm high - uniform height is a hard requirement.
                                  # 7U/49 mm cannot hold the 15.04.2026 order at a sane fill level
                                  # without 1-cell-wide bins; 8U clears the 75 mm drawer easily.
SCRIPT_VERSION = "2026-09-30.2-uniform-label-size"
LABEL_MAX_UPSCALE = 1.35         # the artwork may grow at most this much past its 1-wide size


# The default inventory: the 116 metal-screw positions of the 15.04.2026 order.
# part = a label.alch.shop part type (generator/assets/label_catalogue.json); the label
# text defaults to thread x length. Footprints are the ones the old fill model chose; they
# are plain data now, the editor lets you change them.
SPECS = [
    dict(pos=1, part='allen__countersunk', thread='M6', length=16, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=2, part='allen__countersunk', thread='M6', length=20, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=3, part='allen__countersunk', thread='M6', length=25, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=4, part='allen__countersunk', thread='M6', length=30, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=5, part='allen__countersunk', thread='M6', length=40, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=6, part='allen__countersunk', thread='M6', length=80, grid_w=2, grid_d=3, norm='ISO 10642'),
    dict(pos=7, part='allen__countersunk', thread='M3', length=6, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=8, part='allen__countersunk', thread='M3', length=8, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=9, part='allen__countersunk', thread='M3', length=10, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=10, part='allen__countersunk', thread='M3', length=12, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=11, part='allen__countersunk', thread='M3', length=16, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=12, part='allen__countersunk', thread='M3', length=20, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=13, part='allen__countersunk', thread='M4', length=8, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=14, part='allen__countersunk', thread='M4', length=10, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=15, part='allen__countersunk', thread='M4', length=12, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=16, part='allen__countersunk', thread='M4', length=16, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=17, part='allen__countersunk', thread='M4', length=20, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=18, part='allen__countersunk', thread='M4', length=25, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=19, part='allen__countersunk', thread='M4', length=30, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=20, part='allen__countersunk', thread='M5', length=10, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=21, part='allen__countersunk', thread='M5', length=12, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=22, part='allen__countersunk', thread='M5', length=16, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=23, part='allen__countersunk', thread='M5', length=20, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=24, part='allen__countersunk', thread='M5', length=25, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=25, part='allen__countersunk', thread='M5', length=30, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=26, part='allen__countersunk', thread='M5', length=40, grid_w=2, grid_d=1, norm='ISO 10642'),
    dict(pos=27, part='allen__socket', thread='M3', length=6, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=28, part='allen__socket', thread='M3', length=8, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=29, part='allen__socket', thread='M3', length=10, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=30, part='allen__socket', thread='M3', length=12, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=31, part='allen__socket', thread='M3', length=16, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=32, part='allen__socket', thread='M3', length=20, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=33, part='allen__socket', thread='M3', length=25, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=34, part='allen__socket', thread='M3', length=30, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=35, part='allen__socket', thread='M4', length=8, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=36, part='allen__socket', thread='M4', length=10, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=37, part='allen__socket', thread='M4', length=12, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=38, part='allen__socket', thread='M4', length=16, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=39, part='allen__socket', thread='M4', length=20, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=40, part='allen__socket', thread='M4', length=25, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=41, part='allen__socket', thread='M4', length=30, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=42, part='allen__socket', thread='M4', length=40, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=43, part='allen__socket', thread='M5', length=10, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=44, part='allen__socket', thread='M5', length=12, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=45, part='allen__socket', thread='M5', length=16, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=46, part='allen__socket', thread='M5', length=20, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=47, part='allen__socket', thread='M5', length=25, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=48, part='allen__socket', thread='M5', length=30, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=49, part='allen__socket', thread='M5', length=40, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=50, part='allen__socket', thread='M5', length=50, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=51, part='allen__socket', thread='M6', length=16, grid_w=2, grid_d=1, norm='DIN 912'),
    dict(pos=52, part='allen__socket', thread='M6', length=20, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=53, part='allen__socket', thread='M6', length=25, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=54, part='allen__socket', thread='M6', length=30, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=55, part='allen__socket', thread='M6', length=40, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=56, part='allen__socket', thread='M6', length=50, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=57, part='allen__socket', thread='M6', length=60, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=58, part='allen__socket', thread='M8', length=20, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=59, part='allen__socket', thread='M8', length=30, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=60, part='allen__socket', thread='M8', length=50, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=61, part='allen__socket', thread='M10', length=30, grid_w=2, grid_d=2, norm='DIN 912'),
    dict(pos=62, part='allen__socket', thread='M10', length=50, grid_w=2, grid_d=3, norm='DIN 912'),
    dict(pos=63, part='phillips__pan', thread='M3', length=6, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=64, part='phillips__pan', thread='M3', length=8, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=65, part='phillips__pan', thread='M3', length=10, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=66, part='phillips__pan', thread='M3', length=12, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=67, part='phillips__pan', thread='M3', length=16, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=68, part='phillips__pan', thread='M3', length=20, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=69, part='phillips__pan', thread='M4', length=8, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=70, part='phillips__pan', thread='M4', length=10, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=71, part='phillips__pan', thread='M4', length=12, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=72, part='phillips__pan', thread='M4', length=16, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=73, part='phillips__pan', thread='M4', length=20, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=74, part='phillips__pan', thread='M4', length=25, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=75, part='phillips__pan', thread='M5', length=10, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=76, part='phillips__pan', thread='M5', length=12, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=77, part='phillips__pan', thread='M5', length=16, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=78, part='phillips__pan', thread='M5', length=20, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=79, part='phillips__pan', thread='M5', length=25, grid_w=2, grid_d=2, norm='DIN 7985'),
    dict(pos=80, part='phillips__pan', thread='M5', length=30, grid_w=2, grid_d=2, norm='DIN 7985'),
    dict(pos=81, part='phillips__pan', thread='M6', length=16, grid_w=2, grid_d=1, norm='DIN 7985'),
    dict(pos=82, part='phillips__pan', thread='M6', length=20, grid_w=2, grid_d=2, norm='DIN 7985'),
    dict(pos=83, part='phillips__pan', thread='M6', length=25, grid_w=2, grid_d=2, norm='DIN 7985'),
    dict(pos=84, part='phillips__pan', thread='M6', length=30, grid_w=2, grid_d=2, norm='DIN 7985'),
    dict(pos=85, part='hex__hex', thread='M3', length=8, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=86, part='hex__hex', thread='M3', length=10, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=87, part='hex__hex', thread='M3', length=12, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=88, part='hex__hex', thread='M3', length=16, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=89, part='hex__hex', thread='M3', length=20, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=90, part='hex__hex', thread='M4', length=10, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=91, part='hex__hex', thread='M4', length=12, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=92, part='hex__hex', thread='M4', length=16, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=93, part='hex__hex', thread='M4', length=20, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=94, part='hex__hex', thread='M4', length=25, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=95, part='hex__hex', thread='M4', length=30, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=96, part='hex__hex', thread='M5', length=12, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=97, part='hex__hex', thread='M5', length=16, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=98, part='hex__hex', thread='M5', length=20, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=99, part='hex__hex', thread='M5', length=25, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=100, part='hex__hex', thread='M5', length=30, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=101, part='hex__hex', thread='M5', length=40, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=102, part='hex__hex', thread='M5', length=50, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=103, part='hex__hex', thread='M6', length=16, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=104, part='hex__hex', thread='M6', length=20, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=105, part='hex__hex', thread='M6', length=25, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=106, part='hex__hex', thread='M6', length=30, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=107, part='hex__hex', thread='M6', length=40, grid_w=2, grid_d=2, norm='DIN 933'),
    dict(pos=108, part='hex__hex', thread='M6', length=50, grid_w=2, grid_d=2, norm='DIN 933'),
    dict(pos=109, part='hex__hex', thread='M6', length=60, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=110, part='hex__hex', thread='M8', length=20, grid_w=2, grid_d=1, norm='DIN 933'),
    dict(pos=111, part='hex__hex', thread='M8', length=30, grid_w=2, grid_d=2, norm='DIN 933'),
    dict(pos=112, part='hex__hex', thread='M8', length=50, grid_w=2, grid_d=2, norm='DIN 933'),
    dict(pos=113, part='hex__hex', thread='M8', length=80, grid_w=2, grid_d=3, norm='DIN 933'),
    dict(pos=114, part='hex__hex', thread='M10', length=30, grid_w=2, grid_d=2, norm='DIN 933'),
    dict(pos=115, part='hex__hex', thread='M10', length=50, grid_w=2, grid_d=3, norm='DIN 933'),
    dict(pos=116, part='hex__hex', thread='M10', length=80, grid_w=2, grid_d=3, norm='DIN 933'),
]


BIN_HEIGHT_U = UNIFORM_HEIGHT_U

for _spec in SPECS:
    _spec['height_u'] = BIN_HEIGHT_U


# Filament (extruder) slots baked into the exported 3MF part settings.
BODY_FILAMENT = 1
LABEL_FILAMENT = 2


def export_parts_3mf(parts: list[tuple[str, trimesh.Trimesh, int]],
                     output_path: Path, assembly_name: str) -> None:
    """Write a self-contained 3MF with one assembly of material-addressable parts.

    parts = [(name, mesh, filament)], one or more.

    This intentionally does NOT use trimesh.exchange.threemf.export_3MF, because that
    exporter may require optional lxml internals and previously failed with
    ``NameError: etree is not defined``. The package below uses only ElementTree + zipfile.

    Result:
      objects 1..n = the parts
      object n+1   = assembly containing 1..n
      build        = one item referencing object n+1
    """
    core_ns = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
    rel_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    ct_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    ET.register_namespace('', core_ns)

    model = ET.Element(f'{{{core_ns}}}model', {'unit': 'millimeter'})
    resources = ET.SubElement(model, f'{{{core_ns}}}resources')

    def add_mesh_object(object_id: int, name: str, mesh: trimesh.Trimesh) -> None:
        mesh = mesh.copy()
        mesh.remove_unreferenced_vertices()
        obj = ET.SubElement(resources, f'{{{core_ns}}}object', {
            'id': str(object_id), 'name': name, 'type': 'model'
        })
        mesh_el = ET.SubElement(obj, f'{{{core_ns}}}mesh')
        vertices_el = ET.SubElement(mesh_el, f'{{{core_ns}}}vertices')
        for x, y, z in np.asarray(mesh.vertices, dtype=float):
            ET.SubElement(vertices_el, f'{{{core_ns}}}vertex', {
                'x': f'{x:.6f}', 'y': f'{y:.6f}', 'z': f'{z:.6f}'
            })
        triangles_el = ET.SubElement(mesh_el, f'{{{core_ns}}}triangles')
        for a, b, c in np.asarray(mesh.faces, dtype=np.int64):
            ET.SubElement(triangles_el, f'{{{core_ns}}}triangle', {
                'v1': str(int(a)), 'v2': str(int(b)), 'v3': str(int(c))
            })

    for n, (name, mesh, _filament) in enumerate(parts, 1):
        add_mesh_object(n, name, mesh)
    parent_id = str(len(parts) + 1)

    parent = ET.SubElement(resources, f'{{{core_ns}}}object', {
        'id': parent_id, 'name': assembly_name, 'type': 'model'
    })
    components = ET.SubElement(parent, f'{{{core_ns}}}components')
    for n in range(1, len(parts) + 1):
        ET.SubElement(components, f'{{{core_ns}}}component', {'objectid': str(n)})

    build = ET.SubElement(model, f'{{{core_ns}}}build')
    ET.SubElement(build, f'{{{core_ns}}}item', {
        'objectid': parent_id, 'partnumber': assembly_name
    })
    model_xml = ET.tostring(model, encoding='utf-8', xml_declaration=True)

    # NOTE: ET.register_namespace('', ...) can hold the empty prefix for only ONE
    # URI at a time (last call wins), and it is already used for the 3MF core
    # namespace above. Building these two OPC parts with ElementTree would emit
    # ns0: prefixes, which BambuStudio's relationship parser cannot read
    # (expat is created without namespace processing and compares raw tag names),
    # so the whole file fails to load. They are fixed content -> emit literals.
    content_types_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        b'</Types>'
    )
    rel_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        b'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        b'</Relationships>'
    )

    # BambuStudio part settings: part id == the 3MF object id of the component.
    # LABEL_INLAY is pre-assigned to filament 2 so the lettering/pictogram prints
    # in a second color without touching the GUI. This file is parsed even though
    # the package is not a Bambu project.
    def _xa(value: str) -> str:
        return (str(value).replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))

    part_xml = ''.join(
        f'    <part id="{n}" subtype="normal_part">\n'
        f'      <metadata key="name" value="{_xa(name)}"/>\n'
        f'      <metadata key="extruder" value="{filament}"/>\n'
        f'      <mesh_stat face_count="{len(mesh.faces)}" edges_fixed="0" degenerate_facets="0"'
        ' facets_removed="0" facets_reserved="0" backwards_edges="0"/>\n'
        '    </part>\n'
        for n, (name, mesh, filament) in enumerate(parts, 1))
    model_settings_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n'
        f'  <object id="{parent_id}">\n'
        f'    <metadata key="name" value="{_xa(assembly_name)}"/>\n'
        '    <metadata key="extruder" value="1"/>\n'
        + part_xml +
        '  </object>\n'
        '</config>\n'
    ).encode('utf-8')

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
        zout.writestr('[Content_Types].xml', content_types_xml)
        zout.writestr('_rels/.rels', rel_xml)
        zout.writestr('3D/3dmodel.model', model_xml)
        zout.writestr('Metadata/model_settings.config', model_settings_xml)


def rounded_prism(width: float, depth: float, radius: float, height: float) -> cq.Workplane:
    radius = min(radius, width/2-0.01, depth/2-0.01)
    sketch = cq.Sketch().rect(width, depth).vertices().fillet(radius)
    return cq.Workplane('XY').placeSketch(sketch).extrude(height)


def make_gridfinity_foot() -> cq.Workplane:
    # Reference foot: 0.8 mm lower 45deg chamfer + 1.8 mm vertical + 2.15 mm upper 45deg chamfer.
    # Top footprint is 41.5 mm; bottom is 35.6 mm.
    lower = rounded_prism(37.2, 37.2, 1.6, 0.8).edges('<Z').chamfer(0.79)
    middle = rounded_prism(37.2, 37.2, 1.6, 1.8).translate((0,0,0.8))
    upper = rounded_prism(41.5, 41.5, 3.75, 2.15).translate((0,0,2.6)).edges('<Z').chamfer(2.14)
    return lower.union(middle).union(upper)


def label_side(spec: dict) -> str:
    side = spec.get('label_side', DEFAULT_LABEL_SIDE)
    if side not in LABEL_SIDES:
        raise ValueError(f"label_side must be one of {LABEL_SIDES}, got {side!r}")
    return side


def _polygon_to_cq_solid(poly, depth: float) -> cq.Shape:
    def wire(coords):
        pts = [cq.Vector(float(x), float(y), 0) for x, y in list(coords)[:-1]]
        return cq.Wire.makePolygon(pts, close=True)
    outer = wire(poly.exterior.coords)
    holes = [wire(r.coords) for r in poly.interiors]
    return cq.Solid.extrudeLinear(outer, holes, cq.Vector(0, 0, depth))


def part_key(spec: dict) -> str:
    key = spec['part']
    label_art.part(key)            # raises on an unknown part type
    return key


def label_text(spec: dict) -> str:
    """What the label says: the spec's own text, else 'M6×20' (screws) / 'M6' (nuts, washers)."""
    text = (spec.get('text') or '').strip()
    return text or label_art.default_text(part_key(spec), spec['thread'], spec.get('length'))


def _label_geometry(spec: dict, base_z: float, thickness: float,
                    area: tuple[float, float, float]) -> cq.Workplane:
    """The label artwork (icons + text), fitted into a safe area and extruded between
    base_z and base_z + thickness. area = (centre_y, safe_w, safe_h).

    The scale depends on the label height only (standard icon height into safe_h), never on
    the text, so every label of one label width has the same icon and text size. A text too
    long for the box is condensed (label_art.compose), not the whole label shrunk.
    """
    shelf_y, safe_w, safe_h = area
    scale = min(safe_h / label_art.ICON_H, LABEL_MAX_UPSCALE)
    polygons = label_art.compose(part_key(spec), label_text(spec), max_width=safe_w / scale)
    graphic = cq.Workplane(obj=cq.Compound.makeCompound(
        [_polygon_to_cq_solid(p, thickness) for p in polygons]))

    shape = graphic.val()
    # Scale only in XY. Z must remain exactly the requested inlay/pocket depth.
    shape = shape.transformGeometry(cq.Matrix([
        [scale, 0, 0, 0],
        [0, scale, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
    ]))
    bb = shape.BoundingBox()
    dx = -(bb.xmin + bb.xmax)/2
    dy = shelf_y                   # the icon line (plate y = 0) on the label's centre line, so a
    dz = base_z - bb.zmin          # shorter icon (wing nut) sits centred like every other
    shape = shape.translate(cq.Vector(dx, dy, dz))
    result = cq.Workplane(obj=shape)

    bb = result.val().BoundingBox()
    assert bb.xmin >= -safe_w/2 - 0.05 and bb.xmax <= safe_w/2 + 0.05, (spec, bb.xmin, bb.xmax, safe_w)
    assert bb.ymin >= shelf_y-safe_h/2-0.05 and bb.ymax <= shelf_y+safe_h/2+0.05, (spec, bb.ymin, bb.ymax, shelf_y, safe_h)
    return result


def make_bin_shell(grid_w: int, grid_d: int, height_u: int) -> cq.Workplane:
    """Walls, floor and Gridfinity feet; no label feature."""
    outer_x = GRID * grid_w - XY_CLEARANCE
    outer_y = GRID * grid_d - XY_CLEARANCE
    top_z = height_u * 7.0
    outer = rounded_prism(outer_x, outer_y, BODY_RADIUS, top_z - FOOT_H).translate((0,0,FOOT_H))
    inner_x = outer_x - 2*WALL
    inner_y = outer_y - 2*WALL
    inner = rounded_prism(inner_x, inner_y, max(0.8, BODY_RADIUS-WALL), top_z-FLOOR_Z+1.0).translate((0,0,FLOOR_Z))
    body = outer.cut(inner)
    foot = make_gridfinity_foot()
    for i in range(grid_w):
        for j in range(grid_d):
            body = body.union(foot.translate(((i - (grid_w-1)/2) * GRID, (j - (grid_d-1)/2) * GRID, 0)))
    return body


def _inner_outline(grid_w: int, grid_d: int) -> tuple[float, float, float]:
    outer_x = GRID * grid_w - XY_CLEARANCE
    outer_y = GRID * grid_d - XY_CLEARANCE
    return outer_x - 2*WALL, outer_y - 2*WALL, max(0.8, BODY_RADIUS - WALL)


def _rrect_wire(width: float, depth: float, radius: float, z: float) -> cq.Wire:
    radius = min(radius, width/2 - 0.01, depth/2 - 0.01)
    pts = [(-width/2, -depth/2), (width/2, -depth/2), (width/2, depth/2), (-width/2, depth/2)]
    wire = cq.Wire.makePolygon([cq.Vector(x, y, z) for x, y in pts], close=True)
    return wire.fillet2D(radius, wire.Vertices())


def _tapered_prism(width: float, depth: float, radius: float, z: float, height: float,
                   taper: float) -> cq.Workplane:
    """Rounded-rectangle extrusion; positive taper (degrees) shrinks it going up."""
    return (cq.Workplane('XY').add(_rrect_wire(width, depth, radius, z))
            .toPending().extrude(height, taper=taper))


def plate_depth(spec: dict) -> float:
    """Depth of the label from the inner wall (the 'label width' in the editor), in mm."""
    depth = float(spec.get('label_depth', DEFAULT_PLATE_DEPTH))
    lo, hi = PLATE_DEPTH_RANGE
    if not lo <= depth <= hi:
        raise ValueError(f"label_depth must be within {lo}-{hi} mm, got {depth}")
    _, iy, _ = _inner_outline(spec['grid_w'], spec['grid_d'])
    if depth > iy / 2:
        raise ValueError(f"a {depth} mm label covers more than half of the bin's {iy:.1f} mm inside")
    return depth


def _mw_offset_stack(ix: float, iy: float, ir: float, top_z: float, base: float,
                     segments: list[tuple[float, float, float, float]]) -> cq.Workplane:
    """Union of rounded-rect bands following a side profile.

    Each segment is (depth_top, depth_bottom, inset_top, inset_bottom), measured below top_z
    and in from the outline (ix, iy, ir) grown by -base. Insets change at 45 degrees or not at all.
    """
    solid = None
    for d0, d1, t0, t1 in segments:
        t0, t1 = t0 + base, t1 + base
        z_bot, h = top_z - d1, d1 - d0
        if abs(t0 - t1) < 1e-9:
            part = rounded_prism(ix - 2*t1, iy - 2*t1, max(0.2, ir - t1), h).translate((0, 0, z_bot))
        else:
            taper = 45 if t0 > t1 else -45      # shrinking going up = positive taper
            # Corner radius: clamp at the segment's SMALL end and let it grow towards the big
            # end. Taking the natural offset at the bottom instead let a shrinking segment's
            # corner radius run through zero part-way up (the label cavity foot, inset beyond
            # the 2.55 mm inner radius): a degenerate corner OCC accepts but that meshes with
            # non-manifold edges. Segments that never reach the clamp are unchanged.
            r_small = max(0.2, ir - max(t0, t1))
            r_bot = r_small + h if t0 > t1 else r_small
            part = _tapered_prism(ix - 2*t1, iy - 2*t1, r_bot, z_bot, h, taper)
        solid = part if solid is None else solid.union(part)
    return solid


def label_detent(spec: dict) -> bool:
    return bool(spec.get('label_detent', False))


def _mw_lip_section() -> list[tuple[float, float]]:
    """The lip profile as a plan-view outline: (inset from the wall, position along the wall),
    centred along the wall, continued 0.6 mm into the 1.2 mm wall so it unions cleanly. Not
    the full 1.2: a piece face lying on the bin's outer surface made OCC drop ~100 mm3 of
    material per piece while still reporting a valid solid."""
    prof = [(0.0, 0.0), (MW_P1, MW_P1), (MW_P1, MW_P2), (MW_P4, MW_P3), (0.0, MW_P3 + MW_P4)]
    half = (MW_P3 + MW_P4) / 2
    return [(u, t - half) for u, t in prof] + [(-WALL / 2, half), (-WALL / 2, -half)]


def _detent_pieces(grid_w: int, grid_d: int, height_u: int, slot: bool = False) -> list[cq.Workplane]:
    """The eight vertical lip pieces (two per wall, DETENT_OFFSET from each corner).

    Built in a local frame (x = inset from the wall, y = along it, z = 0 at the bin top, down
    negative), then turned onto each wall. slot=True returns the label's cutters instead: the
    section grown by DETENT_CLEARANCE, from just above where the piece stands proud of the lip
    down past the label skirt. Returned separately: unioning disjoint detail solids into one
    compound first gave OCC an invalid bin once already.
    """
    from shapely.geometry import Polygon
    ix, iy, _ = _inner_outline(grid_w, grid_d)
    top_z = height_u * 7.0
    lip_bottom = MW_P3 + MW_P4
    if slot:
        section = list(Polygon(_mw_lip_section()).buffer(DETENT_CLEARANCE, join_style=2).exterior.coords)[:-1]
        z0, z1 = -(MW_DEPTH + 1.0), -(MW_P2 - 0.1)        # the label skirt below the lip's straight face
        local = cq.Workplane('XY', origin=(0, 0, z0)).polyline(section).close().extrude(z1 - z0)
    else:
        bottom = lip_bottom + DETENT_DROP
        z0, z1 = -(bottom + 0.5), -MW_P1                  # starts inside the lip, overlapping it
        plan = cq.Workplane('XY', origin=(0, 0, z0)).polyline(_mw_lip_section()).close().extrude(z1 - z0)
        # side view: 45 degree chamfer at the bottom end, the reach shrinking to 0 at `bottom`
        side = (cq.Workplane('XZ')
                .polyline([(-1.3, 0.0), (MW_P1 + 0.1, 0.0),
                           (MW_P1 + 0.1, -(bottom - MW_P1 - 0.1)), (-1.3, -(bottom + 1.3))]).close()
                .extrude(5.0, both=True))
        local = plan.intersect(side)
    pieces = []
    for along in (-1, 1):
        # left/right walls (inset along +x / -x), pieces near the front and back corners
        y = along * (iy/2 - DETENT_OFFSET)
        pieces.append(local.rotate((0, 0, 0), (0, 0, 1), 0).translate((-ix/2, y, top_z)))
        pieces.append(local.rotate((0, 0, 0), (0, 0, 1), 180).translate((ix/2, y, top_z)))
        # front/back walls (inset along +y / -y), pieces near the left and right corners
        x = along * (ix/2 - DETENT_OFFSET)
        pieces.append(local.rotate((0, 0, 0), (0, 0, 1), 90).translate((x, -iy/2, top_z)))
        pieces.append(local.rotate((0, 0, 0), (0, 0, 1), -90).translate((x, iy/2, top_z)))
    return pieces


def make_mw_bin(grid_w: int, grid_d: int, height_u: int, detent: bool = False) -> cq.Workplane:
    """MakerWorld-style bin: shell plus the lip the replica label clips round, all round the
    top inside, optionally with the eight vertical detent pieces. Independent of the label."""
    top_z = height_u * 7.0
    ix, iy, ir = _inner_outline(grid_w, grid_d)
    e = 0.3                                  # cutter overshoot: no face coincides with the wall
    lip_depth = MW_P3 + MW_P4                # the underside meets the wall face 45 degrees below the bump
    slab = (rounded_prism(ix + 1.2, iy + 1.2, ir + 0.6, lip_depth).translate((0, 0, top_z - lip_depth)))
    cutter = _mw_offset_stack(ix, iy, ir, top_z, 0.0, [
        (-e, MW_P1, -e, MW_P1),              # top chamfer, continued above the rim
        (MW_P1, MW_P2, MW_P1, MW_P1),        # vertical face the label recess sits on
        (MW_P2, MW_P3, MW_P1, MW_P4),        # lower chamfer the label bump hooks under
        (MW_P3, lip_depth + e, MW_P4, -e),   # 45 degree underside back into the wall
    ])
    body = make_bin_shell(grid_w, grid_d, height_u).union(slab.cut(cutter))
    if detent:
        for piece in _detent_pieces(grid_w, grid_d, height_u):
            body = body.union(piece)
    return body


def label_fit(spec: dict) -> float:
    """Gap between the MakerWorld label's clip profile and the bin lip, per side, in mm.
    Only the label changes with it; the bin is the same for every fit."""
    fit = round(float(spec.get('label_fit', DEFAULT_MW_FIT)), 3)
    lo, hi = MW_FIT_RANGE
    if not lo - 1e-9 <= fit <= hi + 1e-9:
        raise ValueError(f"label_fit must be within {lo}-{hi} mm, got {fit}")
    return fit


def _mw_label_area(spec: dict) -> tuple[float, float, float]:
    ix, iy, _ = _inner_outline(spec['grid_w'], spec['grid_d'])
    depth, c = plate_depth(spec), max(0.0, label_fit(spec))
    centre = -iy/2 + (c + depth)/2
    if label_side(spec) == 'back':
        centre = -centre
    return (centre, ix - 2*c - 2*MW_SAFE, depth - c - 2*MW_SAFE)


def make_mw_label(spec: dict) -> tuple[cq.Workplane, cq.Workplane]:
    """MakerWorld-replica label in its installed position: (tray with pocket, flush inlay).

    Its face is flush with the bin top. Skirt on both sides and the wall-side edge; the free
    inner edge is a plain cut. For a back label the tray is mirrored, the artwork only moved.
    """
    w, d, top_z = spec['grid_w'], spec['grid_d'], spec['height_u'] * 7.0
    ix, iy, ir = _inner_outline(w, d)
    depth = plate_depth(spec)
    c = label_fit(spec)
    outer = _mw_offset_stack(ix, iy, ir, top_z, c, [
        (0.0, MW_P1, 0.0, MW_P1),
        (MW_P1, MW_P2, MW_P1, MW_P1),
        (MW_P2, MW_P3, MW_P1, MW_P4),
        (MW_P3, MW_DEPTH, MW_P4, MW_P4),
    ])
    cavity = _mw_offset_stack(ix, iy, ir, top_z, c, [
        (MW_PLATE, MW_DEPTH - 1.0, MW_SKIRT, MW_SKIRT),
        (MW_DEPTH - 1.0, MW_DEPTH + 0.5, MW_SKIRT, MW_SKIRT - 1.5),   # chamfered skirt foot
    ])
    band = (cq.Workplane('XY')
            .box(ix + 2, depth + 1, MW_DEPTH + 2, centered=(True, False, False))
            .translate((0, -iy/2 - 1, top_z - MW_DEPTH - 1)))
    if label_side(spec) == 'back':
        band = band.mirror('XZ')
    tray = outer.cut(cavity).intersect(band)
    if label_detent(spec):
        for cutter in _detent_pieces(w, d, spec['height_u'], slot=True):
            tray = tray.cut(cutter)
    area = _mw_label_area(spec)
    inlay = _label_geometry(spec, top_z - LABEL_INLAY_DEPTH, LABEL_INLAY_DEPTH, area=area)
    pocket = _label_geometry(
        spec, top_z - LABEL_INLAY_DEPTH - LABEL_POCKET_BOTTOM_CLEARANCE,
        LABEL_INLAY_DEPTH + LABEL_POCKET_BOTTOM_CLEARANCE + LABEL_POCKET_OVERTRAVEL, area=area)
    return tray.cut(pocket), inlay


def label_print_transform() -> np.ndarray:
    """Installed position -> print position: face down on the bed (flip about X)."""
    return trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0])


def _slug(text: str) -> str:
    text = unicodedata.normalize('NFKD', text.replace('×', 'x'))
    return re.sub(r'[^A-Za-z0-9.]+', '-', text).strip('-') or 'blank'


def screw_name(spec: dict) -> str:
    """Part type + label text, e.g. allen-countersunk_M6x80 or nyloc-nut_M6."""
    return f"{_slug(part_key(spec).replace('__', '-'))}_{_slug(label_text(spec))}"


def mw_bin_stem(grid_w: int, grid_d: int, height_u: int, count: int | None = None,
                detent: bool = False) -> str:
    """MakerWorld-style bin, shared by every screw of that size whatever its label:
    BIN_2x1_8U_MW_x83, with detent pieces BIN_2x1_8U_MW_DET_x83."""
    return (f"BIN_{grid_w}x{grid_d}_{height_u}U_MW{'_DET' if detent else ''}"
            + (f"_x{count}" if count is not None else ""))


def label_plate_stem(spec: dict) -> str:
    """The label file. It depends on bin width, label depth, fit, detents and edge:
    LABEL_allen-countersunk_M6x80_W2_D10_MW_FIT0.05[_DET][_LBACK] (names unchanged from when
    there were three styles, so earlier builds stay valid)."""
    suffix = '' if label_side(spec) == 'front' else '_LBACK'
    fit = f"_MW_FIT{label_fit(spec):g}" + ('_DET' if label_detent(spec) else '')
    return f"LABEL_{screw_name(spec)}_W{spec['grid_w']}_D{plate_depth(spec):g}{fit}{suffix}"


DRAWER_GRID_W = 25
DRAWER_GRID_D = 14


def validate_spec(spec: dict) -> None:
    """One bin: known part type, drawable text, a footprint that fits the drawer."""
    part_key(spec)
    if not label_art.compose(spec['part'], label_text(spec)):
        raise ValueError(f"{screw_name(spec)}: empty label")
    w, d = spec['grid_w'], spec['grid_d']
    if not (1 <= w <= DRAWER_GRID_W and 1 <= d <= DRAWER_GRID_D):
        raise ValueError(f"{screw_name(spec)}: footprint {w}x{d} does not fit the drawer")


def validate_specs() -> None:
    assert len({s['pos'] for s in SPECS}) == len(SPECS)
    for s in SPECS:
        validate_spec(s)
    assert {s['height_u'] for s in SPECS} == {UNIFORM_HEIGHT_U}   # hard requirement
    assert 0 < LABEL_INLAY_DEPTH < MW_PLATE
    assert UNIFORM_HEIGHT_U * 7.0 <= 75.0                # drawer inner height


def _to_mesh(shape: cq.Workplane, tolerance: float, angular: float) -> trimesh.Trimesh:
    with tempfile.TemporaryDirectory(prefix='cq2mesh_') as tmp:
        path = Path(tmp) / 'part.stl'
        cq.exporters.export(shape, str(path), tolerance=tolerance, angularTolerance=angular)
        return trimesh.load_mesh(path, force='mesh')


def build_mw_bin_3mf(grid_w: int, grid_d: int, height_u: int, path: Path,
                     detent: bool = False) -> None:
    """The bin with its lip (and detent pieces), one part on filament 1."""
    bin_mesh = _to_mesh(make_mw_bin(grid_w, grid_d, height_u, detent), 0.09, 0.22)
    export_parts_3mf([('BIN', bin_mesh, BODY_FILAMENT)], path, path.stem)


def build_label_plate_3mf(spec: dict, path: Path) -> None:
    """The clip-on label + flush inlay, laid face down for printing."""
    plate, inlay = make_mw_label(spec)
    plate_mesh = _to_mesh(plate, 0.05, 0.18)
    inlay_mesh = _to_mesh(inlay, 0.05, 0.18)
    flip = label_print_transform()
    for mesh in (plate_mesh, inlay_mesh):
        mesh.apply_transform(flip)
    dz = -plate_mesh.bounds[0][2]
    centre = plate_mesh.bounds.mean(axis=0)
    for mesh in (plate_mesh, inlay_mesh):
        mesh.apply_translation([-centre[0], -centre[1], dz])
    export_parts_3mf([('LABEL_PLATE', plate_mesh, BODY_FILAMENT),
                      ('LABEL_INLAY', inlay_mesh, LABEL_FILAMENT)], path, path.stem)


def file_plan(specs: list[dict]) -> tuple[list[tuple[tuple, int]], list[dict]]:
    """Distinct bins with their use counts, and the labels.

    A bin does not depend on its label, so its key is (w, d, u), plus a 'det' marker when it
    carries the detent pieces.
    """
    counts: dict[tuple, int] = {}
    for spec in specs:
        key = (spec['grid_w'], spec['grid_d'], spec['height_u'])
        if label_detent(spec):
            key += ('det',)
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items()), list(specs)


def write_manifest(out: Path, specs: list[dict]) -> None:
    with (out/'manifest.csv').open('w', newline='', encoding='utf-8') as f:
        cols = ['name', 'part', 'part_name', 'text', 'thread', 'length', 'grid_w', 'grid_d',
                'height_u', 'label_side', 'norm']
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in specs:
            row = {**s, 'name': screw_name(s), 'part_name': label_art.part_name(s['part']),
                   'text': label_text(s), 'label_side': label_side(s)}
            w.writerow({k: row.get(k, '') for k in cols})


def generate(out: Path, positions: set[int] | None = None, side: str | None = None,
             label_depth: float = DEFAULT_PLATE_DEPTH, fit: float = DEFAULT_MW_FIT,
             detent: bool = False) -> None:
    validate_specs()
    selected = [s for s in SPECS if positions is None or s['pos'] in positions]
    if side is not None:
        selected = [{**s, 'label_side': side} for s in selected]
    selected = [{**s, 'label_depth': label_depth, 'label_fit': fit, 'label_detent': detent}
                for s in selected]
    if not selected:
        raise ValueError('No matching positions selected')

    bin_dir, label_dir = out/'bins_3mf', out/'labels_3mf'
    for p in (bin_dir, label_dir):
        p.mkdir(parents=True, exist_ok=True)
    bins, labels = file_plan(selected)
    for key, count in bins:
        det = len(key) == 4
        path = bin_dir / f"{mw_bin_stem(*key[:3], count, det)}.3mf"
        build_mw_bin_3mf(*key[:3], path, det)
        print(f"bin   {path.name}")
    done: set[str] = set()
    for n, s in enumerate(labels, 1):
        stem = label_plate_stem(s)
        if stem in done:
            continue
        done.add(stem)
        path = label_dir / f"{stem}.3mf"
        build_label_plate_3mf(s, path)
        print(f"[{n:03d}/{len(labels):03d}] {path.name}")
    write_manifest(out, selected)
    print(f"Generated {len(bins)} distinct bins and {len(done)} labels in {out}")


def parse_positions(value: str | None) -> set[int] | None:
    if not value:
        return None
    result: set[int] = set()
    for token in value.split(','):
        token = token.strip()
        if not token:
            continue
        if '-' in token:
            a, b = token.split('-', 1)
            result.update(range(int(a), int(b)+1))
        else:
            result.add(int(token))
    return result


def main():
    print(f"Gridfinity label bin generator {SCRIPT_VERSION}")
    ap = argparse.ArgumentParser(description='Generate Gridfinity drawer bins with two-colour '
                                 'label.alch.shop-style labels, fully offline.')
    ap.add_argument('-o', '--output', default='gridfinity_bins')
    ap.add_argument('--positions', help='Optional positions of the default set, e.g. 7,62,113-116. Default: all.')
    ap.add_argument('--build', action='store_true', help='Accepted for compatibility; building is the default.')
    ap.add_argument('--label-side', choices=LABEL_SIDES, default=None,
                    help="Edge that carries the label: 'front' (default, drawer-front side) or 'back'.")
    ap.add_argument('--label-depth', type=float, default=DEFAULT_PLATE_DEPTH,
                    help=f"Label depth from the wall in mm (default {DEFAULT_PLATE_DEPTH:g}, "
                         f"{PLATE_DEPTH_RANGE[0]:g}-{PLATE_DEPTH_RANGE[1]:g}).")
    ap.add_argument('--label-fit', type=float, default=DEFAULT_MW_FIT,
                    help=f"Gap between label clip and bin lip per side in mm "
                         f"(default {DEFAULT_MW_FIT:g}, {MW_FIT_RANGE[0]:g} to {MW_FIT_RANGE[1]:g}; "
                         f"negative = press fit).")
    ap.add_argument('--detents', action='store_true',
                    help="Short vertical lip pieces near every corner + matching "
                         "slots in the label, so it cannot slide.")
    args = ap.parse_args()
    generate(Path(args.output), parse_positions(args.positions), side=args.label_side,
             label_depth=args.label_depth, fit=args.label_fit, detent=args.detents)


if __name__ == '__main__':
    main()

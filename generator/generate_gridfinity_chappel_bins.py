#!/usr/bin/env python3
"""Generate the complete Gridfinity screw-bin set for the 1077 x 602 x 75 mm drawer.

The screw data and final reviewed bin sizes are hardcoded below. No Excel/PDF import.

Output per screw:
  - one two-part 3MF assembly (BODY + LABEL), suitable for AMS assignment in Bambu Studio/OrcaSlicer
  - one label STL
Shared body geometries are generated once in body_library/.

Dependencies:
  pip install cadquery trimesh shapely networkx playwright
  python -m playwright install chromium

Design choices:
  * Standard 42 mm Gridfinity XY pitch and a standard-compatible 41.5 mm multi-step foot.
  * No stacking lip: these are drawer bins, which gives more clearance and a clean top label shelf.
  * 1.2 mm walls, all bins exactly 7U (49 mm total height).
  * Footprints are 2x1, 2x2 and 2x3 (width x depth).  Every bin is two cells wide, so the
    drawer is a grid of clean columns and every label shelf is a wide 84 mm.  The 2x3 bin
    is portrait, the 2x2 square, the 2x1 flat.
  * Bin volume is sized from real DIN/ISO fastener geometry and a 45 % loose packing
    density: the ordered quantity fills at most 80 % of the usable cavity (6kt M10x80
    is the single exception at 81 %, because 2x3 is the largest allowed footprint).
    EVERY bin is 8U = 56 mm high.
  * Horizontal top-front label shelf stays in the same position.
  * The underside of that shelf is supported by an approximately 45 degree inward ramp, so no support is needed.
  * Visible label geometry comes from Alexandre Chappel's ModuBOX Label Generator via browser automation.
  * The downloaded Raised label is sampled above its thin base; only Chappel's original icon + text outlines are reused.
  * Those outlines become a 0.6 mm deep, perfectly flush AMS inlay in the Gridfinity shelf.
"""

from __future__ import annotations
import argparse
import csv
import math
import shutil
import zipfile
import xml.etree.ElementTree as ET
import re
import time
import unicodedata
import tempfile
import numpy as np
from pathlib import Path
from dataclasses import dataclass
import cadquery as cq
import trimesh
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
LABEL_SHELF_DEPTH = 12.0
LABEL_SHELF_THICK = 1.2
LABEL_INLAY_DEPTH = 0.60        # 3 layers at 0.20 mm
LABEL_INLAY_TOP_RECESS = 0.00    # exactly flush: inlay top plane == BODY label-shelf top plane
LABEL_POCKET_BOTTOM_CLEARANCE = 0.02
LABEL_POCKET_OVERTRAVEL = 0.08  # cutter continues above the top face for robust booleans
LABEL_RAMP_ANGLE_DEG = 45.0     # underside ramp angle, measured from horizontal
UNIFORM_HEIGHT_U = 8              # ALL bins are 56 mm high - uniform height is a hard requirement.
                                  # 7U/49 mm cannot hold the 15.04.2026 order at a sane fill level
                                  # without 1-cell-wide bins; 8U clears the 75 mm drawer easily.
LABEL_SIDE_INSET = 0.00         # shelf is generated full-width, then clipped to the rounded outer envelope
LABEL_SAFE_SIDE = 2.5            # minimum horizontal label-graphics margin from the box envelope
LABEL_SAFE_FRONT_BACK = 1.5      # minimum label-graphics margin within the 12 mm shelf depth
LABEL_ICON_TEXT_GAP = 2.4        # legacy constant, unused with Chappel labels

SCRIPT_VERSION = "2026-09-13.7-volume-checked-uniform-8U"
CHAPPEL_URL = "https://label.alch.shop/"
CHAPPEL_BASE_THICKNESS = 0.30     # current Raised-label default on label.alch.shop
CHAPPEL_EXTRUSION_THICKNESS = 0.15
CHAPPEL_SECTION_FRACTION = 0.50   # sample in the middle of the raised graphic layer
CHAPPEL_CACHE_DIRNAME = "chappel_raw_labels"
CHAPPEL_DOWNLOAD_DELAY_MS = 300
CHAPPEL_MAX_UPSCALE = 1.35

# UI matching uses option text, not hidden website internals. Candidate phrases are tried in order.
CHAPPEL_OPTION_HINTS = {
    # Current label.alch.shop v1.2.1 terminology.  Put the exact current label
    # first; older aliases remain as fallbacks in case Alexandre renames them.
    "SK": {
        "drive": [("allen",), ("hex", "socket"), ("inbus",), ("internal", "hex")],
        "head":  [("countersunk",), ("flat", "head"), ("flat",)],
    },
    "ZK": {
        "drive": [("allen",), ("hex", "socket"), ("inbus",), ("internal", "hex")],
        # IMPORTANT: the current website calls this head "Socket cap", value=socket.
        "head":  [("socket", "cap"), ("socket",), ("cylinder",), ("cylindrical",)],
    },
    "LK": {
        "drive": [("phillips",), ("cross",), ("ph",)],
        # Current site label is simply "Pan" (not "Pan head").
        "head":  [("pan",), ("pan", "head"), ("lens",), ("round", "head")],
    },
    "6kt": {
        "drive": [("external", "hex"), ("hexagon",), ("hex", "head"), ("hex",)],
        # Current site label/value: Hex [hex].
        "head":  [("hex",), ("hex", "head"), ("hexagon",)],
    },
}


# Actual screw inventory from the invoice, with reviewed storage sizes.
# grid_w/grid_d are normalized to a landscape orientation for the CAD file; bins can of course be rotated in the drawer.
# Uniform-height design: 7U = 49 mm for every bin. In a 75 mm drawer this leaves 26 mm vertical clearance.
# Final portrait design: the sizing below is unchanged, but every bin deeper than one
# row is emitted rotated, so its label shelf lands on the SHORT side: 4x1 -> 1x4 and
# 4x2 -> 2x4.  The 2x1 bin keeps its landscape label on the 84 mm side.
# This eliminates all 1x1 and 3x1/3x2 bins while keeping every bin at 7U.
# The dimensions are re-optimized from the hardcoded screw data below; total footprint: 304/350 cells.
SPECS = [{'pos': 1,
  'kind': 'SK',
  'thread': 'M6',
  'diameter': 6,
  'length': 16,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001367',
  'friend_box': '1x2',
  'fill_ratio_est': 0.236},
 {'pos': 2,
  'kind': 'SK',
  'thread': 'M6',
  'diameter': 6,
  'length': 20,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001368',
  'friend_box': '1x2',
  'fill_ratio_est': 0.278},
 {'pos': 3,
  'kind': 'SK',
  'thread': 'M6',
  'diameter': 6,
  'length': 25,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001369',
  'friend_box': '1x2',
  'fill_ratio_est': 0.332},
 {'pos': 4,
  'kind': 'SK',
  'thread': 'M6',
  'diameter': 6,
  'length': 30,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001370',
  'friend_box': '1x3',
  'fill_ratio_est': 0.254},
 {'pos': 5,
  'kind': 'SK',
  'thread': 'M6',
  'diameter': 6,
  'length': 40,
  'qty': 30,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001371',
  'friend_box': '1x3',
  'fill_ratio_est': 0.194},
 {'pos': 6,
  'kind': 'SK',
  'thread': 'M6',
  'diameter': 6,
  'length': 80,
  'qty': 5,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001374',
  'friend_box': '1x3',
  'fill_ratio_est': 0.061},
 {'pos': 7,
  'kind': 'SK',
  'thread': 'M3',
  'diameter': 3,
  'length': 6,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P005009',
  'friend_box': '1x1',
  'fill_ratio_est': 0.1},
 {'pos': 8,
  'kind': 'SK',
  'thread': 'M3',
  'diameter': 3,
  'length': 8,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P000332',
  'friend_box': '1x2',
  'fill_ratio_est': 0.059},
 {'pos': 9,
  'kind': 'SK',
  'thread': 'M3',
  'diameter': 3,
  'length': 10,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001577',
  'friend_box': '1x2',
  'fill_ratio_est': 0.07},
 {'pos': 10,
  'kind': 'SK',
  'thread': 'M3',
  'diameter': 3,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P002506',
  'friend_box': '1x2',
  'fill_ratio_est': 0.08},
 {'pos': 11,
  'kind': 'SK',
  'thread': 'M3',
  'diameter': 3,
  'length': 16,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P002443',
  'friend_box': '1x1',
  'fill_ratio_est': 0.211},
 {'pos': 12,
  'kind': 'SK',
  'thread': 'M3',
  'diameter': 3,
  'length': 20,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P003632',
  'friend_box': '1x1',
  'fill_ratio_est': 0.255},
 {'pos': 13,
  'kind': 'SK',
  'thread': 'M4',
  'diameter': 4,
  'length': 8,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P000363',
  'friend_box': '1x1',
  'fill_ratio_est': 0.237},
 {'pos': 14,
  'kind': 'SK',
  'thread': 'M4',
  'diameter': 4,
  'length': 10,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P000369',
  'friend_box': '1x2',
  'fill_ratio_est': 0.133},
 {'pos': 15,
  'kind': 'SK',
  'thread': 'M4',
  'diameter': 4,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001351',
  'friend_box': '1x2',
  'fill_ratio_est': 0.152},
 {'pos': 16,
  'kind': 'SK',
  'thread': 'M4',
  'diameter': 4,
  'length': 16,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001353',
  'friend_box': '1x2',
  'fill_ratio_est': 0.19},
 {'pos': 17,
  'kind': 'SK',
  'thread': 'M4',
  'diameter': 4,
  'length': 20,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001354',
  'friend_box': '1x2',
  'fill_ratio_est': 0.228},
 {'pos': 18,
  'kind': 'SK',
  'thread': 'M4',
  'diameter': 4,
  'length': 25,
  'qty': 50,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001355',
  'friend_box': '1x1',
  'fill_ratio_est': 0.286},
 {'pos': 19,
  'kind': 'SK',
  'thread': 'M4',
  'diameter': 4,
  'length': 30,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P003633',
  'friend_box': '1x1',
  'fill_ratio_est': 0.162},
 {'pos': 20,
  'kind': 'SK',
  'thread': 'M5',
  'diameter': 5,
  'length': 10,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001356',
  'friend_box': '1x2',
  'fill_ratio_est': 0.112},
 {'pos': 21,
  'kind': 'SK',
  'thread': 'M5',
  'diameter': 5,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001357',
  'friend_box': '1x2',
  'fill_ratio_est': 0.253},
 {'pos': 22,
  'kind': 'SK',
  'thread': 'M5',
  'diameter': 5,
  'length': 16,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001358',
  'friend_box': '1x2',
  'fill_ratio_est': 0.312},
 {'pos': 23,
  'kind': 'SK',
  'thread': 'M5',
  'diameter': 5,
  'length': 20,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001359',
  'friend_box': '1x2',
  'fill_ratio_est': 0.372},
 {'pos': 24,
  'kind': 'SK',
  'thread': 'M5',
  'diameter': 5,
  'length': 25,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001360',
  'friend_box': '1x2',
  'fill_ratio_est': 0.446},
 {'pos': 25,
  'kind': 'SK',
  'thread': 'M5',
  'diameter': 5,
  'length': 30,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001361',
  'friend_box': '1x3',
  'fill_ratio_est': 0.171},
 {'pos': 26,
  'kind': 'SK',
  'thread': 'M5',
  'diameter': 5,
  'length': 40,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'ISO 10642',
  'material': 'Edelstahl A2',
  'artnr': 'P001362',
  'friend_box': '1x3',
  'fill_ratio_est': 0.22},
 {'pos': 27,
  'kind': 'ZK',
  'thread': 'M3',
  'diameter': 3,
  'length': 6,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003958',
  'friend_box': '1x1',
  'fill_ratio_est': 0.14},
 {'pos': 28,
  'kind': 'ZK',
  'thread': 'M3',
  'diameter': 3,
  'length': 8,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002607',
  'friend_box': '1x2',
  'fill_ratio_est': 0.078},
 {'pos': 29,
  'kind': 'ZK',
  'thread': 'M3',
  'diameter': 3,
  'length': 10,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003066',
  'friend_box': '1x2',
  'fill_ratio_est': 0.089},
 {'pos': 30,
  'kind': 'ZK',
  'thread': 'M3',
  'diameter': 3,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003067',
  'friend_box': '1x2',
  'fill_ratio_est': 0.1},
 {'pos': 31,
  'kind': 'ZK',
  'thread': 'M3',
  'diameter': 3,
  'length': 16,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003069',
  'friend_box': '1x2',
  'fill_ratio_est': 0.121},
 {'pos': 32,
  'kind': 'ZK',
  'thread': 'M3',
  'diameter': 3,
  'length': 20,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000571',
  'friend_box': '1x2',
  'fill_ratio_est': 0.142},
 {'pos': 33,
  'kind': 'ZK',
  'thread': 'M3',
  'diameter': 3,
  'length': 25,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000572',
  'friend_box': '1x1',
  'fill_ratio_est': 0.169},
 {'pos': 34,
  'kind': 'ZK',
  'thread': 'M3',
  'diameter': 3,
  'length': 30,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000573',
  'friend_box': '1x1',
  'fill_ratio_est': 0.196},
 {'pos': 35,
  'kind': 'ZK',
  'thread': 'M4',
  'diameter': 4,
  'length': 8,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003953',
  'friend_box': '1x1',
  'fill_ratio_est': 0.16},
 {'pos': 36,
  'kind': 'ZK',
  'thread': 'M4',
  'diameter': 4,
  'length': 10,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003029',
  'friend_box': '1x2',
  'fill_ratio_est': 0.179},
 {'pos': 37,
  'kind': 'ZK',
  'thread': 'M4',
  'diameter': 4,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P004260',
  'friend_box': '1x2',
  'fill_ratio_est': 0.198},
 {'pos': 38,
  'kind': 'ZK',
  'thread': 'M4',
  'diameter': 4,
  'length': 16,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002244',
  'friend_box': '1x2',
  'fill_ratio_est': 0.236},
 {'pos': 39,
  'kind': 'ZK',
  'thread': 'M4',
  'diameter': 4,
  'length': 20,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000340',
  'friend_box': '1x2',
  'fill_ratio_est': 0.274},
 {'pos': 40,
  'kind': 'ZK',
  'thread': 'M4',
  'diameter': 4,
  'length': 25,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002242',
  'friend_box': '1x2',
  'fill_ratio_est': 0.322},
 {'pos': 41,
  'kind': 'ZK',
  'thread': 'M4',
  'diameter': 4,
  'length': 30,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000341',
  'friend_box': '1x1',
  'fill_ratio_est': 0.369},
 {'pos': 42,
  'kind': 'ZK',
  'thread': 'M4',
  'diameter': 4,
  'length': 40,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000342',
  'friend_box': '1x1',
  'fill_ratio_est': 0.232},
 {'pos': 43,
  'kind': 'ZK',
  'thread': 'M5',
  'diameter': 5,
  'length': 10,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003974',
  'friend_box': '1x2',
  'fill_ratio_est': 0.157},
 {'pos': 44,
  'kind': 'ZK',
  'thread': 'M5',
  'diameter': 5,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003557',
  'friend_box': '1x2',
  'fill_ratio_est': 0.343},
 {'pos': 45,
  'kind': 'ZK',
  'thread': 'M5',
  'diameter': 5,
  'length': 16,
  'qty': 100,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002578',
  'friend_box': '1x3',
  'fill_ratio_est': 0.265},
 {'pos': 46,
  'kind': 'ZK',
  'thread': 'M5',
  'diameter': 5,
  'length': 20,
  'qty': 100,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P001564',
  'friend_box': '1x3',
  'fill_ratio_est': 0.304},
 {'pos': 47,
  'kind': 'ZK',
  'thread': 'M5',
  'diameter': 5,
  'length': 25,
  'qty': 100,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003558',
  'friend_box': '1x3',
  'fill_ratio_est': 0.353},
 {'pos': 48,
  'kind': 'ZK',
  'thread': 'M5',
  'diameter': 5,
  'length': 30,
  'qty': 100,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P001565',
  'friend_box': '1x3',
  'fill_ratio_est': 0.402},
 {'pos': 49,
  'kind': 'ZK',
  'thread': 'M5',
  'diameter': 5,
  'length': 40,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P001566',
  'friend_box': '1x3',
  'fill_ratio_est': 0.25},
 {'pos': 50,
  'kind': 'ZK',
  'thread': 'M5',
  'diameter': 5,
  'length': 50,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002579',
  'friend_box': '1x3',
  'fill_ratio_est': 0.298},
 {'pos': 51,
  'kind': 'ZK',
  'thread': 'M6',
  'diameter': 6,
  'length': 16,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002587',
  'friend_box': '1x2',
  'fill_ratio_est': 0.313},
 {'pos': 52,
  'kind': 'ZK',
  'thread': 'M6',
  'diameter': 6,
  'length': 20,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P001567',
  'friend_box': '1x3',
  'fill_ratio_est': 0.343},
 {'pos': 53,
  'kind': 'ZK',
  'thread': 'M6',
  'diameter': 6,
  'length': 25,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002562',
  'friend_box': '1x3',
  'fill_ratio_est': 0.395},
 {'pos': 54,
  'kind': 'ZK',
  'thread': 'M6',
  'diameter': 6,
  'length': 30,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000343',
  'friend_box': '1x3',
  'fill_ratio_est': 0.446},
 {'pos': 55,
  'kind': 'ZK',
  'thread': 'M6',
  'diameter': 6,
  'length': 40,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000344',
  'friend_box': '2x2',
  'fill_ratio_est': 0.274},
 {'pos': 56,
  'kind': 'ZK',
  'thread': 'M6',
  'diameter': 6,
  'length': 50,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000345',
  'friend_box': '2x2',
  'fill_ratio_est': 0.326},
 {'pos': 57,
  'kind': 'ZK',
  'thread': 'M6',
  'diameter': 6,
  'length': 60,
  'qty': 30,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002838',
  'friend_box': '2x3',
  'fill_ratio_est': 0.149},
 {'pos': 58,
  'kind': 'ZK',
  'thread': 'M8',
  'diameter': 8,
  'length': 20,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P001570',
  'friend_box': '2x2',
  'fill_ratio_est': 0.346},
 {'pos': 59,
  'kind': 'ZK',
  'thread': 'M8',
  'diameter': 8,
  'length': 30,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P001571',
  'friend_box': '2x2',
  'fill_ratio_est': 0.437},
 {'pos': 60,
  'kind': 'ZK',
  'thread': 'M8',
  'diameter': 8,
  'length': 50,
  'qty': 25,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P000347',
  'friend_box': '2x3',
  'fill_ratio_est': 0.204},
 {'pos': 61,
  'kind': 'ZK',
  'thread': 'M10',
  'diameter': 10,
  'length': 30,
  'qty': 25,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P002154',
  'friend_box': '2x2',
  'fill_ratio_est': 0.373},
 {'pos': 62,
  'kind': 'ZK',
  'thread': 'M10',
  'diameter': 10,
  'length': 50,
  'qty': 25,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 912',
  'material': 'Edelstahl A2',
  'artnr': 'P003680',
  'friend_box': '2x3',
  'fill_ratio_est': 0.34},
 {'pos': 63,
  'kind': 'LK',
  'thread': 'M3',
  'diameter': 3,
  'length': 6,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004218',
  'friend_box': '1x1',
  'fill_ratio_est': 0.117},
 {'pos': 64,
  'kind': 'LK',
  'thread': 'M3',
  'diameter': 3,
  'length': 8,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003438',
  'friend_box': '1x2',
  'fill_ratio_est': 0.067},
 {'pos': 65,
  'kind': 'LK',
  'thread': 'M3',
  'diameter': 3,
  'length': 10,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003439',
  'friend_box': '1x2',
  'fill_ratio_est': 0.078},
 {'pos': 66,
  'kind': 'LK',
  'thread': 'M3',
  'diameter': 3,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003564',
  'friend_box': '1x2',
  'fill_ratio_est': 0.088},
 {'pos': 67,
  'kind': 'LK',
  'thread': 'M3',
  'diameter': 3,
  'length': 16,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004219',
  'friend_box': '1x1',
  'fill_ratio_est': 0.228},
 {'pos': 68,
  'kind': 'LK',
  'thread': 'M3',
  'diameter': 3,
  'length': 20,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003440',
  'friend_box': '1x1',
  'fill_ratio_est': 0.272},
 {'pos': 69,
  'kind': 'LK',
  'thread': 'M4',
  'diameter': 4,
  'length': 8,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003464',
  'friend_box': '1x1',
  'fill_ratio_est': 0.277},
 {'pos': 70,
  'kind': 'LK',
  'thread': 'M4',
  'diameter': 4,
  'length': 10,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004216',
  'friend_box': '1x2',
  'fill_ratio_est': 0.153},
 {'pos': 71,
  'kind': 'LK',
  'thread': 'M4',
  'diameter': 4,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003607',
  'friend_box': '1x2',
  'fill_ratio_est': 0.172},
 {'pos': 72,
  'kind': 'LK',
  'thread': 'M4',
  'diameter': 4,
  'length': 16,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004220',
  'friend_box': '1x2',
  'fill_ratio_est': 0.21},
 {'pos': 73,
  'kind': 'LK',
  'thread': 'M4',
  'diameter': 4,
  'length': 20,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003640',
  'friend_box': '1x1',
  'fill_ratio_est': 0.248},
 {'pos': 74,
  'kind': 'LK',
  'thread': 'M4',
  'diameter': 4,
  'length': 25,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P008847',
  'friend_box': '1x1',
  'fill_ratio_est': 0.295},
 {'pos': 75,
  'kind': 'LK',
  'thread': 'M5',
  'diameter': 5,
  'length': 10,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003618',
  'friend_box': '1x2',
  'fill_ratio_est': 0.261},
 {'pos': 76,
  'kind': 'LK',
  'thread': 'M5',
  'diameter': 5,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003646',
  'friend_box': '1x2',
  'fill_ratio_est': 0.291},
 {'pos': 77,
  'kind': 'LK',
  'thread': 'M5',
  'diameter': 5,
  'length': 16,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003667',
  'friend_box': '1x2',
  'fill_ratio_est': 0.35},
 {'pos': 78,
  'kind': 'LK',
  'thread': 'M5',
  'diameter': 5,
  'length': 20,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004217',
  'friend_box': '1x2',
  'fill_ratio_est': 0.409},
 {'pos': 79,
  'kind': 'LK',
  'thread': 'M5',
  'diameter': 5,
  'length': 25,
  'qty': 100,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003641',
  'friend_box': '1x3',
  'fill_ratio_est': 0.319},
 {'pos': 80,
  'kind': 'LK',
  'thread': 'M5',
  'diameter': 5,
  'length': 30,
  'qty': 100,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P003642',
  'friend_box': '1x3',
  'fill_ratio_est': 0.367},
 {'pos': 81,
  'kind': 'LK',
  'thread': 'M6',
  'diameter': 6,
  'length': 16,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004620',
  'friend_box': '1x2',
  'fill_ratio_est': 0.268},
 {'pos': 82,
  'kind': 'LK',
  'thread': 'M6',
  'diameter': 6,
  'length': 20,
  'qty': 100,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004079',
  'friend_box': '1x2',
  'fill_ratio_est': 0.41},
 {'pos': 83,
  'kind': 'LK',
  'thread': 'M6',
  'diameter': 6,
  'length': 25,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004213',
  'friend_box': '1x2',
  'fill_ratio_est': 0.351},
 {'pos': 84,
  'kind': 'LK',
  'thread': 'M6',
  'diameter': 6,
  'length': 30,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 7985',
  'material': 'Stahl verzinkt 4.8',
  'artnr': 'P004239',
  'friend_box': '1x3',
  'fill_ratio_est': 0.403},
 {'pos': 85,
  'kind': '6kt',
  'thread': 'M3',
  'diameter': 3,
  'length': 8,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P000141',
  'friend_box': '1x1',
  'fill_ratio_est': 0.143},
 {'pos': 86,
  'kind': '6kt',
  'thread': 'M3',
  'diameter': 3,
  'length': 10,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P002618',
  'friend_box': '1x1',
  'fill_ratio_est': 0.166},
 {'pos': 87,
  'kind': '6kt',
  'thread': 'M3',
  'diameter': 3,
  'length': 12,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P000151',
  'friend_box': '1x1',
  'fill_ratio_est': 0.188},
 {'pos': 88,
  'kind': '6kt',
  'thread': 'M3',
  'diameter': 3,
  'length': 16,
  'qty': 100,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P002619',
  'friend_box': '1x1',
  'fill_ratio_est': 0.232},
 {'pos': 89,
  'kind': '6kt',
  'thread': 'M3',
  'diameter': 3,
  'length': 20,
  'qty': 10,
  'grid_w': 1,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P002620',
  'friend_box': '1x1',
  'fill_ratio_est': 0.028},
 {'pos': 90,
  'kind': '6kt',
  'thread': 'M4',
  'diameter': 4,
  'length': 10,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001076',
  'friend_box': '1x1',
  'fill_ratio_est': 0.158},
 {'pos': 91,
  'kind': '6kt',
  'thread': 'M4',
  'diameter': 4,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P003547',
  'friend_box': '1x1',
  'fill_ratio_est': 0.177},
 {'pos': 92,
  'kind': '6kt',
  'thread': 'M4',
  'diameter': 4,
  'length': 16,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P003548',
  'friend_box': '1x2',
  'fill_ratio_est': 0.107},
 {'pos': 93,
  'kind': '6kt',
  'thread': 'M4',
  'diameter': 4,
  'length': 20,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001079',
  'friend_box': '1x2',
  'fill_ratio_est': 0.126},
 {'pos': 94,
  'kind': '6kt',
  'thread': 'M4',
  'diameter': 4,
  'length': 25,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001080',
  'friend_box': '1x1',
  'fill_ratio_est': 0.15},
 {'pos': 95,
  'kind': '6kt',
  'thread': 'M4',
  'diameter': 4,
  'length': 30,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001081',
  'friend_box': '1x1',
  'fill_ratio_est': 0.174},
 {'pos': 96,
  'kind': '6kt',
  'thread': 'M5',
  'diameter': 5,
  'length': 12,
  'qty': 100,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P003196',
  'friend_box': '1x2',
  'fill_ratio_est': 0.3},
 {'pos': 97,
  'kind': '6kt',
  'thread': 'M5',
  'diameter': 5,
  'length': 16,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P003197',
  'friend_box': '1x2',
  'fill_ratio_est': 0.18},
 {'pos': 98,
  'kind': '6kt',
  'thread': 'M5',
  'diameter': 5,
  'length': 20,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001085',
  'friend_box': '1x3',
  'fill_ratio_est': 0.138},
 {'pos': 99,
  'kind': '6kt',
  'thread': 'M5',
  'diameter': 5,
  'length': 25,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001086',
  'friend_box': '1x3',
  'fill_ratio_est': 0.162},
 {'pos': 100,
  'kind': '6kt',
  'thread': 'M5',
  'diameter': 5,
  'length': 30,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001087',
  'friend_box': '1x3',
  'fill_ratio_est': 0.187},
 {'pos': 101,
  'kind': '6kt',
  'thread': 'M5',
  'diameter': 5,
  'length': 40,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001089',
  'friend_box': '1x3',
  'fill_ratio_est': 0.236},
 {'pos': 102,
  'kind': '6kt',
  'thread': 'M5',
  'diameter': 5,
  'length': 50,
  'qty': 30,
  'grid_w': 3,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P003551',
  'friend_box': '1x3',
  'fill_ratio_est': 0.171},
 {'pos': 103,
  'kind': '6kt',
  'thread': 'M6',
  'diameter': 6,
  'length': 16,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 1,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001093',
  'friend_box': '1x2',
  'fill_ratio_est': 0.277},
 {'pos': 104,
  'kind': '6kt',
  'thread': 'M6',
  'diameter': 6,
  'length': 20,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001095',
  'friend_box': '2x2',
  'fill_ratio_est': 0.154},
 {'pos': 105,
  'kind': '6kt',
  'thread': 'M6',
  'diameter': 6,
  'length': 25,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001097',
  'friend_box': '2x2',
  'fill_ratio_est': 0.18},
 {'pos': 106,
  'kind': '6kt',
  'thread': 'M6',
  'diameter': 6,
  'length': 30,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001098',
  'friend_box': '2x2',
  'fill_ratio_est': 0.205},
 {'pos': 107,
  'kind': '6kt',
  'thread': 'M6',
  'diameter': 6,
  'length': 40,
  'qty': 50,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001100',
  'friend_box': '2x2',
  'fill_ratio_est': 0.257},
 {'pos': 108,
  'kind': '6kt',
  'thread': 'M6',
  'diameter': 6,
  'length': 50,
  'qty': 50,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P000015',
  'friend_box': '2x3',
  'fill_ratio_est': 0.203},
 {'pos': 109,
  'kind': '6kt',
  'thread': 'M6',
  'diameter': 6,
  'length': 60,
  'qty': 30,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P000266',
  'friend_box': '2x3',
  'fill_ratio_est': 0.142},
 {'pos': 110,
  'kind': '6kt',
  'thread': 'M8',
  'diameter': 8,
  'length': 20,
  'qty': 30,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001105',
  'friend_box': '2x2',
  'fill_ratio_est': 0.182},
 {'pos': 111,
  'kind': '6kt',
  'thread': 'M8',
  'diameter': 8,
  'length': 30,
  'qty': 30,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001112',
  'friend_box': '2x2',
  'fill_ratio_est': 0.237},
 {'pos': 112,
  'kind': '6kt',
  'thread': 'M8',
  'diameter': 8,
  'length': 50,
  'qty': 30,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P000268',
  'friend_box': '2x3',
  'fill_ratio_est': 0.229},
 {'pos': 113,
  'kind': '6kt',
  'thread': 'M8',
  'diameter': 8,
  'length': 80,
  'qty': 30,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P000271',
  'friend_box': '2x3',
  'fill_ratio_est': 0.337},
 {'pos': 114,
  'kind': '6kt',
  'thread': 'M10',
  'diameter': 10,
  'length': 30,
  'qty': 30,
  'grid_w': 2,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001133',
  'friend_box': '2x2',
  'fill_ratio_est': 0.399},
 {'pos': 115,
  'kind': '6kt',
  'thread': 'M10',
  'diameter': 10,
  'length': 50,
  'qty': 30,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P001137',
  'friend_box': '2x3',
  'fill_ratio_est': 0.376},
 {'pos': 116,
  'kind': '6kt',
  'thread': 'M10',
  'diameter': 10,
  'length': 80,
  'qty': 25,
  'grid_w': 3,
  'grid_d': 2,
  'height_u': 7,
  'norm': 'DIN 933',
  'material': 'Stahl verzinkt 8.8',
  'artnr': 'P000275',
  'friend_box': '2x3',
  'fill_ratio_est': 0.454}]


# ---------------------------------------------------------------------------
# Final box-size normalization
# ---------------------------------------------------------------------------
# The screw inventory itself remains fully hardcoded above.  We deliberately
# normalize the footprint here instead of importing any external spreadsheet.
# Footprints (width x depth, in Gridfinity cells) for the 15.04.2026 order.
# The order allows 1x1, 1x2, 1x3, 2x1, 2x2 and 2x3; this design uses only the
# two-cell-wide subset.  Uniform width means every drawer column holds exactly one bin,
# so the boxes can be laid out in strict size order without ragged half columns, and
# every label shelf is the same wide 84 mm instead of a cramped 42 mm one.
ALLOWED_FOOTPRINTS = ((2, 1), (2, 2), (2, 3))
LANDSCAPE_EXCEPTION = (2, 1)   # the flat bin; 2x2 is square, 2x3 is portrait

# --- how full a bin may be -------------------------------------------------------
# The earlier revisions capped the SOLID steel volume at 46 % of the cavity.  That is
# far too much: loose bolts occupy roughly 45 % of the space they fill, so 46 % solid
# means a bin filled to the brim.  We now work with the bulk volume the screws actually
# take up (steel / PACKING_FRACTION) and keep it at or below FILL_TARGET of the cavity.
PACKING_FRACTION = 0.45   # random loose packing of bolts, incl. head; 0.40 is pessimistic
FILL_TARGET = 0.80        # screws reach ~4/5 up the box
FILL_HARD_LIMIT = 0.85    # nothing may exceed this; 2x3 is the largest allowed footprint,
                          # so 6kt M10x80 lands just above the target at 81 %
LENGTH_CLEARANCE = 2.0    # mm of slack along the longest inner dimension

BIN_HEIGHT_U = UNIFORM_HEIGHT_U           # every bin, no exceptions

# Real fastener geometry instead of the old head "factors".
# dk / k for the driven heads, s (across flats) / k for hex heads, in mm.
_SK_HEAD = {3: (6.72, 1.86), 4: (8.96, 2.48), 5: (11.2, 3.1),
            6: (13.4, 3.72), 8: (17.8, 4.96), 10: (22.3, 6.2)}      # ISO 10642
_ZK_HEAD = {3: (5.5, 3.0), 4: (7.0, 4.0), 5: (8.5, 5.0),
            6: (10.0, 6.0), 8: (13.0, 8.0), 10: (16.0, 10.0)}       # DIN 912
_LK_HEAD = {3: (6.0, 2.4), 4: (8.0, 3.1), 5: (9.5, 3.8), 6: (12.0, 4.6)}   # DIN 7985
_HX_HEAD = {3: (5.5, 2.0), 4: (7.0, 2.8), 5: (8.0, 3.5),
            6: (10.0, 4.0), 8: (13.0, 5.3), 10: (17.0, 6.4)}        # DIN 933
_ALLEN = {3: 2.5, 4: 3.0, 5: 4.0, 6: 5.0, 8: 6.0, 10: 8.0}
_MINOR = {3: 2.387, 4: 3.141, 5: 4.019, 6: 4.773, 8: 6.466, 10: 8.160}


def _thread_area(d: int) -> float:
    """Mean cross-section of a metric coarse thread (between major and minor diameter)."""
    return math.pi / 4 * ((d ** 2) + (_MINOR[d] ** 2)) / 2


def screw_volume(kind: str, d: int, length: int) -> float:
    """Steel volume of one screw in mm^3.  Checked against DIN mass tables to ~5 %."""
    shank = _thread_area(d) * length
    if kind == 'SK':                       # countersunk: the length includes the head
        dk, k = _SK_HEAD[d]
        R, r = dk / 2, d / 2
        return math.pi / 3 * k * (R * R + R * r + r * r) - math.pi * r * r * k + shank
    if kind == 'ZK':
        dk, k = _ZK_HEAD[d]
        socket = math.pi * (_ALLEN[d] / 2) ** 2 * 0.6 * k
        return math.pi * (dk / 2) ** 2 * k - socket + shank
    if kind == 'LK':
        dk, k = _LK_HEAD[d]
        return 0.72 * math.pi * (dk / 2) ** 2 * k + shank
    if kind == '6kt':
        s, k = _HX_HEAD[d]
        return (math.sqrt(3) / 2) * s * s * k + shank
    raise ValueError(kind)


def screw_outer_length(kind: str, d: int, length: int) -> float:
    """Overall length of the screw as it lies in the bin."""
    if kind == 'SK':
        return float(length)
    if kind == 'ZK':
        return length + _ZK_HEAD[d][1]
    if kind == 'LK':
        return length + _LK_HEAD[d][1]
    return length + _HX_HEAD[d][1]


def bin_cavity(grid_w: int, grid_d: int, height_u: int) -> tuple[float, float, float]:
    """Inner width, depth and usable volume of a bin, with the label ramp deducted."""
    ix = GRID * grid_w - XY_CLEARANCE - 2 * WALL
    iy = GRID * grid_d - XY_CLEARANCE - 2 * WALL
    top = height_u * 7.0
    ramp_bottom = top - LABEL_SHELF_THICK - LABEL_SHELF_DEPTH
    # below the ramp the full cross-section is available; the 45 deg ramp eats a
    # triangular prism out of the front of the top LABEL_SHELF_DEPTH mm
    volume = (ix * iy * (ramp_bottom - FLOOR_Z)
              + ix * (LABEL_SHELF_DEPTH * iy - LABEL_SHELF_DEPTH ** 2 / 2))
    return ix, iy, volume


def bin_fill(spec: dict, grid_w: int, grid_d: int, height_u: int) -> float:
    """Bulk volume of the screws divided by the usable volume of the bin."""
    _ix, _iy, cavity = bin_cavity(grid_w, grid_d, height_u)
    bulk = screw_volume(spec['kind'], spec['diameter'], spec['length']) * spec['qty']
    return bulk / PACKING_FRACTION / cavity


def _fits_lengthwise(spec: dict, grid_w: int, grid_d: int) -> bool:
    ix, iy, _v = bin_cavity(grid_w, grid_d, BIN_HEIGHT_U)
    return max(ix, iy) >= screw_outer_length(
        spec['kind'], spec['diameter'], spec['length']) + LENGTH_CLEARANCE


def _apply_footprints() -> None:
    """Size every bin: smallest allowed footprint the screws fit into, lengthwise and by
    volume.  All bins share BIN_HEIGHT_U, so the only free variable is the footprint.

    A bin that cannot reach FILL_TARGET even at the largest allowed footprint keeps that
    footprint and is reported; validate_specs() still enforces FILL_HARD_LIMIT.
    """
    for spec in SPECS:
        spec['height_u'] = BIN_HEIGHT_U
        chosen = None
        for grid_w, grid_d in ALLOWED_FOOTPRINTS:
            if not _fits_lengthwise(spec, grid_w, grid_d):
                continue
            if chosen is None:
                chosen = (grid_w, grid_d)          # fall back to the first that fits at all
            if bin_fill(spec, grid_w, grid_d, BIN_HEIGHT_U) <= FILL_TARGET:
                chosen = (grid_w, grid_d)
                break
        else:
            if chosen is None:
                raise RuntimeError(
                    f"P{spec['pos']:03d} {spec['kind']} {spec['thread']}x{spec['length']} "
                    f"does not fit into any allowed footprint")
        spec['grid_w'], spec['grid_d'] = chosen
        spec['fill_ratio_est'] = round(
            bin_fill(spec, spec['grid_w'], spec['grid_d'], BIN_HEIGHT_U), 3)

    for kind, width in DRAWER_ZONE_WIDTH.items():
        block = [s for s in SPECS if s['kind'] == kind]
        used = sum(s['grid_d'] for s in block)
        capacity = (width // 2) * DRAWER_GRID_D
        if used > capacity:
            raise RuntimeError(f'{kind} needs {used} rows but its block holds {capacity}')


# NOTE: _apply_footprints() needs DRAWER_ZONE_WIDTH and is therefore called further
# down, right after the drawer block widths are defined.

KIND_NAME = {'SK':'Senkkopf', 'ZK':'Zylinderkopf', 'LK':'Linsenkopf PH', '6kt':'Sechskant'}
KIND_COLOR = {'SK':[90,180,235,255], 'LK':[245,190,90,255], 'ZK':[100,205,115,255], '6kt':[235,115,105,255]}


# Filament (extruder) slots baked into the exported 3MF part settings.
BODY_FILAMENT = 1
LABEL_FILAMENT = 2


def export_assembled_3mf(body_mesh: trimesh.Trimesh, label_mesh: trimesh.Trimesh,
                         output_path: Path, assembly_name: str) -> None:
    """Write a self-contained 3MF with one assembly and two material-addressable parts.

    This intentionally does NOT use trimesh.exchange.threemf.export_3MF, because that
    exporter may require optional lxml internals and previously failed with
    ``NameError: etree is not defined``. The package below uses only ElementTree + zipfile.

    Result:
      object 1 = BODY
      object 2 = LABEL_INLAY
      object 3 = assembly containing 1 + 2
      build    = one item referencing object 3
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

    add_mesh_object(1, 'BODY', body_mesh)
    add_mesh_object(2, 'LABEL_INLAY', label_mesh)

    parent = ET.SubElement(resources, f'{{{core_ns}}}object', {
        'id': '3', 'name': assembly_name, 'type': 'model'
    })
    components = ET.SubElement(parent, f'{{{core_ns}}}components')
    ET.SubElement(components, f'{{{core_ns}}}component', {'objectid': '1'})
    ET.SubElement(components, f'{{{core_ns}}}component', {'objectid': '2'})

    build = ET.SubElement(model, f'{{{core_ns}}}build')
    ET.SubElement(build, f'{{{core_ns}}}item', {
        'objectid': '3', 'partnumber': assembly_name
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

    model_settings_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n'
        f'  <object id="3">\n'
        f'    <metadata key="name" value="{_xa(assembly_name)}"/>\n'
        '    <metadata key="extruder" value="1"/>\n'
        '    <part id="1" subtype="normal_part">\n'
        '      <metadata key="name" value="BODY"/>\n'
        f'      <metadata key="extruder" value="{BODY_FILAMENT}"/>\n'
        f'      <mesh_stat face_count="{len(body_mesh.faces)}" edges_fixed="0" degenerate_facets="0"'
        ' facets_removed="0" facets_reserved="0" backwards_edges="0"/>\n'
        '    </part>\n'
        '    <part id="2" subtype="normal_part">\n'
        '      <metadata key="name" value="LABEL_INLAY"/>\n'
        f'      <metadata key="extruder" value="{LABEL_FILAMENT}"/>\n'
        f'      <mesh_stat face_count="{len(label_mesh.faces)}" edges_fixed="0" degenerate_facets="0"'
        ' facets_removed="0" facets_reserved="0" backwards_edges="0"/>\n'
        '    </part>\n'
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


def _norm_text(value: str) -> str:
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()


def _choose_select_option(select, candidate_phrases: list[tuple[str, ...]], field_name: str, kind: str) -> str:
    # Read both visible text and the underlying option value.  The native selects
    # on Chappel's site can be hidden, so we deliberately do not use
    # select_option(), which waits for visibility.
    options = select.locator('option').evaluate_all(
        "els => els.map(o => ({text: (o.textContent || '').trim(), value: o.value, disabled: !!o.disabled}))"
    )
    normalized = [(_norm_text(o['text']), o) for o in options if not o.get('disabled')]
    for phrase in candidate_phrases:
        tokens = [_norm_text(t) for t in phrase]
        phrase_norm = " ".join(tokens).strip()

        # Prefer exact visible option text first. This avoids accidentally choosing
        # e.g. "Flange pan" for "Pan" or "Flange hex" for "Hex".
        ordered = [
            (norm, option) for norm, option in normalized if norm == phrase_norm
        ] + [
            (norm, option) for norm, option in normalized
            if norm != phrase_norm and all(t in norm for t in tokens)
        ]
        for norm, option in ordered:
            value = option['value']
            # The site's native select is visually hidden. Playwright can still
            # set it correctly when actionability checks are bypassed with force=True;
            # this also fires the same input/change events a normal selection would.
            try:
                select.select_option(value=value, force=True, timeout=5_000)
            except Exception:
                # Fallback for any browser/site combination where forced native
                # selection still refuses the hidden control.
                select.evaluate(
                    """(el, wanted) => {
                        el.value = wanted;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }""",
                    value,
                )
            actual = select.input_value()
            if actual != value:
                raise RuntimeError(
                    f"Chappel {field_name} selection failed for kind={kind}: "
                    f"wanted value={value!r}, got {actual!r}"
                )
            return option['text']
    printable = [f"{o['text']} [{o['value']}]" for o in options]
    raise RuntimeError(
        f"Could not map Chappel {field_name} for kind={kind}. Available options: {printable}. "
        "The website may have renamed an option; update CHAPPEL_OPTION_HINTS."
    )


def _input_after_text(page, label_text: str):
    """Find the first input following a visible label text, with conservative fallbacks."""
    text_loc = page.get_by_text(label_text, exact=True)
    if text_loc.count() == 0:
        text_loc = page.get_by_text(re.compile(rf'^{re.escape(label_text)}', re.I))
    if text_loc.count() == 0:
        raise RuntimeError(f"Could not locate UI label {label_text!r} on {CHAPPEL_URL}")
    for i in range(min(text_loc.count(), 5)):
        base = text_loc.nth(i)
        candidate = base.locator('xpath=following::input[1]')
        if candidate.count():
            return candidate.first
    raise RuntimeError(f"Could not locate an input after UI label {label_text!r}")


def _ensure_show_icons(page) -> None:
    label = page.get_by_text('Show icons', exact=True)
    if label.count() == 0:
        return
    for xpath in ('xpath=preceding::input[@type="checkbox"][1]', 'xpath=following::input[@type="checkbox"][1]'):
        cb = label.first.locator(xpath)
        if cb.count():
            cb = cb.first
            # Same reason as the selects: the native checkbox may be visually hidden.
            if not cb.is_checked():
                try:
                    cb.check(force=True, timeout=5_000)
                except Exception:
                    cb.evaluate(
                        """el => {
                            el.checked = true;
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }"""
                    )
            return


def _set_chappel_ui_for_spec(page, spec: dict) -> dict:
    # Stable visible controls; no private API/reverse-engineered endpoint is used.
    regular = page.get_by_role('button', name='Regular', exact=True)
    if regular.count():
        regular.first.click()
    cat = page.get_by_role('button', name='Screws & Bolts', exact=True)
    if cat.count() == 0:
        raise RuntimeError('Could not find Screws & Bolts button on Chappel label generator')
    cat.first.click()

    _ensure_show_icons(page)

    # v1.2.x uses hidden native selects with stable ids (#drive / #head) behind
    # custom visible controls.  Address them by id rather than by nth(select),
    # because there is also a Printer select further down the page.
    drive_select = page.locator('#drive')
    head_select = page.locator('#head')
    if drive_select.count() == 0 or head_select.count() == 0:
        # Conservative fallback for a future markup change.
        selects = page.locator('select')
        if selects.count() < 2:
            raise RuntimeError(f'Expected drive/head select elements, found {selects.count()} total selects')
        drive_select = selects.nth(0)
        head_select = selects.nth(1)

    # Category changes can repopulate the options asynchronously. Wait on DOM state,
    # not visibility (the native controls are intentionally hidden).
    page.wait_for_function(
        """() => {
            const d = document.querySelector('#drive') || document.querySelectorAll('select')[0];
            const h = document.querySelector('#head')  || document.querySelectorAll('select')[1];
            return d && h && d.options.length > 0 && h.options.length > 0;
        }""",
        timeout=10_000,
    )
    drive = _choose_select_option(drive_select, CHAPPEL_OPTION_HINTS[spec['kind']]['drive'], 'drive type', spec['kind'])
    head = _choose_select_option(head_select, CHAPPEL_OPTION_HINTS[spec['kind']]['head'], 'head type', spec['kind'])

    text_input = _input_after_text(page, 'Text')
    text_value = f"{spec['thread']}×{spec['length']}"
    if text_input.is_visible():
        text_input.fill(text_value)
    else:
        text_input.evaluate(
            """(el, value) => {
                el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            text_value,
        )

    raised = page.get_by_role('button', name='Raised', exact=True)
    if raised.count() == 0:
        raise RuntimeError('Could not find Raised style button')
    raised.first.click()

    return {'drive': drive, 'head': head}


def fetch_chappel_labels(out: Path, positions: set[int] | None = None, headed: bool = False, overwrite: bool = False) -> None:
    """Download the *actual* Raised label STL from Alexandre Chappel's website for every selected screw.

    This intentionally automates the public website UI with Playwright rather than duplicating its artwork.
    It requires internet access on the computer running the script.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError('Playwright missing. Run: pip install playwright && python -m playwright install chromium') from exc

    selected = [s for s in SPECS if positions is None or s['pos'] in positions]
    cache = out / CHAPPEL_CACHE_DIRNAME
    cache.mkdir(parents=True, exist_ok=True)
    mapping_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(accept_downloads=True)
        page.goto(CHAPPEL_URL, wait_until='networkidle', timeout=120_000)

        for n, spec in enumerate(selected, 1):
            target = canonical_chappel_cache_path(out, spec)
            # Intentionally re-download every selected label on every fetch/build.
            # Alexandre's generator is the source of truth; no cache lookup/migration.
            if target.exists():
                target.unlink()
            try:
                chosen = _set_chappel_ui_for_spec(page, spec)
            except Exception:
                # Save enough state to diagnose a future website UI change without
                # requiring another blind run from the user.
                screenshot = out / f"chappel_ui_error_pos_{spec['pos']:03d}.png"
                html_dump = out / f"chappel_ui_error_pos_{spec['pos']:03d}.html"
                try:
                    page.screenshot(path=str(screenshot), full_page=True)
                    html_dump.write_text(page.content(), encoding='utf-8')
                except Exception:
                    pass
                raise
            page.wait_for_timeout(CHAPPEL_DOWNLOAD_DELAY_MS)

            stl_btn = page.get_by_role('button', name=re.compile(r'^(Download\\s+)?STL$', re.I))
            if stl_btn.count() == 0:
                # Current v1.2 UI renders the button simply as "STL".
                stl_btn = page.get_by_text(re.compile(r'^STL$', re.I), exact=True)
            if stl_btn.count() == 0:
                screenshot = out / 'chappel_ui_error.png'
                page.screenshot(path=str(screenshot), full_page=True)
                raise RuntimeError(f'Could not find STL download control. UI screenshot saved to {screenshot}')

            with page.expect_download(timeout=60_000) as info:
                stl_btn.first.click()
            download = info.value
            download.save_as(str(target))
            mapping_rows.append({
                'pos': spec['pos'], 'file': target.name, 'drive_option': chosen['drive'],
                'head_option': chosen['head'], 'text': f"{spec['thread']}×{spec['length']}"
            })
            print(f"[{n:03d}/{len(selected):03d}] downloaded {target.name}  ({chosen['drive']} / {chosen['head']})")

        browser.close()

    if mapping_rows:
        with (cache/'download_map.csv').open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['pos','file','drive_option','head_option','text'])
            w.writeheader(); w.writerows(mapping_rows)


def _orient_chappel_mesh(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Orient arbitrary downloaded label STL as X=label width, Y=label height, Z=thin thickness."""
    mesh = mesh.copy()
    ext = mesh.extents
    z_axis = int(np.argmin(ext))
    x_axis = int(np.argmax(ext))
    y_axis = ({0,1,2} - {z_axis, x_axis}).pop()
    mesh.vertices = mesh.vertices[:, [x_axis, y_axis, z_axis]]
    mesh.vertices[:, 2] -= mesh.bounds[0, 2]
    return mesh


def _signed_area(coords: np.ndarray) -> float:
    x = coords[:, 0]; y = coords[:, 1]
    return 0.5 * float(np.sum(x[:-1]*y[1:] - x[1:]*y[:-1]))


def _section_loops(mesh: trimesh.Trimesh, z: float) -> list[np.ndarray]:
    section = mesh.section(plane_origin=[0,0,z], plane_normal=[0,0,1])
    if section is None:
        raise RuntimeError(f'No section found in Chappel label at z={z:.3f}')
    # trimesh has no Path3D.to_2D (it is to_planar; the old alias predates trimesh 3.x).
    # Pass the projection explicitly: to_planar() otherwise fits its own plane, and the
    # fitted normal can come back as -Z, which mirrors the label text without any error.
    # This matrix just drops the section to z=0 and preserves X and Y exactly.
    to_2D = np.array([[1.0, 0.0, 0.0, 0.0],
                      [0.0, 1.0, 0.0, 0.0],
                      [0.0, 0.0, 1.0, -z],
                      [0.0, 0.0, 0.0, 1.0]])
    planar, _ = section.to_planar(to_2D=to_2D)
    loops = []
    for arr in planar.discrete:
        arr = np.asarray(arr, dtype=float)
        if len(arr) < 4:
            continue
        if np.linalg.norm(arr[0] - arr[-1]) > 1e-5:
            arr = np.vstack([arr, arr[0]])
        if abs(_signed_area(arr)) > 1e-5:
            loops.append(arr)
    if not loops:
        raise RuntimeError('Chappel label section contained no closed graphic loops')
    return loops


def _loops_to_nested_polygons(loops: list[np.ndarray]):
    """Convert section loops to shapely Polygons while preserving holes without rtree."""
    from shapely.geometry import Polygon
    polys = []
    for arr in loops:
        p = Polygon(arr)
        if not p.is_valid:
            p = p.buffer(0)
        if p.is_empty:
            continue
        if p.geom_type == 'MultiPolygon':
            polys.extend(list(p.geoms))
        else:
            polys.append(p)
    # Unique/meaningful loops only.
    polys = [p for p in polys if p.area > 1e-5]
    depths = []
    reps = [p.representative_point() for p in polys]
    for i,p in enumerate(polys):
        depth = sum(1 for j,q in enumerate(polys) if i != j and q.area > p.area and q.contains(reps[i]))
        depths.append(depth)
    result = []
    for i,p in enumerate(polys):
        if depths[i] % 2:
            continue
        holes = []
        for j,h in enumerate(polys):
            if depths[j] == depths[i] + 1 and p.contains(reps[j]):
                holes.append(list(h.exterior.coords))
        result.append(Polygon(list(p.exterior.coords), holes))
    return result


def _polygon_to_cq_solid(poly, depth: float) -> cq.Shape:
    def wire(coords):
        pts = [cq.Vector(float(x), float(y), 0) for x,y in list(coords)[:-1]]
        return cq.Wire.makePolygon(pts, close=True)
    outer = wire(poly.exterior.coords)
    holes = [wire(r.coords) for r in poly.interiors]
    return cq.Solid.extrudeLinear(outer, holes, cq.Vector(0,0,depth))


def _extract_chappel_graphics(raw_stl: Path, target_depth: float) -> cq.Workplane:
    """Extract only Chappel's raised icon+text, discarding the ModuBOX backing plate.

    We section the STL halfway through the website's Raised extrusion layer. The resulting 2D
    contours are therefore the exact icon/font outlines produced by label.alch.shop, not replicas.
    """
    mesh = trimesh.load_mesh(raw_stl, force='mesh')
    if mesh.is_empty:
        raise RuntimeError(f'Empty Chappel STL: {raw_stl}')
    mesh = _orient_chappel_mesh(mesh)
    total_t = float(mesh.extents[2])
    expected = CHAPPEL_BASE_THICKNESS + CHAPPEL_EXTRUSION_THICKNESS
    if not (expected - 0.10 <= total_t <= expected + 0.12):
        raise RuntimeError(
            f'{raw_stl.name}: downloaded Raised label thickness is {total_t:.3f} mm; expected about {expected:.2f} mm. '
            'The website profile may have changed; inspect the STL before building bins.'
        )
    z = CHAPPEL_BASE_THICKNESS + CHAPPEL_EXTRUSION_THICKNESS * CHAPPEL_SECTION_FRACTION
    polygons = _loops_to_nested_polygons(_section_loops(mesh, z))
    if not polygons:
        raise RuntimeError(f'Could not recover Chappel graphic polygons from {raw_stl}')
    solids = [_polygon_to_cq_solid(p, target_depth) for p in polygons]
    compound = cq.Compound.makeCompound(solids)
    return cq.Workplane(obj=compound)


def _chappel_label_geometry(spec: dict, out: Path, base_z: float, thickness: float) -> cq.Workplane:
    raw = find_chappel_cache_path(out, spec)
    if raw is None:
        expected = canonical_chappel_cache_path(out, spec)
        raise FileNotFoundError(
            f'Missing real Chappel label: {expected}. Run this script with --fetch-labels first.'
        )
    graphic = _extract_chappel_graphics(raw, thickness)

    outer_y = GRID * spec['grid_d'] - XY_CLEARANCE
    outer_x = GRID * spec['grid_w'] - XY_CLEARANCE
    front_inner_y = -outer_y/2 + WALL
    shelf_y = front_inner_y + LABEL_SHELF_DEPTH/2
    safe_w = max(8.0, outer_x - 2*(BODY_RADIUS + LABEL_SAFE_SIDE))
    safe_h = LABEL_SHELF_DEPTH - 2*LABEL_SAFE_FRONT_BACK

    shape = graphic.val()
    bb = shape.BoundingBox()
    scale = min(safe_w/max(bb.xlen, 1e-6), safe_h/max(bb.ylen, 1e-6), CHAPPEL_MAX_UPSCALE)
    # Scale only in XY. Z must remain exactly the requested inlay/pocket depth.
    shape = shape.transformGeometry(cq.Matrix([
        [scale, 0, 0, 0],
        [0, scale, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
    ]))
    bb = shape.BoundingBox()
    dx = -(bb.xmin + bb.xmax)/2
    dy = shelf_y - (bb.ymin + bb.ymax)/2
    dz = base_z - bb.zmin
    shape = shape.translate(cq.Vector(dx, dy, dz))
    result = cq.Workplane(obj=shape)

    bb = result.val().BoundingBox()
    assert bb.xmin >= -safe_w/2 - 0.05 and bb.xmax <= safe_w/2 + 0.05, (spec, bb.xmin, bb.xmax, safe_w)
    assert bb.ymin >= shelf_y-safe_h/2-0.05 and bb.ymax <= shelf_y+safe_h/2+0.05, (spec, bb.ymin, bb.ymax, shelf_y, safe_h)
    return result


def make_label_inlay(spec: dict, out: Path) -> cq.Workplane:
    top_z = spec['height_u'] * 7.0
    base_z = top_z - LABEL_INLAY_DEPTH
    return _chappel_label_geometry(spec, out, base_z, LABEL_INLAY_DEPTH)


def make_label_pocket(spec: dict, out: Path) -> cq.Workplane:
    top_z = spec['height_u'] * 7.0
    base_z = top_z - LABEL_INLAY_DEPTH - LABEL_POCKET_BOTTOM_CLEARANCE
    thickness = LABEL_INLAY_DEPTH + LABEL_POCKET_BOTTOM_CLEARANCE + LABEL_POCKET_OVERTRAVEL
    return _chappel_label_geometry(spec, out, base_z, thickness)

def make_bin_body(grid_w: int, grid_d: int, height_u: int) -> cq.Workplane:
    outer_x = GRID * grid_w - XY_CLEARANCE
    outer_y = GRID * grid_d - XY_CLEARANCE
    top_z = height_u * 7.0

    outer = rounded_prism(outer_x, outer_y, BODY_RADIUS, top_z - FOOT_H).translate((0,0,FOOT_H))
    inner_x = outer_x - 2*WALL
    inner_y = outer_y - 2*WALL
    inner = rounded_prism(inner_x, inner_y, max(0.8, BODY_RADIUS-WALL), top_z-FLOOR_Z+1.0).translate((0,0,FLOOR_Z))
    body = outer.cut(inner)

    # One standard foot per Gridfinity cell.
    foot = make_gridfinity_foot()
    xs = [(i - (grid_w-1)/2) * GRID for i in range(grid_w)]
    ys = [(j - (grid_d-1)/2) * GRID for j in range(grid_d)]
    for x in xs:
        for y in ys:
            body = body.union(foot.translate((x,y,0)))

    # -------------------------------------------------------------------------
    # LABEL GEOMETRY
    # The horizontal label surface stays exactly where it was before.
    # Only its UNDERSIDE changes: instead of a 12 mm horizontal bridge/overhang,
    # a triangular prism rises from the front wall into the bin at ~45 degrees.
    # Printed upright, every new layer grows inward gradually, so the shelf needs
    # no support material.
    # -------------------------------------------------------------------------
    # Span essentially the entire outer width. This intentionally overlaps the
    # left/right side walls, eliminating the two narrow slots which were present in v1.
    # A tiny inset keeps the shelf inside the external envelope while preserving a robust union.
    shelf_w = outer_x + 2.0  # deliberately oversized; clipped to the true rounded envelope below
    front_inner_y = -outer_y/2 + WALL
    rear_shelf_y = front_inner_y + LABEL_SHELF_DEPTH
    shelf_y = (front_inner_y + rear_shelf_y) / 2
    shelf_under_z = top_z - LABEL_SHELF_THICK

    # Horizontal label plate in the original top-front position.
    raw_shelf = (cq.Workplane('XY')
                 .box(shelf_w, LABEL_SHELF_DEPTH, LABEL_SHELF_THICK, centered=(True,True,False))
                 .translate((0, shelf_y, shelf_under_z)))

    # 45-degree underside ramp. At the front it is thickest; it tapers to zero inward.
    angle = math.radians(LABEL_RAMP_ANGLE_DEG)
    ramp_drop = LABEL_SHELF_DEPTH * math.tan(angle)
    max_drop = max(2.0, shelf_under_z - FLOOR_Z - 2.0)
    ramp_drop = min(ramp_drop, max_drop)
    effective_run = ramp_drop / max(math.tan(angle), 1e-9)
    ramp_rear_y = front_inner_y + effective_run
    raw_ramp = (cq.Workplane('YZ')
                .polyline([
                    (front_inner_y, shelf_under_z - ramp_drop),
                    (front_inner_y, shelf_under_z),
                    (ramp_rear_y, shelf_under_z),
                ])
                .close()
                .extrude(shelf_w/2, both=True))

    # CRITICAL: clip both shelf and ramp to the exact rounded outer footprint of the bin.
    # This removes the rectangular 'ears' at the left/right rounded corners while still
    # producing a continuous union into the side walls with no print-hostile side slots.
    envelope = rounded_prism(outer_x, outer_y, BODY_RADIUS, top_z + 1.0)
    shelf = raw_shelf.intersect(envelope)
    ramp = raw_ramp.intersect(envelope)

    return body.union(shelf).union(ramp)


def make_finished_body(spec: dict, out: Path, base_body: cq.Workplane | None = None) -> cq.Workplane:
    """BODY with a pocket cut from the actual Alexandre Chappel label outline."""
    if base_body is None:
        base_body = make_bin_body(spec['grid_w'], spec['grid_d'], spec['height_u'])
    return base_body.cut(make_label_pocket(spec, out))


def filename_stem(spec: dict) -> str:
    return f"P{spec['pos']:03d}_{spec['kind']}_{spec['thread']}x{spec['length']}_Q{spec['qty']}_{spec['grid_w']}x{spec['grid_d']}_{spec['height_u']}U"


def chappel_cache_stem(spec: dict) -> str:
    """Stable cache name for Alexandre Chappel artwork.

    The visible label depends on screw type/diameter/length only. It does NOT depend on
    quantity, Gridfinity footprint, or bin height. Older script versions incorrectly
    encoded quantity/footprint/U-height in the cache filename. This stable name avoids
    re-downloading labels when any storage parameter changes.
    """
    return f"P{spec['pos']:03d}_{spec['kind']}_{spec['thread']}x{spec['length']}_CHAPPEL_ORIGINAL.stl"


def canonical_chappel_cache_path(out: Path, spec: dict) -> Path:
    return out / CHAPPEL_CACHE_DIRNAME / chappel_cache_stem(spec)


def _legacy_chappel_pattern(spec: dict) -> str:
    # Matches every historical variant, e.g.
    # P001_SK_M6x16_Q50_2x1_6U_CHAPPEL_ORIGINAL.stl
    # P001_SK_M6x16_Q50_CHAPPEL_ORIGINAL.stl
    return f"P{spec['pos']:03d}_{spec['kind']}_{spec['thread']}x{spec['length']}*CHAPPEL_ORIGINAL.stl"


def find_chappel_cache_path(out: Path, spec: dict) -> Path | None:
    """Find a real Chappel STL in current or legacy cache locations.

    Search order:
      1) new stable cache name in <out>/chappel_raw_labels
      2) any old filename in that cache directory
      3) any nested legacy location below <out>
      4) sibling output folders' chappel_raw_labels directories
      5) a chappel_raw_labels directory in the current working directory

    This makes upgrades safe even if an older script used 6U/8U names or a different
    output folder.
    """
    canonical = canonical_chappel_cache_path(out, spec)
    if canonical.exists():
        return canonical

    pattern = _legacy_chappel_pattern(spec)
    candidates: list[Path] = []

    cache = out / CHAPPEL_CACHE_DIRNAME
    if cache.exists():
        candidates.extend(sorted(cache.glob(pattern)))

    if out.exists():
        candidates.extend(sorted(out.rglob(pattern)))

    cwd = Path.cwd()
    root_cache = cwd / CHAPPEL_CACHE_DIRNAME
    if root_cache.exists():
        candidates.extend(sorted(root_cache.glob(pattern)))

    # Common case after changing -o: labels are in another sibling output folder.
    try:
        for sibling_cache in cwd.glob(f"*/{CHAPPEL_CACHE_DIRNAME}"):
            candidates.extend(sorted(sibling_cache.glob(pattern)))
    except OSError:
        pass

    # Deduplicate and reject zero-byte/partial downloads.
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if candidate.is_file() and candidate.stat().st_size > 100:
                return candidate
        except OSError:
            continue
    return None


def migrate_chappel_cache(out: Path, specs: list[dict]) -> tuple[int, int]:
    """Copy discovered legacy labels into the new stable cache names.

    Returns (migrated, already_canonical).
    """
    cache = out / CHAPPEL_CACHE_DIRNAME
    cache.mkdir(parents=True, exist_ok=True)
    migrated = 0
    already = 0
    for spec in specs:
        target = canonical_chappel_cache_path(out, spec)
        if target.exists() and target.stat().st_size > 100:
            already += 1
            continue
        found = find_chappel_cache_path(out, spec)
        if found is not None:
            if found.resolve() != target.resolve():
                shutil.copy2(found, target)
            migrated += 1
    return migrated, already


DRAWER_GRID_W = 25
DRAWER_GRID_D = 14
# A practical grouped arrangement used only as a fit proof.  The actual bins can
# still be rearranged freely in the drawer.
# Fit proof for the v7 drawer layout.  Each screw type owns an even number of columns;
# a column is one 2-cell wide bin.  The size-sorted bins are dealt into the currently
# shallowest column, so the diameters form bands across the drawer depth.
# Keep this in sync with ZONE_WIDTH in assemble_final_drawer_v7.py.
DRAWER_ZONE_WIDTH = {'SK': 4, 'LK': 4, 'ZK': 8, '6kt': 8}

# Sizing depends on the block widths above (spare drawer cells before extra height).
_apply_footprints()


def _column_depths(bins: list[dict], ncols: int) -> list[int]:
    """Same dealing rule as the assembly script.  Returns the depth of every column."""
    used = [0] * ncols
    for b in sorted(bins, key=lambda b: (-b['diameter'], -b['length'], b['pos'])):
        i = min(range(ncols), key=lambda j: (used[j], j))
        used[i] += b['grid_d']
    return used


def _assert_grouped_drawer_fit() -> None:
    assert sum(DRAWER_ZONE_WIDTH.values()) <= DRAWER_GRID_W
    for kind, width in DRAWER_ZONE_WIDTH.items():
        assert width % 2 == 0, (kind, width)
        depths = _column_depths([s for s in SPECS if s['kind'] == kind], width // 2)
        assert max(depths) <= DRAWER_GRID_D, (kind, depths)


def validate_specs() -> None:
    assert len(SPECS) == 116
    cells = sum(s['grid_w'] * s['grid_d'] for s in SPECS)
    assert cells == 300, cells
    assert cells <= DRAWER_GRID_W * DRAWER_GRID_D
    assert {s['height_u'] for s in SPECS} == {UNIFORM_HEIGHT_U}   # hard requirement
    assert all((s['grid_w'], s['grid_d']) in ALLOWED_FOOTPRINTS for s in SPECS)
    assert all(s['grid_w'] == 2 for s in SPECS)
    # portrait rule: only the flat 2x1 bin may be wider than it is deep
    assert all(s['grid_w'] <= s['grid_d'] or (s['grid_w'], s['grid_d']) == LANDSCAPE_EXCEPTION
               for s in SPECS)
    _assert_grouped_drawer_fit()
    assert 0 < LABEL_INLAY_DEPTH <= LABEL_SHELF_THICK
    assert 0 < LABEL_RAMP_ANGLE_DEG <= 45.0
    assert max(s['height_u'] for s in SPECS) * 7.0 <= 75.0   # drawer inner height

    for s in SPECS:
        # the screw has to lie flat with a little slack
        ix, iy, _v = bin_cavity(s['grid_w'], s['grid_d'], s['height_u'])
        outer = screw_outer_length(s['kind'], s['diameter'], s['length'])
        assert max(ix, iy) >= outer + LENGTH_CLEARANCE, (s['pos'], outer, ix, iy)
        # the head has to pass between the walls
        assert min(ix, iy) >= 2.5 * s['diameter'], (s['pos'], ix, iy)
        # and the ordered quantity has to fit without filling the bin to the brim
        fill = bin_fill(s, s['grid_w'], s['grid_d'], s['height_u'])
        assert fill <= FILL_HARD_LIMIT + 1e-6, (s['pos'], round(fill, 3))
        # even with pessimistic packing the bin must not overflow
        assert fill * PACKING_FRACTION / 0.40 <= 1.0, (s['pos'], round(fill, 3))


def write_manifest(out: Path, specs: list[dict]) -> None:
    with (out/'manifest.csv').open('w', newline='', encoding='utf-8') as f:
        cols = ['pos','kind','thread','diameter','length','qty','grid_w','grid_d','height_u','norm','material','artnr','friend_box','fill_ratio_est']
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in specs:
            w.writerow({k:s[k] for k in cols})


def generate(out: Path, positions: set[int] | None = None) -> None:
    validate_specs()
    selected = [s for s in SPECS if positions is None or s['pos'] in positions]
    if not selected:
        raise ValueError('No matching positions selected')

    missing = [s for s in selected if not canonical_chappel_cache_path(out, s).exists()]
    if missing:
        sample = ', '.join(str(s['pos']) for s in missing[:12])
        raise FileNotFoundError(
            f"Missing freshly downloaded Chappel label STL(s), e.g. positions {sample}. "
            f"Run this script with --build (which now always downloads labels first), "
            f"or use --fetch-labels for a fetch-only run."
        )

    body_dir = out/'bodies_stl'
    label_dir = out/'labels_inlay_stl'
    mf_dir = out/'assemblies_3mf'
    for p in (body_dir, label_dir, mf_dir):
        p.mkdir(parents=True, exist_ok=True)

    # Cache the generic ramp-supported body before its label pocket is cut.
    base_body_cache: dict[tuple[int,int,int], cq.Workplane] = {}

    for n, s in enumerate(selected, 1):
        key = (s['grid_w'], s['grid_d'], s['height_u'])
        if key not in base_body_cache:
            base_body_cache[key] = make_bin_body(*key)

        stem = filename_stem(s)
        body_path = body_dir / f"{stem}_BODY.stl"
        label_path = label_dir / f"{stem}_LABEL_INLAY.stl"
        assembly_path = mf_dir / f"{stem}.3mf"

        # Always regenerate all outputs for the selected screws.
        finished_body = make_finished_body(s, out, base_body_cache[key])
        cq.exporters.export(finished_body, str(body_path), tolerance=0.09, angularTolerance=0.22)

        inlay = make_label_inlay(s, out)
        cq.exporters.export(inlay, str(label_path), tolerance=0.06, angularTolerance=0.18)

        # Two-part 3MF assembly: BODY + LABEL_INLAY share one origin.
        # Assign each part to a different AMS filament in Bambu Studio/OrcaSlicer.
        body_mesh = trimesh.load_mesh(body_path, force='mesh')
        label_mesh = trimesh.load_mesh(label_path, force='mesh')
        body_mesh.visual.face_colors = [70,70,70,255]
        label_mesh.visual.face_colors = KIND_COLOR[s['kind']]
        export_assembled_3mf(body_mesh, label_mesh, assembly_path, stem)

        print(f"[{n:03d}/{len(selected):03d}] {stem}")

    write_manifest(out, selected)
    readme = f"""Gridfinity Schraubenboxen — Alexandre Chappel labels + 45° ramp + flush AMS inlay

Inventory: 116 reviewed screw bins for a 1077 x 602 x 75 mm drawer.
Grid: 25 x 14 = 350 cells. Full set occupies 300, leaving 50 free.
The 20 chipboard screws (Spanplattenschrauben, pos. 117-136 of the 15.04.2026 order)
are deliberately NOT part of this set.
Bins are two cells wide (2x1, 2x2 or 2x3) and all exactly 8U = 56 mm high.
Every label shelf is 84 mm.

LABEL DESIGN
- The horizontal label surface is in the SAME top-front position as before.
- Directly underneath it, the former horizontal overhang is replaced by an approximately
  {LABEL_RAMP_ANGLE_DEG:.0f} degree inward ramp. This makes the label support printable without supports.
- Visible icon + text outlines are extracted from the REAL Raised STL downloaded from Alexandre Chappel's
  ModuBOX Label Generator. No locally redrawn screw icon or CAD text is used.
- The ModuBOX backing plate is discarded. Chappel's icon/text outlines are re-extruded as a {LABEL_INLAY_DEPTH:.2f} mm
  AMS inlay whose top plane is exactly flush with the BODY label-shelf top plane.
- The label shelf/ramp is clipped to the exact rounded outer box contour. It joins the side walls
  continuously, without rectangular ears outside the rounded corners and without side slots.
- At 0.20 mm layer height the inlay is 3 layers deep, which gives robust AMS color separation.

3MF / AMS
Each 3MF is exported as ONE parent assembly with two aligned child parts:
  BODY        -> base filament
  LABEL_INLAY -> AMS contrast filament
The assembly hierarchy prevents slicers from treating the label as a separate loose object.

This drawer version intentionally has no stacking lip.
Always test one small bin first before committing to the complete batch.
"""
    (out/'README.txt').write_text(readme, encoding='utf-8')
    print(f"Generated {len(selected)} assemblies in {out}")
    print(f"Unique generic ramp bodies: {len(base_body_cache)}")


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
    print(f"Gridfinity/Chappel generator {SCRIPT_VERSION}")
    ap = argparse.ArgumentParser(description='Generate Gridfinity screw bins using the REAL Alexandre Chappel website label geometry.')
    ap.add_argument('-o','--output', default='gridfinity_screw_bins_chappel')
    ap.add_argument('--positions', help='Optional positions, e.g. 7,62,113-116. Default: all 116.')
    ap.add_argument('--fetch-labels', action='store_true', help='Fetch-only mode: download fresh Raised STL labels from label.alch.shop and stop.')
    ap.add_argument('--build', action='store_true', help='Fresh full build: re-download every selected Chappel label, then rebuild BODY + LABEL_INLAY + 3MF.')
    ap.add_argument('--headed', action='store_true', help='Show Chromium while fetching labels; useful if the site UI changed.')
    ap.add_argument('--overwrite-labels', action='store_true', help='Deprecated: labels are always re-downloaded on every fetch/build.')
    args = ap.parse_args()
    out = Path(args.output)
    positions = parse_positions(args.positions)

    # Fresh-generation policy: no cache reuse.
    # --fetch-labels = fetch only.  --build (or no phase flag) = fetch fresh, then build fresh.
    if args.fetch_labels and not args.build:
        fetch_chappel_labels(out, positions, headed=args.headed, overwrite=True)
        return

    fetch_chappel_labels(out, positions, headed=args.headed, overwrite=True)
    generate(out, positions)


if __name__ == '__main__':
    main()

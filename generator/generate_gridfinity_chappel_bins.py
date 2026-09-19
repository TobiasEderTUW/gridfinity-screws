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
# Which edge carries the label shelf, in plan view: 'front' (the drawer-front side, the
# original design) or 'back'. A back label is the front design mirrored in Y; the label
# graphic itself is only moved, never rotated, so it still reads from the drawer front.
LABEL_SIDES = ('front', 'back')
DEFAULT_LABEL_SIDE = 'front'

# Label style. 'integrated': the original design, shelf + 45 degree ramp with the Chappel inlay
# cut into the bin. 'removable': a plain bin with a small snap rim around the inside of the
# top, plus a separately printed two-colour label plate whose edge groove clicks over the rim
# (after the MakerWorld "Removable Label - Gridfinity AddOn", model 431547).
LABEL_STYLES = ('integrated', 'removable', 'makerworld')
DEFAULT_LABEL_STYLE = 'integrated'
SNAP_RIM_DEPTH = 0.60        # how far the rim stands in from the inner wall face
SNAP_RIM_HEIGHT = 1.30       # rim height at the wall; both faces 45 deg -> supportless
PLATE_THICK = 2.40           # label plate; its top is flush with the bin top
DEFAULT_PLATE_DEPTH = 10.0   # label plate depth from the inner wall face; per request
PLATE_DEPTH_RANGE = (6.0, 30.0)
PLATE_CLEARANCE = 0.15       # plate edge to wall, and groove to rim
PLATE_REST_GAP = 0.10        # plate underside to the top of the support ramp below it

# 'makerworld': replica of the snap profile of MakerWorld model 431547 (measured from its
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
PLATE_SAFE_SIDE = 3.0        # graphic margin to the plate's side edges
PLATE_SAFE_FRONT_BACK = 1.8  # graphic margin to the plate's wall and free edges
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

SCRIPT_VERSION = "2026-09-18.4-makerworld-lip-detents"
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
    """Integrated bin: BODY on filament 1 + LABEL_INLAY on filament 2."""
    export_parts_3mf([('BODY', body_mesh, BODY_FILAMENT), ('LABEL_INLAY', label_mesh, LABEL_FILAMENT)],
                     output_path, assembly_name)


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


def label_side(spec: dict) -> str:
    side = spec.get('label_side', DEFAULT_LABEL_SIDE)
    if side not in LABEL_SIDES:
        raise ValueError(f"label_side must be one of {LABEL_SIDES}, got {side!r}")
    return side


def _shelf_center_y(outer_y: float, side: str) -> float:
    front = -outer_y/2 + WALL + LABEL_SHELF_DEPTH/2
    return front if side == 'front' else -front


def label_style(spec: dict) -> str:
    style = spec.get('label_style', DEFAULT_LABEL_STYLE)
    if style not in LABEL_STYLES:
        raise ValueError(f"label_style must be one of {LABEL_STYLES}, got {style!r}")
    return style


def _chappel_label_geometry(spec: dict, out: Path, base_z: float, thickness: float,
                            area: tuple[float, float, float] | None = None) -> cq.Workplane:
    """The Chappel graphic, scaled into a safe area and extruded between base_z and base_z+thickness.

    area = (centre_y, safe_w, safe_h); default is the integrated shelf of this spec.
    """
    raw = find_chappel_cache_path(out, spec)
    if raw is None:
        expected = canonical_chappel_cache_path(out, spec)
        raise FileNotFoundError(
            f'Missing real Chappel label: {expected}. Run this script with --fetch-labels first.'
        )
    graphic = _extract_chappel_graphics(raw, thickness)

    if area is None:
        outer_y = GRID * spec['grid_d'] - XY_CLEARANCE
        outer_x = GRID * spec['grid_w'] - XY_CLEARANCE
        area = (_shelf_center_y(outer_y, label_side(spec)),
                max(8.0, outer_x - 2*(BODY_RADIUS + LABEL_SAFE_SIDE)),
                LABEL_SHELF_DEPTH - 2*LABEL_SAFE_FRONT_BACK)
    shelf_y, safe_w, safe_h = area

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


def _snap_ring(grid_w: int, grid_d: int, top_z: float, depth: float, height: float,
               into_wall: float) -> cq.Workplane:
    """Ring along the inner wall, centred PLATE_THICK/2 below the top.

    It stands `depth` in from the wall face, with 45 degree faces above and below and a
    flat land of (height - 2*depth) at the tip, so it prints without support. Built as a
    rounded slab minus an hourglass of two 45 degree tapered extrusions (OCC's chamfer
    fails on this loop). `into_wall` extends the slab into the wall for a robust union.
    """
    ix, iy, ir = _inner_outline(grid_w, grid_d)
    z0 = top_z - PLATE_THICK/2 - height/2
    land = height - 2*depth
    assert land > 0, (depth, height)
    nx, ny, nr = ix - 2*depth, iy - 2*depth, max(0.2, ir - depth)
    # The 45 degree faces continue e mm past the wall face (into the wall) so no cutter
    # surface coincides with the shell's inner face; coincident faces made the union run
    # away in OCC (it exhausted 8 GB of RAM).
    e = 0.3
    hourglass = (_tapered_prism(ix + 2*e, iy + 2*e, ir + e, z0 - e, depth + e, 45)
                 .union(_tapered_prism(nx, ny, nr, z0 + depth, land, 0))
                 .union(_tapered_prism(nx, ny, nr, z0 + depth + land, depth + e, -45)))
    slab = rounded_prism(ix + 2*into_wall, iy + 2*into_wall, ir + into_wall, height).translate((0, 0, z0))
    return slab.cut(hourglass)


def _support_ramp(grid_w: int, grid_d: int, height_u: int, top_face_z: float, run: float,
                  side: str) -> cq.Workplane:
    """45 degree triangular prism on the inside of the label wall.

    Its flat top at top_face_z reaches `run` mm in from the wall face; below that it falls
    away at 45 degrees back to the wall, so it prints without support. Clipped to the rounded
    outer envelope and unioned into the side walls, like the integrated shelf ramp.
    """
    outer_x = GRID * grid_w - XY_CLEARANCE
    outer_y = GRID * grid_d - XY_CLEARANCE
    wall_y = -outer_y/2 + WALL
    drop = min(run * math.tan(math.radians(LABEL_RAMP_ANGLE_DEG)),
               max(2.0, top_face_z - FLOOR_Z - 2.0))
    run = drop / math.tan(math.radians(LABEL_RAMP_ANGLE_DEG))
    span = outer_x + 2.0
    ramp = (cq.Workplane('YZ')
            .polyline([(wall_y - 0.5, top_face_z - drop - 0.5),   # 0.5 mm into the wall
                       (wall_y - 0.5, top_face_z),
                       (wall_y + run, top_face_z),
                       (wall_y, top_face_z - drop)])
            .close()
            .extrude(span/2, both=True))
    if side == 'back':
        ramp = ramp.mirror('XZ')
    envelope = rounded_prism(outer_x, outer_y, BODY_RADIUS, height_u * 7.0 + 1.0)
    return ramp.intersect(envelope)


def make_snap_bin(grid_w: int, grid_d: int, height_u: int, side: str = DEFAULT_LABEL_SIDE,
                  depth: float = DEFAULT_PLATE_DEPTH) -> cq.Workplane:
    """Removable-label bin: shell, snap rim all round, and a 45 degree ramp under the label
    for the plate to rest on. Depends on the label edge and the plate depth, not the label."""
    top_z = height_u * 7.0
    rim = _snap_ring(grid_w, grid_d, top_z, SNAP_RIM_DEPTH, SNAP_RIM_HEIGHT, 0.6)
    ramp = _support_ramp(grid_w, grid_d, height_u, top_z - PLATE_THICK - PLATE_REST_GAP,
                         depth, side)
    return make_bin_shell(grid_w, grid_d, height_u).union(rim).union(ramp)


def plate_depth(spec: dict) -> float:
    """Depth of the removable label plate (the 'label width' in the editor), in mm."""
    depth = float(spec.get('label_depth', DEFAULT_PLATE_DEPTH))
    lo, hi = PLATE_DEPTH_RANGE
    if not lo <= depth <= hi:
        raise ValueError(f"label_depth must be within {lo}-{hi} mm, got {depth}")
    _, iy, _ = _inner_outline(spec['grid_w'], spec['grid_d'])
    if depth > iy / 2:
        raise ValueError(f"a {depth} mm label covers more than half of the bin's {iy:.1f} mm inside")
    return depth


def _plate_area(spec: dict) -> tuple[float, float, float]:
    ix, iy, _ = _inner_outline(spec['grid_w'], spec['grid_d'])
    depth = plate_depth(spec)
    centre = -iy/2 + (PLATE_CLEARANCE + depth)/2
    if label_side(spec) == 'back':
        centre = -centre
    return (centre, ix - 2*PLATE_CLEARANCE - 2*PLATE_SAFE_SIDE,
            depth - PLATE_CLEARANCE - 2*PLATE_SAFE_FRONT_BACK)


def _plate_graphic(spec: dict, out: Path, base_z: float, thickness: float) -> cq.Workplane:
    return _chappel_label_geometry(spec, out, base_z, thickness, area=_plate_area(spec))


def make_label_plate(spec: dict, out: Path) -> tuple[cq.Workplane, cq.Workplane]:
    """Removable label, in its installed position: (carrier with pocket, flush inlay).

    The plate fills the inner width along the label edge, plate_depth() deep, and has a
    groove matching the snap rim along the three edges that touch a wall. For a back label
    the plate outline is mirrored, the graphic only moved, so it reads from the drawer front.
    """
    w, d, top_z = spec['grid_w'], spec['grid_d'], spec['height_u'] * 7.0
    ix, iy, ir = _inner_outline(w, d)
    z0 = top_z - PLATE_THICK
    blank = rounded_prism(ix - 2*PLATE_CLEARANCE, iy - 2*PLATE_CLEARANCE,
                          max(0.2, ir - PLATE_CLEARANCE), PLATE_THICK).translate((0, 0, z0))
    band = (cq.Workplane('XY')
            .box(ix + 2, plate_depth(spec) + 1, PLATE_THICK + 2, centered=(True, False, False))
            .translate((0, -iy/2 - 1, z0 - 1)))
    if label_side(spec) == 'back':
        band = band.mirror('XZ')
    groove = _snap_ring(w, d, top_z, SNAP_RIM_DEPTH + PLATE_CLEARANCE,
                        SNAP_RIM_HEIGHT + 2*PLATE_CLEARANCE, 2.0)
    plate = blank.intersect(band).cut(groove)
    inlay = _plate_graphic(spec, out, top_z - LABEL_INLAY_DEPTH, LABEL_INLAY_DEPTH)
    pocket = _plate_graphic(spec, out, top_z - LABEL_INLAY_DEPTH - LABEL_POCKET_BOTTOM_CLEARANCE,
                            LABEL_INLAY_DEPTH + LABEL_POCKET_BOTTOM_CLEARANCE + LABEL_POCKET_OVERTRAVEL)
    return plate.cut(pocket), inlay


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


def make_mw_label(spec: dict, out: Path) -> tuple[cq.Workplane, cq.Workplane]:
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
    inlay = _chappel_label_geometry(spec, out, top_z - LABEL_INLAY_DEPTH, LABEL_INLAY_DEPTH, area=area)
    pocket = _chappel_label_geometry(
        spec, out, top_z - LABEL_INLAY_DEPTH - LABEL_POCKET_BOTTOM_CLEARANCE,
        LABEL_INLAY_DEPTH + LABEL_POCKET_BOTTOM_CLEARANCE + LABEL_POCKET_OVERTRAVEL, area=area)
    return tray.cut(pocket), inlay


def label_print_transform() -> np.ndarray:
    """Installed position -> print position: face down on the bed (flip about X)."""
    return trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0])


def make_bin_body(grid_w: int, grid_d: int, height_u: int,
                  side: str = DEFAULT_LABEL_SIDE) -> cq.Workplane:
    outer_x = GRID * grid_w - XY_CLEARANCE
    outer_y = GRID * grid_d - XY_CLEARANCE
    top_z = height_u * 7.0
    body = make_bin_shell(grid_w, grid_d, height_u)

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

    # Back label: the same shelf and ramp, mirrored through the XZ plane (y -> -y).
    # The body without them is symmetric in Y, so nothing else changes.
    if side == 'back':
        raw_shelf = raw_shelf.mirror('XZ')
        raw_ramp = raw_ramp.mirror('XZ')
    elif side != 'front':
        raise ValueError(f"label side must be one of {LABEL_SIDES}, got {side!r}")

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
        base_body = make_bin_body(spec['grid_w'], spec['grid_d'], spec['height_u'], label_side(spec))
    return base_body.cut(make_label_pocket(spec, out))


def screw_name(spec: dict) -> str:
    """Type + size, e.g. SK_M6x80. Unique across the inventory."""
    return f"{spec['kind']}_{spec['thread']}x{spec['length']}"


def filename_stem(spec: dict) -> str:
    """Integrated bin: SK_M6x80_2x3_8U, plus _LBACK for a back label."""
    suffix = '' if label_side(spec) == 'front' else '_LBACK'
    return f"{screw_name(spec)}_{spec['grid_w']}x{spec['grid_d']}_{spec['height_u']}U{suffix}"


def snap_bin_stem(grid_w: int, grid_d: int, height_u: int, side: str = DEFAULT_LABEL_SIDE,
                  depth: float = DEFAULT_PLATE_DEPTH, count: int | None = None) -> str:
    """Removable-style bin, shared by every screw with the same size, label edge and label
    depth (the support ramp depends on both): BIN_2x1_8U_D10_x83, BIN_2x1_8U_D10_LBACK_x5."""
    suffix = '' if side == 'front' else '_LBACK'
    return (f"BIN_{grid_w}x{grid_d}_{height_u}U_D{depth:g}{suffix}"
            + (f"_x{count}" if count is not None else ""))


def mw_bin_stem(grid_w: int, grid_d: int, height_u: int, count: int | None = None,
                detent: bool = False) -> str:
    """MakerWorld-style bin, shared by every screw of that size whatever its label:
    BIN_2x1_8U_MW_x83, with detent pieces BIN_2x1_8U_MW_DET_x83."""
    return (f"BIN_{grid_w}x{grid_d}_{height_u}U_MW{'_DET' if detent else ''}"
            + (f"_x{count}" if count is not None else ""))


def label_plate_stem(spec: dict) -> str:
    """Removable label plate. It depends on bin width and plate depth: LABEL_SK_M6x80_W2_D10."""
    suffix = '' if label_side(spec) == 'front' else '_LBACK'
    style = ''
    if label_style(spec) == 'makerworld':
        style = f"_MW_FIT{label_fit(spec):g}" + ('_DET' if label_detent(spec) else '')
    return f"LABEL_{screw_name(spec)}_W{spec['grid_w']}_D{plate_depth(spec):g}{style}{suffix}"


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


def _to_mesh(shape: cq.Workplane, tolerance: float, angular: float) -> trimesh.Trimesh:
    with tempfile.TemporaryDirectory(prefix='cq2mesh_') as tmp:
        path = Path(tmp) / 'part.stl'
        cq.exporters.export(shape, str(path), tolerance=tolerance, angularTolerance=angular)
        return trimesh.load_mesh(path, force='mesh')


def build_integrated_3mf(spec: dict, out: Path, path: Path,
                         base_body: cq.Workplane | None = None,
                         stl_dirs: tuple[Path, Path] | None = None) -> None:
    """Integrated style: BODY (pocket cut) + LABEL_INLAY in one two-colour 3MF.

    stl_dirs = (bodies, labels) also keeps the two STLs, which the drawer assembly reads.
    """
    body_shape = make_finished_body(spec, out, base_body)
    inlay_shape = make_label_inlay(spec, out)
    body = _to_mesh(body_shape, 0.09, 0.22)
    inlay = _to_mesh(inlay_shape, 0.06, 0.18)
    if stl_dirs is not None:
        body.export(stl_dirs[0] / f"{path.stem}_BODY.stl")
        inlay.export(stl_dirs[1] / f"{path.stem}_LABEL_INLAY.stl")
    export_assembled_3mf(body, inlay, path, path.stem)


def build_snap_bin_3mf(grid_w: int, grid_d: int, height_u: int, path: Path,
                       side: str = DEFAULT_LABEL_SIDE, depth: float = DEFAULT_PLATE_DEPTH) -> None:
    """Removable style: the bin alone, one part on filament 1."""
    bin_mesh = _to_mesh(make_snap_bin(grid_w, grid_d, height_u, side, depth), 0.09, 0.22)
    export_parts_3mf([('BIN', bin_mesh, BODY_FILAMENT)], path, path.stem)


def build_mw_bin_3mf(grid_w: int, grid_d: int, height_u: int, path: Path,
                     detent: bool = False) -> None:
    """MakerWorld style: the bin with its lip (and detent pieces), one part on filament 1."""
    bin_mesh = _to_mesh(make_mw_bin(grid_w, grid_d, height_u, detent), 0.09, 0.22)
    export_parts_3mf([('BIN', bin_mesh, BODY_FILAMENT)], path, path.stem)


def build_label_plate_3mf(spec: dict, out: Path, path: Path) -> None:
    """Removable / MakerWorld style: label + flush inlay, laid face down for printing."""
    maker = make_mw_label if label_style(spec) == 'makerworld' else make_label_plate
    plate, inlay = maker(spec, out)
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


def removable_file_plan(specs: list[dict]) -> tuple[list[tuple[tuple, int]], list[dict]]:
    """Distinct bins with their use counts, and one label per screw.

    Removable bins carry the support ramp, so their key is (w, d, u, side, depth);
    MakerWorld bins are the same for every label, so theirs is (w, d, u), plus a 'det' marker
    when they carry the detent pieces.
    """
    counts: dict[tuple, int] = {}
    for spec in specs:
        key = (spec['grid_w'], spec['grid_d'], spec['height_u'])
        if label_style(spec) == 'removable':
            key += (label_side(spec), plate_depth(spec))
        elif label_detent(spec):
            key += ('det',)
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items()), list(specs)


def write_manifest(out: Path, specs: list[dict]) -> None:
    with (out/'manifest.csv').open('w', newline='', encoding='utf-8') as f:
        cols = ['name','kind','thread','diameter','length','grid_w','grid_d','height_u','label_side','norm','material','artnr','fill_ratio_est']
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in specs:
            row = {**s, 'name': screw_name(s), 'label_side': label_side(s)}
            w.writerow({k: row.get(k, '') for k in cols})


def generate(out: Path, positions: set[int] | None = None, side: str | None = None,
             style: str = DEFAULT_LABEL_STYLE, label_depth: float = DEFAULT_PLATE_DEPTH,
             fit: float = DEFAULT_MW_FIT, detent: bool = False) -> None:
    validate_specs()
    selected = [s for s in SPECS if positions is None or s['pos'] in positions]
    if side is not None:
        selected = [{**s, 'label_side': side} for s in selected]
    selected = [{**s, 'label_style': style, 'label_depth': label_depth, 'label_fit': fit,
                 'label_detent': detent} for s in selected]
    if not selected:
        raise ValueError('No matching positions selected')

    missing = [s for s in selected if find_chappel_cache_path(out, s) is None]
    if missing:
        sample = ', '.join(screw_name(s) for s in missing[:12])
        raise FileNotFoundError(
            f"Missing Chappel label STL(s), e.g. {sample}. "
            f"Run this script with --build (which downloads labels first), "
            f"or use --fetch-labels for a fetch-only run."
        )

    if style in ('removable', 'makerworld'):
        bin_dir, label_dir = out/'bins_3mf', out/'labels_3mf'
        for p in (bin_dir, label_dir):
            p.mkdir(parents=True, exist_ok=True)
        bins, labels = removable_file_plan(selected)
        for key, count in bins:
            if style == 'makerworld':
                det = len(key) == 4
                path = bin_dir / f"{mw_bin_stem(*key[:3], count, det)}.3mf"
                build_mw_bin_3mf(*key[:3], path, det)
            else:
                path = bin_dir / f"{snap_bin_stem(*key, count=count)}.3mf"
                build_snap_bin_3mf(key[0], key[1], key[2], path, key[3], key[4])
            print(f"bin   {path.name}")
        for n, s in enumerate(labels, 1):
            path = label_dir / f"{label_plate_stem(s)}.3mf"
            build_label_plate_3mf(s, out, path)
            print(f"[{n:03d}/{len(labels):03d}] {path.name}")
        write_manifest(out, selected)
        print(f"Generated {len(bins)} distinct bins and {len(labels)} label plates in {out}")
        return

    body_dir = out/'bodies_stl'
    label_dir = out/'labels_inlay_stl'
    mf_dir = out/'assemblies_3mf'
    for p in (body_dir, label_dir, mf_dir):
        p.mkdir(parents=True, exist_ok=True)

    # Cache the generic ramp-supported body before its label pocket is cut.
    base_body_cache: dict[tuple[int,int,int,str], cq.Workplane] = {}

    for n, s in enumerate(selected, 1):
        key = (s['grid_w'], s['grid_d'], s['height_u'], label_side(s))
        if key not in base_body_cache:
            base_body_cache[key] = make_bin_body(*key)
        stem = filename_stem(s)
        build_integrated_3mf(s, out, mf_dir / f"{stem}.3mf", base_body_cache[key],
                             stl_dirs=(body_dir, label_dir))
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
- The horizontal label surface sits at the top rim, on the front edge by default. Files
  ending in _LBACK carry it on the back edge instead (shelf and ramp mirrored; the label
  graphic is not rotated, so it still reads from the drawer front).
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
    ap.add_argument('--label-side', choices=LABEL_SIDES, default=None,
                    help="Edge that carries the label shelf: 'front' (default, drawer-front side) or 'back'.")
    ap.add_argument('--style', choices=LABEL_STYLES, default=DEFAULT_LABEL_STYLE,
                    help="'integrated' (label inlaid in the bin), 'removable' (snap-rim bins with a "
                         "support ramp, plus separate two-colour label plates) or 'makerworld' "
                         "(replica of MakerWorld 431547: lipped bins plus clip-on label trays).")
    ap.add_argument('--label-depth', type=float, default=DEFAULT_PLATE_DEPTH,
                    help=f"Removable style: label plate depth in mm (default {DEFAULT_PLATE_DEPTH:g}, "
                         f"{PLATE_DEPTH_RANGE[0]:g}-{PLATE_DEPTH_RANGE[1]:g}).")
    ap.add_argument('--label-fit', type=float, default=DEFAULT_MW_FIT,
                    help=f"MakerWorld style: gap between label clip and bin lip per side in mm "
                         f"(default {DEFAULT_MW_FIT:g}, {MW_FIT_RANGE[0]:g} to {MW_FIT_RANGE[1]:g}; "
                         f"negative = press fit).")
    ap.add_argument('--detents', action='store_true',
                    help="MakerWorld style: short vertical lip pieces near every corner + matching "
                         "slots in the label, so it cannot slide.")
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
    generate(out, positions, side=args.label_side, style=args.style, label_depth=args.label_depth,
             fit=args.label_fit, detent=args.detents)


if __name__ == '__main__':
    main()
